"""P7 B16/B17 — library delete-impact summary, queued-scan-job guard and the
deferred Meili prune.

Runs against a DB with the REAL procrastinate schema applied so the job
attribution SQL (args ``library_id`` / single ``item_id``) and the queued-scan
guard execute for real. The prune defer is monkeypatched to record its ids.
"""

from __future__ import annotations

import json
import os
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import func, select, text

procrastinate = pytest.importorskip("procrastinate")

BACKEND_DIR = Path(__file__).resolve().parent.parent
BASE = "/api/v1/libraries"
SCAN_TASK = "filearr.tasks.scan.scan_library"
EXTRACT_TASK = "filearr.tasks.extract.extract_item"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture(scope="module")
def p7_pg(module_db):
    import procrastinate as _proc
    import psycopg

    schema_sql = (Path(os.path.dirname(_proc.__file__)) / "sql" / "schema.sql").read_text()
    with psycopg.connect(module_db.get_uri(), autocommit=True) as conn:
        conn.execute(schema_sql)
    return module_db


@pytest.fixture
async def wired(p7_pg, monkeypatch):
    from alembic.config import Config
    from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

    from alembic import command
    from filearr import db as db_mod
    from filearr.api import libraries as lib_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    uri = p7_pg.get_uri()
    monkeypatch.setenv("FILEARR_DATABASE_URL", uri)
    get_settings.cache_clear()
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")

    engine = create_async_engine(_psycopg3(uri))
    async with engine.begin() as conn:
        for table in (
            "alert_events",
            "alert_rules",
            "path_grants",
            "scan_paths",
            "items",
            "scan_runs",
            "libraries",
            "procrastinate_jobs",
        ):
            await conn.execute(text(f"DELETE FROM {table}"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)

    deferred: list[list[str]] = []

    async def _fake_defer_delete_docs(ids):
        deferred.append(list(ids))
        return 1 if ids else 0

    monkeypatch.setattr(lib_mod, "defer_delete_docs", _fake_defer_delete_docs)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    yield {
        "app": app,
        "maker": maker,
        "engine": engine,
        "deferred": deferred,
        "lib_mod": lib_mod,
        "monkeypatch": monkeypatch,
    }
    app.dependency_overrides.clear()
    await engine.dispose()
    get_settings.cache_clear()


def _client(app):
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t")


async def _mk_lib(maker, name):
    from filearr.models import Library

    async with maker() as s:
        lib = Library(name=name, root_path=f"/d/{name}")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(maker, library_id, rel_path):
    from filearr.models import Item

    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="audio",
            file_group="audio-lossy",
            status="active",
            path=f"/d/{rel_path}",
            rel_path=rel_path,
            filename=rel_path,
            extension="mp3",
            size=1,
            mtime=datetime.now(UTC),
            metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id)


async def _seed_job(engine, task, args, status="todo", queue="scan"):
    async with engine.begin() as conn:
        await conn.execute(
            text(
                "INSERT INTO procrastinate_jobs (queue_name, task_name, args, status) "
                "VALUES (:q, :t, CAST(:args AS jsonb), "
                "CAST(:st AS procrastinate_job_status))"
            ),
            {"q": queue, "t": task, "args": json.dumps(args), "st": status},
        )


async def _seed_cascade_rows(maker, lib, other_lib):
    """1 scan run, 1 hot folder, 1 path grant, a library rule with 2 events, and
    an UNSCOPED rule whose event merely references this library (survives the
    delete with library_id nulled — not counted)."""
    from filearr.models import AlertEvent, AlertRule, PathGrant, ScanPath, ScanRun

    async with maker() as s:
        s.add(ScanRun(library_id=lib, status="finished"))
        s.add(ScanPath(library_id=lib, rel_path="hot"))
        s.add(
            PathGrant(
                subject_kind="group",
                subject_id=uuid.uuid4(),
                library_id=lib,
                scope="root",
                action="download",
            )
        )
        lib_rule = AlertRule(name="lib-rule", library_id=lib, event_types=["created"])
        global_rule = AlertRule(name="global-rule", event_types=["created"])
        other_rule = AlertRule(name="other-rule", library_id=other_lib, event_types=["created"])
        s.add_all([lib_rule, global_rule, other_rule])
        await s.flush()
        for i in range(2):
            s.add(
                AlertEvent(
                    rule_id=lib_rule.id, library_id=lib, event_type="created", dedup_key=f"k{i}"
                )
            )
        s.add(
            AlertEvent(rule_id=global_rule.id, library_id=lib, event_type="created", dedup_key="g")
        )
        s.add(
            AlertEvent(
                rule_id=other_rule.id, library_id=other_lib, event_type="created", dedup_key="o"
            )
        )
        await s.commit()


