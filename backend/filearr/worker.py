"""Procrastinate app (Postgres-native job queue — no Redis).

Run worker:  procrastinate --app=filearr.worker.proc_app worker
Queues: scan (walk/diff), extract (per-file metadata), index (Meili sync), maintenance.
"""

import contextlib
from datetime import UTC, datetime, timedelta

import procrastinate
from procrastinate import PsycopgConnector
from procrastinate.exceptions import AlreadyEnqueued, UniqueViolation
from procrastinate.jobs import Status

from filearr.config import get_settings
from filearr.db import SessionLocal as SessionLocal  # noqa: PLC0414 (re-export for periodics)
from filearr.joberrors import capture_job_errors


@contextlib.asynccontextmanager
async def open_pool_if_needed():
    """``proc_app`` usable inside the block — WITHOUT closing a pool we did
    not open.

    Every defer helper used to wrap ``async with proc_app.open_async():``.
    Correct in the API process (nothing had opened the app)… but the SAME
    helpers run inside worker tasks, where the worker CLI owns an open app —
    there the enter was a no-op while the EXIT closed the worker's shared
    connection pool, killing every concurrently running job with AppNotOpen
    until the next defer incidentally reopened it (the live "Task exception
    was never retrieved … AppNotOpen" incident, 2026-08-08). procrastinate
    3.9 has no public is-open probe, so this reads the connector's
    ``_async_pool`` (pinned by a regression test so an upgrade that renames
    it fails loudly instead of silently reintroducing the pool-yank)."""
    if getattr(proc_app.connector, "_async_pool", None) is not None:
        yield  # already open (worker CLI / app lifespan owns it) — never close
        return
    async with proc_app.open_async():
        yield


proc_app = procrastinate.App(
    connector=PsycopgConnector(conninfo=get_settings().procrastinate_dsn),
    # Roadmap §18: every job runs under the joberrors worker middleware, which
    # persists a sanitized message + capped traceback to `job_errors` when a
    # task raises (then re-raises untouched — retries/reaper/status unchanged).
    worker_defaults={"worker_middleware": [capture_job_errors]},
    import_paths=[
        "filearr.tasks.scan",
        "filearr.tasks.extract",
        "filearr.tasks.index_sync",
        "filearr.tasks.alerts",
        # P3-T8 local embedding pipeline (embed queue, lowest priority). Inert
        # until FILEARR_SEMANTIC_ENABLED=true.
        "filearr.tasks.embed",
        # LLM/RAG M2 passage chunking (embed queue). Inert until a library
        # opts in via chunking_enabled.
        "filearr.tasks.chunks",
        # S12/P12 thumbnails: the thumbs-queue ride-along job + the daily orphan
        # GC periodic. Inert until FILEARR_THUMBS_ENABLED (default true).
        "filearr.tasks.thumbs",
        # FIX-11: 5-minutely low-space monitor + emergency thumbnail GC. Inert
        # (returns early) when FILEARR_DISK_MONITOR_ENABLED=false.
        "filearr.tasks.diskmon",
        # P8-T11: 5-minutely agent-offline + replication-stall ops monitor. Inert
        # (returns early) when FILEARR_AGENTS_ENABLED=false or tables absent.
        "filearr.tasks.agentmon",
        # P11-T5/T9: background report-export task (dedicated `exports` queue) +
        # scheduled-delivery evaluation. Inert until a schedule/export exists.
        "filearr.tasks.reports",
    ],
)


def log_startup_disk_status() -> str:
    """Log the current disk status for every watch path at worker startup and
    return the worst status (FIX-11).

    Called once when the worker process boots (from the worker entrypoint / the
    diskmon import). If the worst status is ``critical`` the operator is told the
    thumbnail producer is effectively PAUSED: at critical the fail-closed guard
    (``diskguard.guard_write``) refuses every thumbnail write, so queued
    ``thumb_item`` jobs fail fast with the ``disk_full_guard`` token and write NO
    bytes — the workers stay alive and other queues keep running. Never raises."""
    import logging as _logging

    from filearr import diskguard as _dg
    from filearr.config import get_settings as _gs

    logger = _logging.getLogger("filearr.diskmon")
    try:
        statuses = _dg.monitored_statuses(_gs())
        worst = _dg.overall_status(statuses)
        for st in statuses:
            logger.info(
                "startup disk %s: %s %.1f GiB free of %.1f GiB (%.1f%%)",
                st["status"],
                st["path"],
                st["free"] / _dg.GB,
                st["total"] / _dg.GB,
                st["pct_free"],
            )
        if worst == _dg.CRITICAL:
            logger.warning(
                "startup disk CRITICAL — thumbnail generation is fail-closed "
                "(guarded writes refused); free space before resuming thumbnails."
            )
        return worst
    except Exception:  # noqa: BLE001 - a monitoring log must never break startup
        logger.debug("startup disk status check failed", exc_info=True)
        return _dg.OK


# --- FIX-6: stalled-job reaper ---------------------------------------------
# Fully-qualified name of the long-running library-scan task. It is EXEMPT from
# the absolute-age net (a full walk of a large library legitimately runs long)
# and, when it IS reaped via the heartbeat net (its worker died), it is FAILED
# rather than retried: the ScanRun row is already crash-failed (invariant 7) and
# an operator retriggers a scan from the Libraries page — silently re-running a
# half-finished scan would be surprising.
SCAN_TASK_NAME = "filearr.tasks.scan.scan_library"

#: Whole-catalog jobs that legitimately run for hours and are therefore EXEMPT
#: from the reaper's absolute-age net (they are still reaped by the heartbeat
#: net when their worker truly dies). Live 2026-08-17: nightly_reconcile (a
#: full shadow-index rebuild of 1.37M docs) passed job_stall_seconds while
#: healthy, was requeued, and a SECOND copy ran concurrently -- doubling the
#: Meili load and ending the first with "job not in doing status".
AGE_NET_EXEMPT_TASKS: tuple[str, ...] = (
    SCAN_TASK_NAME,
    "filearr.worker.nightly_reconcile",
    "filearr.tasks.index_sync.rebuild_index",
    "filearr.tasks.index_sync.reproject_library",
    "filearr.tasks.chunks.rebuild_chunks_index",
    "filearr.tasks.embed.embed_missing",
    "filearr.tasks.chunks.chunk_missing",
    "filearr.worker.backup_now",
    "filearr.worker.compact_meili",
    "filearr.worker.content_sniff",
    "filearr.worker.rehash_small_files",
    "filearr.worker.backfill_content_hashes",
    "filearr.worker.rehash_library",
    "filearr.worker.sync_directory",
)


def age_net_exempt_tasks() -> list[str]:
    """Built-in exemptions plus ``job_stall_age_exempt_tasks`` from settings."""
    extra = get_settings().job_stall_age_exempt_tasks
    out = list(AGE_NET_EXEMPT_TASKS)
    for name in extra:
        if name and name not in out:
            out.append(name)
    return out

# Detect doing jobs that are stalled. TWO independent nets ORed together:
#   * heartbeat net (ALL doing jobs): worker_id IS NULL (procrastinate SET NULL
#     when the worker row was pruned) OR the job's worker has not heartbeat
#     within :hb seconds. This mirrors JobManager.get_stalled_jobs' own
#     `select_stalled_jobs_by_heartbeat` predicate exactly.
#   * age net (NON-scan doing jobs only): the job's most recent 'started' event
#     is older than :age seconds. scan_library is excluded from this net.
# Runs over the SAME connector procrastinate uses (so tests that rebind the
# app connector see it), returns (id, task) for every stalled job.
_DETECT_STALLED_SQL = """
WITH stalled_workers AS (
    SELECT id FROM procrastinate_workers
    WHERE last_heartbeat < NOW() - make_interval(secs => %(hb)s)
),
started AS (
    SELECT job_id, max(at) AS started_at
    FROM procrastinate_events
    WHERE type = 'started'
    GROUP BY job_id
)
SELECT j.id AS id, j.task_name AS task, j.args AS args, j.attempts AS attempts
FROM procrastinate_jobs j
LEFT JOIN stalled_workers sw ON sw.id = j.worker_id
LEFT JOIN started s ON s.job_id = j.id
WHERE j.status = 'doing'
  AND (
    (j.worker_id IS NULL OR sw.id IS NOT NULL)
    OR (
      j.task_name <> ALL(%(age_exempt)s)
      AND s.started_at IS NOT NULL
      AND s.started_at < NOW() - make_interval(secs => %(age)s)
    )
  )
ORDER BY j.id
"""


async def _fail_scanrun_for_reaped_scan(args: dict | None) -> None:
    """Mark the running/stopping ScanRun(s) of a reaped scan_library orphan
    ``failed`` (invariant 7: a crashed scan must never be left ``running``).

    The live storm's worker died via OOM, so ``scan_library``'s own crash handler
    never ran and the ScanRun row was left ``running`` — blocking every future
    scheduler tick for that library. Because ``procrastinate_dsn`` and
    ``database_url`` address the SAME Postgres in every real deployment, we run
    this UPDATE over the procrastinate connector already in hand (no second
    engine), guarded by ``to_regclass`` so it is a safe no-op on a bare/queue-only
    DB (unit tests). Best-effort + idempotent: a second reaper pass (or a reaper
    whose own instance stalled) simply matches zero rows. Never raises into the
    reap loop."""
    if not args:
        return
    library_id = args.get("library_id")
    if not library_id:
        return
    rel_path = args.get("rel_path")  # None => full-library scan
    connector = proc_app.job_manager.connector
    try:
        reg = await connector.execute_query_one_async(
            "SELECT to_regclass('scan_runs') AS r"
        )
        if reg["r"] is None:
            return
        await connector.execute_query_async(
            """
            UPDATE scan_runs
            SET status = 'failed',
                finished_at = NOW(),
                stats = coalesce(stats, '{}'::jsonb)
                        || jsonb_build_object(
                             'error', 'scan worker died; reaped by stalled-job reaper',
                             'reaped', true
                           )
            WHERE library_id = %(library_id)s::uuid
              AND status IN ('running', 'stopping')
              AND (
                    (%(rel_path)s::text IS NULL AND rel_path IS NULL)
                 OR rel_path = %(rel_path)s::text
                  )
            """,
            library_id=str(library_id),
            rel_path=rel_path,
        )
    except Exception:  # noqa: BLE001 - reaper must never fail on the ScanRun net
        pass


# --- FIX-15: orphaned/stuck ScanRun reconciler ------------------------------
# Drive a non-terminal ScanRun terminal when its scan job is already GONE. The
# graceful-stop transition ('stopping' -> 'stopped') only ever runs inside a LIVE
# scan worker's between-batch check, and the stalled-job reaper only transitions
# a running/stopping ScanRun when it detects a *stalled 'doing' scan job* that
# same tick -- so a 'stopping' (or orphaned 'running') ScanRun whose job left
# 'doing' (succeeded / failed / cancelled / aborted / purged from job history)
# has no stalled job to reap and never converges. 'stopping' honors the operator
# intent -> 'stopped'; orphaned 'running' -> 'failed' (invariant 7). One bounded,
# idempotent UPDATE guarded by a started_at grace window (protects a scan whose
# job row is momentarily not yet visible right after enqueue).
_RECONCILE_SCAN_SQL = """
UPDATE scan_runs sr
SET status = CASE WHEN sr.status = 'stopping' THEN 'stopped' ELSE 'failed' END,
    finished_at = NOW(),
    stats = coalesce(sr.stats, '{}'::jsonb) || jsonb_build_object(
        'reconciled', true,
        'reaped', true,
        'reconcile_note', CASE WHEN sr.status = 'stopping'
             THEN 'stop requested but no live scan job remained; finalized as '
                  'stopped by the maintenance reconciler (FIX-15)'
             ELSE 'orphaned running scan with no live job; failed by the '
                  'maintenance reconciler (invariant 7, FIX-15)'
        END
    )
WHERE sr.status IN ('running', 'stopping')
  AND sr.started_at < NOW() - make_interval(secs => %(grace)s)
  AND NOT EXISTS (
        SELECT 1 FROM procrastinate_jobs j
        WHERE j.task_name = %(scan_task)s
          AND j.status IN ('todo', 'doing', 'aborting')
          AND j.args->>'library_id' = sr.library_id::text
          AND (
                (sr.rel_path IS NULL AND (j.args->>'rel_path') IS NULL)
             OR (j.args->>'rel_path') = sr.rel_path
              )
  )
RETURNING sr.id AS id, sr.status AS status
"""


