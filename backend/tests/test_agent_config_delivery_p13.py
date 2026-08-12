"""P13 — the agent-plane delivery contract (replaces test_policy_p5t6).

The wire payload of ``GET /agents/{id}/policy`` is FROZEN: a shipped Go binary
(``agent/internal/config.Policy``) must parse it with zero changes after this
refactor. These tests assert the exact key shape, the ETag/304 protocol, the
``?applied=`` generation stamp, and that a contributing group publishing — or a
membership or priority edit that moves no version number — invalidates the
cache. The ``policy`` validation matrix (which now gates a group's ``policy``
section) rides along, and so does the W8-E taxonomy plane.

Runs against the migrated pgserver Postgres.
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
from filearr import taxonomy
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent
from filearr.policy import PolicyValidationError, validate_policy
from tests.agentcfg_helpers import join, make_group, reset_config_groups, set_global

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await reset_config_groups(conn)
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


async def _seed_agent(maker) -> tuple[uuid.UUID, str]:
    """Create an ACTIVE agent (bound fingerprint). Returns (agent_id, fingerprint)."""
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


async def _tax_version(maker) -> int:
    """The current taxonomy_state.version. Read dynamically because the migrated
    Postgres is session-shared, so an earlier taxonomy-editing test may have
    advanced it — the W8-E policy ETag/body carry whatever the live version is."""
    async with maker() as s:
        return int(
            (
                await s.execute(text("SELECT version FROM taxonomy_state WHERE id = 1"))
            ).scalar_one()
        )


async def _restore_tax(maker, version: int) -> None:
    """Undo a taxonomy edit made by a test (delete the probe ext, reset the version
    counter) so the session-shared DB stays net-unchanged for later tests that
    assert an absolute taxonomy version (e.g. test_taxonomy_w8)."""
    async with maker() as s:
        await s.execute(text("DELETE FROM file_group_extensions WHERE ext = 'zzz'"))
        await s.execute(
            text("UPDATE taxonomy_state SET version = :v WHERE id = 1"), {"v": version}
        )
        await s.commit()
    taxonomy.invalidate()


# --------------------------------------------------------------------------- #
# Policy-section validation (unchanged gate, new home)                         #
# --------------------------------------------------------------------------- #
def test_validate_policy_matrix():
    validate_policy({})
    validate_policy(
        {
            "presets": ["system_files"],
            "include_globs": ["*.mkv"],
            "exclude_globs": ["*.tmp"],
            "content_hash_max_bytes": 0,
            "watch_mode": True,
            "reconcile_interval_seconds": 300,
            "poll_interval_seconds": 60,
        }
    )
    # unknown keys pass (preserved by the caller)
    validate_policy({"future_key": {"nested": 1}, "watch_mode": False})
    with pytest.raises(PolicyValidationError):
        validate_policy(["not", "an", "object"])
    with pytest.raises(PolicyValidationError):
        validate_policy({"presets": ["nope_not_real"]})
    with pytest.raises(PolicyValidationError):
        validate_policy({"content_hash_max_bytes": -1})
    with pytest.raises(PolicyValidationError):
        validate_policy({"reconcile_interval_seconds": 299})
    with pytest.raises(PolicyValidationError):
        validate_policy({"poll_interval_seconds": 59})
    with pytest.raises(PolicyValidationError):
        validate_policy({"poll_interval_seconds": 86401})


def test_validate_policy_scan_scheduler_keys():
    """In-daemon scan scheduler keys (2026-08-03): valid combinations pass,
    a bad cron / sub-5-minute interval are rejected."""
    validate_policy(
        {"scan_cron": "0 3 * * *", "scan_interval_seconds": 21600, "scan_on_start": True}
    )
    validate_policy({"scan_interval_seconds": 300})
    with pytest.raises(PolicyValidationError):
        validate_policy({"scan_cron": "not a cron"})
    with pytest.raises(PolicyValidationError):
        validate_policy({"scan_cron": "* * * *"})  # 4 fields
    with pytest.raises(PolicyValidationError):
        validate_policy({"scan_interval_seconds": 299})


# --------------------------------------------------------------------------- #
# The FROZEN wire shape                                                        #
# --------------------------------------------------------------------------- #
async def test_empty_global_still_answers(client):
    """An agent must never fail to get a configuration: with an empty Global and
    no other groups the document is the (empty) group section only."""
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    tv = await _tax_version(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {"scope", "version", "policy"}
    assert body["scope"] == "groups"
    assert body["policy"] == {"group": {}, "taxonomy_version": tv}
    assert r.headers["etag"].startswith('"groups/')
    assert r.headers["etag"].endswith(f'/t:{tv}"')


async def test_wire_payload_matches_the_go_policy_struct(client):
    """The exact key shape a shipped ``config.Policy`` parses: merged policy keys
    at the TOP level, merged settings under ``group``, the lifted local-surface
    keys, plus the server-injected ``taxonomy_version``."""
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await set_global(
        c,
        policy={
            "watch_mode": True,
            "poll_interval_seconds": 120,
            "reconcile_interval_seconds": 3600,
            "scan_cron": "0 3 * * *",
            "extract_enabled": True,
            "upload_rate_bytes_per_sec": 1024,
            "future_key": {"deep": [1, 2]},
        },
        settings={
            "log_level": "verbose",
            "scan_schedule_cron": "30 2 * * *",
            "web_ui_enabled": True,
            "auth_required": False,
        },
    )
    tv = await _tax_version(maker)
    body = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).json()
    pol = body["policy"]

    assert pol["watch_mode"] is True
    assert pol["poll_interval_seconds"] == 120
    assert pol["reconcile_interval_seconds"] == 3600
    assert pol["scan_cron"] == "0 3 * * *"
    assert pol["extract_enabled"] is True
    assert pol["upload_rate_bytes_per_sec"] == 1024
    # unknown keys round-trip verbatim (forward-compat contract)
    assert pol["future_key"] == {"deep": [1, 2]}
    # the settings section rides under `group`, which the Go GroupSettings reads
    assert pol["group"]["scan_schedule_cron"] == "30 2 * * *"
    assert pol["group"]["log_level"] == "verbose"
    # lifted local-surface keys sit at the TOP level for the P7-T4 gate
    assert pol["web_ui_enabled"] is True
    assert pol["auth_required"] is False
    assert pol["taxonomy_version"] == tv
    # ...and `version` is the config GENERATION (an int the Go PolicyDoc stores)
    assert isinstance(body["version"], int) and body["version"] > 0


async def test_merged_document_reaches_the_agent(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True, "poll_interval_seconds": 300})
    g = await make_group(c, "high", priority=500, policy={"watch_mode": False})
    await join(c, agent_id, [g["id"]])
    pol = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).json()["policy"]
    assert pol["watch_mode"] is False  # overridden per key
    assert pol["poll_interval_seconds"] == 300  # inherited from Global


# --------------------------------------------------------------------------- #
# ETag / 304 / cache invalidation                                              #
# --------------------------------------------------------------------------- #
async def test_304_on_matching_if_none_match(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True})
    first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    etag = first.headers["etag"]
    again = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag},
    )
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert again.content == b""


async def test_group_publish_invalidates_the_etag(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    g = await make_group(c, "g", priority=500, settings={"log_level": "info"})
    await join(c, agent_id, [g["id"]])
    etag1 = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).headers["etag"]

    await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}", json={"settings": {"log_level": "debug"}}
    )
    after = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert after.status_code == 200
    assert after.headers["etag"] != etag1
    assert after.json()["policy"]["group"] == {"log_level": "debug"}


async def test_any_contributing_group_publish_invalidates(client):
    """Not just the agent's own group: a GLOBAL publish must invalidate too."""
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    g = await make_group(c, "g", priority=500, settings={"log_level": "info"})
    await join(c, agent_id, [g["id"]])
    etag1 = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).headers["etag"]
    await set_global(c, policy={"watch_mode": True})
    after = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert after.status_code == 200
    assert after.json()["policy"]["watch_mode"] is True


