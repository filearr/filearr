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
    failed_tasks: int | None = None
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
            failed_tasks, last_failed_task = await _recent_failed_tasks(c, s.meili_index)
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
        "last_failed_task": last_failed_task,
    }


async def _recent_failed_tasks(c, index_uid: str) -> tuple[int | None, dict | None]:
    """``(total_failed, newest_failed)`` for ``index_uid`` from Meili's task
    list (``GET /tasks?statuses=failed&indexUids=...&limit=1``, newest first).
    The SDK's ``get_tasks`` has no status filter, so this goes through its raw
    HTTP layer. Best-effort: any error -> ``(None, None)`` so the snapshot stays
    total. ``newest_failed`` is a small dict (uid, type, finished_at, code,
    message) -- message truncated for the UI."""
    try:
        resp = await c._http_requests.get(  # noqa: SLF001 - SDK has no status filter
            f"tasks?statuses=failed&indexUids={index_uid}&limit=1"
        )
        body = resp.json()
    except Exception:  # noqa: BLE001
        return None, None
    results = body.get("results") or []
    newest = None
    if results:
        t = results[0]
        err = t.get("error") or {}
        newest = {
            "uid": t.get("uid"),
            "type": t.get("type"),
            "finished_at": t.get("finishedAt"),
            "code": err.get("code"),
            "message": (err.get("message") or "")[:500],
        }
    return body.get("total"), newest
