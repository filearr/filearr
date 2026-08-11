"""Semantic-search observability snapshot for ``/stats`` (P3-T8).

Cheap grouped counts over Postgres truth (``items.metadata`` JSONB): how many
active items carry a CURRENT-fingerprint vector, how many are still pending, and
how many carry a DRIFTED (old-model) vector that ``build_doc`` is omitting from
the projection. Read-only; degrades to zeros when semantic search is disabled."""

from __future__ import annotations

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.embed import FINGERPRINT_KEY, embedder_fingerprint
from filearr.models import Item, ItemStatus


async def semantic_snapshot(session: AsyncSession) -> dict:
    """Return ``{enabled, model, embedded_count, pending, fp_mismatches}``.

    * ``embedded_count`` — active items whose stored fingerprint matches the
      configured embedder (vector is live in the projection).
    * ``pending`` — active items with NO embedding fingerprint yet (never embedded
      / awaiting backfill).
    * ``fp_mismatches`` — active items whose stored fingerprint DIFFERS from the
      configured one (model changed → drift; vector omitted until re-embedded).

    When semantic search is disabled everything is zero (no scan is done)."""
    s = get_settings()
    if not s.semantic_enabled:
        return {
            "enabled": False,
            "model": s.embed_model,
            "embedded_count": 0,
            "pending": 0,
            "fp_mismatches": 0,
        }

    fp = embedder_fingerprint(s.embedder_config)
    has_fp = Item.metadata_.has_key(FINGERPRINT_KEY)
    fp_col = Item.metadata_[FINGERPRINT_KEY].astext
    active = Item.status == ItemStatus.active

    # Two queries rather than the one obvious three-way FILTER, because the
    # obvious one hung /stats on the live instance (2026-08-11): reading
    # ``metadata -> key`` forces Postgres to materialize the WHOLE metadata blob
    # for EVERY active row, and OCR/PDF-text extraction pushes many of those
    # blobs over the TOAST threshold. At ~1.09M items that is a million
    # de-TOASTs per dashboard load — minutes, not milliseconds.
    #
    # Three separate counts, and the separation is the whole point: the tests
    # have to sit in WHERE, not in an aggregate FILTER. A FILTER is evaluated
    # per row AFTER the scan, so ``count(*) FILTER (WHERE metadata ? key)``
    # re-reads every blob exactly like the version this replaces — only a WHERE
    # predicate can be answered from ix_items_metadata (GIN jsonb_ops, which
    # indexes ``?``). That is why the sibling extract-error count is fast.
    #
    #   * total  — no JSONB touched at all.
    #   * with_fp — bitmap index scan; still no VALUE read.
    #   * drift  — the only query that reads values, and the GIN predicate has
    #     already narrowed it to embedded rows (empty until something is
    #     embedded, never larger than the embedded corpus).
    # pending and embedded then fall out by subtraction instead of by scanning.
    async def _count(*where) -> int:
        return int((await session.execute(select(func.count()).where(*where))).scalar_one())

    total_active = await _count(active)
    with_fp = await _count(active, has_fp)
    mismatches = await _count(active, has_fp, fp_col != fp)
    pending = total_active - with_fp
    embedded = with_fp - mismatches
    return {
        "enabled": True,
        "model": s.embed_model,
        "embedded_count": embedded,
        "pending": pending,
        "fp_mismatches": mismatches,
    }
