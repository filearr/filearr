"""QH-T6 (2026-08-12): the ``rehash_sweep`` agent command.

Until 2026-07-18 both hashers read a fixed 64 KiB head and appended the tail only
above 131072 bytes, so a file in the 65537..131072 band had its middle and its
tail silently unhashed — false duplicates, and a mis-keyed move-detection tier.
QH-T1 fixed the hashers; QH-T4's ``rehash_small_files`` converged central's own
rows. Agent-owned rows are unreachable from central (it does not host the files,
and ``agentsync.apply_batch`` never writes ``policy_version`` for them, so no
central query can even tell a stale agent hash from a correct one), and the
agent's scan re-hashes only files whose size or mtime moved — so a stable file in
the band keeps its wrong hash forever. ``rehash_sweep`` is the operator-triggered
migration that fixes them. 98,628 affected rows across seven libraries on the
live fleet when this shipped.

Covered here: the enqueue contract (kind + resolved band in the payload), the
``max_items`` and band validation boundaries, the 409 single-sweep guard, the
long TTL, scope enforcement, and the migration round-trip that widens/narrows the
``agent_commands.kind`` CHECK. Also, deliberately: that ``rehash_sweep`` and the
long-standing item-scoped ``rehash_check`` stay separate things.

Runs against the migrated pgserver Postgres (mirrors test_agent_reextract's
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
from filearr.api.agent_commands import (
    REHASH_DEFAULT_MAX_SIZE,
    REHASH_DEFAULT_MIN_SIZE,
    REHASH_MAX_ITEMS_CEILING,
    REHASH_MAX_SIZE_CEILING,
    REHASH_TTL_SECONDS,
)
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parent.parent

# The revision this module's migration test steps back to: the agent
# capabilities_at revision, which knows every kind through 'reextract' but not
# 'rehash_sweep'.
REHASH_PRED = "b2e6d048f317"


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
            text("UPDATE agent_commands SET status = :st WHERE kind = 'rehash_sweep'"),
            {"st": status},
        )
        await s.commit()


async def _count(maker, kind: str = "rehash_sweep") -> int:
    async with maker() as s:
        return (
            await s.execute(
                text("SELECT COUNT(*) FROM agent_commands WHERE kind = :k"), {"k": kind}
            )
        ).scalar()


# --------------------------------------------------------------------------- #
# Enqueue contract                                                             #
# --------------------------------------------------------------------------- #
async def test_rehash_sweep_enqueues_agent_scoped_command(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "rehash_sweep"
    assert body["status"] == "pending"
    # Agent-scoped: the sweep targets a whole size band of the local index, never
    # one item. (This is the structural difference from ``rehash_check``.)
    assert body["item_id"] is None
    assert body["agent_id"] == str(agent_id)
    # The band is RESOLVED into the payload rather than left as None: the agent
    # would default it to the same numbers, but the command row is the operator's
    # record of what was asked for, and "null" is not a record.
    assert body["payload"] == {
        "force": False,
        "max_items": None,
        "min_size": REHASH_DEFAULT_MIN_SIZE,
        "max_size": REHASH_DEFAULT_MAX_SIZE,
    }
    assert await _count(maker) == 1


async def test_the_default_band_is_the_defect_band(client):
    """65537..131072 inclusive, and neither edge is an accident: a file of 65536
    bytes or fewer was hashed IN FULL by the old code (the fixed read(65536)
    truncated at EOF) and is already correct, while above 131072 the tail branch
    fired then and fires now."""
    assert REHASH_DEFAULT_MIN_SIZE == 65_537
    assert REHASH_DEFAULT_MAX_SIZE == 131_072

    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep")
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["min_size"] == 65_537
    assert r.json()["payload"]["max_size"] == 131_072


async def test_band_is_overridable_per_run(client):
    """The wide QH-T2 backfill (granting content_hash to the ~1.03M files below
    the band) is a legitimate, deliberate, separate run — ~10x the I/O for a
    different benefit — so it is available and it is never the default."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(
        f"/api/v1/agents/{agent_id}/rehash-sweep",
        json={"min_size": 1, "max_size": 131072, "max_items": 5000, "force": True},
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {
        "force": True,
        "max_items": 5000,
        "min_size": 1,
        "max_size": 131072,
    }


async def test_a_single_size_band_is_accepted(client):
    """min == max is a valid one-size band (0 < min <= max), not an edge case to
    reject — an operator narrowing a re-run to one problematic size."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/rehash-sweep",
        json={"min_size": 70000, "max_size": 70000},
    )
    assert r.status_code == 201, r.text


async def test_inverted_band_is_422_not_swapped(client):
    """An inverted band selects zero rows, and the agent would then stamp that
    fingerprint FINISHED — permanently short-circuiting the real sweep at that
    band. Swapping the values silently would enqueue a command the operator did
    not ask for; clamping would do the same. So: refuse."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(
        f"/api/v1/agents/{agent_id}/rehash-sweep",
        json={"min_size": 131072, "max_size": 65537},
    )
    assert r.status_code == 422, r.text
    assert "min_size" in r.json()["detail"]
    assert await _count(maker) == 0  # nothing enqueued on a rejected body


