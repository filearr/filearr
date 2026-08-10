"""P9-T4 — periodic Meilisearch LMDB compaction.

Covers the Meili-touching entry point ``meili_ops.compact_if_fragmented`` (the
decision → ``AsyncIndex.compact()`` wiring, the FIX-11 disk-guard refusal, and
every "degrade to a structured skip, never raise" path), the
``filearr.worker.compact_meili`` periodic and its maintenance-mode gate, and the
maintenance-registry entry that makes it visible/triggerable on the Jobs page.

Meilisearch is a minimal in-memory fake (no live server in the test env) in the
same shape as ``test_rebuild_swap_p9.py``'s. The PURE decision logic
(``fragmentation_ratio`` / ``should_compact``) is already covered by
``test_meili_ops_scaffold.py`` and is deliberately not re-tested here — what is
tested is that this entry point *applies* it, at the configured threshold, with
the strict ``>`` boundary intact.

Invariant 1 framing throughout: the index is a DISPOSABLE projection, so every
refusal/skip below is a free choice, never a risk to data.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from filearr import maintenance, meili_ops
from filearr import search as search_mod
from filearr.config import get_settings
from filearr.meili_ops import DEFAULT_COMPACTION_THRESHOLD, compact_if_fragmented


# --------------------------------------------------------------------------- #
# Minimal in-memory fake Meilisearch                                          #
# --------------------------------------------------------------------------- #
class FakeIndex:
    def __init__(self, client: FakeClient, uid: str):
        self._client = client
        self.uid = uid

    async def get_stats(self):
        if self._client.stats_error is not None:
            raise self._client.stats_error
        return SimpleNamespace(
            database_size=self._client.database_size,
            used_database_size=self._client.used_database_size,
            number_of_documents=0,
        )

    async def compact(self):
        self._client.compacts.append(self.uid)
        if self._client.compact_error is not None:
            raise self._client.compact_error
        return SimpleNamespace(task_uid=77, index_uid=self.uid)


class FakeClient:
    def __init__(
        self,
        *,
        database_size: int | None = 100,
        used_database_size: int | None = 100,
        task_status: str = "succeeded",
        stats_error: Exception | None = None,
        compact_error: Exception | None = None,
        wait_error: Exception | None = None,
    ):
        self.database_size = database_size
        self.used_database_size = used_database_size
        self.task_status = task_status
        self.stats_error = stats_error
        self.compact_error = compact_error
        self.wait_error = wait_error
        self.compacts: list[str] = []
        self.waited: list[tuple[int, int | None]] = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, uid):
        return FakeIndex(self, uid)

    async def wait_for_task(self, task_id, *, timeout_in_ms=None, **kw):
        self.waited.append((task_id, timeout_in_ms))
        if self.wait_error is not None:
            raise self.wait_error
        return SimpleNamespace(uid=task_id, status=self.task_status)


@pytest.fixture
def settings(monkeypatch):
    """Live settings object with the compaction knobs at their defaults, and the
    disk guard NOT applicable (``meili_data_path`` unset — the compose-stack
    default where Meili owns its own invisible volume)."""
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "meili_compaction_enabled", True)
    monkeypatch.setattr(s, "meili_compaction_threshold", 1.3)
    monkeypatch.setattr(s, "meili_compaction_wait_s", 1800.0)
    monkeypatch.setattr(s, "meili_data_path", None)
    yield s
    get_settings.cache_clear()


def _use(monkeypatch, fake: FakeClient) -> FakeClient:
    """Point the entry point's client factory at ``fake`` (it imports ``client``
    from ``filearr.search`` at call time, exactly like ``rebuild_via_swap``)."""
    monkeypatch.setattr(search_mod, "client", lambda: fake)
    return fake


# --------------------------------------------------------------------------- #
# The threshold decision, applied                                             #
# --------------------------------------------------------------------------- #
async def test_compacts_when_ratio_exceeds_threshold(settings, monkeypatch, caplog):
    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))

    with caplog.at_level("INFO", logger="filearr.meili_ops"):
        res = await compact_if_fragmented()

    assert res["status"] == "compacted" and res["compacted"] is True
    assert res["ratio"] == pytest.approx(2.0) and res["task_uid"] == 77
    assert fake.compacts == [settings.meili_index]
    # the compaction task is awaited within the configured wall-clock budget
    assert fake.waited == [(77, int(settings.meili_compaction_wait_s * 1000))]
    # an operator must be able to read the decision AND the sizes it was made on
    assert "compacting" in caplog.text and "database_size=200" in caplog.text


@pytest.mark.parametrize(
    ("db_size", "used"),
    [
        (130, 100),  # EXACTLY at the threshold — strict `>` must not fire
        (129, 100),  # just below
        (100, 100),  # a healthy, unfragmented index
    ],
)
async def test_does_not_compact_at_or_below_threshold(
    settings, monkeypatch, caplog, db_size, used
):
    fake = _use(monkeypatch, FakeClient(database_size=db_size, used_database_size=used))

    with caplog.at_level("INFO", logger="filearr.meili_ops"):
        res = await compact_if_fragmented()

    assert res["status"] == "not_fragmented" and res["compacted"] is False
    assert fake.compacts == [] and fake.waited == []
    # "checked, not fragmented" is as operationally important as "compacting"
    assert "not fragmented" in caplog.text


@pytest.mark.parametrize(
    ("db_size", "used"),
    [
        (None, 100),   # stat absent from the response
        (100, None),
        (None, None),
        (100, 0),      # zero divisor -> unmeasurable, not "infinitely fragmented"
        (0, 0),
    ],
)
async def test_missing_or_zero_stat_never_compacts(settings, monkeypatch, db_size, used):
    """The ``0.0`` unmeasurable sentinel must read as "nothing to reclaim" — a
    missing stat triggering a 2x-disk compaction would be the worst failure mode
    here (the module's own None-tolerance contract)."""
    fake = _use(monkeypatch, FakeClient(database_size=db_size, used_database_size=used))

    res = await compact_if_fragmented()

    assert res["status"] == "not_fragmented" and res["compacted"] is False
    assert res["ratio"] == 0.0
    assert fake.compacts == []


async def test_configured_threshold_is_wired_through(settings, monkeypatch):
    """The setting — not the module default — decides. Raising it above the live
    ratio must suppress a compaction that the default would have triggered."""
    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))

    monkeypatch.setattr(settings, "meili_compaction_threshold", 3.0)
    assert (await compact_if_fragmented())["status"] == "not_fragmented"
    assert fake.compacts == []

    monkeypatch.setattr(settings, "meili_compaction_threshold", 1.5)
    assert (await compact_if_fragmented())["status"] == "compacted"
    assert fake.compacts == [settings.meili_index]


