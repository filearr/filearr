"""Jobs-page maintenance registry: status API, schedule overrides, run-now,
and the override-aware worker tick (JOBS-M1).

Real-Postgres module DB (create_all — migration coverage rides
test_migration's round trip). Procrastinate schema is absent here, so the
status projection's last_run gracefully reads null and run-now is exercised
through a stubbed deferrer; the tick's due-evaluation runs for real against
``maintenance_schedules`` rows with a recording fake defer.
"""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from filearr import maintenance
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Base, MaintenanceSchedule

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def db(module_db, monkeypatch):
    uri = module_db.get_uri().replace("postgresql://", "postgresql+psycopg://", 1)
    engine = create_async_engine(uri)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        from sqlalchemy import text as sqltext

        await conn.execute(sqltext("DELETE FROM maintenance_schedules"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    import filearr.db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
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


async def test_registry_is_coherent():
    """Every task documents itself; tick-scheduled tasks are exactly the
    editable-with-cron subset; keys/task names never collide."""
    specs = list(maintenance.MAINT_TASKS.values())
    assert len(specs) >= 20
    assert len({s.task_name for s in specs}) == len(specs)
    for s in specs:
        assert s.title and len(s.description) > 30, s.key
        if s.editable:
            assert s.default_cron, s.key  # editable implies scheduled
    assert set(maintenance.TICK_SCHEDULED) == {
        s for s in specs if s.editable and s.default_cron
    }
    # the tick itself is registered, fixed, and not runnable
    tick = maintenance.MAINT_TASKS["maintenance_tick"]
    assert not tick.editable and not tick.runnable


async def test_status_overrides_and_validation(client):
    r = await client.get("/api/v1/system/maintenance")
    assert r.status_code == 200
    tasks = {t["key"]: t for t in r.json()["tasks"]}
    assert set(tasks) == set(maintenance.MAINT_TASKS)

    prb = tasks["purge_recycle_bin"]
    assert prb["cron"] == "0 4 * * *" and not prb["overridden"] and prb["editable"]
    assert prb["next_run_at"] is not None and prb["last_run"] is None
    assert len(prb["description"]) > 30
    # on-demand: no schedule, no next run
    ri = tasks["rebuild_index"]
    assert ri["cron"] is None and ri["next_run_at"] is None and ri["runnable"]
    # fixed infrastructure tick: visible, not editable, not runnable
    ss = tasks["schedule_scans"]
    assert not ss["editable"] and not ss["runnable"] and ss["cron"] == "* * * * *"

    # override an editable schedule
    r = await client.patch(
        "/api/v1/system/maintenance/purge_recycle_bin", json={"cron": "30 2 * * *"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["cron"] == "30 2 * * *" and body["overridden"]
    assert body["next_run_at"].endswith("02:30:00+00:00") or "02:30" in body["next_run_at"]

    # disable / re-enable
    r = await client.patch(
        "/api/v1/system/maintenance/purge_recycle_bin", json={"enabled": False}
    )
    assert r.status_code == 200
    assert r.json()["enabled"] is False and r.json()["next_run_at"] is None
    r = await client.patch(
        "/api/v1/system/maintenance/purge_recycle_bin", json={"enabled": True}
    )
    assert r.json()["enabled"] is True

    # reset to default (cron explicitly null)
    r = await client.patch(
        "/api/v1/system/maintenance/purge_recycle_bin", json={"cron": None}
    )
    assert r.status_code == 200
    assert r.json()["cron"] == "0 4 * * *" and not r.json()["overridden"]

    # guardrails
    r = await client.patch(
        "/api/v1/system/maintenance/purge_recycle_bin", json={"cron": "not a cron"}
    )
    assert r.status_code == 422
    r = await client.patch("/api/v1/system/maintenance/nope", json={"cron": "0 4 * * *"})
    assert r.status_code == 404
    r = await client.patch(
        "/api/v1/system/maintenance/schedule_scans", json={"cron": "0 4 * * *"}
    )
    assert r.status_code == 409


async def test_run_now_endpoint(client, monkeypatch):
    calls: list[str] = []

    async def fake_run_now(key: str):
        calls.append(key)
        return 4242

    monkeypatch.setattr(maintenance, "run_now", fake_run_now)
    r = await client.post("/api/v1/system/maintenance/gc_thumbnails/run")
    assert r.status_code == 202 and r.json() == {"job_id": 4242}
    assert calls == ["gc_thumbnails"]

    r = await client.post("/api/v1/system/maintenance/nope/run")
    assert r.status_code == 404
    # minutely infrastructure ticks are not triggerable
    r = await client.post("/api/v1/system/maintenance/pump_alerts/run")
    assert r.status_code == 409

    async def already(key: str):
        raise maintenance.AlreadyQueued("lock held")

    monkeypatch.setattr(maintenance, "run_now", already)
    r = await client.post("/api/v1/system/maintenance/gc_thumbnails/run")
    assert r.status_code == 409


async def test_tick_fires_due_tasks_once(db):
    deferred: list[tuple[str, datetime]] = []

    async def fake_defer(spec, occ):
        deferred.append((spec.key, occ))

    # Override one task ONTO the 04:00 slot and disable another that would
    # natively fire there, then tick at exactly 04:00 UTC.
    async with db() as s:
        s.add(MaintenanceSchedule(task_key="purge_item_versions", cron="0 4 * * *"))
        s.add(MaintenanceSchedule(task_key="purge_recycle_bin", enabled=False))
        await s.commit()

    tick = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
    fired = await maintenance.run_maintenance_tick(tick, defer=fake_defer)

    keys = {k for k, _ in deferred}
    assert fired == [k for k, _ in deferred]
    assert "purge_item_versions" in keys        # override moved it here
    assert "purge_recycle_bin" not in keys      # disabled
    assert "nightly_reconcile" not in keys      # default 04:30 — not due
    # every occurrence stamped = consumed: the same tick never re-fires
    deferred.clear()
    assert await maintenance.run_maintenance_tick(tick, defer=fake_defer) == []

    # a later minute fires the hourly tasks again but not the dailies
    tick2 = datetime(2026, 7, 29, 4, 7, tzinfo=UTC)
    fired2 = await maintenance.run_maintenance_tick(tick2, defer=fake_defer)
    assert fired2 == ["reconcile_meili"]

    async with db() as s:
        rows = {
            r.task_key: r
            for r in (await s.execute(select(MaintenanceSchedule))).scalars()
        }
    assert rows["purge_item_versions"].last_cron_fired_at == tick
    assert rows["reconcile_meili"].last_cron_fired_at == tick2


async def test_pre_migration_window_is_graceful(db):
    """Deploy-window race (live 2026-07-29): the new worker's minutely tick ran
    before alembic created ``maintenance_schedules`` and hard-failed into the
    failed-jobs surface. Both the tick and the status projection must degrade
    to a no-op / defaults-only view while the table is absent."""
    from sqlalchemy import text as sqltext

    async with db() as s:
        await s.execute(sqltext("DROP TABLE maintenance_schedules"))
        await s.commit()

    async def boom(spec, occ):  # must never be reached
        raise AssertionError("deferred despite missing table")

    tick = datetime(2026, 7, 29, 4, 0, tzinfo=UTC)
    assert await maintenance.run_maintenance_tick(tick, defer=boom) == []

    async with db() as s:
        rows = await maintenance.maintenance_status(s)
    assert {r["key"] for r in rows} == set(maintenance.MAINT_TASKS)
    assert all(not r["overridden"] for r in rows)
