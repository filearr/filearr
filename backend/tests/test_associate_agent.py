"""T3 parity for agent-replicated libraries.

Replication (agentsync.apply_batch) deliberately never touches ``sidecar_of``,
so nothing associated sidecars that arrive from agents — a bulk .xmp export on
an agent share landed as 400k first-class items (live 2026-08: the July
timeline bar). Covers:

  * ``associate_sidecars_light`` — the streaming link-only pass — over rows
    landed by a real ``apply_batch`` call;
  * apply_batch/reconcile results carrying ``library_ids`` for the endpoint's
    debounced defer;
  * a later replication update NOT clobbering an existing association;
  * the maintenance sweep task registry entry being well-formed.

Reuses the test_replication_p5t4 harness (migrated pgserver Postgres).
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.agentsync import AgentEvent, ReplicationBatch, apply_batch
from filearr.models import Agent, Item, Library
from filearr.tasks.associate import associate_sidecars_light

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_replication_log"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed_agent(maker) -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
            last_contiguous_seq_no=0,
        )
        s.add(agent)
        await s.commit()
        return agent.id


def _ev(seq, rel_path, *, etype="created", size=100, quick_hash="q"):
    return AgentEvent(
        seq_no=seq, event_type=etype, library_ref="/srv/pics",
        rel_path=rel_path, from_rel_path=None, size=size,
        mtime=1_784_000_000.0, quick_hash=quick_hash, content_hash=None,
    )


async def _apply(maker, agent_id, *events):
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await apply_batch(
            s, agent, ReplicationBatch(agent_id=str(agent_id), entries=list(events))
        )


async def _items_by_rel(maker, library_id):
    async with maker() as s:
        rows = (
            await s.execute(select(Item).where(Item.library_id == library_id))
        ).scalars()
        return {i.rel_path: i for i in rows}


async def test_replicated_xmp_sidecars_link_via_light_pass(db_maker):
    agent_id = await _seed_agent(db_maker)
    result = await _apply(
        db_maker, agent_id,
        _ev(1, "2024/IMG_0001.jpg", size=5_000, quick_hash="a"),
        _ev(2, "2024/IMG_0002.jpg", size=9_000_000, quick_hash="b"),
        _ev(3, "2024/IMG_0001.jpg.xmp", size=8, quick_hash="c"),
        _ev(4, "2024/IMG_0002.xmp", size=8, quick_hash="d"),
    )
    # the endpoint contract: library ids ride along for the associate defer
    assert len(result["library_ids"]) == 1
    lib_id = uuid.UUID(result["library_ids"][0])

    async with db_maker() as s:
        stats = await associate_sidecars_light(s, lib_id)
        await s.commit()
    assert stats["sidecars"] == 2
    assert stats["linked"] == 2
    assert stats["changed"] == 2
    assert len(stats["changed_ids"]) == 2

    items = await _items_by_rel(db_maker, lib_id)
    # double-extension form links to the EXACT photo, not the dir's largest
    assert items["2024/IMG_0001.jpg.xmp"].sidecar_of == items["2024/IMG_0001.jpg"].id
    # Adobe same-stem form
    assert items["2024/IMG_0002.xmp"].sidecar_of == items["2024/IMG_0002.jpg"].id
    # photos themselves are not sidecars
    assert items["2024/IMG_0001.jpg"].sidecar_of is None

    # idempotent: a second pass changes nothing
    async with db_maker() as s:
        again = await associate_sidecars_light(s, lib_id)
    assert again["changed"] == 0
    assert again["changed_ids"] == []


async def test_replication_update_preserves_association(db_maker):
    agent_id = await _seed_agent(db_maker)
    result = await _apply(
        db_maker, agent_id,
        _ev(1, "a/photo.jpg", size=1000, quick_hash="p"),
        _ev(2, "a/photo.jpg.xmp", size=8, quick_hash="x"),
    )
    lib_id = uuid.UUID(result["library_ids"][0])
    async with db_maker() as s:
        await associate_sidecars_light(s, lib_id)
        await s.commit()

    # the agent re-reports the sidecar (modified) — the update path must not
    # clobber the association agentsync never manages
    await _apply(
        db_maker, agent_id,
        _ev(3, "a/photo.jpg.xmp", etype="modified", size=9, quick_hash="x2"),
    )
    items = await _items_by_rel(db_maker, lib_id)
    assert items["a/photo.jpg.xmp"].sidecar_of == items["a/photo.jpg"].id


async def test_sweep_registry_entry_wired():
    from filearr.maintenance import _SPECS
    from filearr.worker import associate_agent_library, associate_agent_sidecars

    spec = next(s for s in _SPECS if s.key == "associate_agent_sidecars")
    assert spec.task_name == "filearr.worker.associate_agent_sidecars"
    assert spec.lock == "associate-agent-sidecars"
    # the tasks exist under the exact names the defers/registry reference
    assert associate_agent_sidecars.name == "filearr.worker.associate_agent_sidecars"
    assert associate_agent_library.name == "filearr.worker.associate_agent_library"


async def test_agent_library_visible_to_sweep_query(db_maker):
    agent_id = await _seed_agent(db_maker)
    result = await _apply(db_maker, agent_id, _ev(1, "x/y.jpg", quick_hash="z"))
    lib_id = uuid.UUID(result["library_ids"][0])
    async with db_maker() as s:
        ids = [
            i for i in (
                await s.execute(
                    select(Library.id).where(Library.source_agent_id.is_not(None))
                )
            ).scalars()
        ]
    assert lib_id in ids
