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
    """The total budget is a safety net: past it, sections are SKIPPED.

    Sections run in SEQUENCE, so N slow ones would cost N x the per-section
    bound. Once the budget is spent no further section is started — each is
    named in ``degraded`` as skipped — so the endpoint stays capped at roughly
    budget + one section bound no matter how many aggregates are sick."""
    import asyncio

    loop = asyncio.get_running_loop()
    monkeypatch.setattr(sysapi, "STATS_SECTION_TIMEOUT_S", 0.3)
    monkeypatch.setattr(sysapi, "STATS_TOTAL_BUDGET_S", 0.5)

    async def _never(*a, **k):
        await asyncio.sleep(30)

    for target in ("_by_type", "queue_snapshot", "_thumbnail_stats"):
        monkeypatch.setattr(sysapi, target, _never)

    started = loop.time()
    r = await client.get("/api/v1/stats")
    elapsed = loop.time() - started
    assert r.status_code == 200
    assert elapsed < 2.0, elapsed
    deg = r.json()["degraded"]
    assert "timed out" in deg["by_type"] and "timed out" in deg["queues"]
    # by_type (0.3) + queues (0.3) > 0.5: everything after is skipped, not run.
    assert deg["thumbs"].startswith("skipped")
    assert deg["key_fingerprints"].startswith("skipped")


async def test_slow_section_does_not_shrink_a_later_sections_bound(client, monkeypatch):
    """2026-11-02 live cascade: the semantic count burned ~5 s of a SHARED
    deadline and the disk section after it "timed out after 3.174s". Each
    section now gets its whole bound: a slow-but-successful early section must
    not turn a later slow-but-successful one into a casualty."""
    import asyncio

    monkeypatch.setattr(sysapi, "STATS_SECTION_TIMEOUT_S", 0.6)
    monkeypatch.setattr(sysapi, "STATS_TOTAL_BUDGET_S", 1.5)

    async def _slow_ok(session):
        await asyncio.sleep(0.4)
        return {"document": {"count": 1, "bytes": 1}}

    async def _slow_thumbs(session):
        await asyncio.sleep(0.4)
        return {"count": 7, "bytes": 0, "by_source": {}}

    monkeypatch.setattr(sysapi, "_by_type", _slow_ok)
    monkeypatch.setattr(sysapi, "_thumbnail_stats", _slow_thumbs)
    body = (await client.get("/api/v1/stats")).json()
    assert "by_type" not in body["degraded"]
    assert "thumbs" not in body["degraded"]  # shared deadline would have clipped it
    assert body["thumbs"]["count"] == 7


# ------------------------------------------------- SWR-cached sections (2026-11-02)
async def test_slow_semantic_refresh_never_blocks_stats(client, monkeypatch):
    """The semantic counts run in the background; /stats answers at once with
    null counts + pending_refresh, and does NOT record a degradation."""
    import asyncio

    from filearr import embed_stats

    monkeypatch.setattr(get_settings(), "semantic_enabled", True)
    calls = []

    async def _slow_counts(session):
        calls.append(1)
        await asyncio.sleep(30)

    monkeypatch.setattr(embed_stats, "semantic_counts", _slow_counts)
    loop = asyncio.get_running_loop()
    for _ in range(3):
        t0 = loop.time()
        r = await client.get("/api/v1/stats")
        assert loop.time() - t0 < 3.0
        body = r.json()
        sem = body["semantic"]
        assert sem["enabled"] is True
        assert sem["embedded_count"] is None and sem["pending"] is None
        assert sem["fp_mismatches"] is None
        assert sem["pending_refresh"] is True and sem["computed_at"] is None
        assert "semantic" not in body["degraded"]
    await asyncio.sleep(0.05)
    assert len(calls) == 1  # one refresh in flight across all three requests


