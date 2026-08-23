"""Roadmap §27: filesystem identity capture on scan (hardlinks + symlinks).

Drives the real ``scan._scan_body`` over a tree containing a plain file, a
hardlink to it, and a symlink to it, and asserts:

  * every regular file records nlink/inode/dev from the walk's lstat;
  * the hardlinked pair shares one ``(dev, inode)`` and reports ``nlink == 2``,
    so a hardlink group is identifiable without a content hash;
  * the symlink is catalogued with ``symlink_target`` set and is NEVER queued
    for extraction (hashing would follow the link to the target's bytes).
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, Library, ScanRun

from .conftest import psycopg3_uri

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
async def engine(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    eng = create_async_engine(psycopg3_uri(pg_uri))
    async with eng.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    yield eng
    await eng.dispose()


async def _run_scan(session, library) -> list[str]:
    from filearr.tasks import scan as scan_mod

    captured: list[str] = []

    async def _capture(item_ids, scan_run_id=None):
        captured.extend(item_ids)

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer = scan_mod._defer_extract_batch
    orig_reindex = scan_mod._reindex_library
    scan_mod._defer_extract_batch = _capture
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex
    return captured


async def test_scan_captures_hardlinks_and_symlinks(engine, tmp_path):
    Session = async_sessionmaker(engine, expire_on_commit=False)

    root = tmp_path / "lib"
    root.mkdir()
    original = root / "original.bin"
    original.write_bytes(b"payload-bytes" * 100)
    hardlink = root / "hardlink.bin"
    os.link(original, hardlink)  # same inode, nlink becomes 2
    symlink = root / "shortcut.bin"
    os.symlink(original, symlink)  # symlink_target set, never extracted

    async with Session() as session:
        lib = Library(name="fsid", root_path=str(root))
        session.add(lib)
        await session.commit()
        lib_id = lib.id
        extract_ids = await _run_scan(session, lib)

    async with Session() as session:
        items = {
            i.filename: i
            for i in (
                await session.execute(select(Item).where(Item.library_id == lib_id))
            ).scalars()
        }

    assert set(items) == {"original.bin", "hardlink.bin", "shortcut.bin"}

    orig, hard, sym = items["original.bin"], items["hardlink.bin"], items["shortcut.bin"]

    # Hardlink group: identical (dev, inode), nlink >= 2, no content hash needed.
    assert orig.inode is not None and orig.dev is not None
    assert (orig.dev, orig.inode) == (hard.dev, hard.inode)
    assert orig.nlink == 2 and hard.nlink == 2
    assert orig.symlink_target is None and hard.symlink_target is None

    # Symlink: target captured, distinct inode from the file it points at, and
    # NEVER queued for extraction.
    assert sym.symlink_target == str(original)
    assert str(sym.id) not in extract_ids
    # The two real files WERE queued (they are regular, non-sidecar).
    assert str(orig.id) in extract_ids
    assert str(hard.id) in extract_ids


async def test_rescan_backfills_fs_identity(engine, tmp_path):
    """A row created before §27 (NULL identity) gets nlink/inode/dev filled in on
    the next scan even when size/mtime are unchanged."""
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "lib"
    root.mkdir()
    f = root / "a.bin"
    f.write_bytes(b"x" * 500)

    async with Session() as session:
        lib = Library(name="bf", root_path=str(root))
        session.add(lib)
        await session.commit()
        lib_id = lib.id
        await _run_scan(session, lib)
        # Simulate a pre-§27 row: clear the identity columns.
        await session.execute(
            text(
                "UPDATE items SET nlink=NULL, inode=NULL, dev=NULL, "
                "symlink_target=NULL, quick_hash='h-pre27' WHERE library_id=:l"
            ),
            {"l": lib_id},
        )
        await session.commit()
        # Rescan without touching the file (size/mtime unchanged -> else branch).
        requeued = await _run_scan(session, lib)

    async with Session() as session:
        row = (
            await session.execute(select(Item).where(Item.library_id == lib_id))
        ).scalar_one()
    assert row.inode is not None and row.dev is not None and row.nlink == 1
    # Regression (live 2026-08-22): the NULL->value nlink transition must be a
    # SILENT backfill — it used to satisfy the "changed" test and re-queue
    # extraction for the entire pre-upgrade catalog on the first scan.
    assert requeued == [], "pre-§27 backfill must not re-queue extraction"


async def test_hardlink_count_change_refreshes_identity_without_reextract(engine, tmp_path):
    """A hardlink created elsewhere bumps nlink on an untouched file: the row's
    identity is refreshed, but the bytes are unchanged so nothing is re-queued
    and the change is not counted as a modification."""
    Session = async_sessionmaker(engine, expire_on_commit=False)
    root = tmp_path / "lib"
    root.mkdir()
    f = root / "a.bin"
    f.write_bytes(b"x" * 500)

    async with Session() as session:
        lib = Library(name="hl", root_path=str(root))
        session.add(lib)
        await session.commit()
        lib_id = lib.id
        first = await _run_scan(session, lib)
        assert len(first) == 1  # the new file itself
        # Mark it extracted (the suite never runs the extract worker; a NULL
        # quick_hash would otherwise trip the legitimate never-extracted
        # self-heal and mask what this test is about).
        await session.execute(
            text("UPDATE items SET quick_hash='h-a' WHERE library_id=:l"), {"l": lib_id}
        )
        await session.commit()
        os.link(f, root / "a-link.bin")  # nlink 1 -> 2 on the original
        second = await _run_scan(session, lib)

    async with Session() as session:
        rows = (
            await session.execute(
                select(Item).where(Item.library_id == lib_id).order_by(Item.rel_path)
            )
        ).scalars().all()
    by_rel = {r.rel_path: r for r in rows}
    assert by_rel["a.bin"].nlink == 2 and by_rel["a-link.bin"].nlink == 2
    # Only the NEW link row is queued; the original is not re-extracted.
    assert second == [str(by_rel["a-link.bin"].id)]
