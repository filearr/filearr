"""Extraction parity phase 3 (2026-08-10): the ``reextract`` agent command.

Agent-side extraction runs INSIDE the scan, over the files a scan reports as
new or changed — so an item catalogued before ``extract_enabled`` was turned
on, or before the agent host gained ffprobe/exiftool/poppler/tesseract, is
never enriched (nothing about that file will ever change again). ``reextract``
is the operator-triggered sweep that closes the gap: the agent walks its
existing local index and re-emits those items through the normal replication
path.

Covered here: the enqueue contract (kind + payload), the ``max_items``
validation boundary, the 409 single-sweep guard, scope enforcement, and the
migration round-trip that widens/narrows the ``agent_commands.kind`` CHECK.

Runs against the migrated pgserver Postgres (mirrors test_maintenance_mode's
harness — alembic head, ASGITransport, auth off, agents enabled).
"""

from __future__ import annotations

import uuid
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api.agent_commands import REEXTRACT_MAX_ITEMS_CEILING
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The revision this module's migration test steps back to (the maintenance-mode
# revision, which knows 'suspend'/'agent_maintenance' but not 'reextract').
REEXTRACT_PRED = "a9c4e7f2b581"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker) -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _set_status(maker, status: str) -> None:
    async with maker() as s:
        await s.execute(
            text("UPDATE agent_commands SET status = :st WHERE kind = 'reextract'"),
            {"st": status},
        )
        await s.commit()


# --------------------------------------------------------------------------- #
# Enqueue contract                                                             #
# --------------------------------------------------------------------------- #
async def test_reextract_enqueues_agent_scoped_command(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "reextract"
    assert body["status"] == "pending"
    # Agent-scoped: the sweep targets the whole local index, never one item.
    assert body["item_id"] is None
    assert body["agent_id"] == str(agent_id)
    # Only the two knobs travel — the agent decides the rest from its own cached
    # policy + cursor state.
    assert body["payload"] == {"force": False, "max_items": None}

    async with maker() as s:
        n = (
            await s.execute(
                text("SELECT COUNT(*) FROM agent_commands WHERE kind = 'reextract'")
            )
        ).scalar()
        assert n == 1


async def test_reextract_body_is_optional(client):
    """A bare POST (no body at all) is the common "sweep everything" case and
    must behave exactly like ``{}`` — same ergonomics as /maintenance."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract")
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {"force": False, "max_items": None}


async def test_reextract_force_round_trip(client):
    """``force`` is what an operator uses to re-sweep at an UNCHANGED extraction
    configuration (the fingerprint short-circuit would otherwise no-op)."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={"force": True})
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {"force": True, "max_items": None}


async def test_reextract_max_items_round_trip(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(
        f"/api/v1/agents/{agent_id}/reextract",
        json={"force": True, "max_items": 500},
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {"force": True, "max_items": 500}


@pytest.mark.parametrize(
    "bad",
    [0, -1, REEXTRACT_MAX_ITEMS_CEILING + 1, "many"],
)
async def test_reextract_max_items_rejected_not_normalised(client, bad):
    """A bad bound is a 422, never a silent clamp: the sweep's effect is not
    visible for minutes, so a 201 for a request the operator did not make is
    strictly worse than an error."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={"max_items": bad})
    assert r.status_code == 422, r.text
    async with maker() as s:
        n = (
            await s.execute(
                text("SELECT COUNT(*) FROM agent_commands WHERE kind = 'reextract'")
            )
        ).scalar()
        assert n == 0  # nothing enqueued on a rejected body


async def test_reextract_max_items_ceiling_is_inclusive(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/reextract",
        json={"max_items": REEXTRACT_MAX_ITEMS_CEILING},
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["max_items"] == REEXTRACT_MAX_ITEMS_CEILING


async def test_reextract_unknown_agent_404(client):
    c, _, _ = client
    r = await c.post(f"/api/v1/agents/{uuid.uuid4()}/reextract", json={})
    assert r.status_code == 404


async def test_reextract_revoked_agent_409(client):
    """Same _live_agent gate the suspend/maintenance actions use."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    async with maker() as s:
        await s.execute(text("UPDATE agents SET revoked_at = now()"))
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Single-sweep guard                                                           #
# --------------------------------------------------------------------------- #
async def test_reextract_409_while_queued_or_running(client):
    """Two concurrent sweeps would fight over the agent's one cursor, so this
    takes the ``agent_maintenance`` 409 rather than the ``suspend`` collapse."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    assert r.status_code == 201, r.text
    first_id = r.json()["id"]

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={"force": True})
    assert r.status_code == 409
    assert "already queued or running" in r.json()["detail"]

    # In flight (delivered, not yet completed) is still a 409 — the cursor is
    # being written right now.
    await _set_status(maker, "picked_up")
    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    assert r.status_code == 409

    # A terminal sweep frees the gate; the follow-up is a NEW command row (the
    # knobs of a finished sweep must not leak into the next one).
    await _set_status(maker, "done")
    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={"max_items": 10})
    assert r.status_code == 201, r.text
    assert r.json()["id"] != first_id
    assert r.json()["payload"] == {"force": False, "max_items": 10}


async def test_reextract_guard_is_per_agent(client):
    """The guard is scoped to ONE agent's cursor — a sweep on one agent must not
    block the rest of the fleet."""
    c, maker, _ = client
    a1 = await _seed_agent(maker)
    a2 = await _seed_agent(maker)

    assert (await c.post(f"/api/v1/agents/{a1}/reextract", json={})).status_code == 201
    assert (await c.post(f"/api/v1/agents/{a2}/reextract", json={})).status_code == 201


# --------------------------------------------------------------------------- #
# Scope enforcement                                                            #
# --------------------------------------------------------------------------- #
async def test_reextract_requires_write_scope(client, monkeypatch):
    """Same coarse ``write`` gate as suspend/maintenance — an anonymous caller
    never reaches the enqueue."""
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    assert r.status_code in (401, 403)


# --------------------------------------------------------------------------- #
# Migration round-trip                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pg_uri")
def test_kind_check_widened_and_narrowed(pg_uri):
    """The widened CHECK accepts 'reextract' at head; ``downgrade()`` drops the
    rows holding it and narrows the constraint back (a downgrade that left the
    rows in place would fail Postgres's validation of the new CHECK)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(_psycopg3(pg_uri))
    try:
        with engine.begin() as conn:
            agent_id = conn.execute(
                text(
                    "INSERT INTO agents (name, hostname, platform) "
                    "VALUES ('mig', 'mig', 'linux') RETURNING id"
                )
            ).scalar()
            conn.execute(
                text(
                    "INSERT INTO agent_commands "
                    "(agent_id, kind, payload, status, expires_at) VALUES "
                    "(:a, 'reextract', '{\"force\": false}'::jsonb, 'pending', "
                    "now() + interval '1 hour')"
                ),
                {"a": agent_id},
            )
        # A kind outside the vocabulary is still refused by the same constraint.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO agent_commands "
                        "(agent_id, kind, payload, status, expires_at) VALUES "
                        "(:a, 'reextract_all', '{}'::jsonb, 'pending', "
                        "now() + interval '1 hour')"
                    ),
                    {"a": agent_id},
                )

        command.downgrade(cfg, REEXTRACT_PRED)
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM agent_commands WHERE kind = 'reextract'")
            ).scalar() == 0
            # The predecessor's vocabulary survives the narrowing.
            assert conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'agent_commands_kind_valid'"
                )
            ).scalar().count("agent_maintenance") == 1
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                conn.execute(
                    text(
                        "INSERT INTO agent_commands "
                        "(agent_id, kind, payload, status, expires_at) VALUES "
                        "(:a, 'reextract', '{}'::jsonb, 'pending', "
                        "now() + interval '1 hour')"
                    ),
                    {"a": agent_id},
                )

        # Re-upgrade: the widening is repeatable and the DB is left at head for
        # the rest of the session (the pgserver instance is shared).
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            conn.execute(
                text(
                    "INSERT INTO agent_commands "
                    "(agent_id, kind, payload, status, expires_at) VALUES "
                    "(:a, 'reextract', '{}'::jsonb, 'pending', "
                    "now() + interval '1 hour')"
                ),
                {"a": agent_id},
            )
            conn.execute(text("DELETE FROM agent_commands WHERE agent_id = :a"), {"a": agent_id})
            conn.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
    finally:
        engine.dispose()