@pytest.mark.parametrize(
    "body",
    [
        {"min_size": 0},
        {"min_size": -1},
        {"max_size": 0},
        {"min_size": REHASH_MAX_SIZE_CEILING + 1},
        # Above the ceiling: even the widest legitimate run stops at 131072,
        # because nothing about hashing changed above it. Accepting an arbitrary
        # ceiling would let one console click ask an agent to re-read its entire
        # library over SMB.
        {"max_size": REHASH_MAX_SIZE_CEILING + 1},
        {"min_size": "big"},
        {"max_items": 0},
        {"max_items": -1},
        {"max_items": REHASH_MAX_ITEMS_CEILING + 1},
        {"max_items": "many"},
    ],
)
async def test_bad_knobs_are_rejected_not_normalised(client, body):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json=body)
    assert r.status_code == 422, r.text
    assert await _count(maker) == 0


async def test_ceilings_are_inclusive(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/rehash-sweep",
        json={
            "max_items": REHASH_MAX_ITEMS_CEILING,
            "max_size": REHASH_MAX_SIZE_CEILING,
        },
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["max_items"] == REHASH_MAX_ITEMS_CEILING
    assert r.json()["payload"]["max_size"] == REHASH_MAX_SIZE_CEILING


async def test_rehash_sweep_unknown_agent_404(client):
    c, _, _ = client
    r = await c.post(f"/api/v1/agents/{uuid.uuid4()}/rehash-sweep", json={})
    assert r.status_code == 404


async def test_rehash_sweep_revoked_agent_409(client):
    """Same _live_agent gate the suspend/maintenance/reextract actions use."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    async with maker() as s:
        await s.execute(text("UPDATE agents SET revoked_at = now()"))
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Single-sweep guard                                                           #
# --------------------------------------------------------------------------- #
async def test_rehash_sweep_409_while_queued_or_running(client):
    """Two concurrent sweeps would fight over the agent's one cursor and
    double-emit the rows they raced on, so this takes the ``agent_maintenance``
    409 rather than the ``suspend`` collapse — and the band knobs do not merge
    either."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code == 201, r.text
    first_id = r.json()["id"]

    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={"force": True})
    assert r.status_code == 409
    assert "already queued or running" in r.json()["detail"]

    # In flight (delivered, not yet completed) is still a 409 — the cursor is
    # being written right now.
    await _set_status(maker, "picked_up")
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code == 409

    # A terminal sweep frees the gate; the follow-up is a NEW command row.
    await _set_status(maker, "done")
    r = await c.post(
        f"/api/v1/agents/{agent_id}/rehash-sweep", json={"max_items": 10}
    )
    assert r.status_code == 201, r.text
    assert r.json()["id"] != first_id
    assert r.json()["payload"]["max_items"] == 10


async def test_rehash_sweep_guard_is_per_agent(client):
    """The guard is scoped to ONE agent's cursor — a sweep on one agent must not
    block the rest of the fleet."""
    c, maker, _ = client
    a1 = await _seed_agent(maker)
    a2 = await _seed_agent(maker)

    assert (
        await c.post(f"/api/v1/agents/{a1}/rehash-sweep", json={})
    ).status_code == 201
    assert (
        await c.post(f"/api/v1/agents/{a2}/rehash-sweep", json={})
    ).status_code == 201


async def test_rehash_sweep_does_not_block_the_other_sweeps(client):
    """The guard keys on the KIND. A queued re-extraction and a queued re-hash are
    different jobs with different cursors and must be able to coexist — and
    neither may be blocked by the item-scoped ``rehash_check`` verify."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    assert (
        await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    ).status_code == 201
    assert (
        await c.post(f"/api/v1/agents/{agent_id}/reextract", json={})
    ).status_code == 201
    assert await _count(maker, "rehash_sweep") == 1
    assert await _count(maker, "reextract") == 1


# --------------------------------------------------------------------------- #
# TTL                                                                          #
# --------------------------------------------------------------------------- #
async def test_sweep_gets_a_long_ttl_so_it_is_not_expired_mid_run(client):
    """A sweep runs for hours; ``sweep_decision`` expires on ``expires_at``
    UNCONDITIONALLY (TTL outranks the lease), so the default 1h window would mark
    a faithfully-heartbeating agent's command ``expired`` mid-run and report a
    failure for work that actually completed."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep")
    assert r.status_code == 201, r.text
    body = r.json()
    window = datetime.fromisoformat(body["expires_at"]) - datetime.fromisoformat(
        body["created_at"]
    )
    assert window.total_seconds() == pytest.approx(REHASH_TTL_SECONDS, abs=5)

    # And it is still bounded by the server's own ceiling — a longer window, not
    # an unbounded one.
    settings = get_settings()
    assert REHASH_TTL_SECONDS <= settings.agent_command_ttl_max_seconds
    assert REHASH_TTL_SECONDS > settings.agent_command_ttl_seconds


