"""Scan never-hashed self-heal dedup (2026-08-22).

A row committed but never extracted (``quick_hash IS NULL``) is re-deferred by
every scan as a self-heal. Before this fix the scan never checked for a job that
was ALREADY pending, so a never-extractable file (unreadable over the mount) piled
up one duplicate extract job per scan (live: a 244k-row spurious backlog was only
part of the queue; the rest was duplicates). The scan now loads the library's
pending extract ids once and skips those rows; new/modified rows are never gated.

The suite's Postgres has no procrastinate schema, so this test stands up a
minimal ``procrastinate_jobs`` lookalike (only the columns the lookup reads) and
drops it again — ``to_regclass`` is what gates the lookup, so the shape is enough.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, Library

from .conftest import psycopg3_uri
from .test_fs_identity import _run_scan

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
async def engine(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    eng = create_async_engine(psycopg3_uri(pg_uri))
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    yield eng
    await eng.dispose()


async def test_selfheal_skips_rows_with_pending_extract_job(engine, tmp_path):
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "lib"
    root.mkdir()
    (root / "queued.bin").write_bytes(b"q" * 300)
    (root / "orphan.bin").write_bytes(b"o" * 300)

    async with Session() as session:
        lib = Library(name="dedup", root_path=str(root))
        session.add(lib)
        await session.commit()
        lib_id = lib.id
        first = await _run_scan(session, lib)
        assert len(first) == 2  # both new -> both queued (never gated)

        rows = (
            await session.execute(select(Item).where(Item.library_id == lib_id))
        ).scalars().all()
        by_name = {r.filename: str(r.id) for r in rows}

        # Stand up the lookalike queue with ONE pending job (for queued.bin);
        # orphan.bin's job is "gone" (e.g. the operator purged the queue).
        await session.execute(
            text(
                "CREATE TABLE IF NOT EXISTS procrastinate_jobs ("
                "id bigserial PRIMARY KEY, task_name text, status text, args jsonb)"
            )
        )
        await session.execute(
            text(
                "INSERT INTO procrastinate_jobs (task_name, status, args) VALUES "
                "('filearr.tasks.extract.extract_item', 'todo', "
                "jsonb_build_object('item_id', CAST(:i AS text)))"
            ),
            {"i": by_name["queued.bin"]},
        )
        await session.commit()
        try:
            # Neither row was ever extracted (quick_hash NULL): the self-heal
            # must re-defer ONLY the one without a pending job.
            second = await _run_scan(session, lib)
        finally:
            await session.execute(text("DROP TABLE IF EXISTS procrastinate_jobs"))
            await session.commit()

    assert second == [by_name["orphan.bin"]]
