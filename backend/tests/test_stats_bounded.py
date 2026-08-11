"""/stats must answer even when one of its aggregates does not (2026-08-11).

Live regression: on the ~1.09M-item instance ``GET /api/v1/stats`` connected
instantly and then hung past 15s on every attempt, so the deploy smoke gate
failed while /health, /version and /search all returned 200. The endpoint fans
out to seven independent aggregates and any single one could block the whole
response forever.

These tests pin the structural guarantee rather than the specific slow query:
whatever the section is, /stats returns 200 with the rest of the payload intact,
names the casualty in ``degraded``, and — critically — keeps serving the sections
that come AFTER the failure on the same session.
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import diskguard
from filearr.api import system as sysapi
from filearr.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
async def client(pg_uri, monkeypatch):
    from filearr import db as db_mod
    from filearr.db import get_session
    from filearr.main import create_app

    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    # Deterministic disk section: os.statvfs does not exist on Windows, and these
    # tests are about the BOUND, not about real filesystem headroom.
    monkeypatch.setattr(
        diskguard, "monitored_statuses",
        lambda s: [{
            "path": "/config/thumbnails", "label": "thumbnails", "is_pg": False,
            "exists": True, "total": 100 * diskguard.GB, "free": 90 * diskguard.GB,
            "used": 10 * diskguard.GB, "pct_free": 90.0, "dev": 42,
            "status": diskguard.OK, "reason": "test",
        }],
    )
    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    await engine.dispose()


async def test_healthy_instance_reports_nothing_degraded(client):
    r = await client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert body["degraded"] == {}
    assert {"by_type", "queues", "meili", "semantic", "thumbs", "disk"} <= set(body)


async def test_statement_timeout_is_armed(client, monkeypatch):
    """The clean bound is server-side, so a slow QUERY aborts in Postgres."""
    seen = {}

    async def _peek(session):
        seen["timeout"] = (
            await session.execute(text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()
        return {}

    monkeypatch.setattr(sysapi, "_by_type", _peek)
    assert (await client.get("/api/v1/stats")).status_code == 200
    # 5000ms, rendered by Postgres in its own units.
    assert seen["timeout"] in {"5000ms", "5s"}


async def test_slow_section_is_bounded_not_fatal(client, monkeypatch):
    """A section that never returns costs its own field, not the request."""
    import asyncio

    monkeypatch.setattr(sysapi, "STATS_SECTION_TIMEOUT_S", 0.2)

    async def _never(session):
        await asyncio.sleep(30)

    monkeypatch.setattr(sysapi, "_by_type", _never)
    r = await client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert "timed out" in body["degraded"]["by_type"]
    assert body["by_type"] == {}
    # Everything downstream of the casualty still ran. (Meili is not up in the
    # test env, so at this 0.2s bound it degrades too — which is the point: two
    # independent casualties still leave a complete, honest payload.)
    assert body["disk"]["status"] == diskguard.OK
    assert isinstance(body["thumbs"]["count"], int)


async def test_failed_section_does_not_poison_later_sections(client, monkeypatch):
    """The rollback matters: an aborted transaction would cascade otherwise.

    A statement timeout leaves the transaction in ``InFailedSqlTransaction``, and
    every later section shares this session — so without the recovery rollback a
    single slow aggregate would take the whole payload down with it. Here the
    failure is a genuinely aborted transaction, not a plain Python exception, so
    the cascade is real if the rollback is missing."""

    async def _abort(session):
        await session.execute(text("SELECT no_such_function_at_all()"))
        return {}

    monkeypatch.setattr(sysapi, "_by_type", _abort)
    r = await client.get("/api/v1/stats")
    assert r.status_code == 200
    body = r.json()
    assert set(body["degraded"]) == {"by_type"}  # ONLY the culprit
    assert body["disk"]["status"] and "unavailable" not in body["disk"]
    assert isinstance(body["thumbs"]["count"], int)


async def test_degraded_reason_stays_out_of_the_payload_shape(client, monkeypatch):
    """extract_errors is a library_id->count map; a marker key inside it would
    be indistinguishable from data, so reasons live in the top-level map."""

    async def _boom(session):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(sysapi, "extract_error_counts_by_library", _boom)
    body = (await client.get("/api/v1/stats")).json()
    assert body["extract_errors"] == {}
    assert "extract_errors" in body["degraded"]


async def test_total_budget_caps_the_endpoint(client, monkeypatch):
    """Per-section bounds alone would not have saved the deploy gate.

    Sections run in SEQUENCE, so N slow ones cost N x the per-section bound. The
    shared deadline is what keeps the response under the smoke test's 15s no
    matter how many aggregates are sick."""
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(sysapi, "STATS_TOTAL_BUDGET_S", 1.0)

    async def _never(session):
        await asyncio.sleep(30)

    for target in ("_by_type", "_thumbnail_stats"):
        monkeypatch.setattr(sysapi, target, _never)

    started = loop.time()
    r = await client.get("/api/v1/stats")
    elapsed = loop.time() - started
    assert r.status_code == 200
    # Two 8s sections would be 16s without the deadline; the 0.25s floor per
    # section is the only thing allowed to push past the budget.
    assert elapsed < 4.0, elapsed
    assert {"by_type", "thumbs"} <= set(r.json()["degraded"])