async def reconcile_orphaned_scan_runs_now() -> dict:
    """Finalize non-terminal ScanRuns whose scan job is GONE (FIX-15).

    Any ScanRun in ``running``/``stopping`` older than
    ``scan_run_reconcile_grace_seconds`` with NO scan_library job in
    {todo, doing, aborting} for its (library, scope) is driven terminal in ONE
    bounded, idempotent UPDATE: ``stopping`` -> ``stopped`` (honor the operator's
    stop intent) and ``running`` -> ``failed`` (invariant 7). This is the net for
    the convergence gap the stalled-job reaper cannot cover -- a run whose job has
    left ``doing`` (succeeded / failed / cancelled / aborted / purged) is invisible
    to the reaper's stalled-``doing`` detection, so nothing else ever revisits it,
    and it blocks the scheduler's running-row guard for that library forever.

    Runs over the SAME connector procrastinate uses (``procrastinate_dsn`` and
    ``database_url`` address the same Postgres in every real deployment), guarded
    by ``to_regclass`` so it is a safe no-op on a bare/queue-only DB. Idempotent
    (a second pass finds the rows already terminal). Never raises into the tick.

    Returns ``{reconciled, stopped, failed}``.
    """
    connector = proc_app.job_manager.connector
    try:
        reg = await connector.execute_query_one_async(
            "SELECT to_regclass('scan_runs') AS sr, "
            "to_regclass('procrastinate_jobs') AS pj"
        )
        if reg["sr"] is None or reg["pj"] is None:
            return {"reconciled": 0, "stopped": 0, "failed": 0}
        rows = await connector.execute_query_all_async(
            _RECONCILE_SCAN_SQL,
            grace=get_settings().scan_run_reconcile_grace_seconds,
            scan_task=SCAN_TASK_NAME,
        )
    except Exception:  # noqa: BLE001 - reconciler must never fail the tick
        return {"reconciled": 0, "stopped": 0, "failed": 0}
    stopped = sum(1 for r in rows if r["status"] == "stopped")
    failed = sum(1 for r in rows if r["status"] == "failed")
    return {"reconciled": len(rows), "stopped": stopped, "failed": failed}


async def reap_stalled_jobs_now() -> dict:
    """Requeue or fail jobs orphaned in ``doing`` by a dead/restarted worker.

    Assumes the procrastinate app connector is already OPEN (the maintenance
    periodic task runs inside the worker where it is; the API endpoint wraps this
    in ``proc_app.open_async()``). Steps:

      1. ``prune_stalled_workers`` deletes worker rows with no recent heartbeat;
         the ``worker_id`` FK (``ON DELETE SET NULL``) nulls those workers' jobs,
         so the orphans surface in the heartbeat net below.
      2. Detect stalled ``doing`` jobs (:data:`_DETECT_STALLED_SQL`).
      3. ``scan_library`` orphans are FAILED and their running/stopping ScanRun is
         transitioned to ``failed`` (FIX-8: an OOM-killed scan never ran its own
         crash handler, so the ScanRun was left ``running`` and blocked the
         scheduler forever -- invariant 7). Every other orphan is RETRIED
         (``doing -> todo``, attempts+1) so it runs again on a live worker, UNLESS
         it has already burned ``reap_max_attempts`` (FIX-8: a job whose worker
         keeps dying is FAILED rather than requeued forever -- the live box saw
         attempts=50/51 from unbounded reaper requeues).

    KEY LOCK FINDING (load-bearing): the ``queueing_lock`` unique index is
    partial — ``WHERE status = 'todo'`` ONLY. A ``doing`` job therefore holds NO
    queueing lock; retrying it back to ``todo`` RE-establishes that lock. If a
    fresh periodic tick already enqueued a ``todo`` job holding the same lock (a
    scan/index/alert dedup lock), the retry collides on the partial-unique index
    and raises :class:`UniqueViolation`. We treat that as "a replacement is
    already queued" and FAIL the orphan instead — so the reaper is idempotent and
    never duplicates locked work.

    Returns ``{reaped, retried, failed, pruned_workers}`` (``reaped`` = retried +
    failed). Total on a bare DB (no procrastinate schema): returns all-zeros.
    """
    settings = get_settings()
    hb = settings.job_stall_heartbeat_seconds
    age = settings.job_stall_seconds
    manager = proc_app.job_manager
    connector = manager.connector

    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return {
            "reaped": 0, "retried": 0, "failed": 0, "pruned_workers": 0,
            "scan_runs_reconciled": 0,
        }

    pruned = await manager.prune_stalled_workers(seconds_since_heartbeat=hb)

    rows = await connector.execute_query_all_async(
        _DETECT_STALLED_SQL, hb=hb, age=age, age_exempt=age_net_exempt_tasks()
    )

    retried = 0
    failed = 0
    now = datetime.now(UTC)
    reap_cap = settings.reap_max_attempts
    for row in rows:
        job_id = row["id"]
        if row["task"] == SCAN_TASK_NAME:
            # A stalled scan is FAILED (never silently re-run) AND its ScanRun is
            # transitioned running->failed (invariant 7 + unblocks the scheduler).
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            await _fail_scanrun_for_reaped_scan(row.get("args"))
            failed += 1
            continue
        # FIX-8: bound the requeue budget. A NON-scan job whose worker keeps dying
        # (OOM loop) would otherwise be requeued every reaper tick forever (the
        # live box saw attempts=50/51). Past the cap, FAIL it so it surfaces on the
        # failed-jobs list instead of looping. attempts is 0-based; >= cap means it
        # has already been (re)tried cap times.
        if (row.get("attempts") or 0) >= reap_cap:
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            failed += 1
            continue
        try:
            await manager.retry_job_by_id_async(job_id=job_id, retry_at=now)
            retried += 1
        except UniqueViolation:
            # A replacement todo job already holds this queueing_lock — fail the
            # orphan rather than duplicate the locked work (idempotent ticks).
            await manager.finish_job_by_id_async(
                job_id=job_id, status=Status.FAILED, delete_job=False
            )
            failed += 1

    # FIX-15: after reaping stalled DOING jobs (which flips THEIR running/stopping
    # ScanRuns via _fail_scanrun_for_reaped_scan), sweep any remaining non-terminal
    # ScanRun whose job is already GONE (succeeded/failed/cancelled/purged) -- the
    # case the reaper's stalled-job nets can never see. Bounded, idempotent.
    reconciled = await reconcile_orphaned_scan_runs_now()

    return {
        "reaped": retried + failed,
        "retried": retried,
        "failed": failed,
        "pruned_workers": len(pruned),
        "scan_runs_reconciled": reconciled["reconciled"],
    }


# Every 5 minutes: prune dead workers and requeue/fail their orphaned jobs. The
# queueing_lock collapses overlapping ticks to a single queued run. Bounded,
# read-mostly, idempotent (a second run over the same state acts on nothing new).
@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reap_stalled_jobs",
    queueing_lock="reap-stalled-jobs",
)
async def reap_stalled_jobs(timestamp: int) -> dict:
    """Maintenance tick: reap stalled ``doing`` jobs (FIX-6). Returns the counts
    dict from :func:`reap_stalled_jobs_now` (runs inside the worker, where the
    procrastinate connector is already open)."""
    return await reap_stalled_jobs_now()


# --- FIX-8 (scan-scheduling storm): unfinished-scan dedupe ------------------
# The partial ``queueing_lock`` unique index only covers ``status='todo'`` (see
# the reaper's lock finding), so a scan job that has STARTED (``doing``) — or one
# stalled in ``doing`` because its worker died mid-scan — holds NO lock. The live
# box's storm: a worker OOMed before its ScanRun row committed, so neither the
# lock nor the running-ScanRun guard saw the stalled job, and every due tick
# re-deferred, stacking 5-6 duplicate scan jobs. This belt checks
# ``procrastinate_jobs`` directly for ANY unfinished scan_library job for the
# same (library_id, rel_path) across every non-terminal status.
#
# Procrastinate 3.9 statuses: todo, doing, succeeded, failed, cancelled,
# aborting, aborted. Non-terminal (a job that will or may still run) =
# {todo, doing, aborting}. We match args->>'library_id' and, for a FULL scan,
# require rel_path to be absent/NULL (a scoped job for a subtree must not dedupe
# a full-library scan and vice-versa).
_UNFINISHED_SCAN_SQL = """
SELECT 1
FROM procrastinate_jobs
WHERE task_name = %(task)s
  AND status IN ('todo', 'doing', 'aborting')
  AND args->>'library_id' = %(library_id)s
  AND (
        (%(rel_path)s::text IS NULL AND (args->>'rel_path') IS NULL)
     OR (args->>'rel_path') = %(rel_path)s::text
      )
LIMIT 1
"""


async def scan_job_pending(library_id: str, rel_path: str | None = None) -> bool:
    """True if an unfinished (todo/doing/aborting) scan_library job already
    exists for this (library_id, rel_path). Assumes the proc_app connector is
    reachable (the scheduler runs inside the worker where it is; API defer sites
    open it). Fails SAFE to ``False`` when the procrastinate schema is absent
    (bare DB / unit tests) so it never blocks legitimate scheduling."""
    connector = proc_app.job_manager.connector
    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return False
    rows = await connector.execute_query_all_async(
        _UNFINISHED_SCAN_SQL,
        task="filearr.tasks.scan.scan_library",
        library_id=str(library_id),
        rel_path=rel_path,
    )
    return len(rows) > 0


# --- FIX-15: is a scan RUN genuinely being processed right now? -------------
# ``scan_job_pending`` answers "does ANY unfinished job exist" (todo/doing/
# aborting) and is the FIX-9 scheduling dedupe. Force-clear and the hardened
# stop endpoint need a STRICTER question: is a LIVE worker actually draining this
# (library, scope) right now? That is a ``doing`` scan_library job whose worker
# has heart-beaten within the stall window -- exactly the case where a
# ``stopping`` marker WILL be observed and where refusing a force-clear is
# correct. A ``doing`` job with a stale/pruned worker is NOT active (it is an
# orphan the reaper will fail), and a ``todo``/``aborting`` job is not draining
# this run either. Fails SAFE to ``False`` (not active) when the procrastinate
# schema is absent (bare DB / unit tests) so a manual repair is never blocked by
# an unreachable queue.
_ACTIVE_SCAN_SQL = """
SELECT 1
FROM procrastinate_jobs j
JOIN procrastinate_workers w ON w.id = j.worker_id
WHERE j.task_name = %(task)s
  AND j.status = 'doing'
  AND w.last_heartbeat >= NOW() - make_interval(secs => %(hb)s)
  AND j.args->>'library_id' = %(library_id)s
  AND (
        (%(rel_path)s::text IS NULL AND (j.args->>'rel_path') IS NULL)
     OR (j.args->>'rel_path') = %(rel_path)s::text
      )
LIMIT 1
"""


async def scan_job_active(library_id: str, rel_path: str | None = None) -> bool | None:
    """Tri-state: is a LIVE worker currently draining a scan_library job for this
    (library_id, rel_path)?

      * ``True``  -- a ``doing`` job whose worker heart-beat is fresh (within
        ``job_stall_heartbeat_seconds``) exists: a stop WILL be observed and a
        force-clear must be refused.
      * ``False`` -- procrastinate schema present but NO such live job: the run is
        orphaned (its worker died, or its job already terminated).
      * ``None``  -- the procrastinate schema is absent (bare/queue-less DB, unit
        tests): activeness is UNKNOWN. Callers decide the safe default (the stop
        endpoint keeps the legacy graceful ``stopping`` path; force-clear allows
        the manual repair).

    Assumes the proc_app connector is reachable (callers open it)."""
    connector = proc_app.job_manager.connector
    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return None
    rows = await connector.execute_query_all_async(
        _ACTIVE_SCAN_SQL,
        task="filearr.tasks.scan.scan_library",
        hb=get_settings().job_stall_heartbeat_seconds,
        library_id=str(library_id),
        rel_path=rel_path,
    )
    return len(rows) > 0


async def defer_scan(
    library_id: str,
    *,
    rel_path: str | None = None,
    recursive: bool = True,
    queueing_lock: str | None = None,
    force: bool = False,
    force_empty: bool = False,
) -> int | None:
    """Enqueue a scan for a library, or a *scoped* scan of a subtree (P2-T6) or a
    single file (W9).

    ``recursive`` (W9) is threaded onto the ``scan_library`` job so a non-recursive
    directory scan walks only that dir's direct children. It is only passed as a
    job arg when ``False`` (the ``scan_library`` task defaults ``recursive=True``),
    so a full/hot-folder scan's job args stay byte-for-byte unchanged and old
    queued jobs remain back-compatible.

    ``queueing_lock`` guarantees at most one *queued* scan per lock: Procrastinate
    rejects a second defer with the same lock while one is still waiting in the
    ``todo`` state (the lock frees the moment the job starts running). This is
    what makes a duplicated/late periodic tick — or a tick racing a manual scan —
    idempotent. On a collision we return ``None`` rather than raising, so the
    caller treats it as "already scheduled".

    FIX-8: BEFORE deferring we also check :func:`scan_job_pending` for any
    unfinished scan_library job for the same (library_id, rel_path) — this closes
    the gap the ``todo``-only queueing_lock leaves open once a scan has STARTED or
    STALLED in ``doing`` (the storm the live box hit). ``force=True`` bypasses the
    dedupe for an explicit operator re-trigger. On a dedupe hit we return ``None``
    (same "already scheduled" contract as the lock collision).

    Lock granularity: a full-library scan defaults to ``scan:<library_id>``; a
    scoped scan (``rel_path`` given) defaults to ``scan:<library_id>:<rel_path>``
    so a hot-folder scan and the library's full scan queue independently, and two
    hot folders never collide with each other.
    """
    if queueing_lock is not None:
        lock = queueing_lock
    elif rel_path is not None:
        lock = f"scan:{library_id}:{rel_path}"
    else:
        lock = f"scan:{library_id}"
    kwargs: dict = {"library_id": library_id}
    if rel_path is not None:
        kwargs["rel_path"] = rel_path
    # Only carry the flags when non-default so existing scan job args are
    # unchanged and pre-W9 queued jobs keep working (task defaults apply).
    if not recursive:
        kwargs["recursive"] = False
    # Roadmap §19: the operator's explicit consent to an N->0 walk rides the job.
    if force_empty:
        kwargs["force_empty"] = True
    async with open_pool_if_needed():
        if not force and await scan_job_pending(library_id, rel_path):
            return None  # an unfinished scan for this scope already exists
        try:
            job = await proc_app.configure_task(
                "filearr.tasks.scan.scan_library",
                queue="scan",
                queueing_lock=lock,
                priority=get_settings().scan_priority,  # UI-T14 front-stage lane
            ).defer_async(**kwargs)
        except AlreadyEnqueued:
            return None
    return job