async def test_delete_impact_counts(wired):
    maker, engine = wired["maker"], wired["engine"]
    lib = await _mk_lib(maker, "Music")
    other = await _mk_lib(maker, "Other")
    a = await _mk_item(maker, lib, "a.mp3")
    await _mk_item(maker, lib, "b.mp3")
    other_item = await _mk_item(maker, other, "c.mp3")
    await _seed_cascade_rows(maker, lib, other)
    # Attributable pending jobs: a queued scan (args library_id) + a running
    # extract of one of its items (args item_id).
    await _seed_job(engine, SCAN_TASK, {"library_id": str(lib)}, "todo")
    await _seed_job(engine, EXTRACT_TASK, {"item_id": a}, "doing", queue="extract")
    # NOT counted: finished job, another library's jobs, an item_ids-list batch.
    await _seed_job(engine, EXTRACT_TASK, {"item_id": a}, "succeeded", queue="extract")
    await _seed_job(engine, SCAN_TASK, {"library_id": str(other)}, "todo")
    await _seed_job(engine, EXTRACT_TASK, {"item_id": other_item}, "todo", queue="extract")
    await _seed_job(
        engine, "filearr.tasks.index_sync.sync_items", {"item_ids": [a]}, "todo", queue="index"
    )

    async with _client(wired["app"]) as c:
        r = await c.get(f"{BASE}/{lib}/delete-impact")
    assert r.status_code == 200, r.text
    assert r.json() == {
        "library_id": str(lib),
        "name": "Music",
        "items": 2,
        "scan_runs": 1,
        "alert_rules": 1,
        "alert_events": 2,
        "path_grants": 1,
        "scan_paths": 1,
        "pending_jobs": 2,
        "scan_job_pending": True,
    }


async def test_delete_impact_unknown_404(wired):
    async with _client(wired["app"]) as c:
        r = await c.get(f"{BASE}/{uuid.uuid4()}/delete-impact")
    assert r.status_code == 404


@pytest.mark.parametrize(
    ("args_extra", "status"),
    [({}, "todo"), ({}, "doing"), ({"rel_path": "hot"}, "todo")],
)
async def test_delete_refused_while_scan_job_pending(wired, args_extra, status):
    """B17: a queued (no ScanRun yet) or running scan JOB — full or scoped —
    blocks the delete with 409; nothing is removed or pruned."""
    from filearr.models import Library

    maker, engine = wired["maker"], wired["engine"]
    lib = await _mk_lib(maker, "Queued")
    await _mk_item(maker, lib, "a.mp3")
    await _seed_job(engine, SCAN_TASK, {"library_id": str(lib), **args_extra}, status)

    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Queued")
    assert r.status_code == 409, r.text
    assert "queued or running" in r.json()["detail"]
    async with maker() as s:
        assert (await s.execute(select(func.count()).select_from(Library))).scalar_one() == 1
    assert wired["deferred"] == []


