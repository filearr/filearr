"""Global maintenance mode (2026-08-09).

A single operator-controlled switch that suspends all *regular* background
processing so long-running operator work (pg_dump, VACUUM FULL, reindexing,
storage moves) runs against a quiet system:

* the scan scheduler stops deferring due scans (occurrences are NOT consumed —
  they fire, collapsed to the latest, when the mode lifts),
* watch-mode triggers stop deferring,
* the editable maintenance tick stops deferring purge/reconcile tasks,
* report-export scheduling stops,
* manual scan triggers are refused with 409,
* distributed agents are told to back off: the command-poll response carries an
  ``X-Filearr-Maintenance: 1`` header (new agents pause their replication push
  proactively) and the replication-batch endpoint answers 503 + ``Retry-After``
  (old agents back off through their existing flush-failure backoff). Agents
  keep scanning and collecting inventory locally — their outbox simply
  accumulates until the mode lifts (the block-don't-drop invariant).

What deliberately KEEPS running: the safety reapers (stalled-job reaper,
command TTL sweep, staging cleanup, export reconciler) and the alert pump —
suspending crash-consistency machinery during exactly the window an operator
is restarting things would be self-defeating. Already-queued jobs drain
normally; maintenance mode stops new work *generation*, it does not pause the
worker's consumers.

State is one Postgres row (``maintenance_mode`` id=1, ``taxonomy_state``
singleton pattern) so it survives restarts and is visible to app + worker
processes alike. All readers are guarded by a table-existence check so a
pre-migration deploy window never breaks a scheduler tick.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.models import MaintenanceMode

log = logging.getLogger(__name__)

# Suggested client back-off while maintenance is active (seconds); rides the
# 503 Retry-After header on the replication endpoint.
RETRY_AFTER_SECONDS = 300


async def _table_exists(session: AsyncSession) -> bool:
    row = await session.execute(text("SELECT to_regclass('maintenance_mode')"))
    return row.scalar() is not None


async def get_state(session: AsyncSession) -> dict[str, Any]:
    """The current maintenance-mode state (safe pre-migration: inactive)."""
    if not await _table_exists(session):
        return {"active": False, "reason": None, "started_at": None}
    row = await session.get(MaintenanceMode, 1)
    if row is None:
        return {"active": False, "reason": None, "started_at": None}
    return {
        "active": bool(row.active),
        "reason": row.reason,
        "started_at": row.started_at.isoformat() if row.started_at else None,
    }


async def is_active(session: AsyncSession) -> bool:
    return (await get_state(session))["active"]


async def is_active_standalone() -> bool:
    """Scheduler-side gate: open a short-lived session (the periodic ticks run
    outside any request session). Fails OPEN (False) on any error — a broken
    maintenance check must never wedge normal scheduling."""
    from filearr.db import SessionLocal

    try:
        async with SessionLocal() as session:
            return await is_active(session)
    except Exception:  # pragma: no cover - defensive
        log.exception("maintenance-mode check failed; treating as inactive")
        return False


async def set_state(
    session: AsyncSession, *, active: bool, reason: str | None = None
) -> dict[str, Any]:
    """Flip the mode (idempotent). ``reason`` is stored only while activating;
    deactivating clears it. ``started_at`` stamps the activation edge."""
    row = await session.get(MaintenanceMode, 1)
    if row is None:
        row = MaintenanceMode(id=1, active=False)
        session.add(row)
    was_active = bool(row.active)
    row.active = active
    if active:
        row.reason = reason
        if not was_active:
            row.started_at = datetime.now(UTC)
    else:
        row.reason = None
        row.started_at = None
    await session.commit()
    log.info(
        "maintenance mode %s%s",
        "ACTIVATED" if active else "deactivated",
        f" ({reason})" if active and reason else "",
    )
    return await get_state(session)