async def defer_index_sync(item_ids: list[str]) -> None:
    async with open_pool_if_needed():
        await proc_app.configure_task(
            "filearr.tasks.index_sync.sync_items",
            queue="index",
            priority=get_settings().index_priority,  # UI-T14 default lane
        ).defer_async(item_ids=item_ids)


async def defer_agent_associate(library_ids: list[str]) -> None:
    """Debounced sidecar-association defer for agent-backed libraries (T3 parity
    for replicated items — agentsync never touches ``sidecar_of``). Called by the
    replication/reconcile endpoints AFTER their commit. ``schedule_in`` plus a
    per-library queueing lock collapse a scan's stream of batches into at most
    one queued pass per debounce window."""
    if not library_ids:
        return
    delay = get_settings().agent_associate_debounce_seconds
    async with open_pool_if_needed():
        for lid in library_ids:
            try:
                await proc_app.configure_task(
                    "filearr.worker.associate_agent_library",
                    queue="index",
                    queueing_lock=f"associate-agent:{lid}",
                    schedule_in={"seconds": delay},
                ).defer_async(library_id=lid)
            except AlreadyEnqueued:
                pass


@proc_app.task(queue="index", name="filearr.worker.associate_agent_library")
async def associate_agent_library(library_id: str) -> dict:
    """Link-only sidecar association for ONE agent-backed library, then a
    targeted Meili re-projection of every item whose link changed (the doc
    carries ``is_sidecar``/``sidecar_of``). NFO parsing is skipped — central
    cannot open files that live on the agent's filesystem."""
    import uuid as uuid_mod

    from filearr.tasks.associate import associate_sidecars_light

    async with SessionLocal() as session:
        stats = await associate_sidecars_light(session, uuid_mod.UUID(library_id))
        await session.commit()
    changed = stats.pop("changed_ids", [])
    for start in range(0, len(changed), 1000):
        await defer_index_sync(changed[start : start + 1000])
    return {**stats, "reindexed": len(changed)}


# Scheduled by maintenance_tick (registry default "20 5 * * *"). FIX-8: no retry.
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.associate_agent_sidecars",
    queueing_lock="associate-agent-sidecars",
)
async def associate_agent_sidecars(timestamp: int) -> dict:
    """Sweep: defer the per-library association pass for EVERY agent-backed
    library. Replication triggers the debounced pass on new batches; this sweep
    is the safety net for data that predates the feature or arrives while the
    worker is down (live 2026-08: 446k pre-existing .xmp sidecars)."""
    from sqlalchemy import select

    from filearr.models import Library

    async with SessionLocal() as session:
        lib_ids = [
            str(i)
            for i in (
                await session.execute(
                    select(Library.id).where(Library.source_agent_id.is_not(None))
                )
            ).scalars()
        ]
    deferred = 0
    async with open_pool_if_needed():
        for lid in lib_ids:
            try:
                await proc_app.configure_task(
                    "filearr.worker.associate_agent_library",
                    queue="index",
                    queueing_lock=f"associate-agent:{lid}",
                ).defer_async(library_id=lid)
                deferred += 1
            except AlreadyEnqueued:
                continue
    return {"libraries": len(lib_ids), "deferred": deferred}


# On-demand from the maintenance registry ("Content-sniff extensionless files").
# FIX-8: no retry — re-run from the Jobs page; the pass is idempotent/resumable.
@proc_app.task(queue="extract", name="filearr.worker.content_sniff")
async def content_sniff() -> dict:
    """One bounded libmagic pass over extensionless (other, other) items
    (roadmap §4). No-op unless FILEARR_CONTENT_SNIFF_ENABLED; re-projects and
    re-extracts every reclassified item; run again while ``remaining`` > 0."""
    settings = get_settings()
    if not settings.content_sniff_enabled:
        return {"skipped": "content sniffing disabled (FILEARR_CONTENT_SNIFF_ENABLED)"}
    from filearr.tasks.sniff import sniff_extensionless

    async with SessionLocal() as session:
        stats = await sniff_extensionless(session)
    changed = stats.pop("changed_ids", [])
    for start in range(0, len(changed), 1000):
        chunk = changed[start : start + 1000]
        await defer_index_sync(chunk)
        # A new category may route to a real extractor (video/image/document…) —
        # extraction fills the type-specific metadata the item never had.
        await defer_extract(chunk)
    return {**stats, "reprojected": len(changed)}


async def defer_thumb_item(item_id: str, tier: int) -> None:
    """Enqueue a ``thumb_item`` for one item + tier on the low-priority thumbs
    queue (P12 slice 2). Used by the serve endpoint on a VIDEO thumbnail miss:
    ffmpeg's latency variance must never run inline in a request handler, so a
    miss queues the frame-grab and 404s (the client retries).

    Reuses the connector when it is already open (worker context / tests) and
    only opens+closes its own connection when the caller's process has none (the
    API process, where proc_app is not held open) -- so it never tears down a
    shared pool that an enclosing ``open_async`` still needs."""
    settings = get_settings()

    def _deferrer():
        return proc_app.configure_task(
            "filearr.tasks.thumbs.thumb_item",
            queue=settings.queue_thumbnail,
            priority=settings.thumbs_priority,
        )

    try:
        await _deferrer().defer_async(item_id=item_id, tier=tier)
    except procrastinate.exceptions.AppNotOpen:
        async with open_pool_if_needed():
            await _deferrer().defer_async(item_id=item_id, tier=tier)


async def defer_rebuild_index() -> int | None:
    """Defer a full shadow-swap ``rebuild_index`` on the ``index`` queue and return
    the Procrastinate job id (P9-T5). Used by ``POST /api/v1/system/rebuild-index``
    so an operator can trigger a rebuild/settings-migration rollout on demand
    instead of deferring the task by hand."""
    async with open_pool_if_needed():
        job = await proc_app.configure_task(
            "filearr.tasks.index_sync.rebuild_index",
            queue="index",
            priority=get_settings().index_priority,  # UI-T14 default lane
        ).defer_async()
    return job


async def defer_extract(item_ids: list[str]) -> None:
    """Batch-defer extract jobs from OUTSIDE the worker (e.g. the retry-extracts
    API action).

    The scan task's ``_defer_extract_batch`` helper assumes the procrastinate app
    is already open (it runs inside a worker); the API is not, so this opens the
    connection around the same helper. No-op on an empty list."""
    if not item_ids:
        return
    from filearr.tasks.scan import _defer_extract_batch

    async with open_pool_if_needed():
        await _defer_extract_batch(item_ids)


async def defer_embed(item_ids: list[str]) -> None:
    """Batch-defer ``embed_item`` jobs from OUTSIDE the worker (P3-T8), on the
    ``embed`` queue at the lowest priority. No-op on an empty list or when
    semantic search is disabled (the task itself also no-ops defensively)."""
    if not item_ids:
        return
    settings = get_settings()
    if not settings.semantic_enabled:
        return
    async with open_pool_if_needed():
        deferrer = proc_app.configure_task(
            "filearr.tasks.embed.embed_item",
            queue=settings.queue_embed,
            priority=settings.embed_priority,
        )
        for iid in item_ids:
            await deferrer.defer_async(item_id=iid)


async def defer_embed_missing() -> int | None:
    """Defer the ``embed_missing`` backfill on the ``embed`` queue and return its
    Procrastinate job id (P3-T8). Backs ``POST /api/v1/system/embed-backfill``."""
    async with open_pool_if_needed():
        job = await proc_app.configure_task(
            "filearr.tasks.embed.embed_missing",
            queue=get_settings().queue_embed,
            priority=get_settings().embed_priority,
        ).defer_async()
    return job


# Scheduled by maintenance_tick (registry default "0 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
# FIX-8: periodic maintenance tasks carry NO retry -- a transient failure is
# simply re-run on the next tick, and self-retry here was one source of the
# runaway attempts (50/51) the reaper then compounded.
@proc_app.task(queue="maintenance", name="filearr.worker.purge_recycle_bin")
async def purge_recycle_bin(timestamp: int) -> int:
    """Hard-delete trashed items past the retention window (recycle-bin purge).

    P5-T5 purge-safety watermark (§4.5): a trashed item OWNED BY A LIVE AGENT is
    held back until that agent's last full-manifest reconciliation has observed
    the deletion — i.e. ``agents.last_reconcile_at >= items.deleted_at`` (the
    ``deleted_at`` timestamp IS the trashed-transition instant, the same key the
    retention cutoff uses). A never-reconciled live agent (``last_reconcile_at``
    NULL) blocks its trashed items indefinitely; a revoked/deleted agent (or a
    non-agent local item, ``source_agent_id`` NULL) never blocks purge (R2)."""
    from sqlalchemy import and_, delete, or_, select

    from filearr.db import SessionLocal
    from filearr.models import Agent, Item, ItemStatus
    from filearr.search import delete_docs

    cutoff = datetime.now(UTC) - timedelta(days=get_settings().recycle_retention_days)
    async with SessionLocal() as session:
        rows = await session.execute(
            select(Item.id)
            .outerjoin(Agent, Agent.id == Item.source_agent_id)
            .where(
                Item.status == ItemStatus.trashed,
                Item.deleted_at < cutoff,
                or_(
                    Item.source_agent_id.is_(None),      # local item — no agent gate
                    Agent.id.is_(None),                  # dangling/unknown agent ref
                    Agent.revoked_at.isnot(None),        # revoked agent never blocks
                    and_(                                # reconciled past the deletion
                        Agent.last_reconcile_at.isnot(None),
                        Agent.last_reconcile_at >= Item.deleted_at,
                    ),
                ),
            )
        )
        ids = [str(i) for (i,) in rows]
        if ids:
            await session.execute(delete(Item).where(Item.id.in_(ids)))
            await session.commit()
            await delete_docs(ids)
    return len(ids)