async def test_delete_cascades_and_defers_prune_after_commit(wired):
    """A finished scan job does not block; the cascade removes everything the
    impact listed; the prune is deferred with exactly the library's item ids."""
    from filearr.models import AlertEvent, AlertRule, Library, PathGrant, ScanPath

    maker, engine = wired["maker"], wired["engine"]
    lib = await _mk_lib(maker, "Docs")
    other = await _mk_lib(maker, "Kept")
    ids = {await _mk_item(maker, lib, "a.pdf"), await _mk_item(maker, lib, "b.pdf")}
    await _seed_cascade_rows(maker, lib, other)
    await _seed_job(engine, SCAN_TASK, {"library_id": str(lib)}, "succeeded")

    committed_before_defer: list[bool] = []

    async def _checking_defer(item_ids):
        # Invariant 5: the library row must already be gone (committed) when
        # the prune is deferred.
        async with maker() as s:
            gone = (await s.execute(select(Library).where(Library.id == lib))).first() is None
        committed_before_defer.append(gone)
        wired["deferred"].append(list(item_ids))
        return 1

    wired["monkeypatch"].setattr(wired["lib_mod"], "defer_delete_docs", _checking_defer)

    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Docs")
    assert r.status_code == 204, r.text
    assert committed_before_defer == [True]
    assert {i for batch in wired["deferred"] for i in batch} == ids

    async with maker() as s:
        names = set((await s.execute(select(AlertRule.name))).scalars())
        assert names == {"global-rule", "other-rule"}
        assert (await s.execute(select(func.count()).select_from(PathGrant))).scalar_one() == 0
        assert (await s.execute(select(func.count()).select_from(ScanPath))).scalar_one() == 0
        # the unscoped rule's event survives with library_id nulled
        surviving = (await s.execute(select(AlertEvent.dedup_key, AlertEvent.library_id))).all()
        assert sorted(surviving) == sorted([("g", None), ("o", other)])


async def test_delete_succeeds_when_prune_defer_fails(wired):
    """The Postgres delete already committed; a queue outage only delays the
    prune (reconcile_meili sweeps the orphans) — still 204."""
    from filearr.models import Library

    maker = wired["maker"]
    lib = await _mk_lib(maker, "Flaky")
    await _mk_item(maker, lib, "a.pdf")

    async def _boom(item_ids):
        raise RuntimeError("queue down")

    wired["monkeypatch"].setattr(wired["lib_mod"], "defer_delete_docs", _boom)
    async with _client(wired["app"]) as c:
        r = await c.delete(f"{BASE}/{lib}?confirm=Flaky")
    assert r.status_code == 204, r.text
    async with maker() as s:
        assert (await s.execute(select(func.count()).select_from(Library))).scalar_one() == 0


async def test_defer_delete_docs_batches_jobs(monkeypatch):
    """worker.defer_delete_docs splits ids into DELETE_DOCS_BATCH-sized jobs in
    one batch_defer_async call, and is a no-op for an empty list."""
    import contextlib

    from filearr import worker

    calls: list[dict] = []

    class _Deferrer:
        async def batch_defer_async(self, *job_kwargs):
            calls.append({"jobs": list(job_kwargs)})

    seen_task: list[tuple] = []

    def _configure(task, **kw):
        seen_task.append((task, kw.get("queue")))
        return _Deferrer()

    @contextlib.asynccontextmanager
    async def _noop_pool():
        yield

    monkeypatch.setattr(worker.proc_app, "configure_task", _configure)
    monkeypatch.setattr(worker, "open_pool_if_needed", _noop_pool)
    monkeypatch.setattr(worker, "DELETE_DOCS_BATCH", 2)

    assert await worker.defer_delete_docs([]) == 0
    assert calls == []
    assert await worker.defer_delete_docs(["a", "b", "c", "d", "e"]) == 3
    assert seen_task == [("filearr.tasks.index_sync.delete_documents", "index")]
    assert calls == [
        {"jobs": [{"item_ids": ["a", "b"]}, {"item_ids": ["c", "d"]}, {"item_ids": ["e"]}]}
    ]


async def test_delete_documents_task_prunes_by_id(monkeypatch):
    from filearr.tasks import index_sync

    pruned: list[list[str]] = []

    async def _fake_delete_docs(ids):
        pruned.append(list(ids))

    monkeypatch.setattr(index_sync, "delete_docs", _fake_delete_docs)
    assert await index_sync.delete_documents.func(item_ids=["x", "y"]) == 2
    assert pruned == [["x", "y"]]
    assert await index_sync.delete_documents.func(item_ids=[]) == 0
    assert pruned == [["x", "y"]]
