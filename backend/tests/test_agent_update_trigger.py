"""Central-version-driven agent updates (2026-08-05).

Covers the three new behaviors layered onto the P5-T7 update plane:

* the server-side ``auto_update`` policy gate on the update-manifest poll
  (absent = offer, false = 204, in-flight self_update command overrides);
* the UNSIGNED dist-fallback manifest derived from the agent-dist bake (plus
  ``key_pinned=true`` suppression and the virtual-release artifact download);
* the operator trigger endpoint (``POST /agents/{id}/self-update``) and the
  update surfacing fields on the agents list.

Harness mirrors test_agent_updates_p5t7 (migrated pgserver Postgres).
"""

from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentConfigGroup, AgentConfigGroupVersion
from tests.agentcfg_helpers import reset_config_groups

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent

DIST_VERSION = "main-1a2b3c4"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM agent_releases"))
        await reset_config_groups(conn)
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch, tmp_path):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "agent_releases_dir", str(tmp_path / "releases"))

    # An agent-dist bake with one linux binary.
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "filearr-agent-linux-amd64").write_bytes(b"DIST-LINUX-BYTES")
    (dist / "VERSION").write_text(DIST_VERSION + "\n", encoding="utf-8")
    monkeypatch.setattr(settings, "agent_dist_dir", str(dist))

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, version=None):
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(
            name="a",
            hostname="a",
            platform="linux",
            cert_fingerprint=fp,
            agent_version=version,
        )
        s.add(agent)
        await s.commit()
        return agent.id, fp


async def _set_global_policy(maker, policy: dict):
    """Publish ``policy`` into the permanent Global group (P13: the auto_update
    gate now reads the layered configuration, not a policy scope)."""
    async with maker() as s:
        group = (
            await s.execute(
                select(AgentConfigGroup).where(AgentConfigGroup.is_system.is_(True))
            )
        ).scalars().one()
        group.policy = policy
        group.current_version = 2
        s.add(
            AgentConfigGroupVersion(
                group_id=group.id, version=2, settings={}, policy=policy
            )
        )
        await s.commit()


