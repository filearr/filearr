"""Roadmap §17 — adaptive extract backpressure.

The worker-local ExtractLimiter: loadavg-driven trip with hysteresis, an AIMD
concurrency ceiling (host pressure contracts, extract-queue depth expands),
and refused jobs rescheduled short+jittered via BackpressureReschedule (never
failed, never recorded as a job error).
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from filearr import backpressure
from filearr.backpressure import ExtractLimiter
from filearr.config import get_settings
from filearr.tasks.extract import (
    BackpressureReschedule,
    RescheduleExtract,
    StagedExtractRetry,
)


def _settings(**over):
    base = {
        "extract_backpressure": True,
        "extract_backpressure_min_concurrency": 1,
        "extract_backpressure_high_load": 0.85,
        "extract_backpressure_low_load": 0.60,
        "extract_backpressure_sample_seconds": 0.0,  # sample every acquire
        "extract_backpressure_max_concurrency": 4,
        "extract_backpressure_decrease_factor": 0.5,
        "extract_backpressure_expand_cooldown_seconds": 60.0,
        "worker_concurrency": 4,
        "queue_extract": "extract",
    }
    base.update(over)
    return SimpleNamespace(**base)


def _with_load(monkeypatch, load1: float, cores: int = 4):
    monkeypatch.setattr(
        backpressure.os, "getloadavg", lambda: (load1, 0.0, 0.0), raising=False
    )
    monkeypatch.setattr(backpressure.os, "cpu_count", lambda: cores)


def test_open_when_no_loadavg(monkeypatch):
    """Hosts without loadavg (Windows dev): the limiter never trips."""
    def _boom():
        raise AttributeError("no loadavg here")

    monkeypatch.setattr(backpressure.os, "getloadavg", _boom, raising=False)
    lim = ExtractLimiter()
    s = _settings()
    assert all(lim.try_acquire(s) for _ in range(50))
    assert lim.snapshot()["tripped"] is False


def test_disabled_never_limits(monkeypatch):
    _with_load(monkeypatch, load1=400.0)
    lim = ExtractLimiter()
    s = _settings(extract_backpressure=False)
    assert all(lim.try_acquire(s) for _ in range(50))


def test_trip_caps_concurrency_and_release_frees(monkeypatch):
    _with_load(monkeypatch, load1=4.0, cores=4)  # ratio 1.0 >= 0.85 -> trip
    lim = ExtractLimiter()
    s = _settings(extract_backpressure_min_concurrency=2)
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is False  # capped at 2 while tripped
    assert lim.snapshot()["throttled_total"] == 1
    lim.release()
    assert lim.try_acquire(s) is True  # a freed slot is reusable


def test_hysteresis_recovers_only_below_low_water(monkeypatch):
    lim = ExtractLimiter()
    s = _settings(extract_backpressure_min_concurrency=1)

    _with_load(monkeypatch, load1=4.0, cores=4)  # 1.0 -> trip
    assert lim.try_acquire(s) is True  # 4 -> 2 on the tripping sample
    assert lim.try_acquire(s) is False  # 2 -> 1 (sustained pressure)
    for _ in range(2):
        lim.release()

    # Between the thresholds (0.70): still tripped — no flapping. And no
    # further contraction either: inside the hysteresis band the host is
    # already recovering, so the ceiling holds where it is.
    _with_load(monkeypatch, load1=2.8, cores=4)
    lim._last_sample = 0.0
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is False
    for _ in range(2):
        lim.release()

    # Below the low water (0.5): the trip clears. Recovery of the CEILING is
    # deliberately gradual (AIMD) rather than an instant return to unlimited —
    # the post-contraction cooldown is still running here, so the floor holds.
    _with_load(monkeypatch, load1=2.0, cores=4)
    lim._last_sample = 0.0
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is False
    assert lim.snapshot()["tripped"] is False
    assert lim.snapshot()["ceiling"] == 1


# --------------------------------------------------------------------------
# The AIMD control loop. These drive the limiter through INJECTED pressure and
# depth sources plus a fake clock, so nothing here needs a real loadavg (the
# Windows dev box has none) or a real database.
# --------------------------------------------------------------------------


class _Clock:
    """Stand-in for the module's ``time``: dwell/cooldown windows are the
    behaviour under test, so they must be steppable rather than slept through."""

    def __init__(self) -> None:
        self.now = 1000.0

    def monotonic(self) -> float:
        return self.now

    def time(self) -> float:
        return 1_800_000_000.0 + self.now

    def advance(self, dt: float) -> None:
        self.now += dt


def _controlled(monkeypatch, pressure=0.10, depth=0, **over):
    """A limiter with injected pressure/depth and a fake clock."""
    clock = _Clock()
    monkeypatch.setattr(backpressure, "time", clock)
    box = {"pressure": pressure, "depth": depth, "probe_calls": 0}

    def _pressure():
        return box["pressure"]

    def _depth(settings):
        box["probe_calls"] += 1
        return box["depth"]

    lim = ExtractLimiter(pressure_source=_pressure, depth_probe=_depth)
    s = _settings(extract_backpressure_sample_seconds=15.0, **over)
    return lim, box, clock, s


def _tick(lim, s, clock, dt=15.0):
    """Advance one sampling interval and take/release a slot."""
    clock.advance(dt)
    ok = lim.try_acquire(s)
    if ok:
        lim.release()
    return ok


def test_contraction_is_multiplicative_not_a_jump_to_the_floor(monkeypatch):
    """A brief spike must cost ONE step of throughput, not the whole recovery
    window — that is the entire point of the multiplicative decrease."""
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, extract_backpressure_max_concurrency=8
    )
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 4  # 8 -> 4, not 8 -> 1
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 2
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 1  # sustained pressure reaches the floor
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 1  # and clamps there
    assert lim.snapshot()["contractions_total"] == 3


def test_floor_honours_min_concurrency(monkeypatch):
    lim, box, clock, s = _controlled(
        monkeypatch,
        pressure=2.0,
        extract_backpressure_max_concurrency=8,
        extract_backpressure_min_concurrency=3,
    )
    for _ in range(6):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 3


def test_dwell_blocks_a_second_contraction_inside_one_interval(monkeypatch):
    """Anti-thrash: the sample TTL is the dwell for same-direction moves, so a
    burst of acquires inside one interval cannot collapse the ceiling."""
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, extract_backpressure_max_concurrency=8
    )
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 4
    # Force a re-sample WITHOUT advancing the clock (as a burst of extract jobs
    # arriving in the same second would): the dwell must still hold.
    for _ in range(5):
        lim._last_sample = clock.now - 100.0
        assert lim.try_acquire(s) is True
        lim.release()
    assert lim.snapshot()["ceiling"] == 4
    assert lim.snapshot()["contractions_total"] == 1


def test_expansion_needs_backlog_and_low_pressure(monkeypatch):
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, extract_backpressure_max_concurrency=4
    )
    _tick(lim, s, clock)
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 1

    # Quiet host, but nothing waiting: expanding into an empty queue would just
    # churn the controller, so the ceiling holds.
    box["pressure"] = 0.10
    clock.advance(120.0)  # past the post-contraction cooldown
    for _ in range(4):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 1
    assert lim.snapshot()["expansions_total"] == 0

    # Backlog appears. The probe result lands on the NEXT sample by design
    # (async in production), so the first tick arms it and the second acts.
    box["depth"] = 40
    _tick(lim, s, clock)
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 2
    # Additive: one slot per sample, never a jump back to max.
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 3

    # Pressure back INSIDE the hysteresis band (above low, below high): no
    # expansion there either — that band is where a controller oscillates.
    box["pressure"] = 0.70
    for _ in range(3):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 3


def test_expansion_clamps_at_max(monkeypatch):
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, depth=40, extract_backpressure_max_concurrency=3
    )
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 1
    box["pressure"] = 0.05
    clock.advance(120.0)
    for _ in range(10):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 3  # never above the max
    assert lim.snapshot()["expansions_total"] == 2


def test_no_expansion_during_post_contraction_cooldown(monkeypatch):
    """The 1-minute loadavg lags by ~its own window; expanding before the
    cooldown elapses would react to a number that has not yet caught up."""
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, depth=40, extract_backpressure_max_concurrency=4
    )
    _tick(lim, s, clock)  # trip + contract 4 -> 2
    assert lim.snapshot()["ceiling"] == 2
    box["pressure"] = 0.05
    # Three ticks = 45 s < the 60 s cooldown: the depth probe keeps reading a
    # backlog, and the ceiling still must not move.
    for _ in range(3):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 2
    assert lim.snapshot()["expansions_total"] == 0
    _tick(lim, s, clock)  # now 60 s past the contraction
    assert lim.snapshot()["ceiling"] == 3


def test_depth_probe_runs_at_most_once_per_sample_interval(monkeypatch):
    """The probe is a DB read: it rides the sample TTL, never the acquire rate
    (FIX-17 — this table reached 3.4M rows on the live box)."""
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, depth=40, extract_backpressure_max_concurrency=4
    )
    _tick(lim, s, clock)  # contract; no probe while tripped
    assert box["probe_calls"] == 0
    box["pressure"] = 0.05
    clock.advance(15.0)
    for _ in range(20):  # a burst of extract jobs inside ONE interval
        if lim.try_acquire(s):
            lim.release()
    assert box["probe_calls"] == 1
    _tick(lim, s, clock)
    assert box["probe_calls"] == 2


def test_no_depth_probe_while_the_ceiling_is_already_at_max(monkeypatch):
    """A controller that cannot move up needs no input — the steady state on a
    healthy host costs zero queries."""
    lim, box, clock, s = _controlled(monkeypatch, pressure=0.05, depth=40)
    for _ in range(5):
        _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 4
    assert box["probe_calls"] == 0


def test_history_ring_explains_the_transitions(monkeypatch):
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=1.50, extract_backpressure_max_concurrency=4
    )
    _tick(lim, s, clock)  # contract 4 -> 2
    _tick(lim, s, clock)  # contract 2 -> 1
    box["pressure"] = 0.05
    box["depth"] = 7
    clock.advance(120.0)
    _tick(lim, s, clock)  # arms the depth probe
    _tick(lim, s, clock)  # expand 1 -> 2

    hist = lim.snapshot()["history"]
    assert [h["action"] for h in hist] == [
        "contract",
        "contract",
        None,
        "expand",
    ]
    assert set(hist[0]) == {
        "at",
        "pressure",
        "depth",
        "ceiling",
        "in_flight",
        "tripped",
        "action",
    }
    assert hist[0]["ceiling"] == 2 and hist[0]["tripped"] is True
    assert hist[-1]["ceiling"] == 2 and hist[-1]["tripped"] is False
    assert hist[-1]["depth"] == 7

    # The ring is bounded — a long-lived worker must not accumulate samples.
    for _ in range(backpressure.HISTORY_SAMPLES * 2):
        _tick(lim, s, clock)
    assert len(lim.snapshot()["history"]) == backpressure.HISTORY_SAMPLES


def test_snapshot_keeps_its_original_keys(monkeypatch):
    """The pre-existing fields stay — snapshot() is extended, not replaced."""
    lim, box, clock, s = _controlled(monkeypatch, pressure=0.05)
    _tick(lim, s, clock)
    snap = lim.snapshot()
    for key in ("tripped", "in_flight", "throttled_total", "load_per_core"):
        assert key in snap


def test_controller_never_activates_without_loadavg(monkeypatch):
    """No pressure signal (Windows dev) => no ceiling at all, and no depth
    probe either: the module is inert exactly as it was before the controller."""
    clock = _Clock()
    monkeypatch.setattr(backpressure, "time", clock)
    calls = []
    lim = ExtractLimiter(
        pressure_source=lambda: None,
        depth_probe=lambda s: calls.append(1) or 99,
    )
    s = _settings(extract_backpressure_sample_seconds=15.0)
    for _ in range(20):
        clock.advance(20.0)
        assert lim.try_acquire(s) is True  # no releases: still unlimited
    assert lim.snapshot()["ceiling"] is None
    assert calls == []


def test_disabled_skips_the_controller_entirely(monkeypatch):
    lim, box, clock, s = _controlled(
        monkeypatch, pressure=99.0, depth=40, extract_backpressure=False
    )
    for _ in range(20):
        clock.advance(20.0)
        assert lim.try_acquire(s) is True
    assert lim.snapshot()["ceiling"] is None
    assert lim.snapshot()["history"] == []
    assert box["probe_calls"] == 0


@pytest.mark.asyncio
async def test_async_depth_probe_lands_on_the_next_sample(monkeypatch):
    """Production's probe is a coroutine: it runs off the acquire path as a
    background task, so its reading is used by the FOLLOWING sample."""
    import asyncio

    clock = _Clock()
    monkeypatch.setattr(backpressure, "time", clock)
    calls = []

    async def _probe(settings):
        calls.append(1)
        return 12

    lim = ExtractLimiter(pressure_source=lambda: 1.5, depth_probe=_probe)
    s = _settings(
        extract_backpressure_sample_seconds=15.0,
        extract_backpressure_max_concurrency=4,
    )
    _tick(lim, s, clock)  # trip + contract 4 -> 2
    monkeypatch.setattr(lim, "_pressure_source", lambda: 0.05)
    clock.advance(120.0)
    _tick(lim, s, clock)  # schedules the probe
    await asyncio.sleep(0)  # let the background task run
    assert calls == [1]
    assert lim.snapshot()["depth"] == 12
    _tick(lim, s, clock)
    assert lim.snapshot()["ceiling"] == 3


@pytest.mark.asyncio
async def test_depth_probe_sql_is_bounded(monkeypatch):
    """FIX-17 forbids a naive COUNT(*) here: the count must run inside a
    LIMITed subquery so the reading saturates instead of scanning millions of
    terminal rows, and it must reuse the worker's own session factory."""
    import filearr.db as db_mod

    seen: list[tuple[str, dict | None]] = []

    class _Result:
        def __init__(self, value):
            self._value = value

        def scalar(self):
            return self._value

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt, params=None):
            sql = " ".join(str(stmt).split())
            seen.append((sql, params))
            if "to_regclass" in sql:
                return _Result("procrastinate_jobs")
            return _Result(backpressure.DEPTH_PROBE_LIMIT)

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Session())
    depth = await backpressure.probe_extract_backlog(_settings())
    assert depth == backpressure.DEPTH_PROBE_LIMIT

    sql, params = seen[-1]
    assert "LIMIT :cap" in sql
    assert params == {"q": "extract", "cap": backpressure.DEPTH_PROBE_LIMIT}
    assert "status = 'todo'" in sql  # the PARTIAL index's predicate
    assert "ORDER BY priority DESC, id ASC" in sql  # ... and its key order
    assert "SELECT count(*) FROM procrastinate_jobs" not in sql  # never naive


