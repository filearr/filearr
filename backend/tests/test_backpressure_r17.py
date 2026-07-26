"""Roadmap §17 — adaptive extract backpressure.

The worker-local ExtractLimiter: loadavg-driven trip with hysteresis, capped
concurrency while tripped, refused jobs rescheduled short+jittered via
BackpressureReschedule (never failed, never recorded as a job error).
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
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is False
    for _ in range(2):
        lim.release()

    # Between the thresholds (0.70): still tripped — no flapping.
    _with_load(monkeypatch, load1=2.8, cores=4)
    lim._last_sample = 0.0
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is False
    for _ in range(2):
        lim.release()

    # Below the low water (0.5): recovered, unlimited again.
    _with_load(monkeypatch, load1=2.0, cores=4)
    lim._last_sample = 0.0
    assert lim.try_acquire(s) is True
    assert lim.try_acquire(s) is True
    assert lim.snapshot()["tripped"] is False


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
