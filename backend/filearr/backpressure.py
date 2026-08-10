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
  * The trip no longer slams the ceiling to the floor: it is an AIMD control
    loop (see "the control loop" below) whose ceiling floats between
    ``extract_backpressure_min_concurrency`` and
    ``extract_backpressure_max_concurrency`` (auto: ``worker_concurrency``).
  * Jobs beyond the ceiling are NOT held on a slot (that would starve the
    scan/index/maintenance queues the worker also serves) — they are
    *rescheduled* a short jittered delay into the future via the existing
    attempt-agnostic staged-extract machinery, so worker slots stay free for
    higher-priority work and the queue drains itself when pressure subsides.

The apparent contradiction in the roadmap, and how it is resolved
-----------------------------------------------------------------
§17 says two things that read as opposites, and BOTH notes are current — do
not "fix" one as stale:

  * the deferred bullet: *"keep queue depth as the primary signal"*;
  * the SHIPPED 2026-07-24 note: *"queue depth remains untouched as a signal
    (throttling extract on depth would deepen the very queue) — host load IS
    the 'don't starve the API' signal"*.

They are each right about a different DIRECTION of control, which is exactly
what a one-signal design cannot express:

  * **Host load drives contraction.** Depth must never cause throttling: the
    only way a deep extract queue gets shorter is by running extract jobs, so
    throttling on depth is self-defeating (it deepens the queue it reacts to).
    The 2026-07-24 note is right that the "don't starve the API/scan" signal
    is host pressure, never backlog.
  * **Queue depth drives expansion.** A deep queue on an *idle* host is
    precisely the state where the ceiling should RISE — extra concurrency
    there is free throughput, and it is the only evidence that raising the
    ceiling would be used at all (expanding into an empty queue just churns
    the controller). That is the deferred bullet's point: depth is the
    primary signal for the direction it can actually inform.

So: depth is read (cheaply, bounded — see ``probe_extract_backlog``) and it
only ever pushes the ceiling UP; pressure only ever pushes it DOWN.

The control loop (AIMD, once per sample)
----------------------------------------
  * **Multiplicative decrease** — while pressure is at/above the high water
    mark, ceiling ``*= extract_backpressure_decrease_factor`` (floor:
    ``min_concurrency``). Halving rather than jumping to the floor means a
    brief spike costs one step of throughput, not the whole recovery window,
    while *sustained* pressure still reaches the floor in 2-3 samples
    (4 -> 2 -> 1 = 30 s at the default 15 s cadence).
  * **Additive increase** — one slot per sample, and only when the limiter is
    untripped, pressure is at/below the LOW water mark (not merely below the
    trip point — expanding inside the hysteresis band is how a controller
    oscillates), and the bounded depth probe says there is a backlog waiting.
  * **Anti-thrash** — the sample TTL is itself the minimum dwell between
    same-direction adjustments (at most one move per sample, by construction,
    which is why no separate dwell knob exists to be mistuned), plus
    ``extract_backpressure_expand_cooldown_seconds`` (default 60 s) during
    which no expansion may follow a contraction. 60 s is not arbitrary: the
    input is the *1-minute* loadavg, an exponentially weighted average that
    lags reality by roughly that window, so expanding sooner means reacting
    to a number that has not yet finished reflecting the last contraction.

Invariant preserved: this controller only changes how many extract jobs THIS
worker runs at once. The negative extract queue priority is untouched, so
extraction still never preempts scan-control.

Honest limits, by design:
  * The gauge is per-PROCESS state; a scaled-out second worker trips on its
    own readings (each protects its own host share). There is no cross-worker
    coordination and none is needed — the signal is host load. Two workers on
    one host will each contract on the same shared loadavg, which is the
    conservative direction to be wrong in.
  * On hosts without ``os.getloadavg`` (Windows dev) the controller never
    activates at all: no ceiling, no depth probe, everything runs exactly as
    it did before §17.
  * The depth probe is asynchronous and therefore always one sample stale by
    construction: the reading a decision uses was taken at the previous
    sample. For a boolean-ish "is there a backlog" signal on a 15 s cadence
    that is irrelevant, and it keeps ``try_acquire`` synchronous (the extract
    task calls it before touching the event loop for any real work).
  * State transitions are logged (INFO with the reason and the inputs) rather
    than dashboarded, and the sample ring in ``snapshot()`` is process-local:
    the API process's limiter is idle by construction, so surfacing any of
    this in ``/system/jobs-summary`` would show the wrong process's numbers.
