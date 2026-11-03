"""Process-level stale-while-revalidate cache for slow dashboard probes.

Why this exists (live 2026-10-29..11-02, ~1.1M items): ``/stats`` computed the
semantic coverage counts and the disk headroom INLINE on every request. The
semantic count hit its 5 s statement timeout 50 times in four days (47 in one
hour of an operator clicking around the console), and statvfs on an rclone SMB
mount regularly overran what was left of the shared request budget. Neither
number needs to be per-request fresh — "1,084,212 embedded as of 3 minutes ago"
is exactly as actionable as the live value — so the request path now only READS
the last computed value and a single background task recomputes it.

Contract:
  * :meth:`SWRCache.get` never blocks longer than ``wait_s`` (default 0). It
    returns the last good value (possibly stale) plus metadata, and kicks a
    refresh when the value is missing or older than ``ttl_s``.
  * At most ONE refresh is in flight per cache (per process). A refresh that is
    wedged in the kernel (statvfs on a dead mount) therefore pins one thread,
    not one thread per dashboard poll.
  * A failed refresh keeps the last good value, records a sanitized error, and
    logs ONE warning per failure streak (the next success re-arms it). Retries
    wait ``retry_after_s`` so a query that always times out is not re-run on
    every request.
  * ``key`` invalidates: when the caller's key differs from the one the cached
    value was computed under (e.g. the embedder fingerprint changed), the value
    is treated as absent.

Per process by design: each uvicorn worker keeps its own copy; the cost is one
background recompute per process per TTL."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Snapshot:
    value: Any  # None until the first successful compute
    computed_at: float | None  # wall-clock epoch seconds of the value
    stale: bool  # value exists but is older than the TTL (or a refresh failed)
    refreshing: bool  # a refresh is in flight right now
    error: str | None  # last refresh failure (cleared on success)

    @property
    def computed_at_iso(self) -> str | None:
        if self.computed_at is None:
            return None
        return datetime.fromtimestamp(self.computed_at, tz=UTC).isoformat()


class SWRCache:
    def __init__(
        self,
        name: str,
        compute: Callable[[], Awaitable[Any]],
        *,
        ttl_s: float,
        retry_after_s: float | None = None,
        slow_warn_s: float | None = None,
    ) -> None:
        self.name = name
        self._compute = compute
        self.ttl_s = ttl_s
        self.retry_after_s = ttl_s if retry_after_s is None else retry_after_s
        # A refresh still running after this long is logged once (it keeps
        # running — and keeps the single-flight slot — until it returns).
        self.slow_warn_s = slow_warn_s
        self.reset()

    def reset(self) -> None:
        """Drop the value and abandon any in-flight refresh (tests, settings change)."""
        task = getattr(self, "_task", None)
        if task is not None and not task.done():
            try:
                task.cancel()
            except RuntimeError:  # loop already closed (pytest loop per test)
                pass
        self._task: asyncio.Task | None = None
        self._value: Any = None
        self._key: Any = None
        self._computed_at: float | None = None  # wall clock, for display
        self._computed_mono: float | None = None  # monotonic, for TTL math
        self._failed_mono: float | None = None
        self._error: str | None = None
        self._warned = False

    # ------------------------------------------------------------- internals
    def _in_flight(self) -> bool:
        t = self._task
        if t is None or t.done():
            return False
        try:
            return t.get_loop() is asyncio.get_running_loop()
        except RuntimeError:
            return False

    def _is_fresh(self, key: Any) -> bool:
        return (
            self._computed_mono is not None
            and self._key == key
            and time.monotonic() - self._computed_mono < self.ttl_s
            and self._error is None
        )

    def _may_retry(self) -> bool:
        return self._failed_mono is None or (
            time.monotonic() - self._failed_mono >= self.retry_after_s
        )

    async def _run(self, key: Any) -> None:
        started = time.monotonic()
        try:
            work = asyncio.ensure_future(self._compute())
            if self.slow_warn_s:
                done, _ = await asyncio.wait({work}, timeout=self.slow_warn_s)
                if not done:
                    log.warning(
                        "%s: refresh still running after %gs (serving the last value)",
                        self.name, self.slow_warn_s,
                    )
            value = await work
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - keep the last good value
            from filearr import errors

            self._failed_mono = time.monotonic()
            self._error = errors.sanitize_error(exc)
            if not self._warned:
                self._warned = True
                log.warning(
                    "%s: background refresh failed after %.1fs (keeping the "
                    "last value; further failures are silent until a success): %s",
                    self.name, time.monotonic() - started, exc,
                )
            else:
                log.debug("%s: background refresh failed again: %s", self.name, exc)
            return
        self._value = value
        self._key = key
        self._computed_at = time.time()
        self._computed_mono = time.monotonic()
        self._failed_mono = None
        self._error = None
        self._warned = False

    def _snapshot(self, key: Any) -> Snapshot:
        have = self._computed_mono is not None and self._key == key
        return Snapshot(
            value=self._value if have else None,
            computed_at=self._computed_at if have else None,
            stale=have and not self._is_fresh(key),
            refreshing=self._in_flight(),
            error=self._error,
        )

    # ------------------------------------------------------------- public API
    def kick(self, key: Any = None) -> None:
        """Start a refresh if the value is missing/stale and none is in flight."""
        if self._in_flight() or self._is_fresh(key) or not self._may_retry():
            return
        self._task = asyncio.get_running_loop().create_task(
            self._run(key), name=f"swr-refresh:{self.name}"
        )

    async def get(self, key: Any = None, *, wait_s: float = 0.0) -> Snapshot:
        """The current snapshot; waits at most ``wait_s`` for a refresh it kicked."""
        self.kick(key)
        if wait_s > 0 and self._in_flight() and self._snapshot(key).value is None:
            assert self._task is not None
            # wait() never cancels the task on timeout — the refresh keeps
            # running in the background and fills the cache for the next call.
            await asyncio.wait({self._task}, timeout=wait_s)
        return self._snapshot(key)
