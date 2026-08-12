"""P13 — the phased-rollout engine driven from the static minute tick.

``worker._advance_config_rollouts(tick)`` takes the tick instant as an ARGUMENT,
which is what makes this suite deterministic: the whole lifecycle (scheduled →
running → tier → tier → completed) is driven by handing it later and later
timestamps, with no sleeps and no clock patching. Coverage is asserted through
the real resolution path (``resolve_effective_config``), not by reading the
rollout row back — the row is the mechanism, "which document does this agent
get" is the behaviour.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.agent_config import agent_bucket, resolve_effective_config
from filearr.config import get_settings
from filearr.models import (
    Agent,
    AgentConfigGroup,
    AgentConfigGroupMember,
    AgentConfigGroupVersion,
    AgentConfigRollout,
)
from filearr.worker import _advance_config_rollouts
from tests.agentcfg_helpers import reset_config_groups

BACKEND_DIR = Path(__file__).resolve().parent.parent

T0 = datetime(2026, 8, 11, 12, 0, tzinfo=UTC)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await reset_config_groups(conn)
        await conn.execute(text("DELETE FROM agents"))
    sm = async_sessionmaker(engine, expire_on_commit=False)
    # The tick opens its OWN session from filearr.db.SessionLocal.
    monkeypatch.setattr(db_mod, "SessionLocal", sm)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "agents_enabled", True)
    yield sm
    await engine.dispose()


async def _group_with_two_versions(sm, *, name="rolling") -> uuid.UUID:
    """A group published at v1 (``{"watch_mode": False}``) with a v2 snapshot
    (``{"watch_mode": True}``) waiting behind a rollout."""
    async with sm() as s:
        g = AgentConfigGroup(
            name=name, priority=500, settings={}, policy={"watch_mode": True},
            current_version=1,
        )
        s.add(g)
        await s.flush()
        s.add(
            AgentConfigGroupVersion(
                group_id=g.id, version=1, settings={}, policy={"watch_mode": False}
            )
        )
        s.add(
            AgentConfigGroupVersion(
                group_id=g.id, version=2, settings={}, policy={"watch_mode": True}
            )
        )
        await s.commit()
        return g.id


async def _agents_in(sm, group_id: uuid.UUID, count: int) -> list[uuid.UUID]:
    ids: list[uuid.UUID] = []
    async with sm() as s:
        for i in range(count):
            a = Agent(name=f"a{i}", hostname=f"a{i}", platform="linux")
            s.add(a)
            await s.flush()
            s.add(AgentConfigGroupMember(agent_id=a.id, group_id=group_id))
            ids.append(a.id)
        await s.commit()
    return ids


async def _add_rollout(sm, group_id, tiers, *, starts_at=None) -> uuid.UUID:
    async with sm() as s:
        r = AgentConfigRollout(
            group_id=group_id,
            target_version=2,
            tiers=tiers,
            status="scheduled",
            current_tier=-1,
            starts_at=starts_at,
        )
        s.add(r)
        await s.commit()
        return r.id


async def _rollout(sm, rid) -> AgentConfigRollout:
    async with sm() as s:
        return await s.get(AgentConfigRollout, rid)


async def _watch_mode_for(sm, agent_id) -> bool:
    async with sm() as s:
        agent = await s.get(Agent, agent_id)
        eff = await resolve_effective_config(s, agent)
        return eff.document["watch_mode"]


# --------------------------------------------------------------------------- #
# Lifecycle                                                                    #
# --------------------------------------------------------------------------- #
async def test_scheduled_starts_running_at_tier_zero(maker):
    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(
        maker, gid, [{"percent": 50, "delay_minutes": 0}, {"percent": 100, "delay_minutes": 60}]
    )
    assert await _advance_config_rollouts(T0) == [str(rid)]
    r = await _rollout(maker, rid)
    assert r.status == "running"
    assert r.current_tier == 0
    assert r.started_at is not None and r.tier_started_at is not None
    # An idempotent re-tick one minute later does nothing (the next tier's delay
    # has not elapsed) — a duplicated/late tick must not skip a tier.
    assert await _advance_config_rollouts(T0 + timedelta(minutes=1)) == []
    assert (await _rollout(maker, rid)).current_tier == 0


async def test_starts_at_in_the_future_is_not_started(maker):
    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(
        maker, gid, [{"percent": 100}], starts_at=T0 + timedelta(hours=2)
    )
    assert await _advance_config_rollouts(T0) == []
    assert (await _rollout(maker, rid)).status == "scheduled"
    assert await _advance_config_rollouts(T0 + timedelta(hours=2)) == [str(rid)]
    assert (await _rollout(maker, rid)).status == "completed"


async def test_tier_zero_delay_holds_coverage_at_nobody(maker):
    """A first tier with a delay opens the window but covers nobody until the
    delay elapses — otherwise "wait 30 minutes, then 10%" would ship instantly."""
    gid = await _group_with_two_versions(maker)
    [agent_id] = await _agents_in(maker, gid, 1)
    rid = await _add_rollout(
        maker,
        gid,
        [{"percent": 100, "delay_minutes": 30}],
    )
    await _advance_config_rollouts(T0)
    r = await _rollout(maker, rid)
    assert r.status == "running" and r.current_tier == -1
    assert await _watch_mode_for(maker, agent_id) is False  # still on v1

    assert await _advance_config_rollouts(T0 + timedelta(minutes=29)) == []
    assert await _advance_config_rollouts(T0 + timedelta(minutes=30)) == [str(rid)]
    assert (await _rollout(maker, rid)).status == "completed"
    assert await _watch_mode_for(maker, agent_id) is True


async def test_full_tier_walk_completes_and_publishes(maker):
    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(
        maker,
        gid,
        [
            {"percent": 10, "delay_minutes": 0},
            {"percent": 50, "delay_minutes": 15},
            {"percent": 100, "delay_minutes": 30},
        ],
    )
    await _advance_config_rollouts(T0)
    assert (await _rollout(maker, rid)).current_tier == 0

    # One tier per tick, even when several delays have lapsed at once: each tier
    # exists so somebody can look at the fleet between them.
    far = T0 + timedelta(hours=6)
    await _advance_config_rollouts(far)
    assert (await _rollout(maker, rid)).current_tier == 1
    assert (await _rollout(maker, rid)).status == "running"

    await _advance_config_rollouts(far + timedelta(hours=6))
    done = await _rollout(maker, rid)
    assert done.current_tier == 2
    assert done.status == "completed"
    assert done.finished_at is not None
    async with maker() as s:
        assert (await s.get(AgentConfigGroup, gid)).current_version == 2
    # Nothing left live -> the tick is a no-op afterwards.
    assert await _advance_config_rollouts(far + timedelta(days=1)) == []


async def test_cancelled_rollout_is_ignored_by_the_tick(maker):
    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(maker, gid, [{"percent": 10}, {"percent": 100, "delay_minutes": 5}])
    await _advance_config_rollouts(T0)
    async with maker() as s:
        r = await s.get(AgentConfigRollout, rid)
        r.status = "cancelled"
        await s.commit()
    assert await _advance_config_rollouts(T0 + timedelta(hours=1)) == []
    async with maker() as s:
        # current_version never moved: covered agents fall BACK on their next poll.
        assert (await s.get(AgentConfigGroup, gid)).current_version == 1


# --------------------------------------------------------------------------- #
# Coverage — what the covered / uncovered agents actually resolve               #
# --------------------------------------------------------------------------- #
async def test_tier_covers_only_agents_below_the_percent(maker):
    gid = await _group_with_two_versions(maker)
    agent_ids = await _agents_in(maker, gid, 40)
    await _add_rollout(maker, gid, [{"percent": 50}, {"percent": 100, "delay_minutes": 60}])
    await _advance_config_rollouts(T0)

    for aid in agent_ids:
        expected = agent_bucket(aid) < 50
        assert await _watch_mode_for(maker, aid) is expected
    # The split is real, not "everyone" or "nobody".
    covered = [a for a in agent_ids if agent_bucket(a) < 50]
    assert 0 < len(covered) < len(agent_ids)


async def test_covered_agents_are_flagged_via_rollout(maker):
    gid = await _group_with_two_versions(maker)
    agent_ids = await _agents_in(maker, gid, 30)
    await _add_rollout(maker, gid, [{"percent": 50}, {"percent": 100, "delay_minutes": 60}])
    await _advance_config_rollouts(T0)
    async with maker() as s:
        for aid in agent_ids:
            eff = await resolve_effective_config(s, await s.get(Agent, aid))
            contribution = next(c for c in eff.contributors if c.group_id == gid)
            if agent_bucket(aid) < 50:
                assert contribution.via_rollout is True
                assert contribution.version_used == 2
            else:
                assert contribution.via_rollout is False
                assert contribution.version_used == 1


async def test_completion_covers_everyone(maker):
    gid = await _group_with_two_versions(maker)
    agent_ids = await _agents_in(maker, gid, 20)
    await _add_rollout(maker, gid, [{"percent": 10}, {"percent": 100, "delay_minutes": 5}])
    await _advance_config_rollouts(T0)
    await _advance_config_rollouts(T0 + timedelta(minutes=5))
    for aid in agent_ids:
        assert await _watch_mode_for(maker, aid) is True


async def test_rollout_never_moves_an_agent_backwards(maker):
    """A rollout whose target is OLDER than the published version covers nobody —
    an operator cancelling a rollout after a rollback must not have covered
    agents silently downgraded by the stale row."""
    gid = await _group_with_two_versions(maker)
    [agent_id] = await _agents_in(maker, gid, 1)
    async with maker() as s:
        g = await s.get(AgentConfigGroup, gid)
        g.current_version = 2  # v2 published to everyone
        await s.commit()
    async with maker() as s:
        s.add(
            AgentConfigRollout(
                group_id=gid,
                target_version=1,
                tiers=[{"percent": 100, "delay_minutes": 0}],
                status="running",
                current_tier=0,
                started_at=T0,
                tier_started_at=T0,
            )
        )
        await s.commit()
    assert await _watch_mode_for(maker, agent_id) is True  # stays on v2


# --------------------------------------------------------------------------- #
# Guards                                                                        #
# --------------------------------------------------------------------------- #
async def test_one_live_rollout_per_group_is_enforced_by_the_index(maker):
    gid = await _group_with_two_versions(maker)
    await _add_rollout(maker, gid, [{"percent": 100}])
    with pytest.raises(Exception):  # noqa: B017 — the DB partial-unique index
        await _add_rollout(maker, gid, [{"percent": 100}])


async def test_maintenance_mode_defers_the_whole_tick(maker, monkeypatch):
    from filearr import maintmode

    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(maker, gid, [{"percent": 100}])

    async def _active() -> bool:
        return True

    monkeypatch.setattr(maintmode, "is_active_standalone", _active)
    assert await _advance_config_rollouts(T0) == []
    assert (await _rollout(maker, rid)).status == "scheduled"


async def test_completed_rollouts_are_audited(maker):
    gid = await _group_with_two_versions(maker)
    rid = await _add_rollout(maker, gid, [{"percent": 100}])
    await _advance_config_rollouts(T0)
    async with maker() as s:
        rows = (
            await s.execute(
                text(
                    "SELECT details FROM security_events "
                    "WHERE event_type = 'agent_config_rollout_completed'"
                )
            )
        ).all()
    assert any(r.details["rollout_id"] == str(rid) for r in rows)


async def test_snapshot_rows_survive_group_edits(maker):
    """The engine resolves from SNAPSHOTS, so authoring v3 while v2 rolls out
    must not leak the un-published document to covered agents."""
    gid = await _group_with_two_versions(maker)
    [agent_id] = await _agents_in(maker, gid, 1)
    await _add_rollout(maker, gid, [{"percent": 100}])
    await _advance_config_rollouts(T0)
    async with maker() as s:
        g = await s.get(AgentConfigGroup, gid)
        g.policy = {"watch_mode": False, "unpublished": True}  # authoring buffer
        await s.commit()
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        doc = (await resolve_effective_config(s, agent)).document
    assert doc["watch_mode"] is True  # the v2 SNAPSHOT, not the live columns
    assert "unpublished" not in doc


async def test_versions_query_is_scoped_to_the_agents_groups(maker):
    """A second group's snapshots must not leak into an agent that is not in it."""
    gid = await _group_with_two_versions(maker, name="mine")
    other = await _group_with_two_versions(maker, name="theirs")
    async with maker() as s:
        g = await s.get(AgentConfigGroup, other)
        g.policy = {"leaked": True}
        for v in (
            await s.execute(
                select(AgentConfigGroupVersion).where(
                    AgentConfigGroupVersion.group_id == other
                )
            )
        ).scalars():
            v.policy = {"leaked": True}
        await s.commit()
    [agent_id] = await _agents_in(maker, gid, 1)
    async with maker() as s:
        doc = (
            await resolve_effective_config(s, await s.get(Agent, agent_id))
        ).document
    assert "leaked" not in doc