async def test_explicit_threshold_argument_overrides_the_setting(settings, monkeypatch):
    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))
    res = await compact_if_fragmented(threshold=5.0)
    assert res["status"] == "not_fragmented" and res["threshold"] == 5.0
    assert fake.compacts == []


def test_compaction_threshold_setting_mirrors_meili_ops_default(settings):
    """The config default and the module constant must not drift apart (same
    lockstep guard the searchCutoffMs pair carries)."""
    assert type(settings).model_fields["meili_compaction_threshold"].default == (
        DEFAULT_COMPACTION_THRESHOLD
    )


# --------------------------------------------------------------------------- #
# Clean skips: feature off, Meili unreachable, Meili erroring                 #
# --------------------------------------------------------------------------- #
async def test_disabled_flag_is_a_clean_skip_without_touching_meili(
    settings, monkeypatch
):
    def _boom():
        raise AssertionError("Meili must not be contacted while compaction is off")

    monkeypatch.setattr(settings, "meili_compaction_enabled", False)
    monkeypatch.setattr(search_mod, "client", _boom)

    res = await compact_if_fragmented()
    assert res == {"status": "skipped", "reason": "disabled", "compacted": False}


async def test_unreachable_meili_is_a_clean_skip(settings, monkeypatch, caplog):
    """A disposable projection's housekeeping must never raise into the worker:
    an unreachable Meili burns no failed-job slot, it just re-runs next week."""
    from meilisearch_python_sdk.errors import MeilisearchCommunicationError

    _use(
        monkeypatch,
        FakeClient(stats_error=MeilisearchCommunicationError("connection refused")),
    )

    with caplog.at_level("WARNING", logger="filearr.meili_ops"):
        res = await compact_if_fragmented()

    assert res["status"] == "skipped" and res["reason"] == "unavailable"
    assert res["compacted"] is False and "connection refused" in res["error"]
    assert "Meilisearch unavailable" in caplog.text


