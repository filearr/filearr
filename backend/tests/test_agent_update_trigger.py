# ruff: noqa: E501
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
        await conn.execute(text("DELETE FROM agent_release_rollouts"))
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
        group.current_version = (group.current_version or 1) + 1
        s.add(
            AgentConfigGroupVersion(
                group_id=group.id, version=group.current_version, settings={}, policy=policy
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


# --------------------------------------------------------------------------- #
# Scheduled / held updates (2026-08-18): update_window + update_not_before      #
# --------------------------------------------------------------------------- #
async def test_update_not_before_holds_then_click_bypasses(client):
    c, maker, _ = client
    await _set_global_policy(maker, {"update_not_before": "2999-01-01T00:00:00Z"})
    aid, fp = await _seed_agent(maker, version="main-0000000")
    assert (await _poll(c, aid, fp, "main-0000000")).status_code == 204
    r = await c.get("/api/v1/agents")
    me = next(a for a in r.json()["items"] if a["id"] == str(aid))
    assert me["update_available"] is True
    assert "held until" in (me["update_hold"] or "")
    # the operator's click is the authorization
    assert (await c.post(f"/api/v1/agents/{aid}/self-update")).status_code == 201
    r = await _poll(c, aid, fp, "main-0000000")
    assert r.status_code == 200 and r.json()["version"] == DIST_VERSION


async def test_update_window_outside_holds_inside_offers(client, monkeypatch):
    c, maker, _ = client
    # A window that is certainly closed now vs certainly open now, without
    # depending on the wall clock: pick the current local weekday and hour.
    from datetime import datetime

    now = datetime.now().astimezone()
    days = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"]
    today = days[now.weekday()]
    other = days[(now.weekday() + 3) % 7]
    aid, fp = await _seed_agent(maker, version="main-0000000")
    await _set_global_policy(maker, {"update_window": f"{other} 00:00-23:59"})
    assert (await _poll(c, aid, fp, "main-0000000")).status_code == 204
    r = await c.get("/api/v1/agents")
    me = next(a for a in r.json()["items"] if a["id"] == str(aid))
    assert "outside the update window" in (me["update_hold"] or "")
    await _set_global_policy(maker, {"update_window": f"{today} 00:00-23:59"})
    assert (await _poll(c, aid, fp, "main-0000000")).status_code == 200


async def test_update_gate_keys_validated_on_group_save(client):
    c, maker, _ = client
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "held-fleet", "policy": {"update_window": "sometime soon"}},
    )
    assert r.status_code == 422, r.text
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "held-fleet", "policy": {"update_not_before": "later"}},
    )
    assert r.status_code == 422, r.text
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "held-fleet", "policy": {"update_window": "sat,sun 02:00-05:00",
                                               "update_not_before": "2026-09-01T02:00"}},
    )
    assert r.status_code == 201, r.text


# --------------------------------------------------------------------------- #
# Phased release rollouts on the tier engine (roadmap §23, 2026-08-19)          #
# --------------------------------------------------------------------------- #
async def _seed_agents_by_bucket(maker, n=40, version="main-0000000"):
    """Seed agents until we hold at least one whose bucket is < 10 and one
    >= 50 (buckets are a stable sha256 of the id, so this is deterministic
    per id but we don't control ids -- seed a handful and pick)."""
    from filearr.agent_config import agent_bucket

    low = high = None
    for _ in range(n):
        aid, fp = await _seed_agent(maker, version=version)
        b = agent_bucket(aid)
        if b < 10 and low is None:
            low = (aid, fp, b)
        if b >= 50 and high is None:
            high = (aid, fp, b)
        if low and high:
            break
    assert low and high, "could not seed both buckets"
    return low, high


