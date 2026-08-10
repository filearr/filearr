"""Admin-visible effective policy — ``GET /agent-policies/effective/{agent_id}``.

The console needs to show what an agent ACTUALLY receives; the agent-plane
``GET /agents/{id}/policy`` requires an agent credential, so before this endpoint
an operator had no way to see it. These tests pin the two semantics that are easy
to get wrong in a GUI:

* **whole-document wins** — a narrower scope REPLACES the broader one; every key
  of the winning document is attributed to that scope and the loser's keys vanish
  (they must NOT reappear as inherited values);
* **config-group lift** — ``web_ui_enabled`` / ``local_access_enabled`` /
  ``auth_required`` set on the assigned group surface as top-level keys and are
  attributed to ``group-settings``.

Harness mirrors ``test_maintenance_mode`` / ``test_policy_p5t6`` (alembic head on
the pgserver Postgres, ASGITransport, auth off, agents enabled).
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
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentConfigGroup, PolicyVersion

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM policy_versions"))
        await conn.execute(text("UPDATE agents SET config_group_id = NULL"))
        await conn.execute(text("DELETE FROM agent_config_groups"))
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


async def _seed_agent(maker, rollout_group: str = "default") -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            rollout_group=rollout_group,
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _add_policy(maker, scope_type, scope_id, version, policy) -> None:
    async with maker() as s:
        s.add(
            PolicyVersion(
                scope_type=scope_type,
                scope_id=scope_id,
                version=version,
                policy=policy,
            )
        )
        await s.commit()


async def _assign_group(maker, agent_id: uuid.UUID, settings: dict) -> uuid.UUID:
    async with maker() as s:
        group = AgentConfigGroup(name="g-" + uuid.uuid4().hex[:8], settings=settings)
        s.add(group)
        await s.commit()
        agent = await s.get(Agent, agent_id)
        agent.config_group_id = group.id
        await s.commit()
        return group.id


def _url(agent_id) -> str:
    return f"/api/v1/agent-policies/effective/{agent_id}"


# --------------------------------------------------------------------------- #
# No documents anywhere                                                        #
# --------------------------------------------------------------------------- #
async def test_no_documents_reports_none_scope_and_all_defaults(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)

    body = (await c.get(_url(agent_id))).json()
    assert body["scope"] == "none"
    assert body["version"] == 0
    assert body["policy"] == {}
    assert body["group"] is None
    # Every known PolicyModel key is reported as an (unset) default.
    assert set(body["source_keys"].values()) == {"default"}
    for key in ("watch_mode", "read_only", "auto_update", "poll_interval_seconds"):
        assert body["source_keys"][key] == "default"


async def test_taxonomy_version_is_not_injected(client):
    """Documented divergence from the agent plane: the server-computed
    ``taxonomy_version`` is deliberately absent — it is not operator-editable, so
    the console must never show it as a settable key."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await c.put("/api/v1/agent-policies/global", json={"policy": {"watch_mode": True}})

    body = (await c.get(_url(agent_id))).json()
    assert "taxonomy_version" not in body["policy"]
    assert "taxonomy_version" not in body["source_keys"]


