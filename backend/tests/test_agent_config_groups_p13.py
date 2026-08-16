"""P13 — the unified config-group admin surface (replaces test_agent_config_groups_w6
and test_agent_policy_effective).

Covers: CRUD + audit; the settings/policy validation matrices; versioned history
+ rollback; the LAYERED per-key merge across priorities (order, tie-break,
section isolation, lift precedence); Global-group protections; multi-group
membership; the effective-config endpoint's provenance/generation; rollout
creation + tier 422s + one-live-per-group; installer distribution + register.

Runs against the migrated pgserver Postgres (mirrors test_agent_commands's harness).
"""

from __future__ import annotations

import json
import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from joserfc.jwk import ECKey
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import agent_config
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent
from tests.agentcfg_helpers import (
    effective,
    global_group_id,
    join,
    make_group,
    reset_config_groups,
    set_global,
)

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# DB harness                                                                   #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await reset_config_groups(conn)
        await conn.execute(text("DELETE FROM agents"))
        await conn.execute(text("DELETE FROM enrollment_tokens"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


# Register refuses (503, token kept) unless central can mint a step-ca OTT, so
# every client fixture supplies a throwaway provisioner key + CA URL.
_TEST_PROVISIONER_JWK = json.dumps(ECKey.generate_key("P-256").as_dict(private=True))


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "ca_provisioner_jwk", _TEST_PROVISIONER_JWK)
    monkeypatch.setattr(settings, "ca_url", "https://ca.filearr.lan:9000")
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, *, hostname="nas"):
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name=hostname, hostname=hostname, platform="linux", cert_fingerprint=fp)
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


async def _events(maker, event_type: str) -> list[dict]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT details FROM security_events WHERE event_type = :et"),
                {"et": event_type},
            )
        ).all()
    return [r.details for r in rows]


# --------------------------------------------------------------------------- #
# Pure — settings validation matrix (unchanged by P13, still the settings half) #
# --------------------------------------------------------------------------- #
def test_validate_settings_accepts_env_and_glob_specs():
    agent_config.validate_settings(
        {
            "log_level": "debug",
            "scan_selections": [
                {
                    "preset": "user-documents",
                    "paths": [
                        "%USERPROFILE%/Documents",
                        "$HOME/documents",
                        "~/Documents",
                        "/home/*/documents",
                        "/data/{a,b}/[abc]*",
                    ],
                    "include_regex": [r".*\.pdf$"],
                    "exclude_regex": [r"^~\$"],
                    "enabled": True,
                }
            ],
            "inventory": {"enabled": True, "collectors": ["stat", "owner"]},
            "scan_schedule_cron": "0 3 * * *",
        }
    )


def test_validate_settings_all_presets_ok():
    for name in agent_config.SCAN_PRESET_NAMES:
        agent_config.validate_settings({"scan_selections": [{"preset": name}]})


@pytest.mark.parametrize(
    "bad",
    [
        {"log_levl": "debug"},  # unknown top-level key
        {"scan_selections": [{"preset": "nope"}]},
        {"scan_selections": [{"include_regex": ["("]}]},
        {"scan_schedule_cron": "not a cron"},
        {"scan_selections": [{"paths": ["/home/[user"]}]},
    ],
)
def test_validate_settings_rejects(bad):
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(bad)


def test_validate_settings_oversize():
    # 50 selections * ~4000-char path spec > 64 KiB compact-JSON ceiling
    big = {"scan_selections": [{"paths": ["/p/" + "a" * 4000]} for _ in range(50)]}
    with pytest.raises(agent_config.GroupSettingsValidationError):
        agent_config.validate_settings(big)


def test_group_settings_validate_local_surface_keys():
    from pydantic import ValidationError

    from filearr.agent_config import GroupSettings

    s = GroupSettings(web_ui_enabled=True, local_access_enabled=False, auth_required=True)
    assert s.web_ui_enabled is True and s.local_access_enabled is False
    with pytest.raises(ValidationError):
        GroupSettings(webui_enabled=True)  # typo stays a hard 422