async def test_compact_call_failure_is_a_clean_skip(settings, monkeypatch):
    """An index that rejects /compact (e.g. a Meili older than 1.23, where the
    route 404s) degrades the same way — logged, never raised."""
    from meilisearch_python_sdk.errors import MeilisearchError

    fake = _use(
        monkeypatch,
        FakeClient(
            database_size=200,
            used_database_size=100,
            compact_error=MeilisearchError("compact route not found (404)"),
        ),
    )

    res = await compact_if_fragmented()
    assert res["status"] == "skipped" and res["reason"] == "unavailable"
    assert fake.compacts == [settings.meili_index]  # attempted, then swallowed


async def test_wait_timeout_reports_without_failing_the_job(settings, monkeypatch):
    """Our observation timed out; Meili keeps compacting server-side. Reporting
    that beats failing a job for a task that is still making progress."""
    from meilisearch_python_sdk.errors import MeilisearchTimeoutError

    _use(
        monkeypatch,
        FakeClient(
            database_size=200,
            used_database_size=100,
            wait_error=MeilisearchTimeoutError("timed out"),
        ),
    )

    res = await compact_if_fragmented()
    assert res["status"] == "timeout" and res["compacted"] is True
    assert res["task_uid"] == 77


async def test_failed_meili_task_is_reported_as_error(settings, monkeypatch):
    _use(
        monkeypatch,
        FakeClient(database_size=200, used_database_size=100, task_status="failed"),
    )
    res = await compact_if_fragmented()
    assert res["status"] == "error" and res["task_status"] == "failed"


# --------------------------------------------------------------------------- #
# FIX-11 disk guard: compaction needs ~2x the index size while it runs        #
# --------------------------------------------------------------------------- #
async def test_disk_critical_refuses_to_start_a_compaction(
    settings, monkeypatch, caplog
):
    from filearr import diskguard

    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))
    monkeypatch.setattr(settings, "meili_data_path", "/config/meili")
    monkeypatch.setattr(diskguard, "is_critical", lambda path, s, **kw: True)

    with caplog.at_level("WARNING", logger="filearr.meili_ops"):
        res = await compact_if_fragmented()

    assert res["status"] == "skipped" and res["reason"] == "disk_critical"
    assert res["compacted"] is False and res["ratio"] == pytest.approx(2.0)
    assert fake.compacts == []  # never started
    assert "critical disk floor" in caplog.text


async def test_headroom_above_the_floor_still_compacts(settings, monkeypatch):
    from filearr import diskguard

    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))
    monkeypatch.setattr(settings, "meili_data_path", "/config/meili")
    monkeypatch.setattr(diskguard, "is_critical", lambda path, s, **kw: False)

    assert (await compact_if_fragmented())["status"] == "compacted"
    assert fake.compacts == [settings.meili_index]


async def test_disk_guard_is_only_consulted_when_compaction_is_wanted(
    settings, monkeypatch
):
    """Order matters: an unfragmented index on a full volume must report the
    honest "not fragmented", not a disk refusal for work nobody wanted."""
    from filearr import diskguard

    def _boom(path, s, **kw):
        raise AssertionError("disk guard consulted for a non-fragmented index")

    _use(monkeypatch, FakeClient(database_size=100, used_database_size=100))
    monkeypatch.setattr(settings, "meili_data_path", "/config/meili")
    monkeypatch.setattr(diskguard, "is_critical", _boom)

    assert (await compact_if_fragmented())["status"] == "not_fragmented"


async def test_unset_data_path_skips_the_guard_entirely(settings, monkeypatch):
    """Compose-stack default: Meili's volume is invisible to the worker, so there
    is nothing to statvfs and the guard is simply not applicable."""
    from filearr import diskguard

    def _boom(path, s, **kw):
        raise AssertionError("disk guard consulted with no meili_data_path set")

    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))
    monkeypatch.setattr(diskguard, "is_critical", _boom)

    assert (await compact_if_fragmented())["status"] == "compacted"
    assert fake.compacts == [settings.meili_index]


# --------------------------------------------------------------------------- #
# The worker task: maintenance-mode gate + delegation                         #
# --------------------------------------------------------------------------- #
def _task_fn():
    """The task's wrapped coroutine (Procrastinate wraps it in a Task object)."""
    from filearr.worker import compact_meili

    return getattr(compact_meili, "func", compact_meili)


