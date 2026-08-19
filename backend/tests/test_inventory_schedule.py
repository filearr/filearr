"""2026-08-20: config-group inventory scheduling (central tick) + validation."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.agent_config import GroupSettings, GroupSettingsValidationError, validate_settings
from filearr.models import Agent, AgentCommand, AgentConfigGroup, AgentConfigGroupVersion

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM agent_config_group_members"))
        await conn.execute(text("DELETE FROM agent_config_group_versions"))
        await conn.execute(text("DELETE FROM agent_config_groups"))
        await conn.execute(text("DELETE FROM agents"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


def test_schema_validates_schedule():
    ok = {
        "inventory": {
            "enabled": True,
            "collectors": ["stat", "permissions"],
            "schedule_cron": "0 3 * * *",
            "paths": ["D:\\\\"],
        }
    }
    GroupSettings.model_validate(ok)
    with pytest.raises(GroupSettingsValidationError):
        validate_settings(
            {"inventory": {"enabled": True, "schedule_cron": "not a cron", "paths": ["/x"]}}
        )
    # a schedule with nothing to walk is refused
    with pytest.raises(GroupSettingsValidationError):
        validate_settings({"inventory": {"enabled": True, "schedule_cron": "0 3 * * *"}})


async def _seed(maker, settings: dict):
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux", cert_fingerprint="FP:" + uuid.uuid4().hex
        )
        s.add(agent)
        g = AgentConfigGroup(name="Global", is_system=True, priority=0, current_version=1)
        s.add(g)
        await s.flush()
        s.add(AgentConfigGroupVersion(group_id=g.id, version=1, settings=settings, policy={}))
        await s.commit()
        return agent.id


async def test_tick_enqueues_once_per_occurrence(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "agents_enabled", True)
    agent_id = await _seed(
        maker,
        {
            "inventory": {
                "enabled": True,
                "collectors": ["stat", "permissions"],
                "schedule_cron": "0 3 * * *",
                "paths": ["/srv/share"],
                "preset": "user-documents",
            }
        },
    )
    tick = int(datetime(2026, 8, 20, 3, 0, tzinfo=UTC).timestamp())
    n = await worker_mod.schedule_agent_inventories(tick)
    assert n == 1
    async with maker() as s:
        cmd = (await s.execute(select(AgentCommand))).scalars().one()
        assert cmd.agent_id == agent_id and cmd.kind == "inventory"
        assert cmd.payload == {
            "scheduled": True,
            "collectors": ["stat", "permissions"],
            "paths": ["/srv/share"],
            "preset": "user-documents",
        }
        assert cmd.status == "pending"
    # same occurrence again -> nothing new; also an unfinished run suppresses
    assert await worker_mod.schedule_agent_inventories(tick) == 0
    next_tick = int(datetime(2026, 8, 21, 3, 0, tzinfo=UTC).timestamp())
    assert await worker_mod.schedule_agent_inventories(next_tick) == 0  # still pending
    async with maker() as s:
        cmd = (await s.execute(select(AgentCommand))).scalars().one()
        cmd.status = "done"
        await s.commit()
    assert await worker_mod.schedule_agent_inventories(next_tick) == 1


async def test_tick_skips_unscheduled_disabled_and_revoked(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "agents_enabled", True)
    # enabled but NO schedule -> never enqueued by the tick
    await _seed(maker, {"inventory": {"enabled": True, "collectors": ["stat"]}})
    tick = int(datetime(2026, 8, 20, 3, 0, tzinfo=UTC).timestamp())
    assert await worker_mod.schedule_agent_inventories(tick) == 0
    # revoked agent -> skipped even with a schedule
    async with maker() as s:
        agent = (await s.execute(select(Agent))).scalars().one()
        agent.revoked_at = datetime.now(UTC)
        v = (await s.execute(select(AgentConfigGroupVersion))).scalars().one()
        v.settings = {
            "inventory": {
                "enabled": True,
                "collectors": ["stat"],
                "schedule_cron": "0 3 * * *",
                "paths": ["/x"],
            }
        }
        await s.commit()
    assert await worker_mod.schedule_agent_inventories(tick) == 0