# --------------------------------------------------------------------------- #
# CRUD + audit                                                                 #
# --------------------------------------------------------------------------- #
async def test_global_group_exists_and_is_first(client):
    c, _, _ = client
    rows = (await c.get("/api/v1/agents/config-groups")).json()
    assert rows[0]["is_system"] is True
    assert rows[0]["priority"] == 0
    assert rows[0]["name"] == "Global"
    assert rows[0]["current_version"] == 1


async def test_create_get_list_group(client):
    c, maker, _ = client
    r = await c.post(
        "/api/v1/agents/config-groups",
        json={
            "name": "workstations",
            "description": "office desktops",
            "priority": 200,
            "settings": {"log_level": "info"},
            "policy": {"watch_mode": True},
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    gid = body["id"]
    assert body["member_count"] == 0
    assert body["settings"] == {"log_level": "info"}
    assert body["policy"] == {"watch_mode": True}
    assert body["current_version"] == 1
    assert body["active_rollout"] is None

    got = (await c.get(f"/api/v1/agents/config-groups/{gid}")).json()
    assert got["name"] == "workstations"
    # v1 is snapshotted on create, so history is never empty.
    assert [v["version"] for v in got["versions"]] == [1]
    assert got["versions"][0]["seq"] > 0

    lst = (await c.get("/api/v1/agents/config-groups")).json()
    # merge order: Global (0) first, then the new group.
    assert [g["name"] for g in lst] == ["Global", "workstations"]

    assert any(
        d["name"] == "workstations" for d in await _events(maker, "agent_config_group_created")
    )


async def test_create_duplicate_name_409(client):
    c, _, _ = client
    await make_group(c, "dup")
    r = await c.post("/api/v1/agents/config-groups", json={"name": "dup"})
    assert r.status_code == 409


async def test_create_invalid_settings_or_policy_422(client):
    c, _, _ = client
    bad_settings = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "bad1", "settings": {"scan_schedule_cron": "nope"}},
    )
    assert bad_settings.status_code == 422
    bad_policy = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "bad2", "policy": {"poll_interval_seconds": 5}},
    )
    assert bad_policy.status_code == 422
    # read_only=False stays rejected (the local surface is read-only by invariant)
    ro = await c.post(
        "/api/v1/agents/config-groups",
        json={"name": "bad3", "policy": {"read_only": False}},
    )
    assert ro.status_code == 422


async def test_oversize_policy_413(client):
    c, _, settings = client
    big = {"blob": "x" * (settings.agent_policy_max_bytes + 100)}
    r = await c.post("/api/v1/agents/config-groups", json={"name": "huge", "policy": big})
    assert r.status_code == 413


async def test_update_publishes_a_new_version(client):
    c, maker, _ = client
    g = await make_group(c, "g", settings={"log_level": "info"})
    ok = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"settings": {"log_level": "warn"}, "description": "d", "note": "why"},
    )
    assert ok.status_code == 200
    assert ok.json()["settings"] == {"log_level": "warn"}
    assert ok.json()["current_version"] == 2

    hist = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history")).json()
    assert [h["version"] for h in hist] == [2, 1]
    assert hist[0]["note"] == "why"
    assert hist[0]["settings"] == {"log_level": "warn"}
    assert hist[1]["settings"] == {"log_level": "info"}
    # generations are globally monotonic, so the newer snapshot has the higher seq
    assert hist[0]["seq"] > hist[1]["seq"]

    published = await _events(maker, "agent_config_version_published")
    assert any(e["version"] == 2 and e["immediate"] is True for e in published)


async def test_update_metadata_only_does_not_publish(client):
    c, _, _ = client
    g = await make_group(c, "g")
    r = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}", json={"description": "just a note"}
    )
    assert r.status_code == 200
    assert r.json()["current_version"] == 1
    hist = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history")).json()
    assert len(hist) == 1