# --------------------------------------------------------------------------- #
# Scope enforcement                                                            #
# --------------------------------------------------------------------------- #
async def test_rehash_sweep_requires_admin_scope(client, monkeypatch):
    """``admin``, deliberately narrower than the ``write`` gate on the sibling
    sweep actions: this one commands a remote machine to spend hours re-reading
    its filesystem, which is closer to the agent admin surface than to an
    ordinary catalogue write."""
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    monkeypatch.setattr(settings, "auth_enabled", True)
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code in (401, 403)


async def test_rehash_sweep_404s_when_agents_are_disabled(client, monkeypatch):
    """``require_agents_enabled``: the whole agent surface 404s when the feature
    flag is off, and this endpoint is not an exception to it."""
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    r = await c.post(f"/api/v1/agents/{agent_id}/rehash-sweep", json={})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# rehash_sweep is NOT rehash_check                                             #
# --------------------------------------------------------------------------- #
async def test_rehash_check_is_still_item_scoped_and_separate(client):
    """One word apart, opposite jobs. ``rehash_check`` verifies ONE item and
    writes nothing; ``rehash_sweep`` migrates a size band and rewrites rows. The
    generic enqueue still REQUIRES an item_id for the verify, and still refuses
    one for the sweep — which is the enforced form of that distinction."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "rehash_check", "payload": {}},
    )
    assert r.status_code == 422, r.text  # item-scoped: item_id is mandatory

    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "rehash_sweep", "item_id": str(uuid.uuid4()), "payload": {}},
    )
    assert r.status_code == 422, r.text  # agent-scoped: item_id is forbidden


# --------------------------------------------------------------------------- #
# Migration round-trip                                                         #
# --------------------------------------------------------------------------- #
@pytest.mark.usefixtures("pg_uri")
def test_kind_check_widened_and_narrowed(pg_uri):
    """The widened CHECK accepts 'rehash_sweep' at head; ``downgrade()`` drops the
    rows holding it and narrows the constraint back (a downgrade that left the
    rows in place would fail Postgres's validation of the new CHECK)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_engine(_psycopg3(pg_uri))

    def _insert(conn, agent_id, kind):
        conn.execute(
            text(
                "INSERT INTO agent_commands "
                "(agent_id, kind, payload, status, expires_at) VALUES "
                "(:a, :k, '{}'::jsonb, 'pending', now() + interval '1 hour')"
            ),
            {"a": agent_id, "k": kind},
        )

    try:
        with engine.begin() as conn:
            agent_id = conn.execute(
                text(
                    "INSERT INTO agents (name, hostname, platform) "
                    "VALUES ('mig', 'mig', 'linux') RETURNING id"
                )
            ).scalar()
            _insert(conn, agent_id, "rehash_sweep")
            # The sibling kind it is NOT must survive the widening untouched.
            _insert(conn, agent_id, "rehash_check")
        # A kind outside the vocabulary is still refused by the same constraint.
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _insert(conn, agent_id, "rehash")

        command.downgrade(cfg, REHASH_PRED)
        with engine.connect() as conn:
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM agent_commands "
                        "WHERE kind = 'rehash_sweep'"
                    )
                ).scalar()
                == 0
            )
            # The predecessor's vocabulary survives the narrowing — including
            # rehash_check, which predates all of this and its row with it.
            constraint = conn.execute(
                text(
                    "SELECT pg_get_constraintdef(oid) FROM pg_constraint "
                    "WHERE conname = 'agent_commands_kind_valid'"
                )
            ).scalar()
            assert constraint.count("reextract") == 1
            assert constraint.count("rehash_check") == 1
            assert "rehash_sweep" not in constraint
            assert (
                conn.execute(
                    text(
                        "SELECT count(*) FROM agent_commands "
                        "WHERE kind = 'rehash_check'"
                    )
                ).scalar()
                == 1
            )
        with pytest.raises(IntegrityError):
            with engine.begin() as conn:
                _insert(conn, agent_id, "rehash_sweep")

        # Re-upgrade: the widening is repeatable and the DB is left at head for
        # the rest of the session (the pgserver instance is shared).
        command.upgrade(cfg, "head")
        with engine.begin() as conn:
            _insert(conn, agent_id, "rehash_sweep")
            conn.execute(
                text("DELETE FROM agent_commands WHERE agent_id = :a"), {"a": agent_id}
            )
            conn.execute(text("DELETE FROM agents WHERE id = :a"), {"a": agent_id})
    finally:
        engine.dispose()
