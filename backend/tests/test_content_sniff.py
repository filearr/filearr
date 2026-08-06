"""Content-sniffing for extensionless files (roadmap §4, 2026-08-06).

Covers: candidate selection (extensionless, category 'other', non-sidecar,
central-only, not already sniffed), reclassification through the live-taxonomy
group rollup, the sniffed-stamp idempotence, the enabled gate on the worker
task, and the changed-id re-projection contract. Real Postgres; libmagic is
ALWAYS monkeypatched (an incompatible libmagic DLL on a dev host can
hard-crash the process — real-magic behavior belongs to the Linux image).

Mirrors the test_ops_t4_reclassify harness.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.config import get_settings
from filearr.models import Item, Library
from filearr.tasks import sniff as sniff_mod
from filearr.tasks.sniff import SNIFFED_KEY, resolve_group, sniff_extensionless

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent

# A real minimal PDF header is enough for libmagic to say application/pdf.
PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n<< >>\n%%EOF\n"
PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n" + b"\x00\x00\x00\rIHDR" + b"\x00\x00\x00\x01\x00\x00\x00\x01"
    + b"\x08\x02\x00\x00\x00" + b"\x90wS\xde" + b"\x00" * 32
)


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "content_sniff_enabled", True)
    # NEVER import real python-magic in the suite: an incompatible libmagic DLL
    # on the host crashes the process at import (ctypes load), uncatchably.
    monkeypatch.setattr(sniff_mod, "_magic_available", lambda: True)
    yield maker, settings
    await engine.dispose()


async def _mk_lib(maker) -> uuid.UUID:
    async with maker() as s:
        lib = Library(name="L", root_path="/data/l")
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(
    maker,
    lib_id,
    rel_path,
    path,
    *,
    extension=None,
    category="other",
    group="other",
    sidecar_of=None,
    source_agent_id=None,
    metadata=None,
):
    async with maker() as s:
        item = Item(
            library_id=lib_id,
            file_category=category,
            file_group=group,
            status="active",
            path=path,
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension=extension,
            size=100,
            mtime=datetime.now(UTC),
            metadata_=metadata or {},
            user_metadata={},
            external_ids={},
            tags=[],
            sidecar_of=sidecar_of,
            source_agent_id=source_agent_id,
        )
        s.add(item)
        await s.commit()
        return item.id


def _fake_magic(mapping: dict[bytes, str]):
    def sniffer(head: bytes) -> str | None:
        for prefix, mime in mapping.items():
            if head.startswith(prefix):
                return mime
        return "application/octet-stream"

    return sniffer


def test_resolve_group_vocabulary():
    assert resolve_group("video/x-matroska") == "video"
    assert resolve_group("application/pdf; charset=binary") == "pdf"
    assert resolve_group("image/x-canon-cr2") == "raster-photo"  # prefix fallback
    assert resolve_group("application/octet-stream") is None
    assert resolve_group("text/x-unknown-thing") is None  # no text/* fallback
    assert resolve_group("") is None


async def test_sniff_reclassifies_and_stamps(db, tmp_path, monkeypatch):
    maker, _ = db
    lib = await _mk_lib(maker)
    pdf = tmp_path / "no_ext_pdf"
    pdf.write_bytes(PDF_BYTES)
    png = tmp_path / "no_ext_png"
    png.write_bytes(PNG_BYTES)
    blob = tmp_path / "no_ext_blob"
    blob.write_bytes(b"\x00\x01\x02\x03 nothing recognizable")
    pdf_id = await _mk_item(maker, lib, "docs/no_ext_pdf", str(pdf))
    png_id = await _mk_item(maker, lib, "pics/no_ext_png", str(png))
    blob_id = await _mk_item(maker, lib, "misc/no_ext_blob", str(blob))

    monkeypatch.setattr(
        sniff_mod,
        "_sniff_bytes",
        _fake_magic({b"%PDF": "application/pdf", b"\x89PNG": "image/png"}),
    )

    async with maker() as s:
        stats = await sniff_extensionless(s)
    assert stats["sniffed"] == 3
    assert stats["reclassified"] == 2
    assert stats["unmapped"] == 1
    assert stats["remaining"] == 0
    assert set(stats["changed_ids"]) == {str(pdf_id), str(png_id)}

    async with maker() as s:
        p = await s.get(Item, pdf_id)
        assert (p.file_category, p.file_group) == ("document", "pdf")
        assert p.metadata_[SNIFFED_KEY].startswith("application/pdf")
        g = await s.get(Item, png_id)
        assert (g.file_category, g.file_group) == ("image", "raster-photo")
        b = await s.get(Item, blob_id)
        assert (b.file_category, b.file_group) == ("other", "other")
        assert SNIFFED_KEY in b.metadata_  # stamped even when unmapped

    # idempotent: a second pass finds nothing new
    async with maker() as s:
        again = await sniff_extensionless(s)
    assert again["candidates"] == 0 and again["changed_ids"] == []


async def test_candidate_selection_excludes(db, tmp_path, monkeypatch):
    maker, _ = db
    lib = await _mk_lib(maker)
    f = tmp_path / "candidate"
    f.write_bytes(PDF_BYTES)
    target = await _mk_item(maker, lib, "a/candidate", str(f))
    # excluded: has an extension
    await _mk_item(maker, lib, "a/has_ext.bin", str(f), extension="bin")
    # excluded: already classified
    await _mk_item(maker, lib, "a/classified", str(f), category="video", group="video")
    # excluded: sidecar
    await _mk_item(maker, lib, "a/side", str(f), sidecar_of=target)
    # excluded: agent-owned
    agent_id = uuid.uuid4()
    await _mk_item(maker, lib, "a/agent_file", str(f), source_agent_id=agent_id)
    # excluded: already sniffed
    await _mk_item(maker, lib, "a/sniffed", str(f), metadata={SNIFFED_KEY: "x"})

    monkeypatch.setattr(
        sniff_mod, "_sniff_bytes", _fake_magic({b"%PDF": "application/pdf"})
    )

    async with maker() as s:
        stats = await sniff_extensionless(s)
    assert stats["candidates"] == 1
    assert stats["changed_ids"] == [str(target)]


async def test_unreadable_path_left_unstamped_for_retry(db, tmp_path, monkeypatch):
    maker, _ = db
    lib = await _mk_lib(maker)
    gone = await _mk_item(maker, lib, "a/gone", str(tmp_path / "does-not-exist"))
    monkeypatch.setattr(sniff_mod, "_sniff_bytes", _fake_magic({}))
    async with maker() as s:
        stats = await sniff_extensionless(s)
    assert stats["read_errors"] == 1 and stats["sniffed"] == 0
    async with maker() as s:
        row = await s.get(Item, gone)
        assert SNIFFED_KEY not in row.metadata_  # retried on the next run


async def test_worker_task_gates_on_setting(db, monkeypatch):
    maker, settings = db
    monkeypatch.setattr(settings, "content_sniff_enabled", False)
    from filearr import worker as worker_mod

    result = await worker_mod.content_sniff()
    assert "skipped" in result


async def test_worker_task_defers_for_changed(db, tmp_path, monkeypatch):
    maker, _ = db
    from filearr import db as db_mod
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(worker_mod, "SessionLocal", maker)
    synced: list[list[str]] = []
    extracted: list[list[str]] = []

    async def _fake_sync(ids):
        synced.append(list(ids))

    async def _fake_extract(ids):
        extracted.append(list(ids))

    monkeypatch.setattr(worker_mod, "defer_index_sync", _fake_sync)
    monkeypatch.setattr(worker_mod, "defer_extract", _fake_extract)

    lib = await _mk_lib(maker)
    f = tmp_path / "vid"
    f.write_bytes(b"\x1aE\xdf\xa3 fake mkv")
    vid = await _mk_item(maker, lib, "v/vid", str(f))
    monkeypatch.setattr(
        sniff_mod, "_sniff_bytes", _fake_magic({b"\x1aE\xdf\xa3": "video/x-matroska"})
    )

    result = await worker_mod.content_sniff()
    assert result["reclassified"] >= 1
    assert result["reprojected"] == len(sum(synced, []))
    assert [str(vid)] in synced and [str(vid)] in extracted