async def test_update_invalid_settings_422_and_leaves_version_alone(client):
    c, _, _ = client
    g = await make_group(c, "g")
    bad = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}", json={"settings": {"log_level": "loud"}}
    )
    assert bad.status_code == 422
    assert (await c.get(f"/api/v1/agents/config-groups/{g['id']}")).json()["current_version"] == 1


async def test_history_keyset_and_cap(client):
    c, _, _ = client
    g = await make_group(c, "g")
    for i in range(3):
        await c.patch(
            f"/api/v1/agents/config-groups/{g['id']}",
            json={"policy": {"poll_interval_seconds": 60 + i}},
        )
    hist = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history")).json()
    assert [h["version"] for h in hist] == [4, 3, 2, 1]
    limited = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history?limit=1")).json()
    assert [h["version"] for h in limited] == [4]
    before = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history?before=3")).json()
    assert [h["version"] for h in before] == [2, 1]


async def test_rollback_copies_forward(client):
    c, maker, _ = client
    g = await make_group(c, "g", policy={"watch_mode": True})
    await c.patch(f"/api/v1/agents/config-groups/{g['id']}", json={"policy": {"watch_mode": False}})
    r = await c.post(f"/api/v1/agents/config-groups/{g['id']}/rollback", json={"version": 1})
    assert r.status_code == 200
    # forward-only: the restored document lands as a NEW version 3
    assert r.json()["current_version"] == 3
    assert r.json()["policy"] == {"watch_mode": True}
    hist = (await c.get(f"/api/v1/agents/config-groups/{g['id']}/history")).json()
    assert [h["version"] for h in hist] == [3, 2, 1]
    assert "rollback" in hist[0]["note"]
    assert any(
        e.get("rollback_of") == 1 for e in await _events(maker, "agent_config_version_published")
    )


async def test_rollback_unknown_version_404(client):
    c, _, _ = client
    g = await make_group(c, "g")
    r = await c.post(f"/api/v1/agents/config-groups/{g['id']}/rollback", json={"version": 99})
    assert r.status_code == 404


async def test_delete_group_removes_membership(client):
    c, maker, _ = client
    g = await make_group(c, "g")
    agent_id, _ = await _seed_agent(maker)
    assert (await join(c, agent_id, [g["id"]])).status_code == 200
    assert (await c.get(f"/api/v1/agents/config-groups/{g['id']}")).json()["member_count"] == 1

    d = await c.delete(f"/api/v1/agents/config-groups/{g['id']}")
    assert d.status_code == 204
    # Membership cascaded; the agent still resolves Global.
    eff = await effective(c, agent_id)
    assert [grp["name"] for grp in eff["groups"]] == ["Global"]
    assert any(e["members_reset"] == 1 for e in await _events(maker, "agent_config_group_deleted"))


# --------------------------------------------------------------------------- #
# Global-group protections                                                     #
# --------------------------------------------------------------------------- #
async def test_global_cannot_be_deleted(client):
    c, _, _ = client
    gid = await global_group_id(c)
    assert (await c.delete(f"/api/v1/agents/config-groups/{gid}")).status_code == 409


async def test_global_name_and_priority_are_fixed(client):
    c, _, _ = client
    gid = await global_group_id(c)
    assert (
        await c.patch(f"/api/v1/agents/config-groups/{gid}", json={"name": "Everything"})
    ).status_code == 409
    assert (
        await c.patch(f"/api/v1/agents/config-groups/{gid}", json={"priority": 500})
    ).status_code == 409


async def test_global_documents_are_editable(client):
    c, _, _ = client
    r = await set_global(c, policy={"watch_mode": True})
    assert r.status_code == 200
    assert r.json()["current_version"] == 2
    assert r.json()["policy"] == {"watch_mode": True}


