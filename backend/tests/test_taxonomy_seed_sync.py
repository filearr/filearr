"""taxonomy sync-seed x the widened seed — the composition test.

These two shipped together but were built independently: the seed grew by 79
extensions (Blue Iris .bvr and friends, the whole MIDI family, tracker modules),
and ``POST /taxonomy/sync-seed`` is what carries a widened seed into a DB that
has already been seeded. Neither is useful without the other, and the failure
mode if they do not compose is SILENT — the catalogue simply keeps classifying
.bvr as ``other`` with no error anywhere to explain it.

The fresh-install path is not the interesting one (the migration seeds the full
payload, so sync-seed has nothing to do). What matters is the UPGRADE path, so
this test manufactures it: strip the extensions this release added, make an
operator edit, and assert sync-seed restores the former without touching the
latter."""
from pathlib import Path

import pytest
from alembic.config import Config
from httpx import ASGITransport, AsyncClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import taxonomy
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import FileGroupExtension

BACKEND_DIR = Path(__file__).resolve().parent


def _psycopg3(uri): return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client(pg_uri, monkeypatch):
    cfg = Config(str(Path("d:/repos/filearr/backend") / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setenv("FILEARR_AUTH_ENABLED", "false")
    get_settings.cache_clear()
    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s
    app.dependency_overrides[get_session] = _sess
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://t") as c:
        yield c, maker
    await engine.dispose()
    get_settings.cache_clear()


async def test_sync_seed_carries_new_extensions_into_an_edited_taxonomy(client):
    """NOTE ON ISOLATION: the suite shares one pgserver database and the taxonomy
    fixtures in test_taxonomy_w8 assume a freshly-migrated one — that module
    asserts ``taxonomy_state.version == 1``. This module sorts BEFORE it, so
    every mutation here is snapshotted and restored in the finally below.
    Without that, sync-seed's version bump makes three unrelated tests fail with
    ``assert 2 == 1``, which is a genuinely confusing way to learn about test
    pollution (it happened, 2026-08-10)."""
    c, maker = client
    async with maker() as s:
        before_version = (await s.execute(
            text("SELECT version FROM taxonomy_state WHERE id = 1"))).scalar_one()
        before_mp4 = (await s.execute(
            text("SELECT group_id FROM file_group_extensions WHERE ext = 'mp4'"))).scalar_one()
    try:
        await _run_sync_seed_case(c, maker)
    finally:
        # Put the shared database back exactly as it was found.
        async with maker() as s:
            await s.execute(
                text("UPDATE file_group_extensions SET group_id = :g WHERE ext = 'mp4'"),
                {"g": before_mp4},
            )
            await s.execute(
                text("UPDATE taxonomy_state SET version = :v WHERE id = 1"),
                {"v": before_version},
            )
            await s.commit()
        taxonomy.invalidate()


async def _run_sync_seed_case(c, maker):
    # Simulate a deployment seeded by an OLDER release: strip the extensions this
    # release added, and make an operator edit that must survive untouched.
    async with maker() as s:
        await s.execute(text(
            "DELETE FROM file_group_extensions WHERE ext IN "
            "('bvr','g64','g64x','mid','midi','264','265','icns','pcapng','vcf')"))
        await s.execute(text(
            "UPDATE file_group_extensions SET group_id = "
            "(SELECT id FROM file_groups WHERE key = 'audio-lossy') WHERE ext = 'mp4'"))
        await s.commit()

    # Dry run first: it must report the additions and change nothing.
    r = await c.post("/api/v1/taxonomy/sync-seed?dry_run=true")
    assert r.status_code == 200, r.text
    dry = r.json()
    assert dry["dry_run"] is True

    assert dry["added_count"] >= 10, dry
    async with maker() as s:
        wrote = (await s.execute(text(
            "SELECT count(*) FROM file_group_extensions WHERE ext = 'bvr'"))).scalar_one()
    assert wrote == 0, "dry run must not write"

    r = await c.post("/api/v1/taxonomy/sync-seed")
    assert r.status_code == 200, r.text
    real = r.json()

    async with maker() as s:
        got = {row.ext for row in (await s.execute(select(FileGroupExtension))).scalars()}
    for ext in ("bvr", "g64", "mid", "midi", "264", "icns", "pcapng"):
        assert ext in got, f"{ext} did not reach the DB taxonomy"

    async with maker() as s:
        grp = (await s.execute(text(
            "SELECT g.key FROM file_group_extensions e JOIN file_groups g "
            "ON g.id = e.group_id WHERE e.ext = 'mp4'"))).scalar_one()
    assert grp == "audio-lossy", f"sync-seed clobbered an operator edit: mp4 -> {grp}"
    assert any(x.get("ext") == "mp4" for x in real["skipped"]), real["skipped"][:5]

    # Idempotent: a second run adds nothing.
    r2 = await c.post("/api/v1/taxonomy/sync-seed")
    assert r2.json()["added_count"] == 0, r2.json()
    print("SYNC-SEED OK: added", real["added_count"], "| dry-run reported", dry["added_count"])