async def test_sweep_gets_a_long_ttl_so_it_is_not_expired_mid_run(client):
    """A sweep runs for hours; ``sweep_decision`` expires on ``expires_at``
    UNCONDITIONALLY (TTL outranks the lease), so the default 1h window would mark
    a faithfully-heartbeating agent's command ``expired`` mid-run and report a
    failure for work that actually completed."""
    from filearr.api.agent_commands import REEXTRACT_TTL_SECONDS

    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reextract")
    assert r.status_code == 201, r.text
    body = r.json()
    window = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["created_at"]
    )
    assert window.total_seconds() == pytest.approx(REEXTRACT_TTL_SECONDS, abs=5)

    # And it is still bounded by the server's own ceiling — this is a longer
    # window, not an unbounded one.
    settings = get_settings()
    assert REEXTRACT_TTL_SECONDS <= settings.agent_command_ttl_max_seconds
    assert REEXTRACT_TTL_SECONDS > settings.agent_command_ttl_seconds


async def test_the_short_lived_agent_commands_keep_the_default_ttl(client):
    """The longer window is scoped to the sweep: a maintenance pass finishes in
    seconds, so widening its TTL would only delay the expiry of a command whose
    agent never came back."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/maintenance")
    assert r.status_code == 201, r.text
    body = r.json()
    window = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["created_at"]
    )
    assert window.total_seconds() == pytest.approx(
        get_settings().agent_command_ttl_seconds, abs=5
    )