async def test_release_rollout_gates_by_bucket_then_promotes(client):
    from datetime import UTC, datetime

    from filearr.worker import _advance_release_rollouts

    c, maker, _ = client
    (lo, lo_fp, _), (hi, hi_fp, hi_b) = await _seed_agents_by_bucket(maker)

    # unknown version -> 404, bad tiers -> 422
    r = await c.post("/api/v1/agent-releases/9.9.9-nope/rollouts", json={"tiers": [{"percent": 100}]})
    assert r.status_code == 404, r.text
    r = await c.post(
        f"/api/v1/agent-releases/{DIST_VERSION}/rollouts", json={"tiers": [{"percent": 50}]}
    )
    assert r.status_code == 422, r.text

    # 10% now, 100% after 60 min
    r = await c.post(
        f"/api/v1/agent-releases/{DIST_VERSION}/rollouts",
        json={"tiers": [{"percent": 10, "delay_minutes": 0}, {"percent": 100, "delay_minutes": 60}]},
    )
    assert r.status_code == 201, r.text
    rid = r.json()["id"]
    assert r.json()["status"] == "scheduled"
    # second live rollout of the same version -> 409
    r = await c.post(
        f"/api/v1/agent-releases/{DIST_VERSION}/rollouts", json={"tiers": [{"percent": 100}]}
    )
    assert r.status_code == 409

    # scheduled = covers nobody yet: both agents 204, list explains why
    assert (await _poll(c, lo, lo_fp, "main-0000000")).status_code == 204
    r = await c.get("/api/v1/agents")
    me = next(a for a in r.json()["items"] if a["id"] == str(lo))
    assert me["update_available"] is True and "scheduled to start" in me["update_hold"]

    # tick -> running at tier 0 (10%): low bucket offered, high bucket held
    t0 = datetime.now(UTC)
    assert await _advance_release_rollouts(t0) == [rid]
    assert (await _poll(c, lo, lo_fp, "main-0000000")).status_code == 200
    assert (await _poll(c, hi, hi_fp, "main-0000000")).status_code == 204
    r = await c.get("/api/v1/agents")
    me = next(a for a in r.json()["items"] if a["id"] == str(hi))
    assert f"bucket ({hi_b})" in (me["update_hold"] or "")
    r = await c.get("/api/v1/agent-release-rollouts")
    assert [x["covered_percent"] for x in r.json()] == [10]

    # the operator's click still bypasses the rollout
    assert (await c.post(f"/api/v1/agents/{hi}/self-update")).status_code == 201
    assert (await _poll(c, hi, hi_fp, "main-0000000")).status_code == 200

    # promote -> last tier -> completed -> everyone offered
    r = await c.post(f"/api/v1/agent-release-rollouts/{rid}/promote")
    assert r.status_code == 200 and r.json()["status"] == "completed"
    (lo2, lo2_fp, _), (hi2, hi2_fp, _) = await _seed_agents_by_bucket(maker)
    assert (await _poll(c, hi2, hi2_fp, "main-0000000")).status_code == 200
    # promote/cancel on a finished rollout -> 409
    assert (await c.post(f"/api/v1/agent-release-rollouts/{rid}/promote")).status_code == 409
    assert (await c.post(f"/api/v1/agent-release-rollouts/{rid}/cancel")).status_code == 409


async def test_release_rollout_cancel_stops_offering(client):
    from datetime import UTC, datetime

    from filearr.worker import _advance_release_rollouts

    c, maker, _ = client
    (lo, lo_fp, _), _hi = await _seed_agents_by_bucket(maker)
    r = await c.post(
        f"/api/v1/agent-releases/{DIST_VERSION}/rollouts",
        json={"tiers": [{"percent": 10, "delay_minutes": 0}, {"percent": 100, "delay_minutes": 60}]},
    )
    rid = r.json()["id"]
    await _advance_release_rollouts(datetime.now(UTC))
    assert (await _poll(c, lo, lo_fp, "main-0000000")).status_code == 200
    r = await c.post(f"/api/v1/agent-release-rollouts/{rid}/cancel")
    assert r.status_code == 200 and r.json()["status"] == "cancelled"
    # cancelled = not live = no restriction AND no offer via the rollout: the
    # plain channel offers again (cancel means "stop phasing", the version is
    # still central's newest -- documented: cancel cannot un-swap binaries).
    assert (await c.get("/api/v1/agent-release-rollouts")).json() == []


def test_step_rollout_state_machine():
    from datetime import UTC, datetime, timedelta

    from filearr.worker import _step_rollout

    class R:
        def __init__(self, tiers, starts_at=None):
            self.tiers, self.starts_at = tiers, starts_at
            self.status, self.current_tier = "scheduled", -1
            self.started_at = self.tier_started_at = self.finished_at = None

    t = datetime(2026, 8, 19, 12, 0, tzinfo=UTC)
    r = R([{"percent": 10, "delay_minutes": 0}, {"percent": 100, "delay_minutes": 30}])
    assert _step_rollout(r, t) and r.status == "running" and r.current_tier == 0
    assert not _step_rollout(r, t + timedelta(minutes=29))
    assert _step_rollout(r, t + timedelta(minutes=30)) and r.status == "completed"
    # scheduled in the future waits; tier-0 delay waits at tier -1
    r2 = R([{"percent": 100, "delay_minutes": 5}], starts_at=t + timedelta(minutes=10))
    assert not _step_rollout(r2, t)
    assert _step_rollout(r2, t + timedelta(minutes=10)) and r2.current_tier == -1
    assert _step_rollout(r2, t + timedelta(minutes=15)) and r2.status == "completed"
