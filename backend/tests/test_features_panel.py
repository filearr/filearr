"""Optional-feature gate visibility (2026-08-09).

Operators ran the semantic/chunking on-demand maintenance tasks and each took
0 s because the underlying optional feature was off, with nothing in the UI
saying so (Procrastinate persists no task return value). Two surfaces cover
that: ``GET /system/features`` (every gate's live state) and a ``gate`` field
on the maintenance rows, present only while the gate is OFF.

Runs against the migrated pgserver Postgres (harness mirrors
test_maintenance_mode.py).
"""

from __future__ import annotations

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
from filearr.models import Library

BACKEND_DIR = Path(__file__).resolve().parent.parent

EXPECTED_KEYS = {
    "semantic",
    "chunking",
    "ocr",
    "content_sniff",
    "agents",
    "auth",
    "update_check_auto",
    "log_db",
    "thumbnail_budget",
}


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def settings(monkeypatch):
    """Pristine settings for each test — the module cache is process-wide."""
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)
    yield s
    get_settings.cache_clear()


@pytest.fixture
async def client(db_maker, settings, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _seed_library(maker, *, name: str, chunking: bool = False, ocr: bool = False):
    async with maker() as s:
        s.add(
            Library(
                name=name,
                root_path=f"/data/{name}",
                chunking_enabled=chunking,
                ocr_enabled=ocr,
            )
        )
        await s.commit()


async def _features(c) -> dict[str, dict]:
    r = await c.get("/api/v1/system/features")
    assert r.status_code == 200, r.text
    return {f["key"]: f for f in r.json()["features"]}


async def _tasks(c) -> dict[str, dict]:
    r = await c.get("/api/v1/system/maintenance")
    assert r.status_code == 200, r.text
    return {t["key"]: t for t in r.json()["tasks"]}


# --------------------------------------------------------------------------- #
# /system/features                                                             #
# --------------------------------------------------------------------------- #
async def test_features_defaults(client):
    """Every gate is reported, with the shipped defaults: the opt-in features
    off, auth on, and no library opting into chunking/OCR."""
    c, _ = client
    feats = await _features(c)
    assert set(feats) == EXPECTED_KEYS

    for key in ("semantic", "content_sniff", "agents"):
        assert feats[key]["enabled"] is False, key
        assert feats[key]["scope"] == "env"
        assert feats[key]["env"].startswith("FILEARR_")
    # auth_enabled is monkeypatched off for the test client, but the row must
    # still track the live setting rather than a hardcoded value.
    assert feats["auth"]["enabled"] is get_settings().auth_enabled
    assert feats["auth"]["env"] == "FILEARR_AUTH_ENABLED"

    for key in ("chunking", "ocr"):
        assert feats[key]["scope"] == "libraries"
        assert feats[key]["env"] is None
        assert feats[key]["enabled"] is False
        assert feats[key]["count"] == 0
        assert feats[key]["total"] == 0

    thumb = feats["thumbnail_budget"]
    assert thumb["value_gb"] == pytest.approx(get_settings().thumbnail_budget_gb)
    assert thumb["env"] == "FILEARR_THUMBNAIL_BUDGET_GB"

    # Every row is self-describing for the operator.
    assert all(f["detail"] and f["title"] for f in feats.values())


async def test_env_gate_follows_settings(client, settings, monkeypatch):
    c, _ = client
    monkeypatch.setattr(settings, "semantic_enabled", True)
    monkeypatch.setattr(settings, "content_sniff_enabled", True)
    feats = await _features(c)
    assert feats["semantic"]["enabled"] is True
    assert feats["content_sniff"]["enabled"] is True


async def test_library_gate_counts(client):
    """The per-library opt-ins report how many of how many libraries are on —
    and stay independent of each other."""
    c, maker = client
    await _seed_library(maker, name="docs", chunking=True)
    await _seed_library(maker, name="video")

    feats = await _features(c)
    assert feats["chunking"]["enabled"] is True
    assert feats["chunking"]["count"] == 1
    assert feats["chunking"]["total"] == 2
    assert feats["ocr"]["enabled"] is False
    assert feats["ocr"]["count"] == 0
    assert feats["ocr"]["total"] == 2


async def test_thumbnail_budget_reports_configured_gb(client, settings, monkeypatch):
    """The card reports the configured GB knob (the only budget source since
    the legacy bytes override was removed 2026-08-09)."""
    c, _ = client
    monkeypatch.setattr(settings, "thumbnail_budget_gb", 50.0)
    feats = await _features(c)
    assert feats["thumbnail_budget"]["value_gb"] == pytest.approx(50.0)


# --------------------------------------------------------------------------- #
# gate field on the maintenance rows                                           #
# --------------------------------------------------------------------------- #
async def test_maintenance_gate_present_while_feature_off(client):
    c, _ = client
    tasks = await _tasks(c)

    embed = tasks["embed_missing"]["gate"]
    assert embed["enabled"] is False
    assert "FILEARR_SEMANTIC_ENABLED" in embed["reason"]

    for key in ("chunk_missing", "rebuild_chunks_index"):
        assert tasks[key]["gate"]["enabled"] is False
        assert "chunking" in tasks[key]["gate"]["reason"]

    sniff = tasks["content_sniff"]["gate"]
    assert "FILEARR_CONTENT_SNIFF_ENABLED" in sniff["reason"]

    # Ungated housekeeping never grows the field.
    assert "gate" not in tasks["purge_recycle_bin"]


async def test_maintenance_gate_absent_once_semantic_enabled(
    client, settings, monkeypatch
):
    c, _ = client
    monkeypatch.setattr(settings, "semantic_enabled", True)
    tasks = await _tasks(c)
    assert "gate" not in tasks["embed_missing"]
    # The unrelated chunking gate is untouched.
    assert tasks["chunk_missing"]["gate"]["enabled"] is False


async def test_maintenance_gate_absent_once_a_library_chunks(client):
    c, maker = client
    await _seed_library(maker, name="docs", chunking=True)
    tasks = await _tasks(c)
    assert "gate" not in tasks["chunk_missing"]
    assert "gate" not in tasks["rebuild_chunks_index"]
    # Semantic is still off, so its gate stays.
    assert tasks["embed_missing"]["gate"]["enabled"] is False
