"""Semantic-search observability snapshot for ``/stats`` (P3-T8).

Grouped counts over Postgres truth (``items.metadata`` JSONB): how many active
items carry a CURRENT-fingerprint vector, how many are still pending, and how
many carry a DRIFTED (old-model) vector that ``build_doc`` is omitting from the
projection. Read-only; zeros when semantic search is disabled.

Two layers since 2026-11-02:
  * :func:`semantic_counts` — the exact computation, on a caller's session.
  * :func:`semantic_snapshot` — what ``/stats`` serves: a process-level
    stale-while-revalidate cache (:mod:`filearr.swrcache`) refreshed in the
    background on its OWN session with a long statement timeout. The request
    path never runs the counts. Live evidence for why: 50 statement-timeout
    WARNINGs in four days on the ~1.1M-item instance (47 in one hour of console
    use), each of which also burned 5 s of the shared /stats budget and starved
    the disk section that runs after it."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.embed import FINGERPRINT_KEY, embedder_fingerprint
from filearr.swrcache import SWRCache

#: Recompute at most this often per process. Coverage moves at embed-queue speed
#: (lowest priority); five minutes is far finer than anyone acts on.
SEMANTIC_TTL_S = 300.0
#: The background refresh's own statement timeout. It is off the request path,
#: so it can afford to finish on a cold cache; the /stats 5 s bound no longer
#: applies to it.
SEMANTIC_STATEMENT_TIMEOUT_MS = 60_000

# The predicates are LITERAL SQL on purpose, and must stay textually equal to
# ix_items_embedding_fp's (alembic 9c3f1e7a2b58): a partial index is only usable
# when the planner can PROVE the query's WHERE implies the index's WHERE, and a
# bound ``metadata ? $1`` proves nothing under a generic plan (psycopg prepares
# a statement after 5 executions, and the dashboard repeats this one forever).
# Only the fingerprint value is a bind — it is the index KEY, which a generic
# plan can still use as an Index Cond.
if FINGERPRINT_KEY != "_embedding_fp":  # the literals below must track embed.py
    raise RuntimeError("embed_stats SQL literals are out of sync with FINGERPRINT_KEY")
_ACTIVE_SQL = text("SELECT count(*) FROM items WHERE status = 'active'")
_WITH_FP_SQL = text(
    "SELECT count(*) FROM items WHERE status = 'active' AND metadata ? '_embedding_fp'"
)
_CURRENT_SQL = text(
    "SELECT count(*) FROM items WHERE status = 'active' AND metadata ? '_embedding_fp'"
    " AND (metadata ->> '_embedding_fp') = :fp"
)


async def semantic_counts(session: AsyncSession) -> dict:
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

    # History. 2026-08-11: the one obvious three-way ``count(*) FILTER (...)``
    # hung /stats — reading ``metadata -> key`` materializes the WHOLE metadata
    # blob for every active row, and OCR/PDF text pushes many of them over the
    # TOAST threshold (a million de-TOASTs per dashboard load). That was split
    # into WHERE-clause counts answerable from the GIN index, which removed the
    # hang but not the cost: once most of the catalog is embedded, "GIN-narrowed"
    # is ~1M rows, the bitmap goes lossy and the recheck de-TOASTs anyway, and
    # the drift count read the fingerprint VALUE of every embedded row. Live
    # 2026-10/11: the with-fingerprint count hit the 5 s statement timeout 50x.
    #
    # Now (2026-11-02), with ix_items_embedding_fp — a narrow btree over
    # ``metadata ->> '_embedding_fp'`` restricted to active embedded rows:
    #   * total    — no JSONB touched at all (seq/bitmap on status).
    #   * with_fp  — index-only scan of the partial index (its predicate IS the
    #     query): ~17 ms for 190k matches on a 200k-row seeded table vs ~270 ms.
    #   * current  — Index Cond on the indexed expression = :fp; heap visits are
    #     visibility checks on the main tuple only, never a de-TOAST.
    #   * drift    = with_fp - current — no query reads a fingerprint value
    #     (the old ``<> fp`` scan was ~320 ms on the same table, index or not).
    # pending and embedded fall out by subtraction, as before. (A JSON-null
    # fingerprint now counts as drift rather than vanishing — it IS one.)
    async def _count(stmt, **params) -> int:
        return int((await session.execute(stmt, params)).scalar_one())

    total_active = await _count(_ACTIVE_SQL)
    with_fp = await _count(_WITH_FP_SQL)
    current = await _count(_CURRENT_SQL, fp=fp)
    return {
        "enabled": True,
        "model": s.embed_model,
        "embedded_count": current,
        "pending": total_active - with_fp,
        "fp_mismatches": with_fp - current,
    }


async def _refresh_counts() -> dict:
    """Background refresh: its own session + its own (long) statement timeout."""
    from filearr import db as _db  # late: tests swap SessionLocal

    async with _db.SessionLocal() as session:
        await session.execute(
            text("SELECT set_config('statement_timeout', :ms, true)"),
            {"ms": str(SEMANTIC_STATEMENT_TIMEOUT_MS)},
        )
        counts = await semantic_counts(session)
        await session.rollback()  # read-only; end the transaction explicitly
    return counts


_cache = SWRCache(
    "stats.semantic",
    _refresh_counts,
    ttl_s=SEMANTIC_TTL_S,
    slow_warn_s=SEMANTIC_STATEMENT_TIMEOUT_MS / 1000,
)


def reset_cache() -> None:
    _cache.reset()


async def semantic_snapshot() -> dict:
    """The ``/stats`` ``semantic`` section — never blocks on the counts.

    ``enabled``/``model`` are live config reads. The three counts come from the
    cache: the last computed values (``stale: true`` once older than the TTL or
    after a failed refresh), or ``null`` before the first refresh has finished
    (``pending_refresh: true`` while it runs — the UI renders "computing…",
    never 0). ``computed_at`` is when the counts were taken (ISO-8601 UTC)."""
    s = get_settings()
    if not s.semantic_enabled:
        return {
            "enabled": False,
            "model": s.embed_model,
            "embedded_count": 0,
            "pending": 0,
            "fp_mismatches": 0,
            "computed_at": None,
            "stale": False,
            "pending_refresh": False,
        }
    # Keyed by the fingerprint: an embedder change makes the old counts wrong
    # (every vector becomes drift), so they are dropped rather than served.
    snap = await _cache.get(key=embedder_fingerprint(s.embedder_config))
    counts = snap.value or {}
    out = {
        "enabled": True,
        "model": s.embed_model,
        "embedded_count": counts.get("embedded_count"),
        "pending": counts.get("pending"),
        "fp_mismatches": counts.get("fp_mismatches"),
        "computed_at": snap.computed_at_iso,
        "stale": snap.stale,
        "pending_refresh": snap.refreshing,
    }
    if snap.error:
        out["refresh_error"] = snap.error
    return out