async def test_global_member_count_is_the_whole_fleet(client):
    c, maker, _ = client
    await _seed_agent(maker, hostname="a")
    await _seed_agent(maker, hostname="b")
    rows = (await c.get("/api/v1/agents/config-groups")).json()
    assert next(g for g in rows if g["is_system"])["member_count"] == 2


async def test_global_cannot_be_added_as_explicit_membership(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    gid = await global_group_id(c)
    r = await join(c, agent_id, [gid])
    assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Membership + the LAYERED merge                                               #
# --------------------------------------------------------------------------- #
async def test_membership_put_replaces_the_whole_set(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    a = await make_group(c, "a", priority=100)
    b = await make_group(c, "b", priority=200)

    r = await join(c, agent_id, [a["id"], b["id"]])
    assert r.status_code == 200
    assert set(r.json()["group_ids"]) == {a["id"], b["id"]}

    # PUT semantics: the second call REPLACES, it does not add.
    r2 = await join(c, agent_id, [b["id"]])
    assert r2.json()["group_ids"] == [b["id"]]
    eff = await effective(c, agent_id)
    assert [g["name"] for g in eff["groups"]] == ["Global", "b"]

    # ...and an empty list removes everything explicit.
    assert (await join(c, agent_id, [])).json()["group_ids"] == []
    assert [g["name"] for g in (await effective(c, agent_id))["groups"]] == ["Global"]
    assert await _events(maker, "agent_config_group_assigned")


async def test_membership_unknown_agent_or_group_404(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    assert (await join(c, uuid.uuid4(), [])).status_code == 404
    assert (await join(c, agent_id, [str(uuid.uuid4())])).status_code == 404


async def test_layered_merge_last_priority_wins_per_key(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    await set_global(c, policy={"watch_mode": True, "poll_interval_seconds": 300})
    low = await make_group(c, "low", priority=100, policy={"watch_mode": False})
    high = await make_group(c, "high", priority=900, policy={"poll_interval_seconds": 600})
    await join(c, agent_id, [high["id"], low["id"]])

    eff = await effective(c, agent_id)
    # Per-KEY: `low` overrode only watch_mode, `high` only poll_interval_seconds,
    # and Global's untouched keys survive rather than being replaced wholesale.
    assert eff["document"]["watch_mode"] is False
    assert eff["document"]["poll_interval_seconds"] == 600
    assert [g["name"] for g in eff["groups"]] == ["Global", "low", "high"]
    assert eff["provenance"]["policy.watch_mode"]["group_name"] == "low"
    assert eff["provenance"]["policy.poll_interval_seconds"]["group_name"] == "high"


async def test_layered_merge_equal_priority_breaks_ties_by_name(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    alpha = await make_group(c, "alpha", priority=500, policy={"watch_mode": True})
    zulu = await make_group(c, "zulu", priority=500, policy={"watch_mode": False})
    await join(c, agent_id, [zulu["id"], alpha["id"]])
    eff = await effective(c, agent_id)
    assert [g["name"] for g in eff["groups"]] == ["Global", "alpha", "zulu"]
    assert eff["document"]["watch_mode"] is False  # zulu sorts last, so zulu wins


async def test_layered_merge_sections_are_independent(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    await set_global(c, settings={"log_level": "info"}, policy={"watch_mode": True})
    over = await make_group(c, "over", priority=300, settings={"log_level": "debug"})
    await join(c, agent_id, [over["id"]])
    eff = await effective(c, agent_id)
    # The settings override did not disturb the policy section.
    assert eff["document"]["group"]["log_level"] == "debug"
    assert eff["document"]["watch_mode"] is True
    assert eff["provenance"]["settings.log_level"]["group_name"] == "over"
    assert eff["provenance"]["policy.watch_mode"]["group_name"] == "Global"


async def test_layered_merge_nested_objects_replace_wholesale(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    await set_global(c, settings={"inventory": {"enabled": True, "collectors": ["stat", "owner"]}})
    g = await make_group(c, "narrow", priority=400, settings={"inventory": {"enabled": True}})
    await join(c, agent_id, [g["id"]])
    eff = await effective(c, agent_id)
    # Documented: shallow merge at the top of each section — the nested object is
    # replaced, not deep-merged, so `collectors` is gone rather than half-kept.
    assert eff["document"]["group"]["inventory"] == {"enabled": True}


async def test_layered_merge_lift_precedence(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    await set_global(c, policy={"web_ui_enabled": True})
    g = await make_group(c, "locked", priority=300, settings={"web_ui_enabled": False})
    await join(c, agent_id, [g["id"]])
    eff = await effective(c, agent_id)
    # Settings win the lift, so a group can turn OFF what the raw policy turned on.
    assert eff["document"]["web_ui_enabled"] is False
    assert eff["document"]["group"]["web_ui_enabled"] is False


async def test_effective_config_reports_generation_and_hash(client):
    c, maker, _ = client
    agent_id, _ = await _seed_agent(maker)
    before = await effective(c, agent_id)
    await set_global(c, policy={"watch_mode": True})
    after = await effective(c, agent_id)
    assert after["generation"] > before["generation"]
    assert after["hash"] != before["hash"]
    assert after["confirmed_generation"] is None  # never polled
    assert after["agent_id"] == str(agent_id)


async def test_effective_config_unknown_agent_404(client):
    c, _, _ = client
    assert (await c.get(f"/api/v1/agents/{uuid.uuid4()}/effective-config")).status_code == 404


# --------------------------------------------------------------------------- #
# Rollouts (API half; the tick engine is test_config_rollout_engine_p13)        #
# --------------------------------------------------------------------------- #
async def test_publish_with_rollout_holds_current_version(client):
    c, maker, _ = client
    g = await make_group(c, "g", policy={"watch_mode": False})
    r = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={
            "policy": {"watch_mode": True},
            "rollout": {"tiers": [{"percent": 10}, {"percent": 100, "delay_minutes": 60}]},
        },
    )
    assert r.status_code == 200
    body = r.json()
    # The new version exists but nobody has it yet — current_version is untouched.
    assert body["current_version"] == 1
    assert body["active_rollout"]["target_version"] == 2
    assert body["active_rollout"]["status"] == "scheduled"
    assert body["active_rollout"]["current_tier"] == -1
    assert any(
        e["target_version"] == 2 for e in await _events(maker, "agent_config_rollout_created")
    )


async def test_second_live_rollout_409(client):
    c, _, _ = client
    g = await make_group(c, "g")
    first = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": True}, "rollout": {"tiers": [{"percent": 100}]}},
    )
    assert first.status_code == 200
    second = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": False}, "rollout": {"tiers": [{"percent": 100}]}},
    )
    assert second.status_code == 409


@pytest.mark.parametrize(
    "tiers",
    [
        [],
        [{"percent": 50}],  # last is not 100
        [{"percent": 50}, {"percent": 20}, {"percent": 100}],  # not ascending
        [
            {"percent": 1},
            {"percent": 2},
            {"percent": 3},
            {"percent": 4},
            {"percent": 5},
            {"percent": 100},
        ],  # six tiers
        [{"percent": 100, "delay_minutes": -5}],
        [{"percent": 0}, {"percent": 100}],
    ],
)
async def test_bad_tiers_422(client, tiers):
    c, _, _ = client
    g = await make_group(c, f"g-{uuid.uuid4().hex[:8]}")
    r = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": True}, "rollout": {"tiers": tiers}},
    )
    assert r.status_code == 422


async def test_rollout_without_document_change_422(client):
    c, _, _ = client
    g = await make_group(c, "g")
    r = await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"description": "x", "rollout": {"tiers": [{"percent": 100}]}},
    )
    assert r.status_code == 422


async def test_rollout_list_cancel_and_fallback(client):
    c, maker, _ = client
    g = await make_group(c, "g", policy={"watch_mode": False})
    await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": True}, "rollout": {"tiers": [{"percent": 100}]}},
    )
    live = (await c.get("/api/v1/agents/config-rollouts")).json()
    assert len(live) == 1 and live[0]["group_name"] == "g"
    assert live[0]["covered_percent"] == 0

    rid = live[0]["id"]
    cancelled = await c.post(f"/api/v1/agents/config-rollouts/{rid}/cancel")
    assert cancelled.status_code == 200 and cancelled.json()["status"] == "cancelled"
    # current_version never moved, so members stay on the old document.
    assert (await c.get(f"/api/v1/agents/config-groups/{g['id']}")).json()["current_version"] == 1
    assert (await c.get("/api/v1/agents/config-rollouts")).json() == []
    assert (await c.post(f"/api/v1/agents/config-rollouts/{rid}/cancel")).status_code == 409
    assert await _events(maker, "agent_config_rollout_cancelled")


async def test_promote_requires_running(client):
    c, _, _ = client
    g = await make_group(c, "g")
    await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": True}, "rollout": {"tiers": [{"percent": 100}]}},
    )
    rid = (await c.get("/api/v1/agents/config-rollouts")).json()[0]["id"]
    # still `scheduled` (the worker tick has not run) -> promote is a 409
    assert (await c.post(f"/api/v1/agents/config-rollouts/{rid}/promote")).status_code == 409