# --- P4-T9: ItemVersion audit-retention purge ------------------------------
# Bounds unbounded per-rescan audit growth: extractor-sourced version rows
# (source != 'user') older than FILEARR_ITEM_VERSION_RETENTION_DAYS are hard-
# deleted. source='user' (manual API/UI edit) rows are EXEMPT and never touched
# regardless of age. Runs at 04:15, between the 04:00 recycle-bin purge and the
# 04:30 nightly reconcile, so the three maintenance jobs never overlap. Mirrors
# the recycle-bin purge shape (periodic maintenance task). Never touches
# Meilisearch (audit rows are not projected). FIX-8: no retry -- a transient DB
# fault is simply retried on the next daily tick.
# Scheduled by maintenance_tick (registry default "15 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_item_versions"  # FIX-8: no retry
)
async def purge_item_versions(timestamp: int) -> int:
    """Hard-delete non-'user' ItemVersion rows past the retention window (P4-T9).
    source='user' rows are exempt. Returns the number of rows deleted."""
    from sqlalchemy import delete

    from filearr.db import SessionLocal
    from filearr.models import ItemVersion

    cutoff = datetime.now(UTC) - timedelta(
        days=get_settings().audit_retention_days
    )
    async with SessionLocal() as session:
        result = await session.execute(
            delete(ItemVersion).where(
                ItemVersion.source != "user", ItemVersion.changed_at < cutoff
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- P8-T14: alert_events retention purge -----------------------------------
# Keeps alert_events bounded (mirrors the recycle-bin / audit purges, invariant
# 4). Deletes only TERMINAL rows older than FILEARR_ALERT_EVENTS_RETENTION_DAYS:
# delivered=true OR retries-exhausted (delivery_attempts >= max). A PENDING alert
# (still deliverable) or a ceiling-HELD row (attempts untouched) is NEVER purged
# regardless of age -- we never silently drop an undelivered alert. Runs at 04:45,
# clear of the 04:00/04:15/04:30 maintenance jobs. FIX-8: no retry -- a transient
# DB fault is retried on the next daily tick; alert_events are never projected.
# Scheduled by maintenance_tick (registry default "45 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_alert_events"  # FIX-8: no retry
)
async def purge_alert_events(timestamp: int) -> int:
    """Hard-delete terminal alert_events past the retention window (P8-T14).
    Delivered OR retries-exhausted only; pending/held rows are exempt. Returns the
    number of rows deleted."""
    from sqlalchemy import delete, or_

    from filearr.db import SessionLocal
    from filearr.models import AlertEvent

    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(days=settings.alert_events_retention_days)
    max_attempts = settings.alert_max_delivery_attempts
    async with SessionLocal() as session:
        result = await session.execute(
            delete(AlertEvent).where(
                AlertEvent.occurred_at < cutoff,
                or_(
                    AlertEvent.delivered.is_(True),
                    AlertEvent.delivery_attempts >= max_attempts,
                ),
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- P6-T9: security_events retention purge ---------------------------------
# Keeps the audit log bounded (invariant-4 discipline). Noisy ``login_failure``
# rows are purged after the shorter FILEARR_SECURITY_AUDIT_FAILURE_RETENTION_DAYS
# window; every other (higher-value) event is kept for
# FILEARR_SECURITY_AUDIT_RETENTION_DAYS. Runs at 04:20, clear of the other
# maintenance jobs. FIX-8: no retry — a transient DB fault is retried next daily
# tick; security_events are append-only and never projected.
# Scheduled by maintenance_tick (registry default "20 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance", name="filearr.worker.purge_security_events"  # no retry
)
async def purge_security_events(timestamp: int) -> int:
    """Hard-delete security_events past their per-class retention window (P6-T9).
    Returns the number of rows deleted."""
    from sqlalchemy import and_, delete, or_

    from filearr.db import SessionLocal
    from filearr.models import SecurityEvent

    settings = get_settings()
    now = datetime.now(UTC)
    failure_cutoff = now - timedelta(days=settings.security_audit_failure_retention_days)
    other_cutoff = now - timedelta(days=settings.security_audit_retention_days)
    async with SessionLocal() as session:
        result = await session.execute(
            delete(SecurityEvent).where(
                or_(
                    and_(
                        SecurityEvent.event_type == "login_failure",
                        SecurityEvent.ts < failure_cutoff,
                    ),
                    and_(
                        SecurityEvent.event_type != "login_failure",
                        SecurityEvent.ts < other_cutoff,
                    ),
                )
            )
        )
        await session.commit()
    return result.rowcount or 0


# --- FIX-8/FIX-17: procrastinate job-history retention ----------------------
# The failed-jobs list on the Admin + Jobs pages (and the succeeded-job backlog
# that powers the queue-card "done" counters + the extract-rate ETA) grew
# UNBOUNDED — procrastinate never purges terminal rows on its own. This
# maintenance task hard-deletes terminal rows via the vetted
# JobManager.delete_old_jobs query (age is measured from the latest
# procrastinate_events row — there is no finished_at column on
# procrastinate_jobs). todo/doing jobs are NEVER touched (not final states).
#
# FIX-17 (live incident 2026-07-26): daily-at-04:50 with one 14-day window was
# the wrong shape for succeeded rows. A full rescan of the 1M-item library
# minted ~3.4M succeeded extract/thumbs/index rows in ~48h; at that mass
# procrastinate_fetch_job ran ~56s per call, the concurrency-4 worker starved
# at ~2 jobs/min, extraction "stalled" — and this purge sat wedged in the very
# maintenance backlog it exists to prevent. Now: HOURLY at :50, and succeeded
# rows age out on their own short window
# (FILEARR_JOB_HISTORY_SUCCEEDED_RETENTION_HOURS, default 48) while failed/
# cancelled/aborted keep the long forensic window
# (FILEARR_JOB_HISTORY_RETENTION_DAYS). Deleted counts are logged per status.
#
# Count (grouped by final status) of the rows delete_old_jobs would remove, for
# the log line. Mirrors procrastinate's delete_old_jobs predicate EXACTLY (latest
# event `at` older than nb_hours, final statuses only) so the logged numbers
# match what is deleted.
_PURGE_COUNT_SQL = """
SELECT job.status::text AS status, count(*) AS n
FROM (
    SELECT DISTINCT ON (j.id) j.id, j.status, e.at AS latest_at
    FROM procrastinate_jobs j
    JOIN procrastinate_events e ON j.id = e.job_id
    ORDER BY j.id, e.at DESC
) job
WHERE job.status = ANY(
        ARRAY['succeeded','failed','cancelled','aborted']::procrastinate_job_status[]
      )
  AND job.latest_at < NOW() - make_interval(hours => %(nb_hours)s)
GROUP BY job.status
"""


async def purge_job_history_now() -> dict:
    """Hard-delete terminal procrastinate rows older than the retention window.

    Assumes the procrastinate app connector is already OPEN (the maintenance
    periodic runs inside the worker, where it is — same contract as
    :func:`reap_stalled_jobs_now`). Uses the vetted
    ``JobManager.delete_old_jobs`` with every terminal status enabled; a
    count-before pass (the same predicate) feeds the log line. NEVER touches
    todo/doing jobs (they are not final states, so the status filter excludes
    them). Total on a bare DB (no procrastinate schema): returns all-zeros.

    Returns ``{deleted, by_status}`` where ``by_status`` maps each final status
    to how many rows aged out this run.
    """
    import logging

    manager = proc_app.job_manager
    connector = manager.connector

    schema = await connector.execute_query_one_async(
        "SELECT to_regclass('procrastinate_jobs') AS r"
    )
    if schema["r"] is None:
        return {"deleted": 0, "by_status": {}}

    nb_hours = get_settings().job_history_retention_days * 24
    succ_hours = get_settings().job_history_succeeded_retention_hours
    # Two windows, two counts (the SQL groups by status; take succeeded from
    # the short-window pass, everything else from the long one).
    rows_long = await connector.execute_query_all_async(
        _PURGE_COUNT_SQL, nb_hours=nb_hours
    )
    by_status = {
        row["status"]: int(row["n"])
        for row in rows_long
        if row["status"] != "succeeded"
    }
    rows_succ = await connector.execute_query_all_async(
        _PURGE_COUNT_SQL, nb_hours=succ_hours
    )
    for row in rows_succ:
        if row["status"] == "succeeded":
            by_status["succeeded"] = int(row["n"])
    deleted = sum(by_status.values())

    # FIX-17: succeeded first on its SHORT window (delete_old_jobs removes
    # ONLY succeeded rows unless include_* flags widen it)...
    await manager.delete_old_jobs(succ_hours)
    # ...then the long forensic window for failed/cancelled/aborted.
    await manager.delete_old_jobs(
        nb_hours,
        include_failed=True,
        include_cancelled=True,
        include_aborted=True,
    )

    if deleted:
        logging.getLogger("filearr.worker").info(
            "purge_job_history: deleted %d terminal job rows older than %dd (%s)",
            deleted,
            get_settings().job_history_retention_days,
            ", ".join(f"{k}={v}" for k, v in sorted(by_status.items())),
        )

    # Roadmap §18: the persisted failure text (job_errors) ages out on the SAME
    # window, so an error row never outlives — by more than one purge cycle —
    # the procrastinate row it annotates. Own session/table (ours, not
    # Procrastinate's); tolerant of the table not existing yet (pre-migration).
    try:
        from sqlalchemy import delete as sa_delete

        from filearr.models import JobError

        cutoff = datetime.now(UTC) - timedelta(hours=nb_hours)
        async with SessionLocal() as session:
            result = await session.execute(
                sa_delete(JobError).where(JobError.created_at < cutoff)
            )
            await session.commit()
        if result.rowcount:
            logging.getLogger("filearr.worker").info(
                "purge_job_history: deleted %d job_errors rows", result.rowcount
            )
    except Exception:  # noqa: BLE001 — never fail the purge over the annex table
        logging.getLogger("filearr.worker").exception(
            "purge_job_history: job_errors purge failed"
        )
    return {"deleted": deleted, "by_status": by_status}


# Scheduled by maintenance_tick (registry default "50 * * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_job_history",
    queueing_lock="purge-job-history",  # FIX-8: no retry (periodic re-runs)
)
async def purge_job_history(timestamp: int) -> int:
    """Maintenance tick: hard-delete terminal procrastinate rows past their
    retention windows (FIX-8/FIX-17: succeeded = short hours window, other
    terminal states = long days window). Returns the number of rows deleted."""
    return (await purge_job_history_now())["deleted"]


@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_app_logs",
    queueing_lock="purge-app-logs",  # FIX-8: no retry (periodic re-runs)
)
async def purge_app_logs(timestamp: int) -> dict:
    """Bound the console log stream (``app_logs``): delete rows past the
    retention window, then — log-storm backstop — trim to the newest
    ``log_max_rows`` regardless of age. Returns counts per phase."""
    return await purge_app_logs_now()


async def purge_app_logs_now() -> dict:
    """The purge itself (task-independent, same convention as
    ``purge_job_history_now``)."""
    from sqlalchemy import text as sql_text

    settings = get_settings()
    async with SessionLocal() as session:
        aged = await session.execute(
            sql_text(
                "DELETE FROM app_logs "
                "WHERE ts < now() - make_interval(days => :days)"
            ),
            {"days": settings.log_retention_days},
        )
        # (cap+1)th-newest id; NULL (table smaller than cap) deletes nothing.
        capped = await session.execute(
            sql_text(
                "DELETE FROM app_logs WHERE id <= ("
                "  SELECT id FROM app_logs ORDER BY id DESC"
                "  OFFSET :cap LIMIT 1)"
            ),
            {"cap": settings.log_max_rows},
        )
        await session.commit()
    return {"aged": aged.rowcount, "capped": capped.rowcount}


# --- QH-T4: small-file re-hash sweep ----------------------------------------
# The QH-T1..T3 hashing fix (quick_hash 64-128 KiB partial-read repair, small-file
# unconditional content_hash, xxh3-64 -> xxh3-128) changed the stored hashes for
# every file <= 128 KiB, but policy_version fingerprints CONFIG, not hashing
# behavior, so the ~3,930-group (brief §3a) backlog of items hashed under the old
# algorithm would never re-hash on their own (the scan self-heal only re-queues
# quick_hash IS NULL rows — it can't tell "never hashed" from "hashed under a
# since-fixed algorithm"). This nightly, rate-limited sweep re-enqueues a bounded
# batch of active, size <= 128 KiB items still on the OLD provenance scheme through
# the NORMAL extract path (which recomputes both hashes correctly and re-stamps
# policy_version = cfg2). Once re-extracted, an item's cfg2 fingerprint excludes it
# next tick -> idempotent convergence, no blocking data migration.
#
# ARCHITECT RULING (binding): agent-owned items (library.source_agent_id set) are
# EXCLUDED — central cannot open an agent's files, so it cannot re-hash them; they
# correct via the agent's own rescan (a hash change re-emits modified events for
# the band) + replication. The sweep also NEVER touches items outside the affected
# size <= 128 KiB band: a >128 KiB file's sampled quick_hash was never wrong, and
# its content_hash migrates lazily on natural rescan.
_SMALL_FILE_CEILING = 2 * 65536  # 128 KiB — mirrors extract.QUICK_CHUNK*2
# Bounded per-tick enqueue so a large backlog never spikes the extract queue on
# deploy. A module constant (not a config knob — config.py is out of scope here);
# ~4,000 backlog / 1,000 per nightly tick converges in a few nights.
REHASH_SWEEP_BATCH = 1000


async def rehash_small_files_now() -> dict:
    """Re-enqueue a bounded batch of active <=128 KiB items still on an old
    provenance scheme through the normal extract path (QH-T4). Excludes agent-owned
    items (architect ruling) and never touches files outside the affected band.
    Idempotent: a re-extracted item advances to cfg2 and drops out next run.
    Returns ``{requeued}``."""
    from sqlalchemy import select

    from filearr.db import SessionLocal
    from filearr.models import Item, ItemStatus, Library
    from filearr.provenance import _SCHEME
    from filearr.tasks.scan import _defer_extract_batch

    async with SessionLocal() as session:
        rows = await session.execute(
            select(Item.id)
            .join(Library, Library.id == Item.library_id)
            .where(
                Item.status == ItemStatus.active,
                Item.size <= _SMALL_FILE_CEILING,
                # Extracted under an OLD scheme (non-null, not the current prefix).
                # A NULL policy_version is an unextracted row handled by the normal
                # extract/self-heal path — not this migration's concern.
                Item.policy_version.isnot(None),
                ~Item.policy_version.like(f"{_SCHEME}:%"),
                Library.source_agent_id.is_(None),  # ruling: never sweep agent items
            )
            .order_by(Item.id)
            .limit(REHASH_SWEEP_BATCH)
        )
        ids = [str(i) for (i,) in rows]
    if ids:
        # _defer_extract_batch assumes the proc app is open; mirror defer_extract.
        async with open_pool_if_needed():
            await _defer_extract_batch(ids)
    return {"requeued": len(ids)}


