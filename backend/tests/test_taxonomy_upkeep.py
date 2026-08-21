"""Watermark-guarded taxonomy upkeep (2026-08-21, filearr.taxonomy_ops).

The ``taxonomy_upkeep`` maintenance task adopts a changed shipped seed and
reconverges item classifications on version drift — both event-driven via
app_settings watermarks, so a run with nothing to do is two reads.

ISOLATION: the suite shares one database and ``test_taxonomy_w8`` (which sorts
after this module) asserts ``taxonomy_state.version == 1`` — every mutation
here is restored in a ``finally`` (same discipline as test_taxonomy_seed_sync).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import app_settings, taxonomy, taxonomy_ops
from filearr.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    app_settings.reset_for_tests()
    taxonomy.invalidate()
    yield maker
    # Restore shared-DB state: watermarks gone, cache reset.
    async with maker() as s:
        await s.execute(text(
            "DELETE FROM app_settings WHERE key IN "
            "('taxonomy_seed_fingerprint', 'taxonomy_reclassified_version')"
        ))
        await s.commit()
    app_settings.reset_for_tests()
    taxonomy.invalidate()
    await engine.dispose()


async def _mk_item(maker, rel_path, ext, category, group):
    from filearr.models import Item, Library

    async with maker() as s:
        lib = Library(name=f"upkeep-{rel_path}", root_path="/d")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id, file_category=category, file_group=group,
            status="active", path=f"/d/{rel_path}", rel_path=rel_path,
            filename=rel_path, extension=ext, size=1,
            mtime=datetime.now(UTC), metadata_={},
        )
        s.add(item)
        await s.commit()
        return str(item.id), str(lib.id)


async def test_upkeep_noop_when_watermarks_current(maker, monkeypatch):
    from filearr import worker as worker_mod

    deferred: list[list[str]] = []

    async def _fake(ids):
        deferred.append(list(ids))

    monkeypatch.setattr(worker_mod, "defer_index_sync", _fake)
    async with maker() as s:
        first = await taxonomy_ops.upkeep_now(s)
    # First run on a fresh DB establishes both watermarks (seed already covered
    # -> no version bump; reclassify converges an empty/converged catalogue).
    assert first["seed_synced"] is None or first["seed_synced"]["added_count"] == 0
    async with maker() as s:
        second = await taxonomy_ops.upkeep_now(s)
    assert second == {"seed_synced": None, "reclassified": None}


async def test_upkeep_adopts_missing_seed_ext_and_reclassifies(maker, monkeypatch):
    """Simulates an older DB missing an extension a deploy's seed added: the
    upkeep pass re-adopts it AND converges the misclassified item, end to end."""
    from filearr import worker as worker_mod

    deferred: list[list[str]] = []

    async def _fake(ids):
        deferred.append(list(ids))

    monkeypatch.setattr(worker_mod, "defer_index_sync", _fake)

    async with maker() as s:
        before_version = (await s.execute(
            text("SELECT version FROM taxonomy_state WHERE id = 1"))).scalar_one()
        qml_group = (await s.execute(
            text("SELECT group_id FROM file_group_extensions WHERE ext = 'qml'")
        )).scalar_one()

    item_id = lib_id = None
    try:
        async with maker() as s:
            await s.execute(text("DELETE FROM file_group_extensions WHERE ext = 'qml'"))
            await s.commit()
        taxonomy.invalidate()
        # An item scanned while qml was unmapped: classified (other, other).
        item_id, lib_id = await _mk_item(maker, "ui.qml", "qml", "other", "other")

        async with maker() as s:
            out = await taxonomy_ops.upkeep_now(s)
        assert out["seed_synced"] is not None
        assert out["seed_synced"]["added_count"] >= 1
        assert out["reclassified"] is not None and out["reclassified"]["changed"] >= 1

        async with maker() as s:
            row = (await s.execute(text(
                "SELECT file_category, file_group FROM items WHERE id = :i"
            ), {"i": item_id})).one()
        assert row.file_group == "source-code"
        assert row.file_category != "other"
        assert any(item_id in batch for batch in deferred)

        # Watermarks now current: a second pass is a pure no-op.
        async with maker() as s:
            again = await taxonomy_ops.upkeep_now(s)
        assert again == {"seed_synced": None, "reclassified": None}
    finally:
        async with maker() as s:
            # Put the shared DB back exactly as found (group id AND version).
            await s.execute(text(
                "INSERT INTO file_group_extensions (ext, group_id) VALUES ('qml', :g) "
                "ON CONFLICT (ext) DO UPDATE SET group_id = :g"
            ), {"g": qml_group})
            await s.execute(text(
                "UPDATE taxonomy_state SET version = :v WHERE id = 1"
            ), {"v": before_version})
            if item_id:
                await s.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
                await s.execute(text("DELETE FROM libraries WHERE id = :i"), {"i": lib_id})
            await s.commit()
        taxonomy.invalidate()


async def test_upkeep_reclassifies_on_version_drift_without_seed_change(maker, monkeypatch):
    """Version watermark alone: no seed change, but the stored watermark lags
    the live version — one reclassify converges a drifted item, then quiet."""
    from filearr import worker as worker_mod

    monkeypatch.setattr(worker_mod, "defer_index_sync", lambda ids: _noop(ids))

    async def _noop(ids):
        return None

    item_id = lib_id = None
    try:
        # Establish current watermarks first.
        async with maker() as s:
            await taxonomy_ops.upkeep_now(s)
        # Drift: misclassify an item and knock the watermark back.
        item_id, lib_id = await _mk_item(maker, "movie.mkv", "mkv", "other", "other")
        async with maker() as s:
            await app_settings.set_value(
                s, app_settings.KEY_TAXONOMY_RECLASSIFIED_VERSION, -1, updated_by=None
            )
            await s.commit()
        async with maker() as s:
            out = await taxonomy_ops.upkeep_now(s)
        assert out["seed_synced"] is None
        assert out["reclassified"] is not None and out["reclassified"]["changed"] >= 1
        async with maker() as s:
            cat = (await s.execute(text(
                "SELECT file_category FROM items WHERE id = :i"), {"i": item_id}
            )).scalar_one()
        assert cat == "video"
    finally:
        async with maker() as s:
            if item_id:
                await s.execute(text("DELETE FROM items WHERE id = :i"), {"i": item_id})
                await s.execute(text("DELETE FROM libraries WHERE id = :i"), {"i": lib_id})
            await s.commit()


def test_taxonomy_upkeep_is_a_scheduled_maintenance_task():
    from filearr import maintenance

    spec = next(s for s in maintenance.TICK_SCHEDULED if s.key == "taxonomy_upkeep")
    assert spec.task_name == "filearr.worker.taxonomy_upkeep"
    assert spec.default_cron == "*/15 * * * *"
    assert spec.editable
