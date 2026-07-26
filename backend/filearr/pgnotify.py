"""Postgres LISTEN/NOTIFY hub for the scan SSE stream (roadmap §14).

The scan task publishes progress by committing ``ScanRun.stats`` every batch;
the SSE endpoint used to poll that row once per second per open stream. This
module turns that into push: the scan task executes ``pg_notify`` inside the
same transaction as each stats commit (so the poke is delivered exactly when
the new stats become visible), and the API process runs ONE dedicated listener
connection that fans notifications out to per-stream asyncio queues.

Design points:
  * **One listener connection per process**, not per stream — SSE streams keep
    using short-lived pooled sessions for row reads and merely *wait* on an
    in-process queue, so a hundred open streams still cost one LISTEN socket.
  * **Payload is just the scan id.** The listener pokes subscribers; the
    stream re-reads the authoritative row. This dodges the 8000-byte NOTIFY
    payload cap and any stale-payload hazard.
  * **Push is an optimization, never a correctness dependency.** Streams keep
    a slow fallback poll (``api/scans.py``), so a dropped listener connection
    or a NOTIFY sent before the subscription existed degrades to the old
    polling behaviour instead of a stalled stream. The listener itself
    reconnects with backoff.
  * **Test-friendly:** pytest gives every test a fresh event loop, so the hub
    resets its state whenever it notices the running loop changed.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging

import psycopg
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings

log = logging.getLogger(__name__)

SCAN_CHANNEL = "filearr_scan_progress"

# Per-subscriber queue depth. Entries are contentless pokes; one pending poke
# already means "re-read the row", so overflow (queue full) is safely dropped.
_QUEUE_SIZE = 4

_RECONNECT_DELAY_S = 5.0


async def notify_scan_progress(session: AsyncSession, scan_id) -> None:
    """Queue a progress poke on the CURRENT transaction of ``session``.

    Postgres delivers NOTIFY at commit time, so calling this just before
    ``session.commit()`` means listeners wake exactly when the new stats are
    visible — never early, never for a rolled-back write.
    """
    await session.execute(
        text("SELECT pg_notify(:channel, :payload)"),
        {"channel": SCAN_CHANNEL, "payload": str(scan_id)},
    )


def _listen_conninfo() -> str:
    """The plain-psycopg conninfo for the listener connection (the SQLAlchemy
    URL minus its ``+psycopg`` driver marker)."""
    return get_settings().database_url.replace("postgresql+psycopg://", "postgresql://", 1)


class ScanProgressHub:
    """Fan-out from one LISTEN connection to per-stream queues, keyed by scan id."""

    def __init__(self) -> None:
        self._subs: dict[str, set[asyncio.Queue[None]]] = {}
        self._listener: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None

    def _reset_if_foreign_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is not None and self._loop is not loop:
            # A previous loop (earlier test) owned the listener task; it died
            # with that loop. Start fresh on the current one.
            self._subs = {}
            self._listener = None
        self._loop = loop

    def subscribe(self, scan_id: str) -> asyncio.Queue[None]:
        self._reset_if_foreign_loop()
        queue: asyncio.Queue[None] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        self._subs.setdefault(scan_id, set()).add(queue)
        if self._listener is None or self._listener.done():
            self._listener = asyncio.create_task(self._listen_forever())
        return queue

    def unsubscribe(self, scan_id: str, queue: asyncio.Queue[None]) -> None:
        subs = self._subs.get(scan_id)
        if subs is not None:
            subs.discard(queue)
            if not subs:
                self._subs.pop(scan_id, None)
        if not self._subs and self._listener is not None:
            # Last stream closed: drop the LISTEN socket instead of holding a
            # DB connection open for nobody.
            self._listener.cancel()
            self._listener = None

    def _dispatch(self, scan_id: str) -> None:
        for queue in self._subs.get(scan_id, ()):  # copy-free; put_nowait never yields
            with contextlib.suppress(asyncio.QueueFull):
                queue.put_nowait(None)

    async def _listen_forever(self) -> None:
        while True:
            try:
                conn = await psycopg.AsyncConnection.connect(
                    _listen_conninfo(), autocommit=True
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 — degrade to fallback polling
                log.warning("scan SSE listener: connect failed (%s); retrying", exc)
                await asyncio.sleep(_RECONNECT_DELAY_S)
                continue
            try:
                await conn.execute(f'LISTEN "{SCAN_CHANNEL}"')
                async for notice in conn.notifies():
                    self._dispatch(notice.payload)
            except asyncio.CancelledError:
                await conn.close()
                raise
            except Exception as exc:  # noqa: BLE001
                log.warning("scan SSE listener: connection lost (%s); reconnecting", exc)
                with contextlib.suppress(Exception):
                    await conn.close()
                await asyncio.sleep(_RECONNECT_DELAY_S)


scan_hub = ScanProgressHub()
