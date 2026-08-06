"""GET /stats/libraries — per-library catalog footprint (2026-08-06).

Covers: active count/bytes per library, the sidecar subset, missing/trashed
tombstone tails, catalog totals (actives only), size-descending ordering, the
is_agent flag, and empty libraries still listing. Real Postgres, mirrors the
test_reports_p11 harness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, Item, Library

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def api(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_lib(maker, name="Lib", *, source_agent_id=None):
    async with maker() as s:
        lib = Library(name=name, root_path="/data/l", source_agent_id=source_agent_id)
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_agent(maker):
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
            last_contiguous_seq_no=0,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _mk_item(
    maker, lib_id, rel_path, *, size=100, status="active", sidecar_of=None
):
    async with maker() as s:
        item = Item(
            library_id=lib_id,
            file_category="other",
            file_group="other",
            status=status,
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension="bin",
            size=size,
            mtime=datetime.now(UTC),
            metadata_={},
            user_metadata={},
            external_ids={},
            tags=[],
            sidecar_of=sidecar_of,
        )
        s.add(item)
        await s.commit()
        return item.id


async def test_counts_bytes_and_tails(api):
    client, maker = api
    lib = await _mk_lib(maker, name="Main")
    primary = await _mk_item(maker, lib, "a/movie.mkv", size=1000)
    await _mk_item(maker, lib, "a/movie.nfo", size=10, sidecar_of=primary)
    await _mk_item(maker, lib, "gone.bin", size=50, status="missing")
    await _mk_item(maker, lib, "binned.bin", size=60, status="trashed")

    r = await client.get("/api/v1/stats/libraries")
    assert r.status_code == 200
    body = r.json()
    row = next(x for x in body["libraries"] if x["name"] == "Main")
    # active only: the primary + its sidecar; tombstones counted separately
    assert row["file_count"] == 2
    assert row["total_bytes"] == 1010
    assert row["sidecar_count"] == 1
    assert row["missing_count"] == 1
    assert row["trashed_count"] == 1
    assert body["total_files"] == 2 and body["total_bytes"] == 1010


async def test_ordering_agent_flag_and_empty_library(api):
    client, maker = api
    agent = await _mk_agent(maker)
    big = await _mk_lib(maker, name="Big")
    small = await _mk_lib(maker, name="Small", source_agent_id=agent)
    await _mk_lib(maker, name="Empty")
    await _mk_item(maker, big, "x.bin", size=5000)
    await _mk_item(maker, small, "y.bin", size=100)

    body = (await client.get("/api/v1/stats/libraries")).json()
    names = [x["name"] for x in body["libraries"]]
    assert names == ["Big", "Small", "Empty"]  # bytes desc, then name
    by_name = {x["name"]: x for x in body["libraries"]}
    assert by_name["Small"]["is_agent"] is True
    assert by_name["Big"]["is_agent"] is False
    assert by_name["Empty"]["file_count"] == 0
    assert by_name["Empty"]["total_bytes"] == 0
    assert body["total_files"] == 2 and body["total_bytes"] == 5100