# Scheduled by maintenance_tick (registry default "55 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.rehash_small_files",
    queueing_lock="rehash-small-files",  # FIX-8: no retry (nightly re-runs)
)
async def rehash_small_files(timestamp: int) -> int:
    """Maintenance tick: re-enqueue a bounded batch of active <=128 KiB items on an
    old provenance scheme through the normal extract path (QH-T4). Runs at 04:55,
    clear of the other 04:xx maintenance jobs. Returns the number re-queued."""
    return (await rehash_small_files_now())["requeued"]


async def backfill_content_hashes_now(*, max_bytes: int | None = None) -> dict:
    """Roadmap §16 (2026-08-19): stream whole-file ``content_hash`` (+ ``mid_hash``)
    for active items in CENTRAL libraries whose effective hash policy is
    ``quick_only`` (network roots under ``auto``, or declared), so the integrity
    benefit of content hashes -- exact duplicate detection, move confirmation --
    becomes available without paying the cost on the hot scan path.

    Bounded per run by a byte budget (``FILEARR_HASH_BACKFILL_MAX_BYTES``) and a
    throughput cap (``FILEARR_HASH_BACKFILL_RATE_MBPS``, sleep-throttled), commits
    every 100 rows, honours the per-library/global size ceiling and the
    small-file band (<=128 KiB is always hashed in full already), and skips
    agent-owned libraries (central cannot open their files). Opt-in: no default
    schedule; run from the Jobs page or give it a cron there. Returns
    ``{hashed, bytes, remaining, budget_bytes}``."""
    import asyncio
    import time

    from sqlalchemy import func, select
    from sqlalchemy.orm import load_only

    from filearr.db import SessionLocal
    from filearr.hashpolicy import resolve_hash_policy
    from filearr.models import Item, ItemStatus, Library
    from filearr.tasks.extract import QUICK_CHUNK, full_hash, mid_hash

    settings = get_settings()
    budget = max_bytes if max_bytes is not None else int(settings.hash_backfill_max_bytes)
    rate = float(settings.hash_backfill_rate_mbps) * 1_000_000.0  # bytes/sec; <=0 = unthrottled
    hashed = spent = remaining = 0
    started = time.monotonic()
    async with SessionLocal() as session:
        libs = (
            await session.execute(select(Library).where(Library.source_agent_id.is_(None)))
        ).scalars().all()
        for lib in libs:
            resolved = resolve_hash_policy(
                declared=lib.hash_policy or "auto",
                root_path=lib.root_path or "",
                hash_full_max_bytes=lib.hash_full_max_bytes,
                global_max_bytes=settings.scan_hash_full_max_bytes,
            )
            if resolved.compute_content:
                continue  # full policy: the extract path already hashes these
            cond = (
                Item.library_id == lib.id,
                Item.status == ItemStatus.active,
                Item.content_hash.is_(None),
                Item.size > QUICK_CHUNK * 2,
                Item.size <= resolved.full_max_bytes,
            )
            if spent >= budget:
                n = (
                    await session.execute(select(func.count()).select_from(Item).where(*cond))
                ).scalar_one()
                remaining += int(n or 0)
                continue
            # Collect IDs first (no commit): committing inside a server-side
            # cursor stream invalidates it after the first yield_per buffer, so
            # the whole-library backfill would die at row ~201. IDs are cheap and
            # bounded; process them in chunks, each chunk its own transaction.
            todo_ids = [
                r
                for r in (
                    await session.execute(
                        select(Item.id).where(*cond).order_by(Item.id)
                    )
                ).scalars()
            ]
            done = False
            for start in range(0, len(todo_ids), 100):
                if done:
                    remaining += len(todo_ids) - start
                    break
                chunk = todo_ids[start : start + 100]
                items = (
                    await session.execute(
                        select(Item)
                        .options(load_only(Item.id, Item.path, Item.size,
                                           Item.content_hash, Item.mid_hash))
                        .where(Item.id.in_(chunk))
                    )
                ).scalars().all()
                for item in items:
                    if spent >= budget:
                        remaining += 1
                        done = True
                        continue
                    if item.size is None or item.path is None:
                        continue
                    try:
                        digest = await asyncio.to_thread(full_hash, item.path, item.size)
                        mid = await asyncio.to_thread(mid_hash, item.path, item.size)
                    except OSError:
                        continue  # gone / unreadable: the next scan tombstones it
                    item.content_hash = digest
                    if mid is not None:
                        item.mid_hash = mid
                    hashed += 1
                    spent += int(item.size)
                    if rate > 0:
                        # sleep-throttle to the configured average throughput
                        ahead = spent / rate - (time.monotonic() - started)
                        if ahead > 0:
                            await asyncio.sleep(min(ahead, 5.0))
                await session.commit()
    return {"hashed": hashed, "bytes": spent, "remaining": remaining, "budget_bytes": budget}


@proc_app.task(
    queue="maintenance",
    name="filearr.worker.backfill_content_hashes",
    queueing_lock="backfill-content-hashes",
)
async def backfill_content_hashes(timestamp: int) -> dict:
    """Opt-in maintenance task (no default cron): see ``backfill_content_hashes_now``.
    Skipped under maintenance mode (it is exactly the sustained disk/network I/O
    an operator enters that mode to stop)."""
    from filearr import maintmode

    if await maintmode.is_active_standalone():
        return {"status": "skipped", "reason": "maintenance_mode", "hashed": 0}
    return await backfill_content_hashes_now()


# --- LDAP-T1: AD/LDAP directory sync + SID reconciliation --------------------
# Enumerate the directory (users + groups), upsert directory_objects, tombstone
# objects gone from AD, then RECONCILE: every SID an agent pushed into a
# permission snapshot that matches a directory object gets a principal_aliases
# row (source='ldap') mapping the raw SID -> DOMAIN\name (Full Name), so the
# existing permission reports resolve it. Enumeration is blocking ldap3 I/O run
# off-loop; the reconcile is pure SQL. Central-only.


def _sids_in_snapshots_sql() -> str:
    """Distinct principal ids shaped like a Windows SID across every permission
    snapshot's ACEs + owner + group. A GIN-friendly-enough scan; the snapshot
    table is bounded (N per path)."""
    return (
        "SELECT DISTINCT v FROM ("
        "  SELECT ace->'principal'->>'id' AS v "
        "    FROM permission_snapshots, jsonb_array_elements(aces) AS ace "
        "  UNION SELECT owner->>'id' FROM permission_snapshots "
        '  UNION SELECT "group"->>\'id\' FROM permission_snapshots'
        ") t WHERE v LIKE 'S-%'"
    )


async def sync_directory_now(*, connector=None) -> dict:
    """Run one directory enumeration + reconcile. ``connector`` overrides the
    LDAP connection factory (tests inject an offline MOCK_SYNC one). Returns the
    :class:`ldap_directory.ReconcileResult` as a dict. A disabled/misconfigured
    directory returns a ``skipped`` status rather than raising."""
    import asyncio

    from sqlalchemy import select
    from sqlalchemy import text as sql_text
    from sqlalchemy.dialects.postgresql import insert as pg_insert

    from filearr import ldap_directory
    from filearr.db import SessionLocal, scalars_where_in
    from filearr.ldap_auth import LDAPError, connect
    from filearr.models import DirectoryObject, PrincipalAlias

    settings = get_settings()
    if not settings.ldap_directory_sync_enabled:
        return {"status": "skipped", "reason": "directory_sync_disabled"}
    try:
        endpoints = ldap_directory.endpoints_from_settings(settings)
    except LDAPError as exc:
        return {"status": "skipped", "reason": f"ldap_not_configured: {exc.reason}"}
    conn_factory = connector or connect

    res = ldap_directory.ReconcileResult()
    now = datetime.now(UTC)
    synced_labels: list[str] = []
    async with SessionLocal() as session:
        for ep in endpoints:
            # Cross-forest: one unreachable endpoint records an error and is
            # skipped (its label is NOT added to synced_labels, so its objects
            # are never tombstoned this run) — the other forests still sync.
            try:
                entries = await asyncio.to_thread(
                    ldap_directory.enumerate_directory,
                    ep.ldap, ep.dcfg, connector=conn_factory,
                )
            except LDAPError as exc:
                res.errors.append(f"{ep.label}: {exc.reason}: {exc.detail}")
                continue
            # memberOf DNs reference same-forest groups → resolve within this
            # endpoint's own entries.
            dn_to_sid = {
                (e.distinguished_name or "").lower(): e.object_sid
                for e in entries
                if e.distinguished_name and e.object_sid
            }
            for e in entries:
                res.objects += 1
                if e.kind == "user":
                    res.users += 1
                elif e.kind == "group":
                    res.groups += 1
                member_sids = sorted(
                    {
                        dn_to_sid[dn.lower()]
                        for dn in e.member_of_dns
                        if dn.lower() in dn_to_sid and dn_to_sid[dn.lower()]
                    }
                )
                if member_sids:
                    res.memberships_expanded += 1
                values = dict(
                    object_guid=e.object_guid,
                    object_sid=e.object_sid,
                    sam_account_name=e.sam_account_name,
                    display_name=e.display_name,
                    user_principal_name=e.user_principal_name,
                    distinguished_name=e.distinguished_name,
                    kind=e.kind,
                    domain=e.domain,
                    member_of_sids=member_sids,
                    disabled=e.disabled,
                    source_directory=ep.label,
                    last_synced_at=now,
                    deleted_at=None,
                )
                stmt = pg_insert(DirectoryObject).values(**values).on_conflict_do_update(
                    index_elements=[DirectoryObject.object_guid],
                    set_={k: v for k, v in values.items() if k != "object_guid"},
                )
                await session.execute(stmt)
            synced_labels.append(ep.label)
        await session.commit()

        if not synced_labels:
            # Every endpoint failed — surface it as a failure, tombstone nothing.
            return {"status": "failed", "reason": "; ".join(res.errors), **res.as_dict()}

        # Tombstone objects from the endpoints ACTUALLY synced this run that were
        # not re-seen (removed from that directory). Scoped by source_directory
        # so an unreachable forest never tombstones another's rows. Legacy rows
        # (NULL source_directory, from a pre-multiforest sync) are only swept when
        # a SINGLE endpoint is configured — with several forests we cannot know
        # which owned a NULL row, so we leave it until its next successful sync
        # re-labels it (then a genuine removal tombstones correctly).
        null_clause = " OR source_directory IS NULL" if len(endpoints) == 1 else ""
        tomb = await session.execute(
            sql_text(
                "UPDATE directory_objects SET deleted_at = :now "
                f"WHERE (source_directory = ANY(:labels){null_clause}) "
                "AND last_synced_at < :now AND deleted_at IS NULL"
            ),
            {"now": now, "labels": synced_labels},
        )
        res.tombstoned = tomb.rowcount or 0
        await session.commit()

        # Reconcile: resolve the SIDs agents actually pushed.
        sids = [
            r[0] for r in (await session.execute(sql_text(_sids_in_snapshots_sql()))).all()
        ]
        # Load the directory rows for exactly those SIDs in one pass.
        by_sid: dict[str, DirectoryObject] = {}
        if sids:
            rows = await scalars_where_in(
                session, select(DirectoryObject), DirectoryObject.object_sid, sids
            )
            for d in rows:
                if d.object_sid:
                    by_sid[d.object_sid] = d
        for sid in sids:
            d = by_sid.get(sid)
            if d is None:
                res.unresolved_sids += 1
                continue
            canonical = (
                f"{d.domain}\\{d.sam_account_name}"
                if d.domain and d.sam_account_name
                else (d.user_principal_name or d.sam_account_name or sid)
            )
            display = d.display_name or d.sam_account_name or d.user_principal_name
            if d.deleted_at is not None and display:
                display = f"{display} (deleted)"
            # Upsert ONLY our own ('ldap') rows: never clobber a manual override.
            alias_stmt = (
                pg_insert(PrincipalAlias)
                .values(
                    alias=sid, canonical=canonical, display=display, source="ldap"
                )
                .on_conflict_do_update(
                    index_elements=[PrincipalAlias.alias],
                    set_={"canonical": canonical, "display": display, "source": "ldap"},
                    where=(PrincipalAlias.source == "ldap"),
                )
            )
            await session.execute(alias_stmt)
            res.aliases_written += 1
        await session.commit()
    return {"status": "done", **res.as_dict()}


@proc_app.task(
    queue="maintenance",
    name="filearr.worker.sync_directory",
    queueing_lock="sync-directory",
)
async def sync_directory(timestamp: int) -> dict:
    """Scheduled/on-demand AD/LDAP directory sync (see ``sync_directory_now``).
    Skipped under maintenance mode."""
    from filearr import maintmode

    if await maintmode.is_active_standalone():
        return {"status": "skipped", "reason": "maintenance_mode"}
    return await sync_directory_now()


# --- Light per-library hash refresh (hash-scheme migration path) -------------
# The operator-facing half of the HASH_IMPL_VERSION contract (provenance.py):
# when a hashing-behaviour change ships, the scheme prefix bumps, every stored
# row becomes visibly stale (`policy_version NOT LIKE '<scheme>:%'`), the
# Libraries page prompts, and THIS task refreshes the hashes. It is deliberately
# LIGHT: it re-reads file bytes to recompute quick/mid/content hashes and
# re-stamps policy_version — it never touches metadata_/user_metadata and never
# defers extract/embed/chunk/thumbnail work, so converging a million-row library
# costs hash I/O only (unlike rehash_small_files, which rides the full extract
# path and re-parses every file). Verified 2026-08-20 that the xxhash 3.8.1 ->
# 4.0.1 bump did NOT need this (digests byte-identical; tests/test_hash_vectors
# .py is the tripwire) — the mechanism exists so the next bump that DOES change
# output is a button, not a full rescan.