async def test_membership_change_alone_invalidates_the_etag(client):
    """A membership edit moves no version number, so only the content HASH half
    of the validator catches it. That is exactly why the hash is in the ETag."""
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    g = await make_group(c, "g", priority=500, policy={"watch_mode": True})
    etag1 = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).headers["etag"]
    await join(c, agent_id, [g["id"]])
    after = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert after.status_code == 200
    assert after.json()["policy"]["watch_mode"] is True


async def test_priority_flip_alone_invalidates_the_etag(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    a = await make_group(c, "a", priority=100, policy={"watch_mode": True})
    b = await make_group(c, "b", priority=200, policy={"watch_mode": False})
    await join(c, agent_id, [a["id"], b["id"]])
    first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    assert first.json()["policy"]["watch_mode"] is False  # b wins at 200
    etag1 = first.headers["etag"]

    await c.patch(f"/api/v1/agents/config-groups/{a['id']}", json={"priority": 900})
    after = await c.get(
        f"/api/v1/agents/{agent_id}/policy",
        headers={**_auth(fp), "If-None-Match": etag1},
    )
    assert after.status_code == 200
    assert after.json()["policy"]["watch_mode"] is True  # a now outranks b


# --------------------------------------------------------------------------- #
# Generation monotonicity + ?applied stamping                                  #
# --------------------------------------------------------------------------- #
async def test_generation_is_monotonic_across_groups(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    seen = []
    for i in range(3):
        await set_global(c, policy={"poll_interval_seconds": 60 + i})
        seen.append(
            (await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))).json()[
                "version"
            ]
        )
    assert seen == sorted(seen) and len(set(seen)) == 3
    # A DIFFERENT group publishing also advances this agent's generation, because
    # the generation is the global sequence, not a per-group counter.
    g = await make_group(c, "g", priority=500, policy={"watch_mode": True})
    await join(c, agent_id, [g["id"]])
    latest = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).json()["version"]
    assert latest > seen[-1]


