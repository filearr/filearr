"""Roadmap §16 (2026-08-19): content-hash backfill for quick_only libraries."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Agent, Item, Library

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _mk(s, lib_id, path: Path, *, size: int):
    path.write_bytes(b"z" * size)  # noqa: ASYNC240 - test fixture write
    item = Item(
        library_id=lib_id,
        file_category="other",
        file_group="other",
        status="active",
        path=str(path),
        rel_path=path.name,
        filename=path.name,
        extension="bin",
        size=size,
        mtime=datetime.now(UTC),
        quick_hash="q",
    )
    s.add(item)
    await s.flush()
    return item.id


async def test_backfill_hashes_quick_only_libraries_within_budget(maker, monkeypatch, tmp_path):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    st = get_settings()
    monkeypatch.setattr(st, "hash_backfill_rate_mbps", 0.0)  # unthrottled in tests

    big = 300_000  # > 128 KiB so the small-file band doesn't already cover it
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint="FPX")
        s.add(agent)
        await s.flush()
        quick = Library(name="net", root_path=str(tmp_path), hash_policy="quick_only")
        full = Library(name="loc", root_path=str(tmp_path), hash_policy="full")
        agent_lib = Library(
            name="agentlib",
            root_path="/aroot",
            hash_policy="quick_only",
            source_agent_id=agent.id,
            agent_library_ref="/aroot",
        )
        s.add_all([quick, full, agent_lib])
        await s.flush()
        a = await _mk(s, quick.id, tmp_path / "a.bin", size=big)
        b = await _mk(s, quick.id, tmp_path / "b.bin", size=big)
        c = await _mk(s, quick.id, tmp_path / "c.bin", size=big)
        small = await _mk(s, quick.id, tmp_path / "small.bin", size=1000)  # band: skipped
        f = await _mk(s, full.id, tmp_path / "f.bin", size=big)  # full policy: skipped
        g = Item(
            library_id=agent_lib.id,
            file_category="other",
            file_group="other",
            status="active",
            path="/aroot/g.bin",
            rel_path="g.bin",
            filename="g.bin",
            extension="bin",
            size=big,
            mtime=datetime.now(UTC),
            quick_hash="q",
        )
        s.add(g)
        await s.commit()
        g_id = g.id

    # budget covers two of the three quick_only files
    out = await worker_mod.backfill_content_hashes_now(max_bytes=2 * big)
    assert out["hashed"] == 2 and out["bytes"] == 2 * big and out["remaining"] == 1
    async with maker() as s:
        rows = {r.id: r for r in (await s.execute(select(Item))).scalars()}
    hashed = [i for i in (a, b, c) if rows[i].content_hash]
    assert len(hashed) == 2
    assert all(rows[i].mid_hash for i in hashed)
    assert rows[small].content_hash is None
    assert rows[f].content_hash is None
    assert rows[g_id].content_hash is None

    # second run finishes the backlog and then no-ops
    out = await worker_mod.backfill_content_hashes_now(max_bytes=10 * big)
    assert out["hashed"] == 1 and out["remaining"] == 0
    out = await worker_mod.backfill_content_hashes_now(max_bytes=10 * big)
    assert out["hashed"] == 0 and out["remaining"] == 0


def test_backfill_registered_as_opt_in_maintenance_task():
    from filearr import maintenance, worker

    spec = maintenance.MAINT_TASKS["backfill_content_hashes"]
    assert spec.default_cron is None and spec.editable and spec.runnable
    assert spec.task_name == "filearr.worker.backfill_content_hashes"
    assert spec.task_name in worker.AGE_NET_EXEMPT_TASKS
