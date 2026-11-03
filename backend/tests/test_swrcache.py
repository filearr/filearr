"""Unit tests for filearr.swrcache — the stale-while-revalidate cache behind the
/stats semantic and disk sections (2026-11-02)."""

from __future__ import annotations

import asyncio
import logging

from filearr.swrcache import SWRCache


class _Probe:
    def __init__(self):
        self.calls = 0
        self.gate: asyncio.Event | None = None
        self.fail: Exception | None = None
        self.value = 1

    async def __call__(self):
        self.calls += 1
        if self.gate is not None:
            await self.gate.wait()
        if self.fail is not None:
            raise self.fail
        return self.value


async def _settle(cache: SWRCache) -> None:
    if cache._task is not None:
        await asyncio.wait({cache._task}, timeout=2)


async def test_fresh_value_is_served_without_recompute():
    p = _Probe()
    c = SWRCache("t", p, ttl_s=60)
    await c.get()
    await _settle(c)
    snap = await c.get()
    assert snap.value == 1 and not snap.stale and not snap.refreshing
    assert snap.computed_at is not None and snap.computed_at_iso.endswith("+00:00")
    for _ in range(5):
        await c.get()
    assert p.calls == 1


async def test_first_call_does_not_block():
    p = _Probe()
    p.gate = asyncio.Event()
    c = SWRCache("t", p, ttl_s=60)
    snap = await asyncio.wait_for(c.get(), timeout=0.5)
    assert snap.value is None and snap.computed_at is None
    assert snap.refreshing is True
    p.gate.set()
    await _settle(c)
    assert (await c.get()).value == 1


async def test_first_call_wait_is_bounded_and_refresh_continues():
    p = _Probe()
    p.gate = asyncio.Event()
    c = SWRCache("t", p, ttl_s=60)
    loop = asyncio.get_running_loop()
    t0 = loop.time()
    snap = await c.get(wait_s=0.1)
    assert loop.time() - t0 < 0.5
    assert snap.value is None and snap.refreshing
    p.gate.set()  # the refresh was NOT cancelled by the bounded wait
    await _settle(c)
    assert (await c.get()).value == 1


async def test_stale_value_served_with_single_refresh_in_flight():
    p = _Probe()
    c = SWRCache("t", p, ttl_s=60)
    await c.get()
    await _settle(c)
    c.ttl_s = 0  # everything is stale now
    p.gate = asyncio.Event()
    p.value = 2
    snaps = [await c.get() for _ in range(10)]
    assert all(s.value == 1 and s.stale and s.refreshing for s in snaps)
    await asyncio.sleep(0.01)  # let the refresh task start
    assert p.calls == 2  # ONE refresh for ten stale reads
    p.gate.set()
    await _settle(c)
    c.ttl_s = 60
    snap = await c.get()
    assert snap.value == 2 and not snap.stale


async def test_failed_refresh_keeps_last_value_and_logs_once(caplog):
    p = _Probe()
    c = SWRCache("t", p, ttl_s=0, retry_after_s=0)
    await c.get()
    await _settle(c)
    p.fail = RuntimeError("statement timeout")
    with caplog.at_level(logging.WARNING, logger="filearr.swrcache"):
        for _ in range(4):
            snap = await c.get()
            await _settle(c)
    snap = await c.get()
    await _settle(c)
    assert snap.value == 1 and snap.stale
    assert snap.error and "statement timeout" in snap.error
    warns = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warns) == 1, [r.getMessage() for r in warns]
    # A success clears the error and re-arms the warning.
    p.fail = None
    p.value = 3
    await c.get()
    await _settle(c)
    snap = await c.get()
    assert snap.value == 3 and snap.error is None
    p.fail = RuntimeError("again")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="filearr.swrcache"):
        await c.get()
        await _settle(c)
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


async def test_failed_refresh_backs_off():
    p = _Probe()
    p.fail = RuntimeError("down")
    c = SWRCache("t", p, ttl_s=60, retry_after_s=60)
    await c.get()
    await _settle(c)
    for _ in range(5):
        await c.get()
    assert p.calls == 1  # no retry storm while the backoff runs


async def test_key_change_invalidates():
    p = _Probe()
    c = SWRCache("t", p, ttl_s=60)
    await c.get(key="a")
    await _settle(c)
    assert (await c.get(key="a")).value == 1
    p.gate = asyncio.Event()
    snap = await c.get(key="b")
    assert snap.value is None and snap.refreshing  # old-key value never served
    p.gate.set()
    await _settle(c)
    assert (await c.get(key="b")).value == 1


async def test_reset_drops_value():
    p = _Probe()
    c = SWRCache("t", p, ttl_s=60)
    await c.get()
    await _settle(c)
    c.reset()
    p.gate = asyncio.Event()
    assert (await c.get()).value is None
    p.gate.set()
    await _settle(c)
