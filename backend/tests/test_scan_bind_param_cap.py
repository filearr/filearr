"""Live 2026-08-16 regression: the first scan of a 303k-file library crashed at
``Item.id.in_(new_item_ids)`` -- Postgres caps one statement at 65,535 bind
parameters -- and the crash HANDLER then crashed too (expired-attribute read
after ``rollback()`` -> MissingGreenlet), leaving the run 'running' with no
error text and a traceback truncated past the root cause.

Three guards:
  1. ``scalars_where_in`` survives a >65,535-element list against real PG.
  2. A body that fails AFTER doing DB work (transaction open) still yields a
     'failed' run WITH the sanitized error retained (invariant 7).
  3. Traceback truncation keeps the head (root cause) as well as the tail.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import joberrors
from filearr.db import in_chunks, scalars_where_in
from filearr.models import Item, Library, ScanRun

from .conftest import psycopg3_uri

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


@pytest.fixture
async def session(engine):
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s


def test_in_chunks_splits_and_preserves_order():
    assert list(in_chunks([], 3)) == []
    assert list(in_chunks(range(7), 3)) == [[0, 1, 2], [3, 4, 5], [6]]
    assert list(in_chunks(range(3), 3)) == [[0, 1, 2]]


async def test_where_in_over_65535_values_does_not_hit_pg_param_cap(session):
    """70,000 ids in ONE ``in_()`` is the exact live failure ("number of
    parameters must be between 0 and 65535"); the chunked helper must not."""
    lib = Library(name="big", root_path="/nope", enabled_categories=["image"])
    session.add(lib)
    await session.commit()
    ids = [uuid.uuid4() for _ in range(70_000)]
    # A handful of real rows so the result is non-trivial.
    for i, iid in enumerate(ids[:5]):
        session.add(
            Item(
                id=iid,
                library_id=lib.id,
                path=f"/nope/{i}.jpg",
                rel_path=f"{i}.jpg",
                filename=f"{i}.jpg",
                extension="jpg",
                size=1,
                mtime=__import__("datetime").datetime.now(__import__("datetime").UTC),
                file_category="image",
            )
        )
    await session.commit()
    rows = await scalars_where_in(session, select(Item), Item.id, ids)
    assert {r.id for r in rows} == set(ids[:5])
    assert await scalars_where_in(session, select(Item), Item.id, []) == []


async def test_crash_after_db_work_marks_failed_with_error(session, tmp_path, monkeypatch):
    """The body fails with a transaction OPEN (it already ran a statement) so
    the handler's ``rollback()`` expires ``run``/``library``. The handler must
    still mark the run failed and keep the sanitized error, not raise
    MissingGreenlet from an expired-attribute read."""
    from filearr.tasks import scan as scan_mod

    root = tmp_path / "lib"
    root.mkdir()
    lib = Library(name="crashy", root_path=str(root), enabled_categories=["image"])
    session.add(lib)
    await session.commit()

    async def _body_that_worked_then_died(sess, library, run, **kw):
        await sess.execute(select(Item).where(Item.library_id == library.id))
        raise RuntimeError("bind cap \x1b[31mboom")

    monkeypatch.setattr(scan_mod, "_scan_body", _body_that_worked_then_died)
    maker = async_sessionmaker(session.bind, expire_on_commit=False)
    monkeypatch.setattr(scan_mod, "SessionLocal", maker)

    with pytest.raises(RuntimeError, match="bind cap"):
        await scan_mod.scan_library(str(lib.id))

    async with maker() as s:
        run = (await s.execute(select(ScanRun).where(ScanRun.library_id == lib.id))).scalar_one()
    assert run.status == "failed"
    assert run.finished_at is not None
    assert "bind cap" in run.stats["error"]
    assert "\x1b" not in run.stats["error"]


def test_traceback_truncation_keeps_root_cause(monkeypatch):
    """A chained traceback prints the ROOT cause first and the handler crash
    last; truncation must keep both ends."""
    monkeypatch.setattr(joberrors, "TRACEBACK_MAX_CHARS", 800)
    try:
        try:
            raise ValueError("ROOT-CAUSE-MARKER " + "x" * 200)
        except ValueError:
            raise RuntimeError("HANDLER-CRASH-MARKER " + "y" * 250)  # noqa: B904 - implicit chain is the point
    except RuntimeError as exc:
        text_ = joberrors._format_traceback(exc)
    assert "ROOT-CAUSE-MARKER" in text_
    assert "HANDLER-CRASH-MARKER" in text_
    assert "(truncated)" in text_
    assert len(text_) <= 800 + 40
