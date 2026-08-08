"""The open_pool_if_needed guard (live AppNotOpen incident, 2026-08-08).

Defer helpers used to wrap ``async with proc_app.open_async():``. In the
worker process the app is already open, so the enter was a no-op while the
EXIT closed the worker's shared connection pool — every concurrently running
job then died with AppNotOpen ("Task exception was never retrieved" noise in
the container log) until the next defer incidentally reopened the pool. The
guard must never close a pool it did not open."""

from __future__ import annotations

import pytest
from procrastinate.psycopg_connector import PsycopgConnector

import filearr.worker as worker_mod


class _StubConnector:
    def __init__(self, already_open: bool):
        self._async_pool = object() if already_open else None
        self.opened = 0
        self.closed = 0

    async def open_async(self, pool=None):
        self.opened += 1
        self._async_pool = object()

    async def close_async(self):
        self.closed += 1
        self._async_pool = None


@pytest.mark.asyncio
async def test_already_open_pool_is_never_touched(monkeypatch):
    stub = _StubConnector(already_open=True)
    monkeypatch.setattr(worker_mod.proc_app, "connector", stub)
    async with worker_mod.open_pool_if_needed():
        pass
    assert stub.opened == 0 and stub.closed == 0  # the worker's pool survives


@pytest.mark.asyncio
async def test_closed_pool_is_opened_and_closed(monkeypatch):
    stub = _StubConnector(already_open=False)
    monkeypatch.setattr(worker_mod.proc_app, "connector", stub)
    async with worker_mod.open_pool_if_needed():
        assert stub.opened == 1
    assert stub.closed == 1  # we opened it, we close it (API-process case)


def test_guard_introspection_contract_still_holds():
    """The guard reads the connector's private ``_async_pool`` (procrastinate
    3.9 has no public is-open probe). A library upgrade that renames it must
    fail HERE, loudly — not silently reintroduce the pool-yank."""
    assert "_async_pool" in vars(
        PsycopgConnector()
    ), "procrastinate renamed _async_pool — update open_pool_if_needed()"


def test_no_raw_open_async_left_outside_the_guard():
    """Every ``proc_app.open_async()`` context in filearr must go through the
    guard; a raw one inside worker-side code re-creates the incident."""
    import pathlib

    root = pathlib.Path(worker_mod.__file__).resolve().parents[0]
    offenders = []
    for path in root.rglob("*.py"):
        for i, line in enumerate(
            path.read_text(encoding="utf-8").splitlines(), start=1
        ):
            if (
                "async with proc_app.open_async()" in line
                and not line.lstrip().startswith(("#", "*", "Every"))
                # the guard's own fallback in worker.py is the ONE legal site
                and not (path.name == "worker.py" and i < 45)
            ):
                offenders.append(f"{path.name}:{i}")
    assert offenders == [], f"raw open_async contexts found: {offenders}"