async def rehash_library_now(library_id: str) -> dict:
    """Recompute hashes for a central library's active items stamped under an
    OLD provenance scheme; re-stamp ``policy_version``; sync the index docs.

    Per item: ``quick_hash`` always, ``mid_hash`` (>128 KiB), ``content_hash``
    under the same rule the extract path uses (small band always; larger files
    per the resolved T7 policy + ceiling). A stored ``content_hash`` the current
    policy would NOT recompute is set to NULL rather than kept: an old-scheme
    digest surviving next to a current-scheme stamp would masquerade as
    comparable (same length under a same-width algorithm change), silently
    breaking dedupe/move/verify comparisons — the opt-in
    ``backfill_content_hashes`` task restores dropped ones under the current
    scheme. Agent-owned libraries are refused (central cannot open their files;
    the agent's ``rehash_sweep`` command is the equivalent there). Throttled by
    ``FILEARR_HASH_BACKFILL_RATE_MBPS`` like the backfill. Unreadable files are
    counted and skipped (the next scan tombstones them).
    """
    import asyncio
    import time

    from sqlalchemy import select
    from sqlalchemy.orm import load_only

    from filearr.db import SessionLocal
    from filearr.hashpolicy import resolve_hash_policy
    from filearr.models import Item, ItemStatus, Library
    from filearr.provenance import _SCHEME, policy_version
    from filearr.tasks.extract import QUICK_CHUNK, full_hash, mid_hash, quick_hash

    settings = get_settings()
    rate = float(settings.hash_backfill_rate_mbps) * 1_000_000.0  # <=0 = unthrottled
    rehashed = failed = dropped = 0
    read_bytes = 0
    started = time.monotonic()
    changed: list[str] = []
    async with SessionLocal() as session:
        lib = (
            await session.execute(select(Library).where(Library.id == library_id))
        ).scalar_one_or_none()
        if lib is None:
            return {"status": "skipped", "reason": "library_not_found", "rehashed": 0}
        if lib.source_agent_id is not None:
            return {"status": "skipped", "reason": "agent_owned", "rehashed": 0}
        resolved = resolve_hash_policy(
            declared=lib.hash_policy or "auto",
            root_path=lib.root_path or "",
            hash_full_max_bytes=lib.hash_full_max_bytes,
            global_max_bytes=settings.scan_hash_full_max_bytes,
        )
        stamp = policy_version(lib, settings)
        lib_id = lib.id
        # Collect the stale-scheme IDs FIRST, to exhaustion, with NO commit — a
        # server-side cursor does not survive a transaction end, so committing
        # inside the stream would invalidate it and the job would die on the
        # 201st row (yield_per buffer + commit-every-200). IDs are lightweight
        # (~16 B each): a million-row library is ~16 MB, bounded, and never
        # materialises the metadata_ JSONB that OOM'd the pictures scan.
        stale_ids = [
            row
            for row in (
                await session.execute(
                    select(Item.id)
                    .where(
                        Item.library_id == lib_id,
                        Item.status == ItemStatus.active,
                        Item.policy_version.isnot(None),
                        ~Item.policy_version.like(f"{_SCHEME}:%"),
                    )
                    .order_by(Item.id)
                )
            ).scalars()
        ]
        # Then process in ID chunks, each its own transaction: load exactly the
        # columns this loop reads/writes (load_only), mutate, commit.
        for start in range(0, len(stale_ids), 200):
            chunk = stale_ids[start : start + 200]
            items = (
                await session.execute(
                    select(Item)
                    .options(
                        load_only(
                            Item.id,
                            Item.path,
                            Item.size,
                            Item.quick_hash,
                            Item.mid_hash,
                            Item.content_hash,
                            Item.policy_version,
                        )
                    )
                    .where(Item.id.in_(chunk))
                )
            ).scalars().all()
            for item in items:
                if item.size is None or item.path is None:
                    continue
                want_content = item.size <= QUICK_CHUNK * 2 or (
                    resolved.compute_content and item.size <= resolved.full_max_bytes
                )
                try:
                    qh = await asyncio.to_thread(quick_hash, item.path, item.size)
                    mh = await asyncio.to_thread(mid_hash, item.path, item.size)
                    ch = (
                        await asyncio.to_thread(full_hash, item.path, item.size)
                        if want_content
                        else None
                    )
                except OSError:
                    failed += 1
                    continue
                item.quick_hash = qh
                item.mid_hash = mh
                if ch is not None:
                    item.content_hash = ch
                elif item.content_hash is not None:
                    item.content_hash = None
                    dropped += 1
                item.policy_version = stamp
                changed.append(str(item.id))
                rehashed += 1
                # Throttle on approximate bytes actually read (whole file when the
                # content hash streamed it; head+tail+mid samples otherwise).
                read_bytes += (
                    int(item.size) if ch is not None else min(int(item.size), QUICK_CHUNK * 3)
                )
                if rate > 0:
                    ahead = read_bytes / rate - (time.monotonic() - started)
                    if ahead > 0:
                        await asyncio.sleep(min(ahead, 5.0))
            await session.commit()
    # quick_hash/content_hash are searchable/filterable doc fields — refresh the
    # projection for the touched rows instead of waiting for the nightly rebuild.
    for i in range(0, len(changed), 1000):
        await defer_index_sync(changed[i : i + 1000])
    return {
        "status": "done",
        "library_id": library_id,
        "rehashed": rehashed,
        "failed": failed,
        "content_dropped": dropped,
    }


@proc_app.task(queue="maintenance", name="filearr.worker.rehash_library")
async def rehash_library(library_id: str) -> dict:
    """Operator-triggered light hash refresh for one library (see
    ``rehash_library_now``). Skips under maintenance mode — it is exactly the
    sustained disk/network I/O that mode exists to stop."""
    from filearr import maintmode

    if await maintmode.is_active_standalone():
        return {"status": "skipped", "reason": "maintenance_mode", "rehashed": 0}
    return await rehash_library_now(library_id)


async def defer_rehash_library(library_id: str) -> int | None:
    """Enqueue a light re-hash; at most one *queued* job per library (the
    queueing_lock frees when the job starts — same contract as defer_scan).
    Returns the job id, or ``None`` on an already-queued collision."""
    async with open_pool_if_needed():
        try:
            job = await proc_app.configure_task(
                "filearr.worker.rehash_library",
                queue="maintenance",
                queueing_lock=f"rehash-library:{library_id}",
            ).defer_async(library_id=library_id)
        except AlreadyEnqueued:
            return None
    return job


# Scheduled by maintenance_tick (registry default "30 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(queue="maintenance", name="filearr.worker.nightly_reconcile")  # FIX-8: no retry
async def nightly_reconcile(timestamp: int) -> None:
    """Safety net: re-sync the whole search index from Postgres (projection is disposable)."""
    from filearr.tasks.index_sync import rebuild_index

    await rebuild_index()


# --- P9-T7: hourly Postgres<->Meili reconciliation sweep --------------------
# Bounded worst-case index staleness even if every incremental index_sync update
# (or, once P9-T6 lands, every task webhook — Meili webhooks have NO delivery
# retry) was lost. Runs at minute 7 to stay clear of the every-minute scan tick
# and the 04:xx purge/nightly-rebuild window. `queueing_lock` guarantees at most
# one sweep is ever queued: procrastinate's periodic deferrer catches the
# AlreadyEnqueued collision and skips (so a long sweep is never piled onto).
# FIX-8: no retry -- a lost/failed sweep is simply re-run next hour regardless.
# Scheduled by maintenance_tick (registry default "7 * * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reconcile_meili",
    queueing_lock="reconcile-meili",  # FIX-8: no retry (hourly re-runs)
)
async def reconcile_meili(timestamp: int) -> dict:
    """Detect and repair Postgres<->Meili divergence (P9-T7). NEVER writes Postgres."""
    from filearr.tasks.reconcile import run_reconcile_sweep

    return await run_reconcile_sweep()


# --- P9-T5: orphaned shadow-index reaper ------------------------------------
# A crashed or retried shadow-swap rebuild can leave an orphaned `<index>_rebuild_
# <epoch>` shadow index on disk (holding a full extra copy — real disk cost). This
# hourly sweep deletes shadows older than FILEARR_MEILI_SHADOW_MAX_AGE_HOURS (6h)
# by their epoch-embedded name, so a live in-flight rebuild's young shadow is never
# reaped mid-build. Runs at minute 47 to stay clear of the scan tick, the 04:xx
# purge/rebuild window, and the :07 reconcile sweep. queueing_lock collapses any
# duplicate enqueue. FIX-8: no retry -- the hourly tick re-runs on any fault.
# Scheduled by maintenance_tick (registry default "47 * * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reap_shadow_indexes",
    queueing_lock="reap-shadow-indexes",  # FIX-8: no retry (hourly re-runs)
)
async def reap_shadow_indexes(timestamp: int) -> int:
    """Delete orphaned shadow indexes from crashed/retried rebuilds (P9-T5).
    Returns the number reaped. Meili-only; never touches Postgres."""
    from filearr.meili_ops import reap_stale_shadows

    return len(await reap_stale_shadows())


# --- P9-T4: weekly Meilisearch LMDB compaction ------------------------------
# Meili's LMDB store never shrinks by itself, so a long-lived index accumulates
# free pages nothing reads. This weekly job measures the fragmentation ratio and
# compacts only when it is worth the ~2x transient disk cost. Space reclamation
# ONLY: the index is a disposable projection (invariant 1), so a skip is always
# safe and every operational failure inside compact_if_fragmented degrades to a
# structured skip rather than an exception. Registry default "0 6 * * 0" (Sunday
# 06:00 UTC) puts it clear of the whole 03:30-05:20 nightly purge/reconcile
# window; operator-overridable from the Jobs page. FIX-8/FIX-9 discipline: NO
# retry (a transient Meili fault is simply re-measured next week) and the
# queueing_lock collapses any duplicate enqueue onto a still-running compaction.
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.compact_meili",
    queueing_lock="compact-meili",  # FIX-8: no retry (weekly re-runs)
)
async def compact_meili(timestamp: int) -> dict:
    """Compact the search index when it is fragmented past the threshold (P9-T4).

    Meili-only; never touches Postgres. Returns the structured result from
    :func:`filearr.meili_ops.compact_if_fragmented`."""
    from filearr import maintmode
    from filearr.meili_ops import compact_if_fragmented

    # Central maintenance mode: skip, exactly like the other work-generating
    # periodics. The maintenance tick already refuses to DEFER while the mode is
    # active (without consuming the occurrence, so this fires once the mode
    # lifts) — this second gate covers the Jobs-page "Run now" path, which
    # bypasses the tick. Sustained heavy disk I/O is precisely what an operator
    # enters maintenance mode to stop.
    if await maintmode.is_active_standalone():
        return {"status": "skipped", "reason": "maintenance_mode", "compacted": False}

    return await compact_if_fragmented()


@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_meili_failed_tasks",
    queueing_lock="purge-meili-failed-tasks",
)
async def purge_meili_failed_tasks() -> dict:
    """On-demand: clear Meilisearch's failed-task HISTORY for the items index
    (2026-08-18). Records only -- the index itself is untouched. Exists so the
    Jobs-page 'failed Meili tasks' counter can be reset after an incident."""
    from filearr.meili_ops import purge_failed_tasks

    return await purge_failed_tasks(get_settings().meili_index)


