"""Per-library extraction-limit overrides (2026-08-21).

Library.extract_overrides (allow-listed JSONB) overlays the global Settings
for that library's extract jobs: API validation, the read-side cleaner, and
the contextvar overlay the extractor wrappers consume via effective_settings.
"""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.config import clean_extract_overrides, get_settings

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# read-side cleaner (pure unit)                                                #
# --------------------------------------------------------------------------- #
def test_clean_extract_overrides_allowlist_and_types():
    raw = {
        "extract_timeout_seconds": 600,
        "doc_decompressed_max": 6_442_450_944.0,  # float in JSONB -> int field
        "ffprobe_timeout_s": 45,  # int -> float field
        "doc_decompression_ratio": 250.5,
        "not_a_setting": 123,  # unknown key dropped
        "model3d_max_bytes": -1,  # non-positive dropped
        "email_max_bytes": True,  # bool is an int — rejected
        "document_max_bytes": "big",  # non-numeric dropped
    }
    out = clean_extract_overrides(raw)
    assert out == {
        "extract_timeout_seconds": 600,
        "doc_decompressed_max": 6_442_450_944,
        "ffprobe_timeout_s": 45.0,
        "doc_decompression_ratio": 250.5,
    }
    assert isinstance(out["doc_decompressed_max"], int)
    assert isinstance(out["ffprobe_timeout_s"], float)
    assert clean_extract_overrides(None) == {}
    assert clean_extract_overrides("garbage") == {}


# --------------------------------------------------------------------------- #
# API + overlay                                                                #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def wired(pg_uri, monkeypatch):
    from filearr import db as db_mod
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield {"client": c, "maker": maker}
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_api_roundtrip_and_validation(wired):
    c = wired["client"]
    r = await c.post("/api/v1/libraries", json={
        "name": "3dprint", "root_path": "/d",
        "extract_overrides": {"doc_decompressed_max": 6_442_450_944,
                              "extract_timeout_seconds": 900},
    })
    assert r.status_code == 201, r.text
    lib = r.json()
    assert lib["extract_overrides"] == {
        "doc_decompressed_max": 6_442_450_944, "extract_timeout_seconds": 900,
    }

    # unknown key and non-positive value are 422s
    r = await c.patch(f"/api/v1/libraries/{lib['id']}",
                      json={"extract_overrides": {"evil": 1}})
    assert r.status_code == 422
    r = await c.patch(f"/api/v1/libraries/{lib['id']}",
                      json={"extract_overrides": {"extract_timeout_seconds": 0}})
    assert r.status_code == 422

    # patch replaces; explicit null clears back to global settings
    r = await c.patch(f"/api/v1/libraries/{lib['id']}",
                      json={"extract_overrides": {"model3d_max_bytes": 1_073_741_824}})
    assert r.status_code == 200
    assert r.json()["extract_overrides"] == {"model3d_max_bytes": 1_073_741_824}
    r = await c.patch(f"/api/v1/libraries/{lib['id']}",
                      json={"extract_overrides": None})
    assert r.status_code == 200
    assert r.json()["extract_overrides"] is None


async def test_overlay_reaches_effective_settings(wired, monkeypatch):
    from filearr.models import Item, Library
    from filearr.tasks import extract as ex

    maker = wired["maker"]
    # extract.py binds SessionLocal at import time, so in a full-suite run it
    # still points at the real (unreachable) DB — repoint it at the test maker.
    monkeypatch.setattr(ex, "SessionLocal", maker)
    async with maker() as s:
        lib = Library(
            name="tuned", root_path="/d",
            extract_overrides={"extract_timeout_seconds": 777,
                               "doc_decompressed_max": 6_442_450_944},
        )
        s.add(lib)
        await s.commit()
        item = Item(
            library_id=lib.id, file_category="document", file_group="doc",
            status="active", path="/d/a.pdf", rel_path="a.pdf", filename="a.pdf",
            extension="pdf", size=1, mtime=datetime.now(UTC), metadata_={},
        )
        s.add(item)
        await s.commit()
        item_id = str(item.id)

    base = get_settings()
    overlaid, token = await ex._overlay_for_item(item_id, base)
    try:
        assert overlaid.extract_timeout_seconds == 777
        assert overlaid.doc_decompressed_max == 6_442_450_944
        # untouched fields fall through to the global value
        assert overlaid.document_max_bytes == base.document_max_bytes
        # the extractor wrappers see the SAME overlay via the contextvar
        assert ex.effective_settings() is overlaid
    finally:
        assert token is not None
        ex._settings_override.reset(token)
    # after reset the global settings are back
    assert ex.effective_settings() is get_settings()

    # an item whose library has no overrides: no overlay, no token
    async with maker() as s:
        lib2 = Library(name="plain", root_path="/e")
        s.add(lib2)
        await s.commit()
        item2 = Item(
            library_id=lib2.id, file_category="document", file_group="doc",
            status="active", path="/e/b.pdf", rel_path="b.pdf", filename="b.pdf",
            extension="pdf", size=1, mtime=datetime.now(UTC), metadata_={},
        )
        s.add(item2)
        await s.commit()
        item2_id = str(item2.id)
    same, token2 = await ex._overlay_for_item(item2_id, base)
    assert same is base and token2 is None