def _auth(fp: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {fp}"}


async def _poll(c, aid, fp, current, key_pinned=False):
    pinned = "true" if key_pinned else "false"
    return await c.get(
        f"/api/v1/agents/{aid}/update-manifest?current={current}&key_pinned={pinned}",
        headers=_auth(fp),
    )


# --------------------------------------------------------------------------- #
# Dist-fallback manifest                                                       #
# --------------------------------------------------------------------------- #
async def test_dist_fallback_served_when_version_differs(client):
    c, maker, _ = client
    aid, fp = await _seed_agent(maker)
    r = await _poll(c, aid, fp, "main-0000000")
    assert r.status_code == 200, r.text
    m = r.json()
    assert m["version"] == DIST_VERSION
    assert "signature" not in m
    (art,) = m["artifacts"]
    assert art["platform"] == "linux" and art["arch"] == "amd64"
    assert art["url"] == "filearr-agent-linux-amd64"
    assert art["sha256"] == hashlib.sha256(b"DIST-LINUX-BYTES").hexdigest()

    # the virtual-release artifact download serves the dist file
    r = await c.get(
        f"/api/v1/agents/{aid}/releases/{DIST_VERSION}/artifacts/filearr-agent-linux-amd64",
        headers=_auth(fp),
    )
    assert r.status_code == 200
    assert r.content == b"DIST-LINUX-BYTES"
    # but never anything outside the artifact listing
    r = await c.get(
        f"/api/v1/agents/{aid}/releases/{DIST_VERSION}/artifacts/VERSION",
        headers=_auth(fp),
    )
    assert r.status_code == 404


async def test_dist_fallback_204_when_current_matches_or_pinned(client):
    c, maker, _ = client
    aid, fp = await _seed_agent(maker)
    r = await _poll(c, aid, fp, DIST_VERSION)
    assert r.status_code == 204  # exact current build
    r = await _poll(c, aid, fp, "main-0000000", key_pinned=True)
    assert r.status_code == 204  # pinned builds never get unsigned bits


# --------------------------------------------------------------------------- #
# auto_update policy gate                                                      #
# --------------------------------------------------------------------------- #
async def test_auto_update_false_gates_offer_but_still_records_version(client):
    c, maker, _ = client
    await _set_global_policy(maker, {"auto_update": False})
    aid, fp = await _seed_agent(maker)
    r = await _poll(c, aid, fp, "main-0000000")
    assert r.status_code == 204
    async with maker() as s:
        agent = await s.get(Agent, aid)
        assert agent.agent_version == "main-0000000"  # stamp happens before gate


async def test_auto_update_absent_offers(client):
    c, maker, _ = client
    await _set_global_policy(maker, {"web_ui_enabled": True})  # no auto_update key
    aid, fp = await _seed_agent(maker)
    r = await _poll(c, aid, fp, "main-0000000")
    assert r.status_code == 200


async def test_pending_command_overrides_auto_update_false(client):
    c, maker, _ = client
    await _set_global_policy(maker, {"auto_update": False})
    aid, fp = await _seed_agent(maker, version="main-0000000")
    r = await c.post(f"/api/v1/agents/{aid}/self-update")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["target"] == DIST_VERSION
    # gated policy, but the in-flight command authorizes the offer
    r = await _poll(c, aid, fp, "main-0000000")
    assert r.status_code == 200
    assert r.json()["version"] == DIST_VERSION


# --------------------------------------------------------------------------- #
# Trigger endpoint + list surfacing                                            #
# --------------------------------------------------------------------------- #
async def test_trigger_conflicts(client):
    c, maker, _ = client
    aid, _fp = await _seed_agent(maker, version=DIST_VERSION)
    # up to date -> 409
    r = await c.post(f"/api/v1/agents/{aid}/self-update")
    assert r.status_code == 409
    # available -> 201, second click -> 409 (already queued)
    aid2, _ = await _seed_agent(maker, version="main-0000000")
    assert (await c.post(f"/api/v1/agents/{aid2}/self-update")).status_code == 201
    assert (await c.post(f"/api/v1/agents/{aid2}/self-update")).status_code == 409


async def test_agents_list_surfaces_update_fields(client):
    c, maker, _ = client
    stale_id, _ = await _seed_agent(maker, version="main-0000000")
    fresh_id, _ = await _seed_agent(maker, version=DIST_VERSION)
    await c.post(f"/api/v1/agents/{stale_id}/self-update")

    r = await c.get("/api/v1/agents")
    assert r.status_code == 200, r.text
    by_id = {a["id"]: a for a in r.json()["items"]}
    stale = by_id[str(stale_id)]
    assert stale["update_available"] is True
    assert stale["update_target"] == DIST_VERSION
    assert stale["update_pending"] is True
    fresh = by_id[str(fresh_id)]
    assert fresh["update_available"] is False
    assert fresh["update_target"] is None
    assert fresh["update_pending"] is False


async def test_signed_release_beats_dist_fallback(client):
    c, maker, _ = client
    aid, fp = await _seed_agent(maker)
    art = b"SIGNED-RELEASE-BYTES"
    manifest = {
        "version": "9.9.9",
        "created_at": "2026-08-05T00:00:00Z",
        "signature": "sig",
        "artifacts": [
            {
                "platform": "linux",
                "arch": "amd64",
                "sha256": hashlib.sha256(art).hexdigest(),
                "size": len(art),
                "url": "filearr-agent-linux-amd64",
            }
        ],
    }
    r = await c.post("/api/v1/agent-releases", json=manifest)
    assert r.status_code == 201, r.text
    r = await c.put(
        "/api/v1/agent-releases/9.9.9/artifacts/filearr-agent-linux-amd64", content=art
    )
    assert r.status_code == 200, r.text
    # P13: no promote step — a fully-uploaded release is fleet-visible at once.

    r = await _poll(c, aid, fp, "1.0.0")
    assert r.status_code == 200
    assert r.json()["version"] == "9.9.9"  # signed channel wins over dist


# --------------------------------------------------------------------------- #
# Containerized agents: flagged, never offered (2026-08-07)                    #
# --------------------------------------------------------------------------- #
async def _mark_container(maker, aid):
    async with maker() as s:
        agent = await s.get(Agent, aid)
        agent.capabilities = {"container": True, "inventory_version": 1}
        await s.commit()


async def test_container_agent_poll_204_but_version_recorded(client):
    """An agent whose stored capabilities carry ``container: true`` is NEVER
    served a manifest (its update mechanism is an image pull), even when the
    dist version differs — but the poll still records the confirmed running
    version + liveness."""
    c, maker, _ = client
    aid, fp = await _seed_agent(maker)
    await _mark_container(maker, aid)
    r = await _poll(c, aid, fp, "main-0000000")  # differs -> would offer dist
    assert r.status_code == 204
    async with maker() as s:
        agent = await s.get(Agent, aid)
        assert agent.agent_version == "main-0000000"
        assert agent.last_seen_at is not None


async def test_container_agent_trigger_409_but_list_still_flags(client):
    """The operator trigger refuses container agents with a pull-the-image
    message, while the agents list still FLAGS the newer build
    (update_available + target) so the console can badge it."""
    c, maker, _ = client
    aid, _fp = await _seed_agent(maker, version="main-0000000")
    await _mark_container(maker, aid)

    r = await c.post(f"/api/v1/agents/{aid}/self-update")
    assert r.status_code == 409
    assert "pulling a new agent image" in r.json()["detail"]

    r = await c.get("/api/v1/agents")
    row = {a["id"]: a for a in r.json()["items"]}[str(aid)]
    assert row["update_available"] is True
    assert row["update_target"] == DIST_VERSION
    assert row["update_pending"] is False
    assert row["capabilities"]["container"] is True
