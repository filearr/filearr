"""Local agent scan controls, central half (2026-08-10).

The agent's local web UI can pause/resume its own scanning, edit its own scan
schedule, and manage its own scan roots — each gated by a central-authored
permission key. Central's job is to author, validate and deliver those keys;
enforcement is the agent's (``agent/internal/localapi/webcontrol.go``).

What is pinned here:

* the three keys round-trip through ``PUT /agent-policies/{scope}`` and appear
  in the admin effective-policy view with the right source;
* absent, they are MODELLED keys reported as ``default`` (the console renders a
  field per known key) — not forward-compat unknowns;
* they are booleans, and a non-boolean is a 422;
* they are INDEPENDENT of ``read_only``: delegating scan control does not (and
  cannot) make the local surface writable over the catalog, and ``read_only:
  false`` is still rejected while the control keys are on.

Harness mirrors ``test_agent_extraction_parity`` (alembic head on the pgserver
Postgres, ASGITransport, auth off, agents enabled).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api import agent_commands as agent_commands_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent
from filearr.policy import PolicyValidationError, validate_policy

BACKEND_DIR = Path(__file__).resolve().parent.parent

#: The three permissions, with a deliberate false in the middle: an explicit
#: false must survive as an explicit false (the agent's absent-key default is
#: also false, but "central said no" and "central said nothing" are different
#: statements and the effective view must distinguish them).
CONTROL_POLICY = {
    "local_scan_control": True,
    "local_schedule_control": False,
    "local_roots_control": True,
}


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM policy_versions"))
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

    async def _noop(_ids):
        return None

    monkeypatch.setattr(agent_commands_mod, "defer_index_sync", _noop)
    monkeypatch.setattr(agent_commands_mod, "defer_agent_associate", _noop)

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


# --------------------------------------------------------------------------- #
# Round trip + effective view                                                  #
# --------------------------------------------------------------------------- #
async def test_local_control_keys_round_trip(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    r = await c.put("/api/v1/agent-policies/global", json={"policy": CONTROL_POLICY})
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == CONTROL_POLICY

    body = (await c.get(f"/api/v1/agent-policies/effective/{agent_id}")).json()
    assert body["policy"] == CONTROL_POLICY
    for key in CONTROL_POLICY:
        assert body["source_keys"][key] == "global"
    # The explicit false survives as a false, not as an omission.
    assert body["policy"]["local_schedule_control"] is False


async def test_local_control_keys_are_known_defaults_when_absent(client):
    """They must be MODELLED keys (reported as ``default``), not forward-compat
    unknowns — the console renders a field per known key."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    body = (await c.get(f"/api/v1/agent-policies/effective/{agent_id}")).json()
    for key in CONTROL_POLICY:
        assert body["source_keys"][key] == "default"
        assert key not in body["policy"]


async def test_agent_scope_wins_and_can_revoke_a_global_delegation(client):
    """Most-specific-wins with no key merging: an ``agent:`` document that turns
    a permission off is how an operator claws one machine back, and the keys the
    global document was providing fall back to the agent DEFAULT (off), not to
    the global value."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    await c.put("/api/v1/agent-policies/global", json={"policy": CONTROL_POLICY})
    r = await c.put(
        f"/api/v1/agent-policies/agent:{agent_id}",
        json={"policy": {"local_scan_control": False}},
    )
    assert r.status_code == 200, r.text

    body = (await c.get(f"/api/v1/agent-policies/effective/{agent_id}")).json()
    assert body["policy"] == {"local_scan_control": False}
    assert body["source_keys"]["local_scan_control"] == "agent"
    # local_roots_control was granted globally; the narrower document does not
    # carry it, so it is back to the agent default (off), NOT the global true.
    assert body["source_keys"]["local_roots_control"] == "default"
    assert "local_roots_control" not in body["policy"]


# --------------------------------------------------------------------------- #
# Validation                                                                    #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "key", ["local_scan_control", "local_schedule_control", "local_roots_control"]
)
async def test_local_control_keys_must_be_boolean(client, key):
    c, _, _ = client
    r = await c.put("/api/v1/agent-policies/global", json={"policy": {key: "yes please"}})
    assert r.status_code == 422
    assert key in r.json()["detail"]


def test_control_keys_do_not_relax_the_read_only_invariant():
    """Delegating scan control is a different axis from catalog writability. The
    permissions validate happily; ``read_only: false`` is still rejected, with or
    without them."""
    validate_policy({**CONTROL_POLICY, "read_only": True})
    with pytest.raises(PolicyValidationError, match="read_only cannot be disabled"):
        validate_policy({**CONTROL_POLICY, "read_only": False})
    with pytest.raises(PolicyValidationError, match="read_only cannot be disabled"):
        validate_policy({"read_only": False})


def test_control_keys_are_optional_and_additive():
    """A document that says nothing about them is valid — an existing fleet's
    policy documents keep validating unchanged."""
    validate_policy({"scan_cron": "0 3 * * *"})
    validate_policy({})
    validate_policy({"local_scan_control": True})


async def test_the_schedule_keys_they_delegate_keep_their_own_bounds(client):
    """The delegation does not widen what a schedule value may be: the agent
    validates a locally-typed interval against the SAME 300s floor central
    enforces here, so a value accepted locally would be accepted centrally."""
    c, _, _ = client
    r = await c.put(
        "/api/v1/agent-policies/global",
        json={"policy": {"local_schedule_control": True, "scan_interval_seconds": 299}},
    )
    assert r.status_code == 422
    assert "scan_interval_seconds" in r.json()["detail"]

    r = await c.put(
        "/api/v1/agent-policies/global",
        json={"policy": {"local_schedule_control": True, "scan_interval_seconds": 300}},
    )
    assert r.status_code == 200, r.text
