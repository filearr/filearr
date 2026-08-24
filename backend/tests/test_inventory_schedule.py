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
        # The once-per-occurrence cursor is the command's created_at, which the
        # DB stamped with REAL now. Pin it to the simulated first occurrence or
        # this test breaks the day the real clock passes next_tick (it did on
        # 2026-08-21: real-now > Aug-21-03:00 made the occurrence look consumed).
        cmd.created_at = datetime(2026, 8, 20, 3, 0, tzinfo=UTC)
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


# --- 2026-08-23: human-friendly scheduling — schedule_tz ---------------------


def test_schema_validates_schedule_tz():
    base = {
        "enabled": True,
        "collectors": ["stat"],
        "schedule_cron": "0 3 * * *",
        "paths": ["/x"],
    }
    GroupSettings.model_validate({"inventory": {**base, "schedule_tz": "America/Chicago"}})
    GroupSettings.model_validate({"inventory": {**base, "schedule_tz": "agent"}})
    with pytest.raises(GroupSettingsValidationError):
        validate_settings({"inventory": {**base, "schedule_tz": "Not/AZone"}})
    with pytest.raises(GroupSettingsValidationError):
        validate_settings({"inventory": {**base, "schedule_tz": "  "}})
    # a tz without a cron is refused — it would silently mean nothing
    with pytest.raises(GroupSettingsValidationError):
        validate_settings(
            {"inventory": {"enabled": True, "collectors": ["stat"], "schedule_tz": "agent"}}
        )


def test_due_occurrence_wall_clock_tz_and_dst():
    from zoneinfo import ZoneInfo

    from filearr.schedule import due_occurrence

    chi = ZoneInfo("America/Chicago")
    # 04:00 Chicago (CDT, UTC-5) == 09:00 UTC: due exactly then...
    tick = datetime(2026, 8, 20, 9, 0, tzinfo=UTC)
    assert due_occurrence("0 4 * * *", tick, tz=chi) == tick
    # ...and NOT at 04:00 UTC (which the fixed-UTC path would fire at)
    assert due_occurrence("0 4 * * *", datetime(2026, 8, 20, 4, 0, tzinfo=UTC), tz=chi) is None
    # across the 2026-11-01 fall-back the schedule tracks the WALL clock: the
    # day after CDT->CST the occurrence lands an hour later in UTC terms.
    last = datetime(2026, 10, 31, 9, 0, tzinfo=UTC)  # 04:00 CDT
    nxt = due_occurrence("0 4 * * *", datetime(2026, 11, 1, 10, 30, tzinfo=UTC), last, tz=chi)
    assert nxt == datetime(2026, 11, 1, 10, 0, tzinfo=UTC)  # 04:00 CST


def test_resolve_schedule_tz_fail_soft():
    from datetime import timedelta, timezone

    from filearr.schedule import resolve_schedule_tz

    assert resolve_schedule_tz(None) is None
    assert resolve_schedule_tz("") is None
    assert str(resolve_schedule_tz("Europe/Berlin")) == "Europe/Berlin"
    assert resolve_schedule_tz("Nope/Nowhere") is None  # unknown zone -> UTC, never a raise
    assert resolve_schedule_tz("agent") is None  # no reported offset -> UTC
    assert resolve_schedule_tz("agent", agent_utc_offset_minutes="junk") is None
    assert resolve_schedule_tz("agent", agent_utc_offset_minutes=99999) is None
    assert resolve_schedule_tz("agent", agent_utc_offset_minutes=True) is None
    assert resolve_schedule_tz("agent", agent_utc_offset_minutes=-300) == timezone(
        timedelta(minutes=-300)
    )


async def test_tick_honors_named_timezone(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "agents_enabled", True)
    await _seed(
        maker,
        {
            "inventory": {
                "enabled": True,
                "collectors": ["stat"],
                "schedule_cron": "0 3 * * *",
                "schedule_tz": "America/Chicago",
                "paths": ["/srv/share"],
            }
        },
    )
    # 03:00 UTC is NOT 03:00 Chicago — nothing fires...
    tick_utc3 = int(datetime(2026, 8, 20, 3, 0, tzinfo=UTC).timestamp())
    assert await worker_mod.schedule_agent_inventories(tick_utc3) == 0
    # ...03:00 CDT (08:00 UTC) is.
    tick_chi3 = int(datetime(2026, 8, 20, 8, 0, tzinfo=UTC).timestamp())
    assert await worker_mod.schedule_agent_inventories(tick_chi3) == 1


async def test_tick_agent_local_time_from_reported_offset(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "agents_enabled", True)
    await _seed(
        maker,
        {
            "inventory": {
                "enabled": True,
                "collectors": ["stat"],
                "schedule_cron": "0 3 * * *",
                "schedule_tz": "agent",
                "paths": ["/srv/share"],
            }
        },
    )
    tick_utc3 = int(datetime(2026, 8, 20, 3, 0, tzinfo=UTC).timestamp())
    tick_east5 = int(datetime(2026, 8, 20, 8, 0, tzinfo=UTC).timestamp())
    # No capability advertisement yet -> the agent schedules on UTC (fail-soft).
    assert await worker_mod.schedule_agent_inventories(tick_utc3) == 1
    async with maker() as s:
        cmd = (await s.execute(select(AgentCommand))).scalars().one()
        await s.delete(cmd)
        agent = (await s.execute(select(Agent))).scalars().one()
        agent.capabilities = {"inventory_collectors": ["stat"], "utc_offset_minutes": -300}
        await s.commit()
    # With a reported UTC-5 offset, 03:00 agent-local means 08:00 UTC.
    assert await worker_mod.schedule_agent_inventories(tick_utc3) == 0
    assert await worker_mod.schedule_agent_inventories(tick_east5) == 1