@pytest.mark.asyncio
async def test_depth_probe_is_zero_without_the_procrastinate_schema(monkeypatch):
    import filearr.db as db_mod

    class _Result:
        def scalar(self):
            return None

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def execute(self, stmt, params=None):
            return _Result()

    monkeypatch.setattr(db_mod, "SessionLocal", lambda: _Session())
    assert await backpressure.probe_extract_backlog(_settings()) == 0


def test_retry_decision_short_and_jittered():
    from datetime import UTC, datetime

    strategy = StagedExtractRetry()
    job = SimpleNamespace(attempts=0)
    seen = set()
    for _ in range(50):
        d = strategy.get_retry_decision(
            exception=BackpressureReschedule("x"), job=job
        )
        secs = round((d.retry_at - datetime.now(UTC)).total_seconds())
        assert BackpressureReschedule.RETRY_MIN_S - 2 <= secs
        assert secs <= BackpressureReschedule.RETRY_MAX_S + 2
        seen.add(secs)
    assert len(seen) > 1  # actually jittered

    # The plain staged gate keeps its long reschedule (not the short one).
    d = strategy.get_retry_decision(exception=RescheduleExtract("x"), job=job)
    long_secs = round((d.retry_at - datetime.now(UTC)).total_seconds())
    assert abs(long_secs - get_settings().extract_reschedule_seconds) <= 2


