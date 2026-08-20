"""Light per-library hash refresh (xxhash-bump migration path).

Covers the three pieces that make a future digest-changing hash bump a button
instead of a full rescan:

* ``worker.rehash_library_now`` — recomputes quick/mid/content hashes and
  re-stamps ``policy_version`` for OLD-scheme rows only, never touching
  ``metadata_`` and never deferring extract work (only an index sync for the
  changed ids);
* ``GET /libraries/hash-status`` — the per-library staleness counts behind the
  Libraries-page prompt;
* ``POST /libraries/{id}/rehash`` — the trigger (agent-owned refused).
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, Item, Library
from filearr.provenance import _SCHEME
from filearr.tasks.extract import full_hash, mid_hash, quick_hash

BACKEND_DIR = Path(__file__).resolve().parents[1]

OLD = "cfg0:deadbeefdeadbeef"  # a scheme no live row ever carried — visibly stale


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _mk(s, lib_id, path: Path, *, size: int, pv: str | None, **extra):
    path.write_bytes(bytes((i * 7 + 1) & 0xFF for i in range(size)))  # noqa: ASYNC240
    item = Item(
        library_id=lib_id,
        file_category="other",
        file_group="other",
        status="active",
        path=str(path),
        rel_path=path.name,
        filename=path.name,
        extension="bin",
        size=size,
        mtime=datetime.now(UTC),
        policy_version=pv,
        **extra,
    )
    s.add(item)
    await s.flush()
    return item.id


async def test_rehash_refreshes_only_stale_rows_and_never_extract(
    maker, monkeypatch, tmp_path
):
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "hash_backfill_rate_mbps", 0.0)
    synced: list[str] = []

    async def _record_sync(ids):
        synced.extend(ids)

    monkeypatch.setattr(worker_mod, "defer_index_sync", _record_sync)

    big = 200_000
    meta = {"title": "keep me", "exif": {"camera": "X"}}
    async with maker() as s:
        lib = Library(name="loc", root_path=str(tmp_path), hash_policy="full")
        s.add(lib)
        await s.flush()
        stale = await _mk(
            s, lib.id, tmp_path / "stale.bin", size=big, pv=OLD,
            quick_hash="oldq", content_hash="oldc" * 4, metadata_=meta,
        )
        small = await _mk(s, lib.id, tmp_path / "small.bin", size=1000, pv=OLD, quick_hash="oldq")
        current = await _mk(
            s, lib.id, tmp_path / "cur.bin", size=1000,
            pv=f"{_SCHEME}:0123456789abcdef", quick_hash="curq",
        )
        unextracted = await _mk(s, lib.id, tmp_path / "new.bin", size=1000, pv=None)
        await s.commit()

    out = await worker_mod.rehash_library_now(str(lib.id))
    assert out["status"] == "done"
    assert out["rehashed"] == 2 and out["failed"] == 0 and out["content_dropped"] == 0

    async with maker() as s:
        rows = {r.id: r for r in (await s.execute(select(Item))).scalars()}
    st = rows[stale]
    assert st.quick_hash == quick_hash(str(tmp_path / "stale.bin"), big)
    assert st.mid_hash == mid_hash(str(tmp_path / "stale.bin"), big)
    assert st.content_hash == full_hash(str(tmp_path / "stale.bin"), big)
    assert st.policy_version.startswith(f"{_SCHEME}:")
    # LIGHT contract: extracted metadata is untouched byte-for-byte.
    assert st.metadata_ == meta
    sm = rows[small]
    assert sm.quick_hash == quick_hash(str(tmp_path / "small.bin"), 1000)
    assert sm.content_hash == full_hash(str(tmp_path / "small.bin"), 1000)  # small band
    # Current-scheme and never-extracted rows are NOT this migration's concern.
    assert rows[current].quick_hash == "curq"
    assert rows[unextracted].quick_hash is None and rows[unextracted].policy_version is None
    # Exactly the changed rows were synced to the index; nothing else deferred.
    assert set(synced) == {str(stale), str(small)}


async def test_rehash_quick_only_drops_unrecomputable_content_hash(
    maker, monkeypatch, tmp_path
):
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "hash_backfill_rate_mbps", 0.0)

    async def _noop(ids):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)

    big = 200_000
    async with maker() as s:
        lib = Library(name="net", root_path=str(tmp_path), hash_policy="quick_only")
        s.add(lib)
        await s.flush()
        item = await _mk(
            s, lib.id, tmp_path / "n.bin", size=big, pv=OLD,
            quick_hash="oldq", content_hash="0123456789abcdef",
        )
        await s.commit()

    out = await worker_mod.rehash_library_now(str(lib.id))
    assert out["rehashed"] == 1 and out["content_dropped"] == 1
    async with maker() as s:
        row = (await s.execute(select(Item).where(Item.id == item))).scalar_one()
    # An old-scheme content hash the current policy will not recompute must not
    # survive next to a current-scheme stamp — it would masquerade as comparable.
    assert row.content_hash is None
    assert row.quick_hash == quick_hash(str(tmp_path / "n.bin"), big)
    assert row.mid_hash == mid_hash(str(tmp_path / "n.bin"), big)


async def test_rehash_crosses_commit_boundary(maker, monkeypatch, tmp_path):
    """Regression: rehash_library_now commits every 200 rows. It must NOT do so
    inside a live server-side cursor (that invalidates the cursor and the job
    dies at row ~201). 450 stale rows force three commit boundaries; all must be
    rehashed and re-stamped."""
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "hash_backfill_rate_mbps", 0.0)

    async def _noop(ids):
        return None

    monkeypatch.setattr(worker_mod, "defer_index_sync", _noop)

    n = 450
    async with maker() as s:
        lib = Library(name="big", root_path=str(tmp_path), hash_policy="full")
        s.add(lib)
        await s.flush()
        for i in range(n):
            await _mk(s, lib.id, tmp_path / f"f{i}.bin", size=100 + i, pv=OLD, quick_hash="old")
        await s.commit()
        lib_id = lib.id

    out = await worker_mod.rehash_library_now(str(lib_id))
    assert out["rehashed"] == n and out["failed"] == 0
    async with maker() as s:
        stale = (
            await s.execute(
                select(func.count()).select_from(Item).where(
                    Item.library_id == lib_id, ~Item.policy_version.like(f"{_SCHEME}:%")
                )
            )
        ).scalar_one()
    assert stale == 0


async def test_rehash_refuses_agent_owned_library(maker, monkeypatch):
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint="FPX")
        s.add(agent)
        await s.flush()
        lib = Library(
            name="agentlib", root_path="/aroot",
            source_agent_id=agent.id, agent_library_ref="/aroot",
        )
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    out = await worker_mod.rehash_library_now(str(lib_id))
    assert out == {"status": "skipped", "reason": "agent_owned", "rehashed": 0}


def test_rehash_task_is_age_net_exempt():
    from filearr import worker

    assert "filearr.worker.rehash_library" in worker.AGE_NET_EXEMPT_TASKS


# --------------------------------------------------------------------------- #
# API surface                                                                  #
# --------------------------------------------------------------------------- #
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


async def test_hash_status_counts_stale_per_library(api, tmp_path):
    c, maker = api
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint="FPY")
        s.add(agent)
        await s.flush()
        stale_lib = Library(name="loc", root_path=str(tmp_path))
        clean_lib = Library(name="clean", root_path=str(tmp_path))
        agent_lib = Library(
            name="agentlib", root_path="/aroot",
            source_agent_id=agent.id, agent_library_ref="/aroot",
        )
        s.add_all([stale_lib, clean_lib, agent_lib])
        await s.flush()
        await _mk(s, stale_lib.id, tmp_path / "a.bin", size=10, pv=OLD)
        await _mk(s, stale_lib.id, tmp_path / "b.bin", size=10, pv=OLD)
        await _mk(s, clean_lib.id, tmp_path / "c.bin", size=10,
                  pv=f"{_SCHEME}:0123456789abcdef")
        await _mk(s, clean_lib.id, tmp_path / "d.bin", size=10, pv=None)
        await s.commit()
        ids = {"stale": str(stale_lib.id), "clean": str(clean_lib.id),
               "agent": str(agent_lib.id)}

    r = await c.get("/api/v1/libraries/hash-status")
    assert r.status_code == 200
    rows = {row["library_id"]: row for row in r.json()}
    assert rows[ids["stale"]]["stale"] == 2
    assert rows[ids["stale"]]["rehash_pending"] is False
    assert rows[ids["stale"]]["hash_scheme"] == _SCHEME
    assert rows[ids["clean"]]["stale"] == 0
    assert rows[ids["agent"]]["stale"] == 0
    assert rows[ids["agent"]]["agent_owned"] is True


async def test_trigger_rehash_endpoint(api, tmp_path, monkeypatch):
    import uuid as uuid_mod

    from filearr.api import libraries as libraries_api

    c, maker = api
    deferred: list[str] = []

    async def _fake_defer(library_id: str):
        deferred.append(library_id)
        return 42

    monkeypatch.setattr(libraries_api, "defer_rehash_library", _fake_defer)
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint="FPZ")
        s.add(agent)
        await s.flush()
        lib = Library(name="loc", root_path=str(tmp_path))
        agent_lib = Library(
            name="agentlib", root_path="/aroot",
            source_agent_id=agent.id, agent_library_ref="/aroot",
        )
        s.add_all([lib, agent_lib])
        await s.commit()
        lib_id, agent_lib_id = str(lib.id), str(agent_lib.id)

    r = await c.post(f"/api/v1/libraries/{lib_id}/rehash")
    assert r.status_code == 202 and r.json()["job_id"] == 42
    assert deferred == [lib_id]
    # Agent-owned: central cannot open its files — refuse, point at the agent sweep.
    r = await c.post(f"/api/v1/libraries/{agent_lib_id}/rehash")
    assert r.status_code == 422
    assert "agent" in r.json()["detail"]
    r = await c.post(f"/api/v1/libraries/{uuid_mod.uuid4()}/rehash")
    assert r.status_code == 404
