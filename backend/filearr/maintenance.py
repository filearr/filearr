"""Maintenance-task registry, runtime schedules, and on-demand triggers.

One table of every periodic/on-demand housekeeping task the worker runs, with
enough metadata for the Jobs page to explain each job (tooltip), show its
schedule + next/last run, trigger it on demand, and (for the editable subset)
override its cron or disable it at runtime — no worker restart.

Scheduling model (rides the user-approved T5 pattern): Procrastinate periodic
tasks are import-time static, so runtime-editable schedules CANNOT live in
``@proc_app.periodic`` decorators. The editable tasks instead lose their
static decorator and are deferred by ``filearr.worker.maintenance_tick`` — a
single static 1-minute periodic that evaluates each task's *effective* cron
(operator override from ``maintenance_schedules``, else the registry default)
with the same once-per-occurrence ``schedule.due_occurrence`` consumption
contract the scan scheduler uses. Infrastructure ticks (reaper, monitors,
minutely pumps) keep their static decorators — their cadence is part of their
semantics (``editable=False`` here) — and appear in the registry read-only.

The registry is the single source of truth for WHAT may be triggered by the
``/system/maintenance`` API: unknown keys 404, non-runnable ticks 409.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import bindparam, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.models import MaintenanceSchedule


class AlreadyQueued(Exception):
    """Raised by :func:`run_now` when the task's queueing lock says a run is
    already queued/executing (surfaces as HTTP 409)."""


@dataclass(frozen=True)
class MaintTaskSpec:
    key: str            # short API/UI key, e.g. "purge_recycle_bin"
    task_name: str      # full Procrastinate task name
    title: str
    description: str    # operator tooltip: what the job actually does
    category: str       # cleanup | integrity | monitors | system | ondemand
    default_cron: str | None   # None = on-demand only
    queue: str = "maintenance"
    lock: str | None = None    # queueing_lock (must match the task decorator)
    editable: bool = False     # schedule may be overridden / disabled
    runnable: bool = True      # exposed as a "run now" action
    timestamp_arg: bool = True  # task signature takes ``timestamp: int``


_SPECS: tuple[MaintTaskSpec, ...] = (
    # --- cleanup (nightly/hourly purges — schedules editable) ---------------
    MaintTaskSpec(
        key="purge_recycle_bin",
        task_name="filearr.worker.purge_recycle_bin",
        title="Purge recycle bin",
        description=(
            "Permanently deletes tombstoned items (missing/trashed) whose "
            "recycle-bin retention has expired, and removes their search "
            "documents. Retention: FILEARR_RECYCLE_RETENTION_DAYS."
        ),
        category="cleanup", default_cron="0 4 * * *", editable=True,
    ),
    MaintTaskSpec(
        key="purge_item_versions",
        task_name="filearr.worker.purge_item_versions",
        title="Purge item versions",
        description=(
            "Prunes item edit-history (version audit) rows older than the "
            "audit retention window so the history table stays bounded."
        ),
        category="cleanup", default_cron="15 4 * * *", editable=True,
    ),
    MaintTaskSpec(
        key="purge_security_events",
        task_name="filearr.worker.purge_security_events",
        title="Purge security events",
        description=(
            "Prunes security-audit events past their retention windows "
            "(login failures on a shorter window than other events)."
        ),
        category="cleanup", default_cron="20 4 * * *", editable=True,
    ),
    MaintTaskSpec(
        key="purge_alert_events",
        task_name="filearr.worker.purge_alert_events",
        title="Purge alert events",
        description="Prunes delivered/expired alert events past retention.",
        category="cleanup", default_cron="45 4 * * *", editable=True,
    ),
    MaintTaskSpec(
        key="purge_report_exports",
        task_name="filearr.worker.purge_report_exports",
        title="Purge report exports",
        description=(
            "Deletes expired export artifacts from disk (the row is kept and "
            "stamped purged) and fails any export stuck running past its "
            "timeout."
        ),
        category="cleanup", default_cron="40 4 * * *",
        lock="purge-report-exports", editable=True,
    ),
    MaintTaskSpec(
        key="purge_job_history",
        task_name="filearr.worker.purge_job_history",
        title="Purge job history",
        description=(
            "Hard-deletes terminal background-job rows past retention "
            "(succeeded: hours window; failed/other: days window) so the job "
            "tables stay small and fast."
        ),
        category="cleanup", default_cron="50 * * * *",
        lock="purge-job-history", editable=True,
    ),
    MaintTaskSpec(
        key="gc_thumbnails",
        task_name="filearr.tasks.thumbs.gc_thumbnails",
        title="Thumbnail cache GC",
        description=(
            "Deletes orphaned thumbnail files (their item was purged or the "
            "cache entry is stale) and reconciles the cache manifest with "
            "what is actually on disk."
        ),
        category="cleanup", default_cron="30 3 * * *", editable=True,
    ),
    # --- integrity (reconcilers/sweeps — schedules editable) ----------------
    MaintTaskSpec(
        key="reconcile_meili",
        task_name="filearr.worker.reconcile_meili",
        title="Search-index drift check",
        description=(
            "Compares Postgres truth against the search index and re-syncs "
            "any drifted items. Cheap; the nightly reconcile is the deep "
            "pass."
        ),
        category="integrity", default_cron="7 * * * *",
        lock="reconcile-meili", editable=True,
    ),
    MaintTaskSpec(
        key="nightly_reconcile",
        task_name="filearr.worker.nightly_reconcile",
        title="Nightly deep reconcile",
        description=(
            "Full Postgres↔search-index reconciliation: re-projects items "
            "whose documents are missing or stale, and removes orphaned "
            "search documents."
        ),
        category="integrity", default_cron="30 4 * * *", editable=True,
    ),
    MaintTaskSpec(
        key="reap_shadow_indexes",
        task_name="filearr.worker.reap_shadow_indexes",
        title="Reap shadow indexes",
        description=(
            "Drops leftover shadow search indexes abandoned by a crashed "
            "rebuild so they don't accumulate in Meilisearch."
        ),
        category="integrity", default_cron="47 * * * *",
        lock="reap-shadow-indexes", editable=True,
    ),
    MaintTaskSpec(
        key="rehash_small_files",
        task_name="filearr.worker.rehash_small_files",
        title="Re-hash small files",
        description=(
            "Re-enqueues a bounded nightly batch of ≤128 KiB files still "
            "hashed under a superseded algorithm through the normal extract "
            "path. Converges to zero and then no-ops."
        ),
        category="integrity", default_cron="55 4 * * *",
        lock="rehash-small-files", editable=True,
    ),
    # --- monitors (fixed 5-minute cadence; run-now allowed) -----------------
    MaintTaskSpec(
        key="reap_stalled_jobs",
        task_name="filearr.worker.reap_stalled_jobs",
        title="Stalled-job reaper",
        description=(
            "Requeues (or fails) jobs orphaned in 'doing' by a dead or "
            "restarted worker, and prunes dead worker rows. The safety net "
            "behind every crash-recovery invariant."
        ),
        category="monitors", default_cron="*/5 * * * *",
        lock="reap-stalled-jobs",
    ),
    MaintTaskSpec(
        key="monitor_disk",
        task_name="filearr.tasks.diskmon.monitor_disk",
        title="Disk-space monitor",
        description=(
            "Checks free space on the config/DB volumes, raises low-space "
            "alerts, and runs emergency thumbnail GC below the critical "
            "threshold."
        ),
        category="monitors", default_cron="*/5 * * * *",
        lock="monitor-disk",
    ),
    MaintTaskSpec(
        key="monitor_agents",
        task_name="filearr.tasks.agentmon.monitor_agents",
        title="Agent health monitor",
        description=(
            "Flags agents that have gone offline or whose replication has "
            "stalled, and raises the corresponding alerts."
        ),
        category="monitors", default_cron="*/5 * * * *",
        lock="monitor-agents",
    ),
    MaintTaskSpec(
        key="cleanup_staging_transfers",
        task_name="filearr.worker.cleanup_staging_transfers",
        title="Staging cleanup",
        description=(
            "Reaps expired staged agent-file transfers and abandoned partial "
            "uploads, reclaiming central staging disk."
        ),
        category="monitors", default_cron="*/5 * * * *",
        lock="cleanup-staging-transfers",
    ),
    MaintTaskSpec(
        key="reconcile_report_exports",
        task_name="filearr.worker.reconcile_report_exports",
        title="Export crash reconcile",
        description=(
            "Fails any report export stuck 'running' past its timeout so a "
            "crashed export never blocks its schedule."
        ),
        category="monitors", default_cron="*/10 * * * *",
        lock="reconcile-report-exports",
    ),
    # --- system ticks (minutely infrastructure; not runnable) ---------------
    MaintTaskSpec(
        key="schedule_scans",
        task_name="filearr.worker.schedule_scans",
        title="Scan scheduler tick",
        description=(
            "Evaluates every library's and hot-folder's scan cron each "
            "minute and defers due scans. Its cadence is the cron engine's "
            "clock — fixed by design."
        ),
        category="system", default_cron="* * * * *", runnable=False,
    ),
    MaintTaskSpec(
        key="schedule_report_exports",
        task_name="filearr.worker.schedule_report_exports",
        title="Report-schedule tick",
        description="Evaluates report delivery schedules each minute and defers due exports.",
        category="system", default_cron="* * * * *",
        lock="schedule-report-exports", runnable=False,
    ),
    MaintTaskSpec(
        key="pump_alerts",
        task_name="filearr.worker.pump_alerts",
        title="Alert dispatch pump",
        description="Groups pending alert events and dispatches them to notification channels.",
        category="system", default_cron="* * * * *",
        queue="alerts", lock="pump-alerts", runnable=False,
    ),
    MaintTaskSpec(
        key="expire_agent_commands",
        task_name="filearr.worker.expire_agent_commands",
        title="Agent-command sweep",
        description="Expires past-TTL agent commands and re-queues unacknowledged deliveries.",
        category="system", default_cron="* * * * *",
        lock="expire-agent-commands", runnable=False,
    ),
    MaintTaskSpec(
        key="maintenance_tick",
        task_name="filearr.worker.maintenance_tick",
        title="Maintenance scheduler tick",
        description=(
            "Evaluates the editable maintenance schedules on this page each "
            "minute and defers due tasks (override-aware). Fixed by design."
        ),
        category="system", default_cron="* * * * *",
        lock="maintenance-tick", runnable=False,
    ),
    # --- on-demand (no schedule; trigger from here) -------------------------
    MaintTaskSpec(
        key="rebuild_index",
        task_name="filearr.tasks.index_sync.rebuild_index",
        title="Rebuild search index",
        description=(
            "Rebuilds the entire search index from Postgres into a shadow "
            "index and atomically swaps it live — searches never see a "
            "half-built index. Use after settings/extension-map changes or "
            "suspected deep drift. Heavy: indexes every item."
        ),
        category="ondemand", default_cron=None, queue="index",
        timestamp_arg=False,
    ),
    MaintTaskSpec(
        key="embed_missing",
        task_name="filearr.tasks.embed.embed_missing",
        title="Semantic-embedding backfill",
        description=(
            "Queues embedding jobs for every item missing a semantic vector. "
            "No-op unless semantic search is enabled; a first run over a "
            "large corpus is a long background pass."
        ),
        category="ondemand", default_cron=None, queue="embed",
        timestamp_arg=False,
    ),
)

MAINT_TASKS: dict[str, MaintTaskSpec] = {s.key: s for s in _SPECS}

#: Tasks the maintenance tick schedules (editable ⇒ no static periodic decorator).
TICK_SCHEDULED: tuple[MaintTaskSpec, ...] = tuple(
    s for s in _SPECS if s.editable and s.default_cron
)


# --------------------------------------------------------------------------- #
# Deferral                                                                     #
# --------------------------------------------------------------------------- #


def _deferrer(spec: MaintTaskSpec):
    from filearr.worker import proc_app

    kwargs: dict = {"queue": spec.queue}
    if spec.lock:
        kwargs["queueing_lock"] = spec.lock
    if spec.key == "rebuild_index":
        kwargs["priority"] = get_settings().index_priority
    elif spec.key == "embed_missing":
        kwargs["priority"] = get_settings().embed_priority
        kwargs["queue"] = get_settings().queue_embed
    return proc_app.configure_task(spec.task_name, **kwargs)


async def _defer_spec(spec: MaintTaskSpec, at: datetime) -> int | None:
    """Defer ``spec`` (procrastinate app already open — worker context)."""
    kwargs = {"timestamp": int(at.timestamp())} if spec.timestamp_arg else {}
    return await _deferrer(spec).defer_async(**kwargs)


async def run_now(key: str) -> int | None:
    """Defer one registry task immediately (API context: opens the app).

    Raises :class:`KeyError` for unknown keys and :class:`AlreadyQueued` when
    the task's queueing lock reports a queued/executing run."""
    import procrastinate

    from filearr.worker import proc_app

    spec = MAINT_TASKS[key]
    kwargs = {"timestamp": int(datetime.now(UTC).timestamp())} if spec.timestamp_arg else {}
    try:
        async with proc_app.open_async():
            return await _deferrer(spec).defer_async(**kwargs)
    except procrastinate.exceptions.AlreadyEnqueued as exc:
        raise AlreadyQueued(str(exc)) from exc