def test_backpressure_is_transient_for_job_errors():
    """§18 interop: a backpressure reschedule must never be recorded as a job
    failure (it inherits the staged gate's transient marker)."""
    assert BackpressureReschedule("x").filearr_transient is True


@pytest.mark.asyncio
async def test_extract_item_refused_slot_has_zero_side_effects(monkeypatch):
    """A refused job raises BEFORE any DB work (SessionLocal is never touched)
    and, having taken no slot, releases none."""
    import filearr.tasks.extract as extract_mod

    def _forbidden():
        raise AssertionError("DB touched by a refused extract job")

    monkeypatch.setattr(extract_mod, "SessionLocal", _forbidden)
    monkeypatch.setattr(
        extract_mod.extract_limiter, "try_acquire", lambda s: False
    )
    released = []
    monkeypatch.setattr(
        extract_mod.extract_limiter, "release", lambda: released.append(1)
    )
    with pytest.raises(BackpressureReschedule):
        await extract_mod.extract_item("00000000-0000-0000-0000-000000000000")
    assert released == []  # no slot taken, none released


@pytest.mark.asyncio
async def test_extract_item_releases_slot_on_body_exception(monkeypatch):
    """An acquired slot is released even when the body raises (e.g. the staged
    gate reschedule) — a leaked slot would ratchet the limiter shut."""
    import filearr.tasks.extract as extract_mod

    monkeypatch.setattr(
        extract_mod.extract_limiter, "try_acquire", lambda s: True
    )
    released = []
    monkeypatch.setattr(
        extract_mod.extract_limiter, "release", lambda: released.append(1)
    )

    async def _boom(item_id, scan_run_id, settings):
        raise RescheduleExtract("scan walking")

    monkeypatch.setattr(extract_mod, "_extract_item_impl", _boom)
    with pytest.raises(RescheduleExtract):
        await extract_mod.extract_item("00000000-0000-0000-0000-000000000000")
    assert released == [1]