# --------------------------------------------------------------------------- #
# Resolution + provenance                                                      #
# --------------------------------------------------------------------------- #
async def test_global_document_is_returned_and_attributed(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await _add_policy(
        maker, "global", None, 1, {"watch_mode": True, "poll_interval_seconds": 120}
    )

    body = (await c.get(_url(agent_id))).json()
    assert body["scope"] == "global"
    assert body["version"] == 1
    assert body["policy"] == {"watch_mode": True, "poll_interval_seconds": 120}
    assert body["source_keys"]["watch_mode"] == "global"
    assert body["source_keys"]["poll_interval_seconds"] == "global"
    assert body["source_keys"]["auto_update"] == "default"


async def test_agent_document_replaces_global_wholesale(client):
    """The critical semantic: no key merging. The agent-scoped document wins
    ENTIRELY — the global document's other keys do NOT leak through."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await _add_policy(
        maker,
        "global",
        None,
        1,
        {"watch_mode": True, "poll_interval_seconds": 120, "auto_update": False},
    )
    await _add_policy(maker, "agent", str(agent_id), 1, {"watch_mode": False})

    body = (await c.get(_url(agent_id))).json()
    assert body["scope"] == f"agent:{agent_id}"
    assert body["policy"] == {"watch_mode": False}
    assert body["source_keys"]["watch_mode"] == "agent"
    # Dropped, NOT inherited from global.
    assert body["source_keys"]["poll_interval_seconds"] == "default"
    assert body["source_keys"]["auto_update"] == "default"


async def test_rollout_group_document_wins_over_global(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker, rollout_group="canary")
    await _add_policy(maker, "global", None, 1, {"auto_update": False})
    await _add_policy(maker, "group", "canary", 1, {"auto_update": True})

    body = (await c.get(_url(agent_id))).json()
    assert body["scope"] == "group:canary"
    assert body["policy"] == {"auto_update": True}
    assert body["source_keys"]["auto_update"] == "group"


async def test_highest_version_of_the_winning_scope_applies(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await _add_policy(maker, "global", None, 1, {"watch_mode": True})
    await _add_policy(maker, "global", None, 2, {"watch_mode": False})

    body = (await c.get(_url(agent_id))).json()
    assert body["version"] == 2
    assert body["policy"] == {"watch_mode": False}


# --------------------------------------------------------------------------- #
# Config-group fold + local-surface lift                                       #
# --------------------------------------------------------------------------- #
async def test_group_settings_lift_is_attributed_to_group_settings(client):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    group_id = await _assign_group(
        maker,
        agent_id,
        {"web_ui_enabled": True, "auth_required": False, "log_level": "debug"},
    )

    body = (await c.get(_url(agent_id))).json()
    assert body["group"] == {"id": str(group_id), "name": body["group"]["name"]}
    # The whole settings object rides under `group`, and the three local-surface
    # keys are lifted to top level where the agent's P7-T4 gate reads them.
    assert body["policy"]["group"]["log_level"] == "debug"
    assert body["policy"]["web_ui_enabled"] is True
    assert body["policy"]["auth_required"] is False
    assert body["source_keys"]["web_ui_enabled"] == "group-settings"
    assert body["source_keys"]["auth_required"] == "group-settings"
    assert body["source_keys"]["group"] == "group-settings"
    # Not set by the group -> still an agent default.
    assert body["source_keys"]["local_access_enabled"] == "default"


async def test_group_lift_beats_global_but_not_a_narrower_policy_row(client):
    """Precedence parity with the agent plane: a config-group key overrides the
    GLOBAL document, but an explicit agent/rollout-group row keeps its say."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await _add_policy(maker, "global", None, 1, {"web_ui_enabled": False})
    await _assign_group(maker, agent_id, {"web_ui_enabled": True})

    body = (await c.get(_url(agent_id))).json()
    assert body["policy"]["web_ui_enabled"] is True
    assert body["source_keys"]["web_ui_enabled"] == "group-settings"

    # Now an agent-scoped row that sets the same key: the row wins.
    await _add_policy(maker, "agent", str(agent_id), 1, {"web_ui_enabled": False})
    body = (await c.get(_url(agent_id))).json()
    assert body["policy"]["web_ui_enabled"] is False
    assert body["source_keys"]["web_ui_enabled"] == "agent"


async def test_unknown_forward_compat_keys_are_reported(client):
    """PolicyModel is extra='allow'; a forward-compat key an older central does
    not know still reaches the agent — and must show up in the admin view."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    await _add_policy(maker, "global", None, 1, {"future_knob": {"a": 1}})

    body = (await c.get(_url(agent_id))).json()
    assert body["policy"]["future_knob"] == {"a": 1}
    assert body["source_keys"]["future_knob"] == "global"


# --------------------------------------------------------------------------- #
# Gates                                                                        #
# --------------------------------------------------------------------------- #
async def test_unknown_agent_404(client):
    c, _, _ = client
    assert (await c.get(_url(uuid.uuid4()))).status_code == 404


async def test_feature_gate_404_when_agents_disabled(client, monkeypatch):
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    monkeypatch.setattr(settings, "agents_enabled", False)
    assert (await c.get(_url(agent_id))).status_code == 404


async def test_admin_gated(client):
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    settings.auth_enabled = True  # the same cached Settings require_scope reads
    try:
        assert (await c.get(_url(agent_id))).status_code in (401, 403)
    finally:
        settings.auth_enabled = False


async def test_read_only_never_stamps_the_agent(client):
    """Unlike the agent-plane poll, the admin view must not look like a check-in."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    async with maker() as s:
        before = (await s.get(Agent, agent_id)).last_seen_at

    assert (await c.get(_url(agent_id))).status_code == 200

    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.last_seen_at == before
        assert agent.policy_version_applied is None