async def test_rollback_cancels_a_live_rollout(client):
    c, _, _ = client
    g = await make_group(c, "g", policy={"watch_mode": False})
    await c.patch(
        f"/api/v1/agents/config-groups/{g['id']}",
        json={"policy": {"watch_mode": True}, "rollout": {"tiers": [{"percent": 100}]}},
    )
    r = await c.post(f"/api/v1/agents/config-groups/{g['id']}/rollback", json={"version": 1})
    assert r.status_code == 200
    assert (await c.get("/api/v1/agents/config-rollouts")).json() == []


async def test_rollout_unknown_id_404(client):
    c, _, _ = client
    assert (
        await c.post(f"/api/v1/agents/config-rollouts/{uuid.uuid4()}/cancel")
    ).status_code == 404


# --------------------------------------------------------------------------- #
# Installer distribution + register                                            #
# --------------------------------------------------------------------------- #
async def test_installer_config_frozen_shape(client):
    c, maker, _ = client
    g = await make_group(c, "wg")
    r = await c.post(
        "/api/v1/agents/installer-config",
        json={
            "agent_name": "lab-01",
            "config_group_ids": [g["id"]],
            "log_level": "info",
            "central_url_override": "https://filearr.example.com",
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert set(body) == {"sidecar", "token_hash", "expires_at", "install_hint"}
    sc = body["sidecar"]
    assert sc["central_url"] == "https://filearr.example.com"
    assert sc["enrollment_token"]  # raw token present (show-once)
    assert sc["agent_name"] == "lab-01"
    assert sc["config_group_names"] == ["wg"]
    # ...and the single-name key shipped binaries still parse.
    assert sc["config_group"] == "wg"
    assert sc["log_level"] == "info"
    assert set(body["install_hint"]) == {"windows", "linux", "macos"}
    for os_hint in body["install_hint"].values():
        assert "/api/v1/agent-dist/install." in os_hint
        assert "https://filearr.example.com" in os_hint
    async with maker() as s:
        from filearr.models import EnrollmentToken

        tok = await s.get(EnrollmentToken, body["token_hash"])
        assert tok is not None
        # The groups live on the TOKEN, so central applies them at register.
        assert tok.config_group_names == ["wg"]
    ev = await _events(maker, "agent_installer_config_issued")
    assert ev and ev[0]["config_groups"] == ["wg"]
    assert all("enrollment_token" not in str(e) for e in ev)


async def test_installer_config_base_url_default(client):
    c, _, _ = client
    r = await c.post("/api/v1/agents/installer-config", json={})
    assert r.status_code == 201
    assert r.json()["sidecar"]["central_url"].startswith("http://t")
    assert r.json()["sidecar"]["config_group_names"] == []
    assert r.json()["sidecar"]["config_group"] is None


async def test_installer_config_bad_group_and_log_level_422(client):
    c, _, _ = client
    r1 = await c.post(
        "/api/v1/agents/installer-config",
        json={"config_group_ids": [str(uuid.uuid4())]},
    )
    assert r1.status_code == 422
    r2 = await c.post("/api/v1/agents/installer-config", json={"log_level": "loud"})
    assert r2.status_code == 422


async def test_installer_config_admin_gated(client):
    """With auth ENABLED and no admin credential the endpoint is refused (the
    ``admin`` scope dependency runs before any work)."""
    c, _, settings = client
    settings.auth_enabled = True  # same cached Settings object require_scope reads
    try:
        r = await c.post("/api/v1/agents/installer-config", json={})
        assert r.status_code in (401, 403)
    finally:
        settings.auth_enabled = False


async def test_register_joins_token_groups(client):
    c, maker, _ = client
    a = await make_group(c, "fleet-a")
    b = await make_group(c, "fleet-b")
    minted = await c.post(
        "/api/v1/agents/enrollment-tokens",
        json={"config_group_names": ["fleet-a", "fleet-b"]},
    )
    assert minted.status_code == 201
    assert minted.json()["config_group_names"] == ["fleet-a", "fleet-b"]
    reg = await c.post(
        "/api/v1/agents/register",
        json={"token": minted.json()["token"], "hostname": "h", "platform": "linux"},
    )
    assert reg.status_code == 201, reg.text
    assert reg.json()["config_group_warning"] is None
    agent_id = reg.json()["agent_id"]
    eff = await effective(c, agent_id)
    assert {g["name"] for g in eff["groups"]} == {"Global", "fleet-a", "fleet-b"}
    assert {a["id"], b["id"]} == {g["id"] for g in eff["groups"] if not g["is_system"]}


async def test_register_sidecar_config_group_is_additive(client):
    """A shipped binary echoes ONE ``config_group`` from its sidecar; it joins on
    top of whatever the token already records."""
    c, _, _ = client
    await make_group(c, "from-token")
    await make_group(c, "from-sidecar")
    raw = (
        await c.post(
            "/api/v1/agents/enrollment-tokens",
            json={"config_group_names": ["from-token"]},
        )
    ).json()["token"]
    reg = await c.post(
        "/api/v1/agents/register",
        json={
            "token": raw,
            "hostname": "h",
            "platform": "linux",
            "config_group": "from-sidecar",
        },
    )
    assert reg.status_code == 201
    eff = await effective(c, reg.json()["agent_id"])
    assert {g["name"] for g in eff["groups"]} == {
        "Global",
        "from-token",
        "from-sidecar",
    }


async def test_register_unknown_config_group_warns_not_blocks(client):
    c, _, _ = client
    raw = (await c.post("/api/v1/agents/enrollment-tokens", json={})).json()["token"]
    reg = await c.post(
        "/api/v1/agents/register",
        json={
            "token": raw,
            "hostname": "h",
            "platform": "linux",
            "config_group": "ghost",
        },
    )
    assert reg.status_code == 201  # never blocks enrollment
    assert "ghost" in reg.json()["config_group_warning"]
    eff = await effective(c, reg.json()["agent_id"])
    assert [g["name"] for g in eff["groups"]] == ["Global"]


# --------------------------------------------------------------------------- #
# Feature gate                                                                 #
# --------------------------------------------------------------------------- #
async def test_feature_gate_404_when_disabled(client, monkeypatch):
    c, maker, settings = client
    agent_id, _ = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.get("/api/v1/agents/config-groups")).status_code == 404
    assert (await c.get("/api/v1/agents/config-rollouts")).status_code == 404
    assert (await c.get(f"/api/v1/agents/{agent_id}/effective-config")).status_code == 404