# --------------------------------------------------------------------------- #
# The tick                                                                     #
# --------------------------------------------------------------------------- #


async def run_maintenance_tick(tick: datetime, *, defer=_defer_spec) -> list[str]:
    """Defer every editable maintenance task due at ``tick`` (once per cron
    occurrence, override-aware) and return the fired keys.

    Same consumption contract as the scan scheduler: the occurrence is stamped
    into ``maintenance_schedules.last_cron_fired_at`` and committed BEFORE the
    enqueue, so a given occurrence fires at most once even across duplicate or
    late ticks. A queueing-lock collision (previous run still queued) simply
    skips — the occurrence was consumed; the running job covers it."""
    import procrastinate

    from filearr.db import SessionLocal
    from filearr.schedule import due_occurrence

    cap = get_settings().scan_schedule_max_catchup_minutes
    fired: list[str] = []
    async with SessionLocal() as session:
        rows = {
            r.task_key: r
            for r in (await session.execute(select(MaintenanceSchedule))).scalars()
        }
        for spec in TICK_SCHEDULED:
            row = rows.get(spec.key)
            if row is not None and not row.enabled:
                continue
            cron = (row.cron if row is not None and row.cron else None) or spec.default_cron
            last = row.last_cron_fired_at if row is not None else None
            occ = due_occurrence(cron, tick, last, max_catchup_minutes=cap)
            if occ is None:
                continue
            if row is None:
                row = MaintenanceSchedule(task_key=spec.key)
                session.add(row)
            row.last_cron_fired_at = occ
            await session.commit()
            try:
                await defer(spec, occ)
            except procrastinate.exceptions.AlreadyEnqueued:
                continue
            fired.append(spec.key)
    return fired


