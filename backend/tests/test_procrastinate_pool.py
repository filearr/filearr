"""Procrastinate pool sizing (live 2026-10-29: worker PoolTimeout → "Task
exception was never retrieved … pump_alerts"). psycopg_pool's default max (4)
equals the default worker concurrency, leaving no connection for the worker's
side tasks or a job's completion ack."""

from __future__ import annotations

import logging

from filearr.config import Settings


def _s(**kw) -> Settings:
    return Settings(**kw)


def test_auto_bounds_leave_headroom_over_worker_slots():
    assert _s(worker_concurrency=4).procrastinate_pool_bounds() == (2, 10)
    assert _s(worker_concurrency=8).procrastinate_pool_bounds() == (2, 14)


def test_extract_ceiling_above_concurrency_is_honoured():
    s = _s(worker_concurrency=4, extract_backpressure_max_concurrency=12)
    assert s.procrastinate_pool_bounds() == (2, 18)


def test_explicit_override_and_min_clamped():
    s = _s(procrastinate_pool_max_size=3, procrastinate_pool_min_size=5)
    assert s.procrastinate_pool_bounds() == (3, 3)


def test_proc_app_connector_uses_the_bounds(caplog):
    from filearr import worker
    from filearr.config import get_settings

    args = worker.proc_app.connector._pool_args
    assert args["max_size"] >= get_settings().worker_concurrency + 4
    assert args["min_size"] <= args["max_size"]
    with caplog.at_level(logging.INFO, logger="filearr.worker"):
        worker.log_startup_pool()
    assert any("procrastinate pool: min_size=" in r.getMessage() for r in caplog.records)
