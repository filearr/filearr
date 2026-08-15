"""Console Logs panel: the fail-open DB log sink (filearr.logsink), the
/system/logs tail API, and the purge_app_logs retention task.

Real-Postgres module DB. The sink is constructed directly with a private
conninfo (the suite-wide FILEARR_LOG_DB_ENABLED=false in conftest keeps the
process-global install() inert); the flusher runs with a short interval and
the test polls the table."""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import UTC, datetime, timedelta

import httpx
import pytest
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from filearr.config import get_settings
from filearr.db import get_session
from filearr.logsink import DbLogSink
from filearr.main import create_app
from filearr.models import AppLog, Base


@pytest.fixture
async def db(module_db, monkeypatch):
    uri = module_db.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.execute(text("DELETE FROM app_logs"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db, monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with db() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


def _record(
    name: str, level: int, msg: str = "hello %s", args: tuple = ("world",)
) -> logging.LogRecord:
    return logging.LogRecord(name, level, "x.py", 1, msg, args, None)


async def test_policy_filearr_info_others_warning_only(module_db):
    sink = DbLogSink("app", module_db.get_uri())
    assert sink.should_store(_record("filearr.tasks.scan", logging.INFO))
    assert sink.should_store(_record("filearr", logging.INFO))
    assert not sink.should_store(_record("filearr.tasks.scan", logging.DEBUG))
    # non-filearr loggers: warnings and up only
    assert not sink.should_store(_record("procrastinate.worker", logging.INFO))
    assert sink.should_store(_record("procrastinate.worker", logging.WARNING))
    # hard exclusions regardless of level
    assert not sink.should_store(_record("uvicorn.access", logging.ERROR))
    assert not sink.should_store(_record("filearr.logsink", logging.ERROR))


async def test_row_shape_caps_and_traceback(module_db):
    sink = DbLogSink("worker", module_db.get_uri())
    row = sink.row_for(_record("filearr.x", logging.WARNING))
    assert row[1:6] == ("worker", "WARNING", logging.WARNING, "filearr.x", "hello world")
    # message cap
    long = sink.row_for(_record("filearr.x", logging.WARNING, "y" * 5000, ()))
    assert len(long[5]) == 2_000
    # exc_info renders a capped traceback
    try:
        raise ValueError("boom")
    except ValueError:
        rec = logging.LogRecord("filearr.x", logging.ERROR, "x.py", 1, "failed", (), True)
        import sys

        rec.exc_info = sys.exc_info()
    exc_row = sink.row_for(rec)
    assert "ValueError: boom" in exc_row[6]


async def test_flusher_persists_batches(db, module_db):
    sink = DbLogSink("app", module_db.get_uri(), flush_interval=0.05, fail_backoff=0.1)
    sink.start()
    try:
        logger = logging.getLogger("filearr.test_logsink_flush")
        logger.setLevel(logging.INFO)
        logger.addHandler(sink.handler)
        logger.propagate = False
        logger.info("first event %d", 1)
        logger.warning("second event")
        deadline = time.monotonic() + 5.0
        count = 0
        while time.monotonic() < deadline:
            async with db() as s:
                count = (
                    await s.execute(select(func.count()).select_from(AppLog))
                ).scalar()
            if count >= 2:
                break
            await asyncio.sleep(0.1)
        assert count >= 2
        async with db() as s:
            rows = (
                (await s.execute(select(AppLog).order_by(AppLog.id))).scalars().all()
            )
        assert rows[0].message == "first event 1"
        assert rows[0].source == "app"
        assert rows[0].level == "INFO"
        logger.removeHandler(sink.handler)
    finally:
        sink.stop()


async def test_logs_api_filters_and_pagination(client, db):
    t = datetime.now(UTC)
    async with db() as s:
        s.add_all(
            [
                AppLog(ts=t, source="app", level="INFO", levelno=20,
                       logger="filearr.api", message="listed libraries"),
                AppLog(ts=t, source="worker", level="WARNING", levelno=30,
                       logger="filearr.tasks.scan", message="slow walk on smb"),
                AppLog(ts=t, source="worker", level="ERROR", levelno=40,
                       logger="filearr.tasks.extract", message="ffprobe timed out",
                       exc="Traceback ..."),
            ]
        )
        await s.commit()

    r = await client.get("/api/v1/system/logs")
    assert r.status_code == 200
    body = r.json()
    assert [row["message"] for row in body["logs"]] == [
        "ffprobe timed out", "slow walk on smb", "listed libraries",
    ]  # newest first
    assert body["logs"][0]["exc"] == "Traceback ..."
    assert body["next_before_id"] is None  # short page

    r = await client.get("/api/v1/system/logs", params={"min_level": "warning"})
    assert [row["level"] for row in r.json()["logs"]] == ["ERROR", "WARNING"]

    r = await client.get("/api/v1/system/logs", params={"source": "app"})
    assert [row["source"] for row in r.json()["logs"]] == ["app"]

    r = await client.get("/api/v1/system/logs", params={"q": "ffprobe"})
    assert [row["message"] for row in r.json()["logs"]] == ["ffprobe timed out"]

    # keyset paging: limit=1 pages newest -> oldest with before_id
    r = await client.get("/api/v1/system/logs", params={"limit": 1})
    body = r.json()
    assert len(body["logs"]) == 1 and body["next_before_id"] == body["logs"][0]["id"]
    r2 = await client.get(
        "/api/v1/system/logs",
        params={"limit": 1, "before_id": body["next_before_id"]},
    )
    assert r2.json()["logs"][0]["message"] == "slow walk on smb"


async def test_purge_retention_and_row_cap(db, monkeypatch):
    import filearr.worker as worker_mod

    monkeypatch.setattr(worker_mod, "SessionLocal", db)
    old = datetime.now(UTC) - timedelta(days=30)
    now = datetime.now(UTC)
    async with db() as s:
        await s.execute(text("DELETE FROM app_logs"))
        s.add_all(
            [AppLog(ts=old, source="app", level="INFO", levelno=20,
                    logger="filearr.x", message=f"ancient {i}") for i in range(3)]
            + [AppLog(ts=now, source="app", level="INFO", levelno=20,
                      logger="filearr.x", message=f"fresh {i}") for i in range(5)]
        )
        await s.commit()

    result = await worker_mod.purge_app_logs_now()
    assert result["aged"] == 3
    async with db() as s:
        remaining = (
            await s.execute(select(func.count()).select_from(AppLog))
        ).scalar()
    assert remaining == 5

    # storm backstop: cap trims to the newest N regardless of age
    monkeypatch.setattr(get_settings(), "log_max_rows", 2)
    result = await worker_mod.purge_app_logs_now()
    assert result["capped"] == 3
    async with db() as s:
        msgs = [
            r.message
            for r in (
                (await s.execute(select(AppLog).order_by(AppLog.id))).scalars().all()
            )
        ]
    assert msgs == ["fresh 3", "fresh 4"]
