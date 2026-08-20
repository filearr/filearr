"""Read-only Meilisearch health + drift snapshot for ``/api/stats`` (P9-T7/T8).

Mirrors the ``queue_stats`` pattern: a single cheap, total, read-only call the
stats endpoint can always make. Surfaces the same signal the hourly
reconciliation sweep acts on — Postgres active-item count vs Meili
``numberOfDocuments`` — so an operator sees live projection drift between sweeps
(the sweep itself runs in the worker process and only records its outcome to the
log; this recomputes the cheap compare on demand, which is process-independent
and needs no cross-process state or Postgres write).

Total by design: if Meili is unreachable the section degrades to
``healthy: false`` with null Meili fields rather than raising — ``/api/stats``
must never fail just because the disposable projection is down.
"""

from __future__ import annotations

import logging

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.models import Item, ItemStatus
from filearr.search import client

logger = logging.getLogger(__name__)


async def meili_snapshot(session: AsyncSession) -> dict:
    """Live Meili health + document count vs Postgres active-item count.

    Shape::

        {
          "healthy": true,
          "document_count": 1099,       # Meili numberOfDocuments (null if down)
          "is_indexing": false,
          "postgres_active": 1099,      # rows that SHOULD be indexed
          "drift": 0,                   # postgres_active - document_count (null if down)
          "in_sync": true               # |drift| == 0 (null if down)
        }
    """
    s = get_settings()
    postgres_active = (
        await session.execute(
            select(func.count()).select_from(Item).where(Item.status == ItemStatus.active)
        )
    ).scalar_one()

    healthy = False
    document_count: int | None = None
    is_indexing: bool | None = None
    failed_tasks: int | None = None  # last 24 h
    failed_tasks_total: int | None = None  # all Meili still remembers
    last_failed_task: dict | None = None
    try:
        async with client() as c:
            healthy = (await c.health()).status == "available"
            stats = await c.index(s.meili_index).get_stats()
            document_count = stats.number_of_documents
            is_indexing = stats.is_indexing
            # Meili-side task failures are otherwise INVISIBLE: document writes
            # are fire-and-forget (a failed task never fails a procrastinate
            # job). Live 2026-08-17: every scan batch had been failing for
            # weeks (userProvided embedder + docs without _vectors) and nothing
            # said so -- only the drift number hinted. Surface the newest
            # failure so the operator sees WHY, not just that drift exists.
            failed_tasks, failed_tasks_total, last_failed_task = await _recent_failed_tasks(
                c, s.meili_index
            )
    except Exception:  # noqa: BLE001 — stats must stay total even if Meili is down
        logger.warning("meili_snapshot: Meilisearch unreachable", exc_info=True)

    drift = None if document_count is None else postgres_active - document_count
    return {
        "healthy": healthy,
        "document_count": document_count,
        "is_indexing": is_indexing,
        "postgres_active": postgres_active,
        "drift": drift,
        "in_sync": None if drift is None else drift == 0,
        "failed_tasks": failed_tasks,
        "failed_tasks_total": failed_tasks_total,
        "last_failed_task": last_failed_task,
    }


def _is_benign_failed_task(t: dict) -> bool:
    """A self-healing task failure that must NOT alarm the operator.

    An ``indexDeletion`` that failed ``index_not_found`` is the shadow-swap
    rebuild's post-swap delete racing the stale-shadow reaper (or a retried
    rebuild) over the same ``<index>_rebuild_<epoch>`` shadow: whichever loses
    finds the index already gone — which is exactly the desired end state. We
    never delete the LIVE index, so any ``indexDeletion`` here is a shadow
    delete, and ``index_not_found`` on it is always benign. Surfacing it as
    "Latest Meili failure" (live report 2026-08-19) is a false alarm."""
    err = t.get("error") or {}
    return t.get("type") == "indexDeletion" and err.get("code") == "index_not_found"


async def _recent_failed_tasks(
    c, index_uid: str
) -> tuple[int | None, int | None, dict | None]:
    """``(failed_last_24h, failed_total, newest_failed)`` for ``index_uid`` from
    Meili's task list, with self-healing failures (see ``_is_benign_failed_task``)
    excluded so they never alarm the operator. Meili keeps failed tasks until
    they are deleted, so the total is HISTORY (270k on a box that hit the
    _vectors bug for a day, and still 270k a day after the fix) -- the 24 h count
    is the live signal, the total is what "Clear failed search-index tasks"
    removes. The SDK's ``get_tasks`` has no status/date filter, so this uses its
    raw HTTP layer. Best-effort: any error -> ``(None, None, None)``.

    Both counts and the surfaced newest are taken over a bounded recent window
    (``_WINDOW``) so a burst of benign shadow-delete races cannot mask a real
    failure NOR inflate the surfaced count; the raw Meili totals still back the
    "clear failed tasks" action via ``purge_failed_tasks``."""
    from datetime import UTC, datetime, timedelta

    _WINDOW = 50
    try:
        since = (datetime.now(UTC) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        recent = (
            await c._http_requests.get(  # noqa: SLF001 - SDK has no status filter
                f"tasks?statuses=failed&indexUids={index_uid}&limit={_WINDOW}"
                f"&afterFinishedAt={since}"
            )
        ).json()
        total = (
            await c._http_requests.get(  # noqa: SLF001
                f"tasks?statuses=failed&indexUids={index_uid}&limit={_WINDOW}"
            )
        ).json()
    except Exception:  # noqa: BLE001
        return None, None, None

    # 24 h count: Meili's raw total minus the benign entries seen in the window
    # (the window caps how many benign we can discount, which is the right bias —
    # never under-report a real backlog).
    recent_results = recent.get("results") or []
    recent_benign = sum(1 for t in recent_results if _is_benign_failed_task(t))
    recent_count = recent.get("total")
    if recent_count is not None:
        recent_count = max(0, recent_count - recent_benign)

    total_results = total.get("results") or []
    total_benign = sum(1 for t in total_results if _is_benign_failed_task(t))
    total_count = total.get("total")
    if total_count is not None:
        total_count = max(0, total_count - total_benign)

    newest = None
    for t in total_results:  # newest-first
        if _is_benign_failed_task(t):
            continue
        err = t.get("error") or {}
        newest = {
            "uid": t.get("uid"),
            "type": t.get("type"),
            "finished_at": t.get("finishedAt"),
            "code": err.get("code"),
            "message": (err.get("message") or "")[:500],
        }
        break
    return recent_count, total_count, newest
