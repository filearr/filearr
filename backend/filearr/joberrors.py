"""Persisted job failure text (roadmap §18 — T11 follow-up).

Procrastinate 3.9's ``procrastinate_events`` table stores only
``(job_id, type, at)``: the exception message/traceback of a failed job went
exclusively to worker logs, so ``GET /system/failed-jobs`` could say *which*
job failed but never *why* (``error`` was always null). This module closes
that gap with a Procrastinate **worker middleware** (3.9's supported hook that
wraps every job on the event loop): any exception escaping a task is recorded
to the ``job_errors`` table — sanitized message + length-capped traceback,
keyed by the procrastinate job id — and then re-raised untouched, so retry
strategies, the reaper, and job status transitions behave exactly as before.

Recording is strictly best-effort on its OWN connection: a failure to persist
the error must never mask the original failure or poison the worker's session.
Deliberately NOT recorded:

  * ``JobAborted`` — an operator abort is an instruction, not a failure;
  * exceptions marked ``filearr_transient = True`` (e.g. the staged-extract
    ``RescheduleExtract`` control-flow signal) — these fire by design, often
    thousands of times per scan, and are not errors.

Rows are retained ``job_history_retention_days`` (same window as the
procrastinate history purge, FIX-8) and surfaced by ``errors.failed_jobs``:
the newest recorded error per job fills the previously-always-null ``error``
field, with the traceback alongside for the detail view.
"""

from __future__ import annotations

import logging
import traceback as tb_mod

from procrastinate import exceptions as proc_exceptions

from filearr.errors import sanitize_error

log = logging.getLogger(__name__)

# Traceback length cap. Enough for a deep async stack; small enough that a
# pathological repeating frame chain cannot bloat the table. The MESSAGE is
# capped separately by sanitize_error (500 chars).
TRACEBACK_MAX_CHARS = 8_000


def _format_traceback(exc: BaseException) -> str:
    text = "".join(tb_mod.format_exception(type(exc), exc, exc.__traceback__))
    if len(text) > TRACEBACK_MAX_CHARS:
        # Keep the TAIL — the raise site and cause chain live at the end.
        text = "… (truncated)\n" + text[-TRACEBACK_MAX_CHARS:]
    return text


async def record_job_error(context, exc: BaseException) -> None:
    """Persist one failure row for ``context.job``. Best-effort: never raises."""
    try:
        # Local imports: this module is imported by worker.py at proc_app
        # construction time, before the task modules (which import worker back).
        from filearr.db import SessionLocal
        from filearr.models import JobError

        job = context.job
        async with SessionLocal() as session:
            session.add(
                JobError(
                    job_id=int(job.id) if job.id is not None else None,
                    task_name=job.task_name,
                    queue=job.queue,
                    attempt=int(job.attempts or 0),
                    message=sanitize_error(exc),
                    traceback=_format_traceback(exc),
                )
            )
            await session.commit()
    except Exception:  # noqa: BLE001 — recording must never mask the failure
        log.exception("job_errors: failed to record error for job")


async def capture_job_errors(call_next, context, worker) -> object:
    """The worker middleware (procrastinate 3.9 ``worker_middleware``): record
    escaping exceptions, then re-raise untouched."""
    try:
        return await call_next()
    except proc_exceptions.JobAborted:
        raise  # operator abort — an instruction, not an error
    except BaseException as exc:
        if getattr(exc, "filearr_transient", False):
            raise  # control-flow reschedule (staged extract / backpressure)
        await record_job_error(context, exc)
        raise
