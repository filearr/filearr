"""Taxonomy group gating in the scan walk (W8-B successor to P2-T3).

The P2-T3 extension-group refinement (``presets.resolve_enabled_extensions`` +
``libraries.enabled_extension_groups``) was superseded by the taxonomy
``enabled_groups`` gate and deleted end to end in P7 (B6). What remains is the
DB-integration scan over a real on-disk document tree proving the walk narrows
to the enabled categories/groups (R5 union semantics).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, Library, ScanRun

from .conftest import psycopg3_uri

BACKEND_DIR = Path(__file__).resolve().parent.parent


# --- Integration: extension filter in the scan walk ------------------------


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


def _doc_tree(root: Path) -> None:
    for rel in ("a.pdf", "b.docx", "c.epub", "d.txt"):
        (root / rel).write_bytes(b"x")


async def _scan_rels(session, library) -> set[str]:
    """Run a real _scan_body with extract-defer/reindex stubbed; return the
    rel_paths of the items the scan persisted for this library."""
    from filearr.tasks import scan as scan_mod

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer, orig_reindex = scan_mod._defer_extract_batch, scan_mod._reindex_library
    scan_mod._defer_extract_batch = _noop_defer
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        await scan_mod._scan_body(session, library, run)
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex

    rows = (
        await session.execute(select(Item.rel_path).where(Item.library_id == library.id))
    ).scalars()
    return set(rows)


@pytest.mark.parametrize(
    ("categories", "groups", "expected"),
    [
        # W8-B taxonomy gating (the successor to the P2-T3 extension-group scan
        # refinement): a.pdf->pdf, b.docx->document-office, c.epub->ebook,
        # d.txt->document-text (all under file_category 'document').
        (["document"], [], {"a.pdf", "b.docx", "c.epub", "d.txt"}),   # whole category
        ([], ["document-office"], {"b.docx"}),                        # one group
        ([], ["document-office", "ebook"], {"b.docx", "c.epub"}),     # group OR (R5)
    ],
)
async def test_taxonomy_group_filter_in_scan(engine, tmp_path, categories, groups, expected):
    _doc_tree(tmp_path)
    Session = async_sessionmaker(engine, expire_on_commit=False)
    async with Session() as session:
        lib = Library(
            name=f"docs-{'-'.join(categories + groups) or 'all'}",
            root_path=str(tmp_path),
            enabled_categories=categories,
            enabled_groups=groups,
        )
        session.add(lib)
        await session.commit()
        rels = await _scan_rels(session, lib)
    assert rels == expected
