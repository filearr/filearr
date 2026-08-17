"""Roadmap §18 — persisted job failure text.

The joberrors worker middleware records a sanitized message + capped traceback
per failed attempt into `job_errors`; `errors.failed_jobs` joins the newest
row per job so /system/failed-jobs finally carries a non-null `error`; the
FIX-8 history purge ages the annex table on the same retention window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import psycopg
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import joberrors
from filearr.models import JobError

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired_db(pg_uri, monkeypatch):
    """Migrated DB with filearr.db.SessionLocal repointed (the recorder opens
    its OWN session via that symbol)."""
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM job_errors"))
    maker = async_sessionmaker(engine, expire_on_commit=False)

    import filearr.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    yield {"maker": maker, "pg_uri": pg_uri}
    await engine.dispose()


def _ctx(job_id=101, task="filearr.tasks.extract.extract_item", queue="extract", attempts=2):
    return SimpleNamespace(
        job=SimpleNamespace(id=job_id, task_name=task, queue=queue, attempts=attempts)
    )


@pytest.mark.asyncio
async def test_middleware_records_and_reraises(wired_db):
    async def boom():
        raise RuntimeError("database exploded \x1b[31m<control-chars>")

    with pytest.raises(RuntimeError):
        await joberrors.capture_job_errors(boom, _ctx(), worker=None)

    async with wired_db["maker"]() as s:
        row = (await s.execute(select(JobError))).scalar_one()
    assert row.job_id == 101
    assert row.task_name == "filearr.tasks.extract.extract_item"
    assert row.queue == "extract"
    assert row.attempt == 2
    assert "database exploded" in row.message
    assert "\x1b" not in row.message  # sanitize_error strips control chars
    assert "RuntimeError" in row.traceback
    assert "boom" in row.traceback  # the raise site survives capping


@pytest.mark.asyncio
async def test_middleware_skips_transient_and_aborts(wired_db):
    from procrastinate import exceptions as proc_exceptions

    class Transient(Exception):
        filearr_transient = True

    async def reschedule():
        raise Transient("staged gate closed")

    async def aborted():
        raise proc_exceptions.JobAborted()

    with pytest.raises(Transient):
        await joberrors.capture_job_errors(reschedule, _ctx(), worker=None)
    with pytest.raises(proc_exceptions.JobAborted):
        await joberrors.capture_job_errors(aborted, _ctx(), worker=None)

    async with wired_db["maker"]() as s:
        rows = (await s.execute(select(JobError))).scalars().all()
    assert rows == []


@pytest.mark.asyncio
async def test_middleware_success_passthrough_and_recorder_never_raises(
    wired_db, monkeypatch
):
    async def ok():
        return {"fine": True}

    assert await joberrors.capture_job_errors(ok, _ctx(), worker=None) == {"fine": True}

    # A broken recorder (DB down) must not mask the original failure.
    import filearr.db as db_mod

    def _dead_sessionmaker():
        raise ConnectionError("db is gone")

    monkeypatch.setattr(db_mod, "SessionLocal", _dead_sessionmaker)

    async def boom():
        raise ValueError("the real failure")

    with pytest.raises(ValueError, match="the real failure"):
        await joberrors.capture_job_errors(boom, _ctx(), worker=None)


def test_traceback_capping_keeps_tail():
    """Both ends survive: the head (root cause of a chained exception) and the
    tail (the final raise site). See test_scan_bind_param_cap for the chain."""
    try:
        raise RuntimeError("tail" * 5000)
    except RuntimeError as exc:
        text_ = joberrors._format_traceback(exc)
    assert len(text_) <= joberrors.TRACEBACK_MAX_CHARS + 40
    assert text_.startswith("Traceback (most recent call last)")  # head kept
    assert "… (truncated) …" in text_
    assert text_.rstrip().endswith("tail")  # the raise site (tail) is kept


@pytest.mark.asyncio
async def test_failed_jobs_surfaces_recorded_error(wired_db):
    """End-to-end query: a failed procrastinate row + its job_errors annex row
    come back with error/traceback populated (newest attempt wins)."""
    from procrastinate import PsycopgConnector

    from filearr.errors import failed_jobs
    from filearr.worker import proc_app

    pg_uri = wired_db["pg_uri"]
    connector = PsycopgConnector(conninfo=pg_uri)
    original = proc_app.connector
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
    proc_app.connector = original

    with psycopg.connect(pg_uri, autocommit=True) as conn:
        conn.execute("TRUNCATE procrastinate_jobs RESTART IDENTITY CASCADE")
        conn.execute(
            "INSERT INTO procrastinate_jobs (id, queue_name, task_name, args, status) "
            "VALUES (7, 'extract', 'filearr.tasks.extract.extract_item', '{}'::jsonb, "
            "'failed'::procrastinate_job_status)"
        )

    now = datetime.now(UTC)
    async with wired_db["maker"]() as s:
        s.add(
            JobError(
                job_id=7, task_name="filearr.tasks.extract.extract_item",
                queue="extract", attempt=1, message="first attempt failed",
                traceback="tb1", created_at=now - timedelta(minutes=5),
            )
        )
        s.add(
            JobError(
                job_id=7, task_name="filearr.tasks.extract.extract_item",
                queue="extract", attempt=2, message="second attempt failed",
                traceback="tb2", created_at=now,
            )
        )
        await s.commit()
        jobs = await failed_jobs(s, limit=10)

    assert len(jobs) == 1
    assert jobs[0]["error"] == "second attempt failed"  # newest attempt wins
    assert jobs[0]["traceback"] == "tb2"


@pytest.mark.asyncio
async def test_purge_ages_out_job_errors(wired_db, monkeypatch):
    """purge_job_history_now deletes annex rows past the retention window and
    keeps fresh ones."""
    from procrastinate import PsycopgConnector

    import filearr.worker as worker_mod
    from filearr.worker import proc_app, purge_job_history_now

    pg_uri = wired_db["pg_uri"]
    monkeypatch.setattr(worker_mod, "SessionLocal", wired_db["maker"])

    now = datetime.now(UTC)
    retention = worker_mod.get_settings().job_history_retention_days
    async with wired_db["maker"]() as s:
        s.add(
            JobError(
                job_id=1, task_name="t", queue="q", attempt=1, message="old",
                created_at=now - timedelta(days=retention + 1),
            )
        )
        s.add(
            JobError(
                job_id=2, task_name="t", queue="q", attempt=1, message="fresh",
                created_at=now,
            )
        )
        await s.commit()

    connector = PsycopgConnector(conninfo=pg_uri)
    with proc_app.replace_connector(connector):
        async with proc_app.open_async():
            exists = await connector.execute_query_one_async(
                "SELECT to_regclass('procrastinate_jobs') AS r"
            )
            if exists["r"] is None:
                await proc_app.schema_manager.apply_schema_async()
            await purge_job_history_now()

    async with wired_db["maker"]() as s:
        remaining = (await s.execute(select(JobError.message))).scalars().all()
    assert remaining == ["fresh"]