# --------------------------------------------------------------------------- #
# Status projection (Jobs page)                                                #
# --------------------------------------------------------------------------- #

_LAST_RUN_SQL = text(
    """
    SELECT DISTINCT ON (j.task_name)
           j.task_name, j.id, j.status,
           (SELECT max(e.at) FROM procrastinate_events e WHERE e.job_id = j.id) AS at
    FROM procrastinate_jobs j
    WHERE j.task_name IN :names
    ORDER BY j.task_name, j.id DESC
    """
).bindparams(bindparam("names", expanding=True))


async def _last_runs(session: AsyncSession, names: list[str]) -> dict[str, dict]:
    """Latest job row per task name from procrastinate history (any status —
    'todo' means queued right now). Empty dict when the schema is absent.
    Bounded by job-history retention: a daily task's last success is visible
    for the 48h succeeded window; failures persist for days."""
    try:
        exists = (
            await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
        ).scalar()
        if exists is None:
            return {}
        rows = (await session.execute(_LAST_RUN_SQL, {"names": names})).all()
    except Exception:  # noqa: BLE001 - projection must stay total
        return {}
    out: dict[str, dict] = {}
    for task_name, job_id, status, at in rows:
        if at is not None and at.tzinfo is None:
            at = at.replace(tzinfo=UTC)
        out[task_name] = {
            "job_id": job_id,
            "status": status,
            "at": at.isoformat() if at else None,
        }
    return out