async def test_applied_stamps_config_generation(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True})
    poll = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    delivered = poll.json()["version"]
    await c.get(
        f"/api/v1/agents/{agent_id}/policy?applied={delivered}", headers=_auth(fp)
    )
    async with maker() as s:
        a = await s.get(Agent, agent_id)
        assert a.config_generation_applied == delivered
        assert a.last_seen_at is not None
    # ...and the admin view reports it as the CONFIRMED generation.
    eff = (await c.get(f"/api/v1/agents/{agent_id}/effective-config")).json()
    assert eff["confirmed_generation"] == delivered
    assert eff["generation"] == delivered


async def test_confirmed_generation_lags_after_a_publish(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True})
    delivered = (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).json()["version"]
    await c.get(
        f"/api/v1/agents/{agent_id}/policy?applied={delivered}", headers=_auth(fp)
    )
    await set_global(c, policy={"watch_mode": False})
    eff = (await c.get(f"/api/v1/agents/{agent_id}/effective-config")).json()
    assert eff["confirmed_generation"] == delivered
    assert eff["generation"] > delivered  # published, not yet enforced


# --------------------------------------------------------------------------- #
# Auth + feature gate                                                          #
# --------------------------------------------------------------------------- #
async def test_policy_requires_agent_credential(client):
    c, maker, _ = client
    agent_id, fp = await _seed_agent(maker)
    assert (await c.get(f"/api/v1/agents/{agent_id}/policy")).status_code == 401
    bad = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth("nope"))
    assert bad.status_code == 401


async def test_feature_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (
        await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
    ).status_code == 404


# --------------------------------------------------------------------------- #
# W8-E taxonomy: version in the doc/ETag + the agent-plane taxonomy endpoint     #
# --------------------------------------------------------------------------- #
async def test_policy_taxonomy_version_folds_into_etag_and_bumps(client):
    """A taxonomy edit invalidates the agent's policy cache: the /t:<v> ETag
    suffix advances and the policy body's taxonomy_version follows."""
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True})
    start = await _tax_version(maker)
    try:
        first = await c.get(f"/api/v1/agents/{agent_id}/policy", headers=_auth(fp))
        assert first.json()["policy"]["taxonomy_version"] == start
        etag = first.headers["etag"]
        assert etag.endswith(f'/t:{start}"')

        again = await c.get(
            f"/api/v1/agents/{agent_id}/policy",
            headers={**_auth(fp), "If-None-Match": etag},
        )
        assert again.status_code == 304

        r = await c.post(
            "/api/v1/taxonomy/groups/raster-photo/extensions", json={"ext": "zzz"}
        )
        assert r.status_code == 200 and r.json()["version"] == start + 1

        after = await c.get(
            f"/api/v1/agents/{agent_id}/policy",
            headers={**_auth(fp), "If-None-Match": etag},
        )
        assert after.status_code == 200
        assert after.headers["etag"].endswith(f'/t:{start + 1}"')
        assert after.json()["policy"]["taxonomy_version"] == start + 1
    finally:
        await _restore_tax(maker, start)


async def test_agent_taxonomy_endpoint_shape(client):
    """The compact agent taxonomy payload: version + flat maps + primary set."""
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    tv = await _tax_version(maker)
    r = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
    assert r.status_code == 200
    body = r.json()
    assert set(body) == {
        "version",
        "ext_to_group",
        "group_to_category",
        "category_extractor",
        "primary_categories",
    }
    assert body["version"] == tv
    assert body["ext_to_group"]["mkv"] == "video"
    assert body["ext_to_group"]["flac"] == "audio-lossless"
    assert body["group_to_category"]["video"] == "video"
    assert body["group_to_category"]["audio-lossless"] == "audio"
    assert body["category_extractor"]["image"] == "image"
    assert body["category_extractor"]["three-d-cad"] == "model3d"
    assert body["category_extractor"]["archive"] is None
    assert body["primary_categories"] == [
        "image",
        "audio",
        "video",
        "document",
        "three-d-cad",
    ]


async def test_agent_taxonomy_endpoint_reflects_edit(client):
    c, maker, _ = client
    taxonomy.invalidate()
    agent_id, fp = await _seed_agent(maker)
    start = await _tax_version(maker)
    try:
        await c.post(
            "/api/v1/taxonomy/groups/raster-photo/extensions", json={"ext": "zzz"}
        )
        r = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
        assert r.json()["version"] == start + 1
        assert r.json()["ext_to_group"]["zzz"] == "raster-photo"
    finally:
        await _restore_tax(maker, start)


async def test_agent_taxonomy_requires_agent_credential(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    assert (await c.get(f"/api/v1/agents/{agent_id}/taxonomy")).status_code == 401
    bad = await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth("nope"))
    assert bad.status_code == 401


async def test_agent_taxonomy_feature_gated(client, monkeypatch):
    c, maker, settings = client
    agent_id, fp = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (
        await c.get(f"/api/v1/agents/{agent_id}/taxonomy", headers=_auth(fp))
    ).status_code == 404