async def test_task_skips_while_maintenance_mode_is_active(monkeypatch):
    """Sustained heavy disk I/O is exactly what an operator enters maintenance
    mode to stop; the tick already declines to DEFER, and this gate covers the
    Jobs-page "Run now" path that bypasses the tick."""
    from filearr import maintmode

    async def _active():
        return True

    def _boom():
        raise AssertionError("compaction ran during maintenance mode")

    monkeypatch.setattr(maintmode, "is_active_standalone", _active)
    monkeypatch.setattr(meili_ops, "compact_if_fragmented", _boom)

    res = await _task_fn()(timestamp=0)
    assert res == {
        "status": "skipped", "reason": "maintenance_mode", "compacted": False,
    }


async def test_task_delegates_when_mode_is_inactive(settings, monkeypatch):
    from filearr import maintmode

    async def _inactive():
        return False

    monkeypatch.setattr(maintmode, "is_active_standalone", _inactive)
    fake = _use(monkeypatch, FakeClient(database_size=200, used_database_size=100))

    res = await _task_fn()(timestamp=0)
    assert res["status"] == "compacted"
    assert fake.compacts == [settings.meili_index]


async def test_tick_never_defers_it_during_maintenance_mode(monkeypatch):
    """Belt-and-braces on the other half of the contract: the maintenance tick
    consumes NO occurrence while the mode is active, so the weekly slot fires
    (collapsed to the latest occurrence) once the mode lifts."""
    from datetime import UTC, datetime

    from filearr import maintmode

    async def _active():
        return True

    async def _boom(spec, occ):
        raise AssertionError("deferred during maintenance mode")

    monkeypatch.setattr(maintmode, "is_active_standalone", _active)
    tick = datetime(2026, 8, 9, 6, 0, tzinfo=UTC)  # a Sunday 06:00 occurrence
    assert await maintenance.run_maintenance_tick(tick, defer=_boom) == []


# --------------------------------------------------------------------------- #
# Registry entry (Jobs page visibility + trigger path)                        #
# --------------------------------------------------------------------------- #
def test_registry_entry_matches_the_task_decorator():
    from filearr.worker import proc_app

    spec = maintenance.MAINT_TASKS["compact_meili"]
    assert spec.task_name == "filearr.worker.compact_meili"
    assert spec.queue == "maintenance" and spec.lock == "compact-meili"
    assert spec.category == "cleanup"          # scheduled space reclamation
    assert spec.editable and spec.runnable and spec.timestamp_arg
    assert spec.default_cron == "0 6 * * 0"    # Sunday, clear of the 03:30-05:20 window
    assert spec in maintenance.TICK_SCHEDULED  # deferred by maintenance_tick

    # The registry's `lock` MUST match the decorator's queueing_lock, or the
    # "already queued" 409 the Jobs page relies on never fires.
    task = proc_app.tasks[spec.task_name]
    assert task.queueing_lock == spec.lock
    assert task.queue == spec.queue


def test_registry_entry_is_gated_so_the_console_shows_the_no_op_chip():
    assert maintenance._GATED_TASKS["compact_meili"] == "meili_compaction"
    reason = maintenance._GATE_REASONS["meili_compaction"]
    assert "FILEARR_MEILI_COMPACTION_ENABLED" in reason


async def test_run_now_reaches_the_task(monkeypatch):
    """The registry's own trigger path (what POST /system/maintenance/{key}/run
    calls) resolves this key to a real deferral of the real task."""
    import contextlib

    from filearr import worker as worker_mod

    deferred: list[dict] = []

    class _Deferrer:
        async def defer_async(self, **kwargs):
            deferred.append(kwargs)
            return 4242

    @contextlib.asynccontextmanager
    async def _noop_pool():
        yield

    monkeypatch.setattr(worker_mod, "open_pool_if_needed", _noop_pool)
    monkeypatch.setattr(maintenance, "_deferrer", lambda spec: _Deferrer())

    assert await maintenance.run_now("compact_meili") == 4242
    assert list(deferred[0]) == ["timestamp"]  # timestamp_arg=True spec


def test_deferrer_configures_queue_and_lock():
    """Unmocked: the real ``_deferrer`` must produce a job carrying this task's
    queue and queueing lock (overlapping ticks collapse to one queued run)."""
    job = maintenance._deferrer(maintenance.MAINT_TASKS["compact_meili"]).job
    assert job.task_name == "filearr.worker.compact_meili"
    assert job.queue == "maintenance"
    assert job.queueing_lock == "compact-meili"