async def test_semantic_refresh_fills_cache_with_real_counts(client, monkeypatch):
    import asyncio

    from filearr import embed_stats

    monkeypatch.setattr(get_settings(), "semantic_enabled", True)
    first = (await client.get("/api/v1/stats")).json()["semantic"]
    assert first["embedded_count"] is None
    task = embed_stats._cache._task
    assert task is not None
    await asyncio.wait({task}, timeout=10)
    sem = (await client.get("/api/v1/stats")).json()["semantic"]
    # The session DB is shared with other modules, so only the types are pinned
    # here (exact arithmetic: test_embed_p3::test_semantic_snapshot_counts).
    assert all(isinstance(sem[k], int) for k in ("embedded_count", "pending", "fp_mismatches"))
    assert sem["computed_at"] and sem["stale"] is False
    assert sem["pending_refresh"] is False


async def test_semantic_refresh_uses_long_statement_timeout(client, monkeypatch):
    import asyncio

    from filearr import embed_stats

    monkeypatch.setattr(get_settings(), "semantic_enabled", True)
    seen = {}

    async def _peek(session):
        seen["t"] = (
            await session.execute(text("SELECT current_setting('statement_timeout')"))
        ).scalar_one()
        return {"embedded_count": 0, "pending": 0, "fp_mismatches": 0}

    monkeypatch.setattr(embed_stats, "semantic_counts", _peek)
    await client.get("/api/v1/stats")
    await asyncio.wait({embed_stats._cache._task}, timeout=10)
    assert seen["t"] in {"1min", "60s", "60000ms"}


async def test_semantic_index_matches_query_predicates(client, pg_uri):
    """ix_items_embedding_fp only helps if the planner can PROVE the queries'
    WHERE implies the index's — including under a generic (prepared) plan, which
    psycopg switches to after 5 executions. Pin both queries to the index."""
    from filearr import embed_stats

    engine = create_async_engine(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    try:
        async with engine.connect() as conn:
            await conn.execute(text("SET enable_seqscan = off"))
            await conn.execute(text("SET enable_bitmapscan = off"))
            await conn.execute(text("SET plan_cache_mode = force_generic_plan"))
            for stmt, params in (
                (embed_stats._WITH_FP_SQL, {}),
                (embed_stats._CURRENT_SQL, {"fp": "x"}),
            ):
                plan = "\n".join(
                    row[0]
                    for row in await conn.execute(
                        text("EXPLAIN " + str(stmt.text)), params
                    )
                )
                assert "ix_items_embedding_fp" in plan, plan
    finally:
        await engine.dispose()


async def test_slow_disk_probe_reads_unknown_without_costing_other_sections(
    client, monkeypatch
):
    import asyncio
    import time

    monkeypatch.setattr(sysapi, "DISK_FIRST_WAIT_S", 0.2)

    def _wedged(s):
        time.sleep(1.5)  # statvfs on a dead FUSE mount
        return []

    monkeypatch.setattr(diskguard, "monitored_statuses", _wedged)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    body = (await client.get("/api/v1/stats")).json()
    assert loop.time() - t0 < 1.2
    assert body["disk"]["status"] == "unknown"
    assert body["disk"]["pending_refresh"] is True
    # Nothing else was starved (secret_key is a keyguard fact about the shared
    # session DB, not a section bound).
    assert set(body["degraded"]) <= {"secret_key", "ca_root"}
    assert isinstance(body["thumbs"]["count"], int)
    # Let the stuck probe finish so its thread does not outlive the test.
    await asyncio.wait({sysapi._disk_cache._task}, timeout=5)


async def test_stale_disk_value_is_served_while_refreshing(client, monkeypatch):
    import asyncio
    import time

    first = (await client.get("/api/v1/stats")).json()["disk"]
    assert first["status"] == diskguard.OK and first["stale"] is False
    assert first["computed_at"]

    def _wedged(s):
        time.sleep(1.0)
        return []

    monkeypatch.setattr(diskguard, "monitored_statuses", _wedged)
    monkeypatch.setattr(sysapi._disk_cache, "ttl_s", 0)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    disk = (await client.get("/api/v1/stats")).json()["disk"]
    assert loop.time() - t0 < 0.8
    assert disk["status"] == diskguard.OK  # last good value, not "unknown"
    assert disk["stale"] is True and disk["pending_refresh"] is True
    assert disk["paths"] == first["paths"]
    await asyncio.wait({sysapi._disk_cache._task}, timeout=5)
