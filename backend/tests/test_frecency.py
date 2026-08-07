"""Central frecency personal ranking (roadmap §5 P3, 2026-08-06).

Covers: the agent-mirrored scoring shape (bucketed recency weights), the
bounded page-local re-rank, the touch endpoint's recording + upsert +
opportunistic maintenance, per-owner isolation, the feature gate, and the
``/query/assist`` endpoint (which shares this file's app fixture). Real
Postgres via the migration chain (the item_frecency table is new at head).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import frecency
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, ItemFrecency, Library

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
        await conn.execute(text("DELETE FROM item_frecency"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "frecency_enabled", True)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_item(maker) -> uuid.UUID:
    async with maker() as s:
        lib = Library(name=f"L-{uuid.uuid4().hex[:6]}", root_path="/data/l")
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="other", file_group="other",
            status="active",
            path="/data/l/a.bin", rel_path="a.bin", filename="a.bin",
            extension="bin", size=1, mtime=datetime.now(UTC),
            metadata_={}, user_metadata={}, external_ids={}, tags=[],
        )
        s.add(item)
        await s.commit()
        return item.id


# --------------------------------------------------------------------------- #
# Scoring — must stay in lockstep with agent/internal/history                  #
# --------------------------------------------------------------------------- #
def test_recency_weights_mirror_agent():
    assert frecency.recency_weight(60) == 4.0          # < 1h
    assert frecency.recency_weight(7200) == 2.0        # < 1d
    assert frecency.recency_weight(3 * 86400) == 0.5   # < 1w
    assert frecency.recency_weight(30 * 86400) == 0.25 # older


def test_score_combines_rank_and_recency():
    now = datetime.now(UTC)
    fresh = frecency.score(3.0, now - timedelta(minutes=5), now)
    stale = frecency.score(3.0, now - timedelta(days=30), now)
    assert fresh == 12.0 and stale == 0.75


def test_lift_is_bounded_and_sublinear():
    assert frecency.lift(0) == 0
    assert frecency.lift(1) == 1
    assert frecency.lift(4) == 3
    assert frecency.lift(10_000) == frecency.MAX_LIFT


def test_rerank_lifts_within_page_stably():
    hits = [{"id": str(i)} for i in range(10)]
    # id 5 has one fresh use (score 4 -> lift 3): rises from index 5 to 2.
    out = frecency.rerank(hits, {"5": 4.0})
    assert [h["id"] for h in out[:4]] == ["0", "1", "5", "2"]
    # No scores -> identity.
    assert frecency.rerank(hits, {}) == hits


# --------------------------------------------------------------------------- #
# Recording + endpoint                                                        #
# --------------------------------------------------------------------------- #
async def test_touch_records_and_upserts(api):
    client, maker, _ = api
    item = await _mk_item(maker)
    for _ in range(3):
        r = await client.post(f"/api/v1/items/{item}/touch")
        assert r.status_code == 204
    async with maker() as s:
        row = (
            await s.execute(select(ItemFrecency).where(ItemFrecency.item_id == item))
        ).scalar_one()
        assert row.rank == 3.0
        assert row.owner == frecency.ANONYMOUS_OWNER  # auth off -> shared profile


async def test_touch_gate_disabled_writes_nothing(api):
    client, maker, settings = api
    item = await _mk_item(maker)
    settings.frecency_enabled = False
    try:
        r = await client.post(f"/api/v1/items/{item}/touch")
        assert r.status_code == 204  # still a friendly no-op
    finally:
        settings.frecency_enabled = True
    async with maker() as s:
        rows = (await s.execute(select(ItemFrecency))).scalars().all()
        assert rows == []


async def test_owner_isolation_and_scores(api):
    _, maker, _ = api
    item = await _mk_item(maker)
    async with maker() as s:
        await frecency.record_touch(s, "user-a", item)
        await frecency.record_touch(s, "user-a", item)
        await frecency.record_touch(s, "user-b", item)
        await s.commit()
    async with maker() as s:
        a = await frecency.scores_for(s, "user-a", [str(item)])
        b = await frecency.scores_for(s, "user-b", [str(item)])
        none = await frecency.scores_for(s, "user-c", [str(item)])
    assert a[str(item)] == 8.0   # rank 2 x fresh(4.0)
    assert b[str(item)] == 4.0
    assert none == {}


async def test_maintenance_halves_past_cap(api):
    _, maker, _ = api
    item = await _mk_item(maker)
    now = datetime.now(UTC)
    async with maker() as s:
        s.add(ItemFrecency(owner="hoarder", item_id=item, rank=20_000.0, last_used=now))
        await s.commit()
    async with maker() as s:
        await frecency.record_touch(s, "hoarder", item)
        await s.commit()
    async with maker() as s:
        row = (
            await s.execute(
                select(ItemFrecency).where(ItemFrecency.owner == "hoarder")
            )
        ).scalar_one()
        # 20000 + 1 = 20001 > cap -> halved once.
        assert row.rank == pytest.approx(10_000.5)


async def test_retention_prunes_stale_rows(api):
    _, maker, _ = api
    item = await _mk_item(maker)
    old_item = await _mk_item(maker)
    async with maker() as s:
        s.add(
            ItemFrecency(
                owner="u", item_id=old_item, rank=5.0,
                last_used=datetime.now(UTC) - timedelta(days=120),
            )
        )
        await s.commit()
    async with maker() as s:
        await frecency.record_touch(s, "u", item)
        await s.commit()
    async with maker() as s:
        owners_items = {
            str(r.item_id)
            for r in (
                await s.execute(select(ItemFrecency).where(ItemFrecency.owner == "u"))
            ).scalars()
        }
    assert owners_items == {str(item)}  # 120-day-old row pruned


def test_owner_from_actor():
    assert frecency.owner_from_actor("principal:abc-123") == "abc-123"
    assert frecency.owner_from_actor("fk_12345") == frecency.ANONYMOUS_OWNER
    assert frecency.owner_from_actor(None) == frecency.ANONYMOUS_OWNER


# --------------------------------------------------------------------------- #
# /query/assist endpoint (shares this fixture's app)                          #
# --------------------------------------------------------------------------- #
async def test_assist_endpoint_translates(api):
    client, _, _ = api
    r = await client.post(
        "/api/v1/query/assist", json={"text": "videos over 2 gb this week"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["dsl"] == "size:>2G modified:<7d kind:video"
    assert body["source"] == "heuristic"
    assert body["llm_available"] is False


async def test_assist_endpoint_validates_input(api):
    client, _, _ = api
    assert (await client.post("/api/v1/query/assist", json={"text": ""})).status_code == 422
    assert (
        await client.post("/api/v1/query/assist", json={"text": "x" * 501})
    ).status_code == 422