"""

from __future__ import annotations

import inspect
import logging
import os
import time
from collections import deque

log = logging.getLogger(__name__)

# The controller only ever asks "is there a backlog?", never "how deep?" — an
# extra queued job beyond this cap cannot change any decision it makes. The cap
# is what keeps the probe bounded work (see probe_extract_backlog): FIX-17 saw
# procrastinate_jobs reach 3.4M rows and drive procrastinate_fetch_job to ~56 s
# per call, so an unbounded COUNT(*) here would be a self-inflicted repeat of
# exactly that incident, fired every sample interval.
DEPTH_PROBE_LIMIT = 100

# Samples kept for after-the-fact explanation of a transition. 32 samples is
# ~8 minutes at the default 15 s cadence — long enough to cover a spike, its
# contraction, the 60 s cooldown and the re-expansion ramp, small enough that
# it is free to keep in every worker process forever.
HISTORY_SAMPLES = 32

# Used only when neither `extract_backpressure_max_concurrency` nor
# `worker_concurrency` yields a usable number (a hand-rolled Settings stub, or
# a nonsense 0/negative override). Matches the shipped worker_concurrency
# default so the fallback is never a surprise.
FALLBACK_MAX_CONCURRENCY = 4


def host_pressure() -> float | None:
    """1-minute loadavg per core, or None where the host has no loadavg.

    None is the "controller cannot run here" answer, not "no pressure": on
    Windows dev boxes ``os.getloadavg`` does not exist and the limiter must
    stay wide open rather than guess a ceiling from a signal it lacks.
    """
    try:
        load1 = os.getloadavg()[0]
    except (AttributeError, OSError):
        return None
    return load1 / (os.cpu_count() or 1)


async def probe_extract_backlog(settings) -> int:
    """Bounded "is there an extract backlog?" reading, saturating at the cap.

    Deliberately NOT ``count(*)``: the FIX-17 incident (2026-07-26) showed this
    table at 3.4M rows on the live box. The count runs inside a LIMITed
    subquery so the answer saturates at ``DEPTH_PROBE_LIMIT`` and the scan
    stops there, and the ORDER BY is the key order of procrastinate's PARTIAL
    ``procrastinate_jobs_priority_idx_v1`` (``WHERE status = 'todo'``) so the
    planner walks that index — the millions of *succeeded* history rows that
    dominate the table are not in it and are never touched.

    Reuses the worker's existing session factory (``filearr.db.SessionLocal``,
    the same pool every worker task already reads through — see
    ``queue_stats``/``tasks.reconcile`` for the identical pattern); it never
    opens a pool of its own. Imported lazily so importing this module stays
    free for processes that never run a worker.
    """
    from sqlalchemy import text

    from filearr.db import SessionLocal

    async with SessionLocal() as session:
        exists = (
            await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
        ).scalar()
        if exists is None:
            return 0  # queue-only/fresh DB: no schema, no backlog
        n = (
            await session.execute(
                text(
                    "SELECT count(*) FROM ("
                    "  SELECT 1 FROM procrastinate_jobs"
                    "  WHERE status = 'todo' AND queue_name = :q"
                    "  ORDER BY priority DESC, id ASC"
                    "  LIMIT :cap"
                    ") AS bounded"
                ),
                {"q": settings.queue_extract, "cap": DEPTH_PROBE_LIMIT},
            )
        ).scalar()
        return int(n or 0)


class ExtractLimiter:
    """AIMD concurrency ceiling for the extract task (see module docstring).

    ``pressure_source`` and ``depth_probe`` are injectable so the controller is
    testable without a real loadavg or a real database. ``depth_probe`` may be
    sync (tests) or async (the shipped DB probe); an async probe is run as a
    background task and its result lands on the NEXT sample.
    """

    def __init__(self, pressure_source=None, depth_probe=None) -> None:
        self._in_flight = 0
        self._tripped = False
        self._last_sample = 0.0
        self._load_ratio: float | None = None
        self._throttled_total = 0
        self._pressure_source = pressure_source or host_pressure
        self._depth_probe = depth_probe or probe_extract_backlog
        # None = controller inactive (disabled, or no loadavg on this host):
        # no ceiling at all, which is the pre-§17 behaviour we must preserve.
        self._ceiling: int | None = None
        self._depth: int | None = None
        self._last_contract = 0.0
        self._last_expand = 0.0
        self._contractions_total = 0
        self._expansions_total = 0
        self._probe_in_flight = False
        self._probe_task = None
        self._history: deque[dict] = deque(maxlen=HISTORY_SAMPLES)

    # -- ceiling bounds ----------------------------------------------------
    def _floor(self, settings) -> int:
        return max(1, settings.extract_backpressure_min_concurrency)

    def _max_ceiling(self, settings) -> int:
        """Upper bound for the ceiling.

        Defaults (0 = auto) to the worker's OWN concurrency: this process can
        never run more extract jobs than it has slots, so anything higher is a
        ceiling that can never bind. ``worker_concurrency`` is the same setting
        that backs the ``FILEARR_WORKER_CONCURRENCY`` env the compose worker
        command turns into ``--concurrency``, which makes it the closest thing
        to a discoverable value we have (procrastinate does not expose the
        running worker's concurrency to task code). An operator who passes
        ``--concurrency`` on the CLI without setting that env should set
        ``FILEARR_EXTRACT_BACKPRESSURE_MAX_CONCURRENCY`` explicitly.
        """
        configured = getattr(settings, "extract_backpressure_max_concurrency", 0) or 0
        if configured <= 0:
            configured = getattr(settings, "worker_concurrency", 0) or 0
        if configured <= 0:
            configured = FALLBACK_MAX_CONCURRENCY
        return max(self._floor(settings), int(configured))

    # -- depth probe -------------------------------------------------------
    def _kick_depth_probe(self, settings) -> None:
        """Refresh the backlog reading at most once per sample, off the path.

        Only fired when an expansion is actually plausible (untripped, low
        pressure, ceiling below max) — a controller that cannot move needs no
        input, and this keeps the query off the DB entirely on the hosts where
        the ceiling sits at max all day.
        """
        if self._probe_in_flight:
            return
        try:
            result = self._depth_probe(settings)
        except Exception:  # a broken probe must never break extraction
            log.debug("extract backpressure: depth probe failed", exc_info=True)
            return
        if not inspect.isawaitable(result):
            self._depth = None if result is None else int(result)
            return
        import asyncio

        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # No loop (sync caller): close the coroutine so Python does not warn
            # about it never being awaited, and skip this sample's reading.
            result.close()
            return
        self._probe_in_flight = True
        # A strong reference is kept because the event loop only holds a weak
        # one — an unreferenced task can be garbage-collected mid-flight.
        self._probe_task = loop.create_task(self._await_depth(result))

    async def _await_depth(self, awaitable) -> None:
        try:
            value = await awaitable
            self._depth = None if value is None else int(value)
        except Exception:
            # Fail CLOSED for expansion (depth unknown -> no expansion), never
            # for extraction itself: a DB hiccup must not stall the queue.
            self._depth = None
            log.debug("extract backpressure: depth probe failed", exc_info=True)
        finally:
            self._probe_in_flight = False

    # -- control loop ------------------------------------------------------
    def _sample(self, settings) -> None:
        now = time.monotonic()
        if now - self._last_sample < settings.extract_backpressure_sample_seconds:
            return
        self._last_sample = now

        pressure = self._pressure_source()
        if pressure is None:
            # No loadavg on this platform — the controller never activates and
            # the limiter stays permanently open (pre-§17 behaviour).
            self._load_ratio = None
            self._tripped = False
            self._ceiling = None
            return

        self._load_ratio = round(pressure, 3)
        high = settings.extract_backpressure_high_load
        low = settings.extract_backpressure_low_load
        ceiling_max = self._max_ceiling(settings)
        floor = self._floor(settings)
        if self._ceiling is None:
            # First real reading: start wide open at the max and let pressure
            # take slots away, rather than ramping up from the floor and paying
            # an additive-increase warm-up on every worker restart.
            self._ceiling = ceiling_max
        else:
            # Re-clamp in case the bounds were re-read from changed settings.
            self._ceiling = min(max(self._ceiling, floor), ceiling_max)

        if self._tripped:
            if pressure <= low:
                self._tripped = False
                log.info(
                    "extract backpressure: recovered (load/core %.2f <= %.2f); "
                    "ceiling %d/%d, %d jobs were rescheduled while tripped",
                    pressure,
                    low,
                    self._ceiling,
                    ceiling_max,
                    self._throttled_total,
                )
        elif pressure >= high:
            self._tripped = True
            log.info(
                "extract backpressure: tripped (load/core %.2f >= %.2f); "
                "contracting extract concurrency in this worker",
                pressure,
                high,
            )

        action = None
        if pressure >= high:
            # MULTIPLICATIVE DECREASE. Gated on the pressure still being at the
            # high water mark, not merely on being tripped: inside the
            # hysteresis band the host is already recovering and taking more
            # slots away there is how a controller ratchets itself shut.
            action = self._contract(settings, pressure, now, floor)
        elif (
            not self._tripped
            and pressure <= low
            and self._ceiling < ceiling_max
        ):
            action = self._expand(settings, pressure, now, ceiling_max)
            # Refresh the backlog reading for the NEXT sample's decision (see
            # the one-sample-stale note in the module docstring).
            self._kick_depth_probe(settings)

        self._history.append(
            {
                "at": round(time.time(), 3),
                "pressure": self._load_ratio,
                "depth": self._depth,
                "ceiling": self._ceiling,
                "in_flight": self._in_flight,
                "tripped": self._tripped,
                "action": action,
            }
        )

    def _contract(self, settings, pressure, now, floor) -> str | None:
        if self._ceiling <= floor:
            return None
        # The sample TTL is the dwell time for same-direction moves: at most one
        # contraction per sample, so a burst of acquires inside one interval
        # cannot collapse the ceiling.
        if now - self._last_contract < settings.extract_backpressure_sample_seconds:
            return None
        factor = settings.extract_backpressure_decrease_factor
        target = max(floor, int(self._ceiling * factor))
        if target >= self._ceiling:
            return None
        log.info(
            "extract backpressure: contracting %d -> %d (load/core %.2f >= %.2f, "
            "in flight %d)",
            self._ceiling,
            target,
            pressure,
            settings.extract_backpressure_high_load,
            self._in_flight,
        )
        self._ceiling = target
        self._last_contract = now
        self._contractions_total += 1
        return "contract"

    def _expand(self, settings, pressure, now, ceiling_max) -> str | None:
        # No expansion in the shadow of a contraction: the 1-minute loadavg has
        # not finished reflecting it yet (see the cooldown rationale above).
        if now - self._last_contract < settings.extract_backpressure_expand_cooldown_seconds:
            return None
        if now - self._last_expand < settings.extract_backpressure_sample_seconds:
            return None  # one additive step per sample, at most
        if not self._depth:
            # Depth 0 or unknown: nothing is waiting (or we cannot tell), so a
            # bigger ceiling would buy nothing. Expanding on pressure alone
            # would churn the controller against an empty queue.
            return None
        target = min(ceiling_max, self._ceiling + 1)
        if target <= self._ceiling:
            return None
        log.info(
            "extract backpressure: expanding %d -> %d (load/core %.2f <= %.2f, "
            "backlog >=%d waiting, in flight %d)",
            self._ceiling,
            target,
            pressure,
            settings.extract_backpressure_low_load,
            self._depth,
            self._in_flight,
        )
        self._ceiling = target
        self._last_expand = now
        self._expansions_total += 1
        return "expand"

    # -- slots -------------------------------------------------------------
    def try_acquire(self, settings) -> bool:
        """Take an extract slot, or refuse (caller reschedules the job)."""
        if not settings.extract_backpressure:
            self._in_flight += 1
            return True
        self._sample(settings)
        if self._ceiling is not None and self._in_flight >= self._ceiling:
            self._throttled_total += 1
            return False
        self._in_flight += 1
        return True

    def release(self) -> None:
        self._in_flight = max(0, self._in_flight - 1)

    def snapshot(self) -> dict:
        """Process-local controller state. NOT for an API response — the API
        process's limiter is idle by construction (see the module docstring);
        this exists for tests and for a worker-side debug dump."""
        return {
            "tripped": self._tripped,
            "in_flight": self._in_flight,
            "throttled_total": self._throttled_total,
            "load_per_core": self._load_ratio,
            "ceiling": self._ceiling,
            "depth": self._depth,
            "contractions_total": self._contractions_total,
            "expansions_total": self._expansions_total,
            "history": list(self._history),
        }


extract_limiter = ExtractLimiter()