# --- T5: cron-scheduled scanning -------------------------------------------
# One static, import-time periodic task on a 1-minute tick (Procrastinate cannot
# register periodic tasks dynamically, so per-library cron is evaluated here in
# code rather than via one periodic task per library). No worker restart is
# needed when a library's scan_cron changes -- the next tick simply reads the new
# value. `timestamp` is the Unix time of the tick that fired this run and is used
# both as the cron reference minute and as Procrastinate's periodic dedup key
# (a given minute defers at most once, even if the scheduler double-fires).
async def _defer_due_scans(tick: datetime) -> list[str]:
    """Defer every scan due at ``tick`` and return their scheduling keys.

    Two kinds of due work are evaluated on this one static tick (P2-T6 rides T5's
    single tick — Procrastinate periodic tasks are import-time static):

      * **Full-library scans** — a library whose ``scan_cron`` is due. Key is
        ``str(library.id)``. Skipped if ANY scan (full or scoped) is currently
        running for the library: the running-row query below covers both, so a
        long scan is never piled onto.
      * **Scoped (hot-folder) scans** — an enabled ``scan_paths`` row whose own
        ``scan_cron`` (NOT the inherited library one) is due. Key is
        ``"<library.id>:<rel_path>"``. Skipped while a FULL scan is running for
        the library (the full scan already covers the subtree — "full-scan lock
        wins"), and skipped while a scoped scan of the *same* rel_path is running.
        A scoped scan does NOT block a differently-scoped scan or the full sched.

    The queueing lock in :func:`defer_scan` additionally collapses any
    duplicate/racing enqueue for the same lock. A library with zero ``scan_paths``
    rows behaves exactly as T5 (regression guard)."""
    from sqlalchemy import select

    from filearr import maintmode
    from filearr.db import SessionLocal
    from filearr.models import Library, ScanPath, ScanRun
    from filearr.schedule import due_occurrence

    # Maintenance mode: skip the whole tick WITHOUT consuming occurrences —
    # due scans fire (collapsed to the latest occurrence) once the mode lifts.
    if await maintmode.is_active_standalone():
        return []

    cap = get_settings().scan_schedule_max_catchup_minutes
    deferred: list[str] = []
    async with SessionLocal() as session:
        # All enabled libraries (not just those with a library-level cron): a
        # library may schedule only via scan_paths rows. P5-T4: agent-owned
        # libraries (source_agent_id NOT NULL) are EXCLUDED — central never scans
        # a remote agent's corpus; its content arrives via the replication apply
        # path, so a cron/hot-folder scan against a non-existent local root would
        # only tombstone the whole replicated catalog.
        libraries = list(
            (
                await session.execute(
                    select(Library).where(
                        Library.enabled.is_(True),
                        Library.source_agent_id.is_(None),
                    )
                )
            ).scalars()
        )
        for library in libraries:
            # One query for this library's running scans; classify by scope so we
            # can tell a running FULL scan (rel_path IS NULL) from scoped ones.
            running_scopes = list(
                (
                    await session.execute(
                        select(ScanRun.rel_path).where(
                            ScanRun.library_id == library.id,
                            ScanRun.status.in_(("running", "stopping")),
                        )
                    )
                ).scalars()
            )
            any_running = bool(running_scopes)
            full_running = any(rp is None for rp in running_scopes)
            busy_scopes = {rp for rp in running_scopes if rp is not None}

            # --- library-level full scan (FIX-8 once-per-occurrence) ---
            # Fire only for a cron occurrence strictly newer than the one this
            # library last consumed, and stamp it BEFORE the enqueue (committed
            # first, then defer) so a given occurrence fires at most once even
            # across duplicate/late ticks or a mid-scan worker death. If a scan is
            # currently running we do NOT consume the occurrence — it stays due and
            # fires (collapsed to the latest) once the running scan clears.
            occ = (
                due_occurrence(
                    library.scan_cron, tick, library.last_cron_fired_at,
                    max_catchup_minutes=cap,
                )
                if library.scan_cron
                else None
            )
            if occ is not None and not any_running:
                library.last_cron_fired_at = occ
                await session.commit()
                job = await defer_scan(str(library.id))
                if job is not None:
                    deferred.append(str(library.id))

            # --- per-path scoped scans (only rows carrying their OWN cron) ---
            scan_paths = list(
                (
                    await session.execute(
                        select(ScanPath).where(
                            ScanPath.library_id == library.id,
                            ScanPath.enabled.is_(True),
                            ScanPath.scan_cron.isnot(None),
                        )
                    )
                ).scalars()
            )
            for sp in scan_paths:
                sp_occ = due_occurrence(
                    sp.scan_cron, tick, sp.last_cron_fired_at,
                    max_catchup_minutes=cap,
                )
                if sp_occ is None:
                    continue
                if full_running:
                    continue  # full-scan lock wins; the full scan covers this subtree
                if sp.rel_path in busy_scopes:
                    continue  # a scoped scan of this exact subtree is already running
                sp.last_cron_fired_at = sp_occ  # consume in the enqueue commit
                await session.commit()
                job = await defer_scan(str(library.id), rel_path=sp.rel_path)
                if job is not None:
                    deferred.append(f"{library.id}:{sp.rel_path}")
    return deferred


# --- P11-T9: scheduled report delivery + P11-T11 export lifecycle -----------
# Rides the SAME minutely tick contract as scan scheduling: once-per-occurrence
# firing via each schedule's persisted last_cron_fired_at (FIX-8/FIX-9). No
# retry (a transient failure re-evaluates next minute); the queueing_lock
# collapses overlapping ticks. Purge + reconcile ride the nightly maintenance
# lane so a crashed export never sits `running` and an expired artifact is freed.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.schedule_report_exports",
    queueing_lock="schedule-report-exports",  # FIX-8: no retry (minutely re-runs)
)
async def schedule_report_exports(timestamp: int) -> int:
    """Evaluate every enabled report schedule against this minute and enqueue an
    export for each due (un-consumed) occurrence (P11-T9)."""
    from filearr import maintmode
    from filearr.tasks.reports import evaluate_report_schedules

    if await maintmode.is_active_standalone():
        return 0  # maintenance mode: occurrences stay due, fire when it lifts

    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    return len(await evaluate_report_schedules(tick))


# Scheduled by maintenance_tick (registry default "40 4 * * *" in
# filearr.maintenance; operator-overridable from the Jobs page).
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.purge_report_exports",
    queueing_lock="purge-report-exports",  # FIX-8: no retry
)
async def purge_report_exports(timestamp: int) -> int:
    """Delete expired export artifacts (row retained, ``purged_at`` stamped) and
    reconcile any export stuck ``running`` past its timeout to ``failed``
    (invariant 7). Returns the number of artifacts purged (P11-T11)."""
    from filearr import exports

    async with SessionLocal() as session:
        await exports.reconcile_stale_exports(session, get_settings())
        return await exports.purge_expired_exports(session, get_settings())


# BK-T3. NOT periodic and NOT scheduled by default: an unattended pg_dump that
# fills {config} is worse than no dump, so an operator opts in by setting a cron
# on the Jobs page (the registry entry is `editable`, which is what makes an
# override schedulable at all). No retry — a failed backup must be VISIBLE on
# the failed-jobs list, not quietly re-attempted until it happens to fit.
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.backup_now",
    queueing_lock="backup-now",
)
async def backup_now(timestamp: int) -> dict:
    """Write one in-app backup bundle to ``{config}/backups``.

    Deliberately lets :class:`filearr.backup.BackupError` propagate: a refusal
    (pg_dump older than the server, pg_dump absent, disk at the critical floor)
    must land on the Jobs page as a failed run with its reason, never as a
    success with no file behind it."""
    from filearr import backup

    async with SessionLocal() as session:
        manifest = await backup.run_backup(session, get_settings())
    return {
        "bundle": manifest["contents"]["dump"]["file"].removesuffix(".dump"),
        "bytes": manifest["contents"]["dump"]["bytes"],
        "items": manifest["item_count"],
        "complete": manifest["complete"],
    }


@proc_app.periodic(cron="*/10 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.reconcile_report_exports",
    queueing_lock="reconcile-report-exports",  # FIX-8: no retry
)
async def reconcile_report_exports(timestamp: int) -> int:
    """Flip a crashed export stuck ``running`` to ``failed`` every 10 minutes
    (invariant 7 — never leave a job ``running``)."""
    from filearr import exports

    async with SessionLocal() as session:
        return await exports.reconcile_stale_exports(session, get_settings())


# --- P13: phased config-group rollouts --------------------------------------
# Rides the SAME static minute-tick contract as scan scheduling: Procrastinate
# cannot register periodic tasks dynamically, so a rollout's tier schedule is
# evaluated in code on one static tick rather than by registering a task per
# rollout. FIX-8/FIX-9 discipline throughout: NO retry (a transient DB fault
# re-evaluates next minute), the queueing_lock collapses overlapping ticks, and
# state is stamped BEFORE anything observable happens — the `_defer_due_scans`
# rule. Here "observable" is the agents' next poll, so each transition COMMITS
# before the loop moves on; a worker that dies mid-sweep leaves every already-
# advanced rollout advanced exactly once, and re-evaluates the rest next minute.
def _step_rollout(rollout, tick: datetime) -> bool:
    """One tick of the phased-rollout state machine, shared by config-group and
    release rollouts (roadmap §23): mutates ``rollout`` in place and returns
    whether it advanced. Transitions (all against ``tick``):
    scheduled+due -> running (tier 0 now, or waiting on tier 0's delay);
    running with next tier's delay elapsed -> next tier (ONE per tick);
    reaching the last tier -> completed. The caller commits and runs its own
    completion side effect."""
    tiers = rollout.tiers or []
    if not tiers:
        return False
    if rollout.status == "scheduled":
        if rollout.starts_at is not None and rollout.starts_at > tick:
            return False
        rollout.status = "running"
        rollout.started_at = tick
        first_delay = int(tiers[0].get("delay_minutes", 0) or 0)
        rollout.tier_started_at = tick
        if first_delay <= 0:
            rollout.current_tier = 0
            if len(tiers) == 1:
                rollout.status = "completed"
                rollout.finished_at = tick
        return True
    nxt = rollout.current_tier + 1
    if nxt >= len(tiers):
        return False
    anchor = rollout.tier_started_at or rollout.started_at or tick
    delay = int(tiers[nxt].get("delay_minutes", 0) or 0)
    if tick < anchor + timedelta(minutes=delay):
        return False
    rollout.current_tier = nxt
    rollout.tier_started_at = tick
    if nxt == len(tiers) - 1:
        rollout.status = "completed"
        rollout.finished_at = tick
    return True


async def _advance_release_rollouts(tick: datetime) -> list[str]:
    """Advance every due phased RELEASE rollout (roadmap §23). Same state
    machine as the config rollouts; completion has no version to move -- it
    just means 'offered fleet-wide' -- so the side effect is the audit event."""
    from sqlalchemy import select, text

    from filearr import audit, maintmode
    from filearr.db import SessionLocal
    from filearr.models import AgentReleaseRollout

    if await maintmode.is_active_standalone():
        return []
    advanced: list[str] = []
    async with SessionLocal() as session:
        reg = await session.execute(text("SELECT to_regclass('agent_release_rollouts') AS r"))
        if reg.scalar_one() is None:
            return []  # pre-migration DB
        rollouts = list(
            (
                await session.execute(
                    select(AgentReleaseRollout)
                    .where(AgentReleaseRollout.status.in_(("scheduled", "running")))
                    .order_by(AgentReleaseRollout.created_at)
                )
            ).scalars()
        )
        for r in rollouts:
            if not _step_rollout(r, tick):
                continue
            await session.commit()
            advanced.append(str(r.id))
            if r.status == "completed":
                await audit.emit(
                    audit.AGENT_RELEASE_ROLLOUT_COMPLETED,
                    details={"rollout_id": str(r.id), "release_version": r.release_version},
                )
    return advanced


async def _advance_config_rollouts(tick: datetime) -> list[str]:
    """Advance every due config-group rollout; return their ids as strings.

    Three transitions, evaluated against ``tick`` (the cron reference minute, not
    ``datetime.now()`` — so a late tick promotes on the schedule it was FOR, and
    a test can drive the whole lifecycle without sleeping):

      * ``scheduled`` whose ``starts_at`` is due (or NULL = immediately) →
        ``running`` at tier 0, stamping ``started_at``/``tier_started_at``.
      * ``running`` with a next tier whose ``delay_minutes`` has elapsed since
        ``tier_started_at`` → advance one tier (ONE tier per tick, even when
        several delays have lapsed: each tier exists so somebody can look at the
        fleet between them, and collapsing them would silently skip that).
      * the LAST tier (always 100%) → ``completed``, ``finished_at`` stamped and
        the group's ``current_version`` finally moved to ``target_version``, at
        which point coverage stops depending on the rollout at all.

    A cancelled rollout is simply not selected here; its covered agents fall back
    to ``current_version`` on their next poll (documented on the cancel endpoint).
    """
    from sqlalchemy import select

    from filearr import audit, maintmode
    from filearr.db import SessionLocal
    from filearr.models import AgentConfigGroup, AgentConfigRollout

    # Maintenance mode: skip WITHOUT consuming anything — a rollout is a
    # wall-clock schedule, so it simply resumes (and catches up one tier per
    # tick) once the mode lifts.
    if await maintmode.is_active_standalone():
        return []

    async def _finish(session, rollout) -> None:
        """Publish the target version to everyone (the last tier is always 100%)
        and record it. Runs AFTER the rollout row is committed `completed`, so a
        crash between the two re-runs only this idempotent half."""
        group = await session.get(AgentConfigGroup, rollout.group_id)
        if group is not None:
            group.current_version = rollout.target_version
            await session.commit()
        await audit.emit(
            audit.AGENT_CONFIG_ROLLOUT_COMPLETED,
            details={
                "rollout_id": str(rollout.id),
                "group_id": str(rollout.group_id),
                "target_version": rollout.target_version,
            },
        )

    advanced: list[str] = []
    async with SessionLocal() as session:
        rollouts = list(
            (
                await session.execute(
                    select(AgentConfigRollout)
                    .where(AgentConfigRollout.status.in_(("scheduled", "running")))
                    .order_by(AgentConfigRollout.created_at)
                )
            ).scalars()
        )
        for rollout in rollouts:
            tiers = rollout.tiers or []
            if not tiers:
                continue
            if rollout.status == "scheduled":
                if rollout.starts_at is not None and rollout.starts_at > tick:
                    continue
                # Tier 0's own delay counts from the start instant, so a rollout
                # authored as "wait 30 minutes, then 10%" goes running-at-tier-0
                # only once that delay has passed. started_at records WHEN the
                # window opened, which is what the delay is measured from.
                rollout.status = "running"
                rollout.started_at = tick
                first_delay = int(tiers[0].get("delay_minutes", 0) or 0)
                if first_delay > 0:
                    # Hold at tier -1 (covering nobody) until the delay elapses;
                    # tier_started_at anchors that wait.
                    rollout.tier_started_at = tick
                else:
                    rollout.current_tier = 0
                    rollout.tier_started_at = tick
                    if len(tiers) == 1:
                        rollout.status = "completed"
                        rollout.finished_at = tick
                await session.commit()
                if rollout.status == "completed":
                    await _finish(session, rollout)
                advanced.append(str(rollout.id))
                continue

            nxt = rollout.current_tier + 1
            if nxt >= len(tiers):
                continue  # already at the last tier; completion happens below
            anchor = rollout.tier_started_at or rollout.started_at or tick
            delay = int(tiers[nxt].get("delay_minutes", 0) or 0)
            if tick < anchor + timedelta(minutes=delay):
                continue
            rollout.current_tier = nxt
            rollout.tier_started_at = tick
            if nxt == len(tiers) - 1:
                rollout.status = "completed"
                rollout.finished_at = tick
            await session.commit()
            if rollout.status == "completed":
                await _finish(session, rollout)
            advanced.append(str(rollout.id))
    return advanced


