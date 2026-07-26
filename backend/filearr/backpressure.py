"""Roadmap §17 — adaptive extract backpressure (worker-local).

The static knobs (T8) split queues and give extraction a negative priority so
it never *preempts* scan-control — but a worker with ``--concurrency 4`` still
happily runs 4 extract jobs while the host is already saturated by a walking
scan, an OCR burst, or a neighbouring container. This module adds a
load-aware ceiling INSIDE the worker process:

  * A cheap, TTL-sampled pressure gauge (1-minute loadavg per core — the same
    signal an operator eyeballs) drives a two-state trip with hysteresis:
    pressure >= ``extract_backpressure_high_load`` trips the limiter;
    it recovers only once pressure falls to ``extract_backpressure_low_load``
    (no flapping around a single threshold).
  * While tripped, at most ``extract_backpressure_min_concurrency`` extract
    jobs run concurrently in this process. Jobs beyond the ceiling are NOT
    held on a slot (that would starve the scan/index/maintenance queues the
    worker also serves) — they are *rescheduled* a short jittered delay into
    the future via the existing attempt-agnostic staged-extract machinery, so
    worker slots stay free for higher-priority work and the queue drains
    itself when pressure subsides.

Honest limits, by design:
  * The gauge is per-PROCESS state; a scaled-out second worker trips on its
    own readings (each protects its own host share). There is no cross-worker
    coordination and none is needed — the signal is host load.
  * On hosts without ``os.getloadavg`` (Windows dev) the limiter never trips
    and everything runs exactly as before.
  * State transitions are logged (INFO) rather than dashboarded: the API
    process's limiter is idle by construction, so surfacing this in
    ``/system/jobs-summary`` would show the wrong process's numbers.
"""

from __future__ import annotations

import logging
import os
import time

log = logging.getLogger(__name__)


class ExtractLimiter:
    """Trip-with-hysteresis concurrency ceiling for the extract task."""

    def __init__(self) -> None:
        self._in_flight = 0
        self._tripped = False
        self._last_sample = 0.0
        self._load_ratio: float | None = None
        self._throttled_total = 0

    def _sample(self, settings) -> None:
        now = time.monotonic()
        if now - self._last_sample < settings.extract_backpressure_sample_seconds:
            return
        self._last_sample = now
        try:
            load1 = os.getloadavg()[0]
        except (AttributeError, OSError):
            # No loadavg on this platform — the limiter stays permanently open.
            self._load_ratio = None
            self._tripped = False
            return
        ratio = load1 / (os.cpu_count() or 1)
        self._load_ratio = round(ratio, 3)
        if self._tripped:
            if ratio <= settings.extract_backpressure_low_load:
                self._tripped = False
                log.info(
                    "extract backpressure: recovered (load/core %.2f <= %.2f); "
                    "%d jobs were rescheduled while tripped",
                    ratio,
                    settings.extract_backpressure_low_load,
                    self._throttled_total,
                )
        elif ratio >= settings.extract_backpressure_high_load:
            self._tripped = True
            log.info(
                "extract backpressure: tripped (load/core %.2f >= %.2f); "
                "extract concurrency capped at %d in this worker",
                ratio,
                settings.extract_backpressure_high_load,
                settings.extract_backpressure_min_concurrency,
            )

    def try_acquire(self, settings) -> bool:
        """Take an extract slot, or refuse (caller reschedules the job)."""
        if not settings.extract_backpressure:
            self._in_flight += 1
            return True
        self._sample(settings)
        if self._tripped and self._in_flight >= max(
            1, settings.extract_backpressure_min_concurrency
        ):
            self._throttled_total += 1
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    def snapshot(self) -> dict:
        return {
            "tripped": self._tripped,
            "in_flight": self._in_flight,
            "throttled_total": self._throttled_total,
            "load_per_core": self._load_ratio,
        }


extract_limiter = ExtractLimiter()