async def maintenance_status(session: AsyncSession) -> list[dict]:
    """The full registry joined with overrides + last-run history, in registry
    order — one row per task, ready for the Jobs page."""
    from filearr import schedule

    now = datetime.now(UTC)
    rows = {
        r.task_key: r
        for r in (await session.execute(select(MaintenanceSchedule))).scalars()
    }
    last = await _last_runs(session, [s.task_name for s in _SPECS])
    out: list[dict] = []
    for spec in _SPECS:
        row = rows.get(spec.key) if spec.editable else None
        enabled = row.enabled if row is not None else True
        cron = (row.cron if row is not None and row.cron else None) or spec.default_cron
        next_at = (
            schedule.next_occurrence(cron, now)
            if cron and enabled
            else None
        )
        out.append(
            {
                "key": spec.key,
                "title": spec.title,
                "description": spec.description,
                "category": spec.category,
                "queue": spec.queue,
                "cron": cron,
                "default_cron": spec.default_cron,
                "overridden": bool(row is not None and row.cron),
                "enabled": enabled,
                "editable": spec.editable,
                "runnable": spec.runnable,
                "next_run_at": next_at.isoformat() if next_at else None,
                "last_run": last.get(spec.task_name),
            }
        )
    return out


async def set_schedule(
    session: AsyncSession,
    key: str,
    *,
    cron: str | None = None,
    set_cron: bool = False,
    enabled: bool | None = None,
) -> None:
    """Upsert one editable task's override row. ``set_cron=True`` with
    ``cron=None`` resets to the registry default. Cron validation happens at
    the API boundary (``schedule.validate_cron``)."""
    row = await session.get(MaintenanceSchedule, key)
    if row is None:
        row = MaintenanceSchedule(task_key=key)
        session.add(row)
    if set_cron:
        row.cron = cron.strip() if cron else None
    if enabled is not None:
        row.enabled = enabled
    row.updated_at = datetime.now(UTC)
    await session.commit()