@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.advance_config_rollouts",
    queueing_lock="advance-config-rollouts",  # FIX-8: no retry (minutely re-runs)
)
async def advance_config_rollouts(timestamp: int) -> int:
    """Promote every config-group rollout due this minute. Returns how many
    advanced (a cheap no-op on an empty table, so it is not gated on
    ``agents_enabled`` — a fleet disabled mid-rollout must still not leave a
    rollout wedged half-way when it is re-enabled)."""
    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    n = len(await _advance_config_rollouts(tick))
    n += len(await _advance_release_rollouts(tick))
    return n


@proc_app.periodic(cron="* * * * *")
@proc_app.task(queue="maintenance", name="filearr.worker.schedule_scans")
async def schedule_scans(timestamp: int) -> int:
    """Evaluate every enabled library's (and hot folder's) schedule against this
    minute's tick and defer scans for occurrences not yet consumed. FIX-8: firing
    is once-per-occurrence (persisted ``last_cron_fired_at``), deduped against any
    unfinished scan_library job, and never re-fires a missed occurrence per tick.
    Returns the number of scans deferred."""
    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    deferred = await _defer_due_scans(tick)
    return len(deferred)


@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.schedule_agent_inventories",
    queueing_lock="schedule-agent-inventories",  # minutely re-runs; no retry
)
async def schedule_agent_inventories(timestamp: int) -> int:
    """2026-08-20: drive the config-group ``inventory.schedule_cron``.

    For every enrolled, non-revoked agent whose MERGED settings enable
    inventory with a schedule, enqueue the same inventory COMMAND an operator
    fires by hand (collectors + paths/preset from the group document) on each
    due occurrence. Once-per-occurrence state is derived from the newest
    schedule-created inventory command (no new column); an unfinished
    scheduled run suppresses the next one (no pile-ups on a slow walk).
    Returns the number of commands enqueued."""
    settings = get_settings()
    if not settings.agents_enabled:
        return 0
    import logging as _logging

    from sqlalchemy import select

    from filearr.agent_config import resolve_effective_config
    from filearr.db import SessionLocal
    from filearr.models import Agent as AgentRow
    from filearr.models import AgentCommand
    from filearr.schedule import due_occurrence

    log = _logging.getLogger("filearr.worker.inventory_schedule")

    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    enqueued = 0
    async with SessionLocal() as session:
        agents = (
            (
                await session.execute(
                    select(AgentRow).where(
                        AgentRow.cert_fingerprint.is_not(None),
                        AgentRow.revoked_at.is_(None),
                    )
                )
            )
            .scalars()
            .all()
        )
        for agent in agents:
            try:
                cfg = await resolve_effective_config(session, agent)
            except Exception:  # noqa: BLE001 - one agent's bad config never stalls the tick
                log.warning("inventory schedule: config resolve failed for %s", agent.id)
                continue
            inv = (cfg.document.get("group") or {}).get("inventory") or {}
            cron = inv.get("schedule_cron")
            if not (inv.get("enabled") and cron and inv.get("collectors")):
                continue
            # Newest schedule-created inventory command = the once-per-occurrence
            # cursor; an unfinished one also suppresses this tick.
            last_row = (
                await session.execute(
                    select(AgentCommand.created_at, AgentCommand.status)
                    .where(
                        AgentCommand.agent_id == agent.id,
                        AgentCommand.kind == "inventory",
                        AgentCommand.requested_by.is_(None),
                        AgentCommand.payload["scheduled"].as_boolean().is_(True),
                    )
                    .order_by(AgentCommand.created_at.desc())
                    .limit(1)
                )
            ).first()
            last_at = last_row[0] if last_row else None
            if last_row and last_row[1] in ("pending", "picked_up"):
                continue
            try:
                occ = due_occurrence(cron, tick, last_at)
            except Exception:  # noqa: BLE001 - validated at write; belt only
                continue
            if occ is None:
                continue
            payload = {
                "scheduled": True,
                "collectors": list(inv.get("collectors") or []),
                "paths": list(inv.get("paths") or []),
            }
            if inv.get("preset"):
                payload["preset"] = inv["preset"]
            session.add(
                AgentCommand(
                    agent_id=agent.id,
                    kind="inventory",
                    payload=payload,
                    status="pending",
                    expires_at=tick + timedelta(seconds=settings.agent_command_ttl_max_seconds),
                )
            )
            enqueued += 1
        if enqueued:
            await session.commit()
    return enqueued


# --- Jobs-page maintenance schedules ----------------------------------------
# The editable maintenance tasks (nightly purges, reconcilers, thumbnail GC —
# see filearr.maintenance.TICK_SCHEDULED) lost their static @periodic
# decorators so their schedules can be overridden at runtime from the Jobs
# page. This single static tick evaluates each task's EFFECTIVE cron
# (maintenance_schedules override, else the registry default) with the same
# once-per-occurrence due_occurrence consumption contract as schedule_scans.
# Infrastructure ticks/monitors keep their own static decorators above.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.maintenance_tick",
    queueing_lock="maintenance-tick",  # no retry (minutely re-runs)
)
async def maintenance_tick(timestamp: int) -> int:
    """Defer every editable maintenance task due this minute (override-aware).
    Returns the number of tasks deferred."""
    from filearr.maintenance import run_maintenance_tick

    tick = datetime.fromtimestamp(timestamp, tz=UTC)
    return len(await run_maintenance_tick(tick))


# --- T5: watch-mode supervisor entrypoint ----------------------------------
# --- P8-T6/T7/T8/T15: alert dispatch pump -----------------------------------
# One minutely tick drives the state-derived alert pump. Group-wait, group-
# interval, repeat-interval, digest windowing and the per-rule hourly ceiling are
# ALL derived from the alert_events rows every tick (no separate scheduler/state
# table), so a duplicated/late tick simply re-derives the same decision. The
# queueing_lock collapses overlapping ticks to at most one queued run.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="alerts",
    name="filearr.worker.pump_alerts",
    queueing_lock="pump-alerts",
    priority=get_settings().alerts_priority,  # UI-T14: user-facing timeliness
)
async def pump_alerts(timestamp: int) -> dict:
    from filearr.tasks.alerts import dispatch_pending

    return await dispatch_pending(timestamp)


async def _defer_scan_if_idle(library_id: str, rel_path: str | None = None) -> int | None:
    """Defer a scan for ``library_id`` unless a conflicting scan is already
    running (watch-mode trigger). The queueing lock in :func:`defer_scan`
    additionally collapses a burst of watcher events into a single queued scan.

    ``rel_path`` (P2-T6): when a per-path watcher fires, defer a *scoped* scan of
    that subtree. Conflict rules mirror the scheduler: a full watcher (rel_path
    None) defers only if NO scan is running for the library; a scoped watcher
    defers unless a FULL scan is running (full covers the subtree) or a scoped
    scan of the SAME subtree is already running."""
    from sqlalchemy import select

    from filearr import maintmode
    from filearr.db import SessionLocal
    from filearr.models import ScanRun

    # Maintenance mode: watcher events are dropped (the next event — or the
    # next cron occurrence — re-triggers once the mode lifts).
    if await maintmode.is_active_standalone():
        return None

    async with SessionLocal() as session:
        running_scopes = list(
            (
                await session.execute(
                    select(ScanRun.rel_path).where(
                        ScanRun.library_id == library_id,
                        ScanRun.status.in_(("running", "stopping")),
                    )
                )
            ).scalars()
        )
    if rel_path is None:
        if running_scopes:  # any scan running -> full watcher stays idle
            return None
        return await defer_scan(library_id)
    # scoped watcher: full-scan lock wins; same-subtree scoped scan blocks too.
    if any(rp is None for rp in running_scopes):
        return None
    if rel_path in {rp for rp in running_scopes if rp is not None}:
        return None
    return await defer_scan(library_id, rel_path=rel_path)


# --- P10-T1: agent_commands TTL + redelivery sweep -------------------------
# The on-demand command primitive's maintenance tick (research §3.1): flip stale
# `pending` / lease-lapsed `picked_up` rows to `expired` (kept, not deleted, so
# the UI can say "the agent never came back") and re-queue unacked deliveries to
# `pending` (at-least-once), bounded by FILEARR_AGENT_COMMAND_MAX_ATTEMPTS. Runs
# every minute so a picked-up-then-dropped command redelivers within one interval.
# FIX-8/FIX-9 discipline: NO retry (a transient DB fault is retried on the next
# minute tick), `queueing_lock` collapses overlapping ticks to one queued run,
# and the whole thing is a cheap no-op when agents are disabled. Bounded per run.
@proc_app.periodic(cron="* * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.expire_agent_commands",  # FIX-9: no retry (periodic re-runs)
    queueing_lock="expire-agent-commands",
)
async def expire_agent_commands(timestamp: int) -> dict:
    """Expire past-TTL agent commands + re-queue unacked-past-lease deliveries."""
    settings = get_settings()
    if not settings.agents_enabled:
        return {"skipped": "agents disabled"}
    from filearr.agentsync import run_agent_command_sweep
    from filearr.db import SessionLocal

    async with SessionLocal() as session:
        return await run_agent_command_sweep(
            session,
            now=datetime.now(UTC),
            lease_seconds=settings.agent_command_lease_seconds,
            max_attempts=settings.agent_command_max_attempts,
        )


# --- P10-T8: staging TTL cleanup sweep -------------------------------------
# Bounds central staging disk (research §5): reaps ``staging_transfers`` rows +
# their staged files that are past ``expires_at`` (unless a download is actively
# draining them, watermarked by ``last_range_request_at``) and reclaims abandoned
# partial uploads (no progress for FILEARR_STAGING_ABANDONED_UPLOAD_SECONDS) on
# their own shorter schedule. Every 5 minutes so a completed retrieve's staged
# file is freed reasonably soon after its TTL without cutting an in-flight
# download. FIX-8/FIX-9 discipline: NO retry (a transient fault re-runs on the
# next tick), ``queueing_lock`` collapses overlapping ticks, bounded per run. NOT
# gated on ``agents_enabled`` — staged bytes on disk must be reclaimed even if the
# agent fleet was later disabled (the query is a cheap no-op on an empty table).
@proc_app.periodic(cron="*/5 * * * *")
@proc_app.task(
    queue="maintenance",
    name="filearr.worker.cleanup_staging_transfers",  # FIX-9: no retry (periodic re-runs)
    queueing_lock="cleanup-staging-transfers",
)
async def cleanup_staging_transfers(timestamp: int) -> dict:
    """Reap dead/abandoned staging transfers + their files (P10-T8)."""
    from filearr.db import SessionLocal
    from filearr.staging_sweep import run_staging_cleanup_sweep

    settings = get_settings()
    async with SessionLocal() as session:
        return await run_staging_cleanup_sweep(
            session,
            now=datetime.now(UTC),
            download_grace_seconds=settings.staging_download_grace_seconds,
            abandoned_upload_seconds=settings.staging_abandoned_upload_seconds,
        )


def build_watch_supervisor():
    """Construct a :class:`WatchSupervisor` bound to the app DB + scan trigger."""
    from filearr.db import SessionLocal
    from filearr.watch import WatchSupervisor

    return WatchSupervisor(SessionLocal, _defer_scan_if_idle)


async def run_watch_supervisor() -> None:
    """Run the watch-mode supervisor loop (a companion to the Procrastinate
    worker process). It reconciles watchers against library config on a timer, so
    toggling watch_mode or editing a root takes effect without a restart."""
    await build_watch_supervisor().run()
