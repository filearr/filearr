import asyncio
import logging
import os
from typing import Literal

from fastapi import APIRouter, Depends, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import __version__, errors
from filearr.config import get_settings
from filearr.db import get_session
from filearr.embed_stats import semantic_snapshot
from filearr.errors import (
    extract_error_counts_by_library,
    failed_jobs,
    failed_jobs_count,
)
from filearr.jobs_stats import jobs_summary, running_jobs, thumbnail_totals
from filearr.meili_stats import meili_snapshot
from filearr.models import AppLog, Item, Library
from filearr.queue_stats import queue_snapshot
from filearr.schemas import FailedJobPage
from filearr.security import require_scope

router = APIRouter()
log = logging.getLogger("filearr.system")


@router.get("/health")
async def health() -> dict:
    return {"status": "ok", "version": __version__}


@router.get("/version")
async def version() -> dict:
    """Identify the RUNNING build. ``build_stamp`` is written into the image
    by the deploy script (backend/.build-stamp -> /app/.build-stamp) and is
    the ground truth for "which source is this container actually running" —
    the same stamp the deploy's verify step checks. Null outside deployed
    images (dev checkouts have no stamp)."""
    from starlette.concurrency import run_in_threadpool

    # ``source_url`` (AGPL-3.0 §13, FILEARR_SOURCE_URL) is served here so the
    # footer "Source" link can point at a fork's modified source at RUNTIME
    # without a frontend rebuild -- the Vite __SOURCE_URL__ is only the fallback.
    return {
        "app_version": __version__,
        "build_stamp": await run_in_threadpool(_read_stamp),
        "source_url": get_settings().source_url,
        # P5-T1: whether the distributed-agent fleet surface is enabled (drives
        # the Admin -> Agents panel's visibility). Opt-in, default off.
        "agents_enabled": get_settings().agents_enabled,
    }


def _read_stamp() -> str | None:
    import pathlib as _pl

    candidates = (
        _pl.Path("/app/.build-stamp"),
        _pl.Path(__file__).resolve().parents[2] / ".build-stamp",
    )
    for cand in candidates:
        try:
            return cand.read_text().strip() or None
        except OSError:
            continue
    return None


# Rate limit for the over-budget WARNING: /stats is polled by the UI, and the
# old per-call log line repeated the identical fact every poll (and, since the
# Logs panel exists, filled it with duplicates). One reminder per hour.
_BUDGET_WARN_INTERVAL_S = 3600.0
_budget_warned_at: float = 0.0


def _gib(n: int) -> str:
    return f"{n / 1024**3:.1f} GiB"


async def _thumbnail_stats(session: AsyncSession) -> dict:
    """Cheap thumbnail-cache aggregates from ``thumbnail_manifest`` (P12-T12).

    ``count`` / ``bytes`` are the whole-cache totals; ``by_source`` breaks them
    down by generator source (artwork / image / audio_embedded / video) so an
    operator can see, e.g., how much of the store is video poster-frames. The
    grouped aggregate itself comes from :func:`filearr.jobs_stats.thumbnail_totals`
    (single source of truth, shared with the Jobs ``thumbs`` monitor); this layer
    adds the advisory-budget check. A WARNING is logged (at most hourly, never
    blocking anything) while the total exceeds the configured budget
    (``FILEARR_THUMBNAIL_BUDGET_GB``); the actionable surface is the Jobs page
    thumbs card, which renders the same ``over_budget`` flag."""
    global _budget_warned_at
    totals = await thumbnail_totals(session)
    total_count = totals["count"]
    total_bytes = totals["bytes"]

    budget = get_settings().thumbnail_budget_bytes_effective()
    over_budget = budget > 0 and total_bytes > budget
    if over_budget:
        import time as _time

        now = _time.monotonic()
        if now - _budget_warned_at >= _BUDGET_WARN_INTERVAL_S or _budget_warned_at == 0.0:
            _budget_warned_at = now
            largest = max(
                totals["by_source"].items(),
                key=lambda kv: kv[1]["bytes"],
                default=(None, None),
            )
            largest_note = (
                f"; largest source: {largest[0]} at {_gib(largest[1]['bytes'])}"
                if largest[0] is not None
                else ""
            )
            log.warning(
                "thumbnail cache is %s — over the %s advisory budget "
                "(FILEARR_THUMBNAIL_BUDGET_GB%s). This is a planning "
                "signal only: generation continues and nothing is deleted "
                "automatically (the disk-space floors drive emergency LRU "
                "eviction independently). To act: raise the budget (or set it "
                "to 0 to accept the size and silence this), run 'Thumbnail "
                "cache GC' on the Jobs page to drop orphaned files, or turn "
                "off thumbnailing for libraries that don't need it. Reminder "
                "logs at most hourly.",
                _gib(total_bytes),
                _gib(budget),
                largest_note,
            )
    return {
        "count": total_count,
        "bytes": total_bytes,
        "by_source": totals["by_source"],
        "budget_bytes": budget,
        "over_budget": over_budget,
    }


#: Per-section bounds for :func:`stats`. The dashboard endpoint fans out to seven
#: independent aggregates, and before 2026-08-11 ANY of them could hang the whole
#: response indefinitely — which is exactly what happened on the live LXC:
#: /api/v1/stats timed out four times at 15s while /health, /version and /search
#: all answered 200, and the deploy smoke gate failed with the stack otherwise up.
#: Which section blocks is not knowable in advance (a JSONB aggregate that has to
#: de-TOAST a million metadata blobs, a statvfs against a wedged network mount, a
#: procrastinate_jobs table grown past its purge), so the fix is structural: bound
#: every section and degrade the ones that overrun.
#:
#: Three layers, because they fail differently:
#:  * ``STATS_STATEMENT_TIMEOUT_MS`` — server-side, via SET LOCAL. Postgres aborts
#:    the query itself and raises a normal DBAPI error we can catch and recover
#:    from. This is the CLEAN path and it fires first.
#:  * ``STATS_SECTION_TIMEOUT_S`` — client-side backstop for the parts a statement
#:    timeout cannot reach (the Meili HTTP call, the threadpool statvfs).
#:  * ``STATS_TOTAL_BUDGET_S`` — a deadline shared by all sections. Per-section
#:    bounds alone are NOT enough: the sections run in sequence, so seven of them
#:    at 8s apiece is still a 56s response, and the deploy gate would fail exactly
#:    as it did. The deadline is what actually caps the endpoint.
#:
#: The total sits well under the 15s the deploy smoke test allows, so slow
#: sections cost their own fields instead of the whole gate.
STATS_STATEMENT_TIMEOUT_MS = 5000
STATS_SECTION_TIMEOUT_S = 8.0
STATS_TOTAL_BUDGET_S = 10.0


async def _bound_statements(session: AsyncSession) -> None:
    """Arm the per-transaction statement timeout for this request's session.

    ``SET`` takes no bind parameters, hence ``set_config(..., is_local=true)``,
    which is the parameterizable spelling of ``SET LOCAL``. Transaction-scoped:
    it dies with a rollback, so :func:`_section` re-arms after one."""
    await session.execute(
        text("SELECT set_config('statement_timeout', :ms, true)"),
        {"ms": str(STATS_STATEMENT_TIMEOUT_MS)},
    )


async def _section(
    session: AsyncSession,
    degraded: dict[str, str],
    name: str,
    coro,
    fallback,
    deadline: float | None = None,
):
    """Await one stats section under a bound, degrading instead of hanging.

    A section that overruns or errors returns ``fallback`` and records its reason
    in ``degraded``, which /stats surfaces as a top-level map so the dashboard can
    show a gap WITH an explanation rather than a spinner forever or a silent zero.
    A zero would be a lie: "no extraction errors" and "we could not count the
    extraction errors" are different facts and an operator acts differently on
    each. The reason lands OUT of band rather than inside the section because some
    sections (extract_errors) are open key spaces where an injected marker key
    would be indistinguishable from data.

    The failure path rolls back before returning. A statement timeout leaves the
    transaction aborted, and every LATER section shares this session — without
    the rollback one slow aggregate would cascade into ``InFailedSqlTransaction``
    for all of them. Rolling back is safe here because /stats is read-only."""
    budget = STATS_SECTION_TIMEOUT_S
    if deadline is not None:
        # Never below a small floor: at zero remaining we would cancel a section
        # that might have answered instantly, turning one slow aggregate into a
        # cascade of empty ones.
        budget = max(0.25, min(budget, deadline - asyncio.get_running_loop().time()))
    try:
        return await asyncio.wait_for(coro, timeout=budget)
    except Exception as exc:  # noqa: BLE001 - one bad section must not 500 the page
        if isinstance(exc, TimeoutError):
            reason = f"timed out after {budget:g}s"
            log.warning("stats: section %r degraded: %s", name, reason)
        else:
            reason = errors.sanitize_error(exc)
            log.warning("stats: section %r failed: %s", name, exc, exc_info=True)
        try:
            await session.rollback()
            await _bound_statements(session)
        except Exception:  # noqa: BLE001 - best effort; later sections degrade too
            log.warning("stats: could not recover the session after %r", name)
        degraded[name] = reason
        return fallback


async def _by_type(session: AsyncSession) -> dict:
    rows = await session.execute(
        select(Item.file_category, func.count(), func.coalesce(func.sum(Item.size), 0))
        .where(Item.status == "active")
        .group_by(Item.file_category)
    )
    return {(cat or "unclassified"): {"count": c, "bytes": int(b)} for cat, c, b in rows}


@router.get("/stats", dependencies=[Depends(require_scope("read"))])
async def stats(session: AsyncSession = Depends(get_session)) -> dict:
    await _bound_statements(session)
    degraded: dict[str, str] = {}
    deadline = asyncio.get_running_loop().time() + STATS_TOTAL_BUDGET_S

    async def section(name, coro, fallback):
        return await _section(session, degraded, name, coro, fallback, deadline)

    # W8-B: keyed by taxonomy file_category (the successor to media_type). A NULL
    # category (a not-yet-(re)scanned row) buckets under "unclassified". Bounded
    # like the rest: it is a full grouped pass over active items, and once the
    # statement timeout is armed an unbounded call here would 500 the endpoint
    # rather than degrade it.
    by_type = await section("by_type", _by_type(session), {})
    # T8: extraction throughput / queue-depth observability (single aggregate
    # read over procrastinate_jobs; cheap, read-only). Exposes extract backlog
    # depth + done/failed counts so operators can watch a large scan drain.
    queues = await section("queues", queue_snapshot(session), {"queues": {}, "extract": {}})
    # T11: live per-library extraction-error counts (single GIN-indexed aggregate
    # over items.metadata ? '_extract_error'). Authoritative, cheap, read-only.
    errors_by_lib = await section("extract_errors", extract_error_counts_by_library(session), {})
    # P9-T7/T8: live Meili health + projection drift (postgres active count vs
    # Meili numberOfDocuments) — the same cheap signal the hourly reconcile sweep
    # acts on. Total/read-only: degrades to healthy=false if Meili is down.
    meili = await section("meili", meili_snapshot(session), {"healthy": False})
    # P3-T8: semantic-search coverage (embedded/pending/drift). Off => all zeros.
    # The section that blew up live on 2026-08-11 — see embed_stats for why, and
    # for the narrowing that keeps the expensive half off the un-embedded rows.
    # The fallback keeps enabled/model REAL (they are config reads that cannot
    # fail) and zeroes only the counts, so the UI affordances that key off
    # `semantic.enabled` behave identically on a degraded read.
    _s = get_settings()
    semantic = await section(
        "semantic",
        semantic_snapshot(session),
        {
            "enabled": _s.semantic_enabled,
            "model": _s.embed_model,
            "embedded_count": 0,
            "pending": 0,
            "fp_mismatches": 0,
        },
    )
    # P12-T12: thumbnail-cache storage stats (count/bytes/by_source) + soft budget
    # alarm. Cheap grouped aggregate over the disposable manifest projection.
    thumbs = await section(
        "thumbs", _thumbnail_stats(session), {"count": 0, "bytes": 0, "by_source": {}}
    )
    # FIX-11: filesystem headroom for every watch path + the worst rollup status.
    # os.statvfs on a handful of paths — cheap, synchronous, offloaded so a slow
    # network mount cannot block the event loop.
    from starlette.concurrency import run_in_threadpool

    # statvfs against a wedged network mount blocks in the KERNEL — status_for_path
    # fails open on OSError, but an uninterruptible mount never raises one, so the
    # threadpool hop protects the event loop and nothing protects the request.
    disk = await section(
        "disk", run_in_threadpool(_disk_section), {"status": "unknown", "paths": []}
    )
    # BK-T1: two single-row lookups on a tiny table — negligible beside the
    # aggregates above, and worth doing per poll rather than once at startup so
    # the dashboard clears the moment an operator corrects the key. Bounded like
    # every other section: if it cannot be read, the guard itself is what
    # degrades, never the page.
    from filearr import keyguard

    keys = await section("key_fingerprints", keyguard.check_all(session), {})
    # A mismatch is folded into ``degraded`` on purpose. That map is the one
    # place the dashboard ALREADY treats as "something here is not right, and
    # here is why in words" — and the entire defect being fixed is that a wrong
    # FILEARR_SECRET_KEY produces no signal anywhere. Reusing the existing
    # channel means the warning cannot be missed by a UI that forgot to render
    # a new field.
    degraded.update(keyguard.mismatches(keys))
    return {
        "by_type": by_type,
        "queues": queues["queues"],
        "extract": queues["extract"],
        "extract_errors": errors_by_lib,
        "meili": meili,
        "semantic": semantic,
        "thumbs": thumbs,
        "disk": disk,
        # {secret_key: {...}, ca_root: {...}} — see filearr.keyguard.check_all.
        "key_fingerprints": keys,
        # Empty on a healthy instance. {section: reason} for anything that was
        # bounded out, so the UI can label the gap instead of rendering a zero.
        "degraded": degraded,
    }


def _disk_section() -> dict:
    """FIX-11 disk headroom for the dashboard: per-path status + worst rollup.

    ``paths`` is one row per watch target (label/path/free/total/pct_free/status/
    reason); ``status`` is the single worst across them (drives the banner)."""
    from filearr import diskguard

    settings = get_settings()
    statuses = diskguard.monitored_statuses(settings)
    paths = [
        {
            "label": st.get("label", st["path"]),
            "path": st["path"],
            "total": st["total"],
            "free": st["free"],
            "used": st["used"],
            "pct_free": round(st["pct_free"], 2),
            "status": st["status"],
            "reason": st["reason"],
            "is_pg": st.get("is_pg", False),
        }
        for st in statuses
    ]
    return {"status": diskguard.overall_status(statuses), "paths": paths}


@router.get("/system/disk", dependencies=[Depends(require_scope("read"))])
async def system_disk() -> dict:
    """FIX-11: filesystem headroom for every monitored path (admin/read scope).

    Same shape as the ``/stats`` ``disk`` section — ``{status, paths:[...]}`` —
    but a dedicated endpoint so the Jobs/Admin banner (and external monitoring)
    can poll disk alone without the heavier ``/stats`` aggregate. Read-only
    ``os.statvfs`` offloaded to a threadpool so a slow mount never blocks the loop."""
    from starlette.concurrency import run_in_threadpool

    return await run_in_threadpool(_disk_section)


class ShareMapEntryOut(BaseModel):
    """One deploy-written mount→share mapping (OPS-T7). Credential-free."""

    container_prefix: str
    share_url: str
    storage_type: str | None = None
    host: str | None = None
    unc: str | None = None


@router.get(
    "/system/share-map",
    response_model=list[ShareMapEntryOut],
    dependencies=[Depends(require_scope("read"))],
)
async def system_share_map() -> list[dict]:
    """OPS-T7: the deploy-time network-share mount map that auto-populates library
    ``share_prefix`` (read scope).

    The Proxmox deploy wizard writes this from the rclone/NFS mounts it configured
    inside the container; the app reads it read-only to resolve a container path
    back to a user-facing network location. Empty list when no map is present
    (feature simply off). Never carries credentials — ``share_url`` is a
    user-facing reference only."""
    from filearr import share_map

    return [e.model_dump() for e in share_map.get_entries()]


class FileGroupOut(BaseModel):
    """One file-group taxonomy entry (search-UI facet + external reference; see
    ``filearr.file_groups``). ``file_category`` is the group's parent category key
    (W8-B replaced the removed ``media_type`` nominal parent); ``extensions`` is the
    sorted bare-extension member list."""

    id: str
    label: str
    file_category: str
    description: str
    extensions: list[str]


@router.get(
    "/system/file-groups",
    response_model=list[FileGroupOut],
    dependencies=[Depends(require_scope("read"))],
)
async def file_groups() -> list[dict]:
    """The file-group taxonomy registry (read scope) — the finer, extension-derived
    similarity layer beneath ``file_category``.

    Returns one ``{id, label, file_category, description, extensions}`` object per
    group, in canonical registry order, for the search-UI ``file_group`` facet and
    external reference. ``file_group`` is a pure projection of the extension (see
    ``search.build_doc`` / ``filearr.file_groups.detect_group``), filterable and
    facet-searchable.

    NOTE: after the extension map changes, run ``POST /system/rebuild-index`` so
    existing search documents are re-projected with their ``file_group`` value —
    newly scanned/updated items get it automatically."""
    from filearr.file_groups import registry_payload

    return registry_payload()


@router.post(
    "/system/rebuild-index",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def rebuild_index_endpoint() -> dict:
    """Trigger a full rebuild of the search index from Postgres (admin scope).

    Defers the ``rebuild_index`` task (P9-T5 shadow-index + atomic swap) and returns
    its Procrastinate ``job_id``. The rebuild runs on the ``index`` queue in the
    worker: it builds a fresh shadow index, backfills it from Postgres truth, then
    atomically swaps it into place -- concurrent searches NEVER see a half-built
    index, and any failure before the swap leaves the live index untouched. This is
    the on-demand handle for a settings/schema migration rollout or a manual
    re-projection (operators kept deferring this task by hand).

    DISK HEADROOM: a rebuild holds BOTH the live and the shadow index copies on
    disk at once (~2x the index size) until the post-swap delete of the old data --
    same LMDB constraint as native compaction. Keep the Meili data volume sized for
    the transient 2x (see the ops runbook / P9-T11); at homelab scale this is
    trivially affordable, but flag it as a sizing input as the corpus grows."""
    from filearr.worker import defer_rebuild_index

    job_id = await defer_rebuild_index()
    return {"job_id": job_id}


@router.post(
    "/system/retry-extracts",
    dependencies=[Depends(require_scope("write"))],
)
async def retry_all_extracts(session: AsyncSession = Depends(get_session)) -> dict:
    """Requeue extraction for every failed item across ALL libraries
    (2026-08-21, write scope) — the one-click global drain of the errors
    surface. Same semantics as the per-library ``POST
    /libraries/{id}/retry-extracts`` (errored items + never-hashed items with
    no pending job; stale ``_extract_error`` markers cleared first) via the
    shared ``errors.collect_retryable_items``. Returns the total requeued."""
    from filearr.errors import collect_retryable_items
    from filearr.worker import defer_extract

    ids = await collect_retryable_items(session, None)
    await session.commit()
    await defer_extract(ids)
    return {"retried": len(ids)}


@router.post(
    "/system/embed-backfill",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def embed_backfill_endpoint() -> dict:
    """Trigger a semantic-embedding backfill pass (P3-T8, admin scope).

    Defers ``embed_missing``, which enqueues a LOWEST-priority ``embed_item`` for
    each active item lacking a current-fingerprint vector, CAPPED per run
    (``FILEARR_EMBED_BACKFILL_BATCH``) — re-invoke until ``/stats`` reports
    ``semantic.pending == 0``. Requires ``FILEARR_SEMANTIC_ENABLED=true`` (the task
    no-ops otherwise). Returns the Procrastinate ``job_id`` of the backfill task.

    On a first enable over a large corpus this is a background pass (~5.5 h for
    750k items on the live LXC); a full ``rebuild-index`` is NOT needed — each
    embed re-syncs its own item so vectors ride the incremental projection."""
    from filearr.worker import defer_embed_missing

    job_id = await defer_embed_missing()
    return {"job_id": job_id}


@router.get(
    "/system/failed-jobs",
    response_model=FailedJobPage,
    dependencies=[Depends(require_scope("read"))],
)
async def failed_jobs_view(
    limit: int = 25, offset: int = 0, session: AsyncSession = Depends(get_session)
) -> dict:
    """Paginated failed Procrastinate jobs (T11 / FIX-8). Read-only; ``limit``
    capped at 100.

    Returns ``{items, total, limit, offset}`` so the UI can render a real pager
    (the failed-jobs list used to grow unbounded on screen — FIX-8). ``total`` is
    the full failed-row count; ``items`` is the requested page. procrastinate 3.9
    does not persist per-job error text in the DB, so each item's ``error`` is
    null and ``attempted_at`` (last event time) is the actionable signal.
    """
    limit = max(1, min(limit, 100))
    offset = max(0, offset)
    items = await failed_jobs(session, limit=limit, offset=offset)
    total = await failed_jobs_count(session)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


class MaintenanceModeIn(BaseModel):
    active: bool
    # Free-text operator note shown in the console banner and to API readers
    # while the mode is active (e.g. "pg_dump + VACUUM FULL, back ~03:00").
    reason: str | None = Field(default=None, max_length=500)


@router.get(
    "/system/maintenance-mode",
    dependencies=[Depends(require_scope("read"))],
)
async def maintenance_mode_view(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """The global maintenance-mode state (see ``filearr.maintmode``)."""
    from filearr import maintmode

    return await maintmode.get_state(session)


@router.post(
    "/system/maintenance-mode",
    dependencies=[Depends(require_scope("admin"))],
)
async def maintenance_mode_set(
    body: MaintenanceModeIn,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Flip global maintenance mode (admin). While active: scan/maintenance/
    report scheduling is suspended, manual scan triggers 409, and agents are
    advertised to pause their replication push (they keep scanning locally).
    Safety reapers and the alert pump keep running. Idempotent."""
    from filearr import maintmode

    return await maintmode.set_state(
        session, active=body.active, reason=body.reason
    )


@router.get(
    "/system/update-check",
    dependencies=[Depends(require_scope("admin"))],
)
async def update_check_view() -> dict:
    """The cached update-check result. Never contacts GitHub itself — unless
    ``FILEARR_UPDATE_CHECK_AUTO`` is on, in which case a stale (>6h) or absent
    cache is refreshed. ``checked_at: null`` means no check has run yet."""
    from filearr import updatecheck

    if get_settings().update_check_auto:
        return await updatecheck.check()
    return updatecheck.cached() or {
        "checked_at": None,
        "source": get_settings().source_url,
        "components": [],
        "changelog": [],
    }


@router.post(
    "/system/update-check",
    dependencies=[Depends(require_scope("admin"))],
)
async def update_check_run() -> dict:
    """Operator-initiated check: contact GitHub NOW (admin). The only network
    peers are api.github.com / raw.githubusercontent.com; nothing is sent
    beyond the requests themselves. Degrades to an ``error`` field offline."""
    from filearr import updatecheck

    return await updatecheck.check(force=True)


_LOG_LEVELNO = {"info": 20, "warning": 30, "error": 40, "critical": 50}


@router.get(
    "/system/logs",
    dependencies=[Depends(require_scope("read"))],
)
async def logs_view(
    min_level: Literal["info", "warning", "error", "critical"] = "info",
    source: Literal["app", "worker"] | None = None,
    q: str | None = None,
    limit: int = 200,
    before_id: int | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Tail of the unified app+worker log stream (the Jobs page Logs panel).

    Newest first, keyset-paginated by row id (``before_id`` = the last id of
    the previous page). ``q`` substring-matches message and logger name.
    Same read scope as the page's other monitoring views (failed jobs already
    expose error text and paths). ``enabled`` reflects the sink config so the
    UI can explain an empty panel; a pre-migration DB degrades to empty."""
    limit = max(1, min(limit, 1000))
    stmt = select(AppLog).where(AppLog.levelno >= _LOG_LEVELNO[min_level])
    if source is not None:
        stmt = stmt.where(AppLog.source == source)
    if q:
        pat = f"%{q}%"
        stmt = stmt.where(or_(AppLog.message.ilike(pat), AppLog.logger.ilike(pat)))
    if before_id is not None:
        stmt = stmt.where(AppLog.id < before_id)
    stmt = stmt.order_by(AppLog.id.desc()).limit(limit)
    try:
        rows = (await session.execute(stmt)).scalars().all()
    except Exception:  # noqa: BLE001 - table absent pre-migration
        await session.rollback()
        rows = []
    return {
        "enabled": get_settings().log_db_enabled,
        "logs": [
            {
                "id": r.id,
                "ts": r.ts.isoformat(),
                "source": r.source,
                "level": r.level,
                "logger": r.logger,
                "message": r.message,
                "exc": r.exc,
            }
            for r in rows
        ],
        "next_before_id": rows[-1].id if len(rows) == limit else None,
    }


@router.get(
    "/system/jobs/running",
    dependencies=[Depends(require_scope("read"))],
)
async def jobs_running_view(session: AsyncSession = Depends(get_session)) -> list[dict]:
    """Currently-executing Procrastinate jobs (UI-T10). Read-only; capped at 50.

    Shape per job: ``{id, queue, task, args, started_at, seconds_running,
    rel_path}``. ``args`` is an ALLOWLISTED reference subset only (item_id/
    library_id/scan_run_id/rel_path) — never the raw kwargs. ``rel_path`` is
    resolved from ``item_id`` where present so humans see filenames. Returns
    ``[]`` when the procrastinate schema is absent."""
    return await running_jobs(session)


@router.get(
    "/system/jobs/summary",
    dependencies=[Depends(require_scope("read"))],
)
async def jobs_summary_view(session: AsyncSession = Depends(get_session)) -> dict:
    """One composite snapshot the Jobs dashboard polls (UI-T10). Read-only.

    Composes existing helpers: per-queue rollup + flat extract summary, the
    in-flight ``running`` list, the 10 most recent ``failed_recent`` jobs, the
    Meili drift snapshot, and ``scans_running`` (running ScanRuns + library
    name). One URL so the UI polls a single endpoint."""
    return await jobs_summary(session)


@router.post(
    "/system/jobs/reap",
    dependencies=[Depends(require_scope("admin"))],
)
async def reap_stalled_jobs_endpoint() -> dict:
    """Requeue or fail jobs orphaned in ``doing`` by a dead/restarted worker
    (FIX-6, admin scope). Runs the SAME reaper the every-5-minutes maintenance
    tick runs, inline, so an operator does not have to wait for the next tick.

    Prunes stalled worker rows, then for each stalled ``doing`` job: a crashed
    ``scan_library`` is FAILED (its ScanRun is already crash-failed; retrigger
    from the Libraries page), every other orphan is RETRIED back to ``todo`` to
    run again — unless a replacement is already queued under the same
    ``queueing_lock`` (collision), in which case the orphan is FAILED instead.

    Returns ``{reaped, retried, failed, pruned_workers}``."""
    from filearr.worker import open_pool_if_needed, reap_stalled_jobs_now

    async with open_pool_if_needed():
        return await reap_stalled_jobs_now()


# --- Optional-feature gate visibility (Jobs page "Optional features" card) --
#
# WHY this exists (live 2026-08-09): operators ran the on-demand maintenance
# tasks "Semantic-embedding backfill", "Chunk documents for RAG" and "Rebuild
# the passages search index", each finished in 0 s, and nothing explained why —
# the underlying optional features were simply off. Procrastinate stores no
# task RESULT (no result column), so a per-run "skipped because…" cannot be
# replayed from history. Instead we publish the DETERMINISTIC gate state,
# derived from live config + DB, so the UI can say up front which features are
# off and where to turn them on. Read-only by design: env-backed gates need a
# process restart, so offering a toggle here would lie.


@router.get(
    "/system/features",
    dependencies=[Depends(require_scope("read"))],
)
async def features_view(session: AsyncSession = Depends(get_session)) -> dict:
    """Every optional feature's current gate state (read scope).

    Each entry carries ``key``, ``title``, ``enabled``, ``scope`` (``"env"``
    for a process-level setting, ``"libraries"`` for a per-library opt-in),
    the ``env`` variable name (null for per-library gates) and an
    operator-facing ``detail``. Per-library gates add ``count``/``total``;
    the thumbnail budget adds ``value_gb``."""
    s = get_settings()

    # One grouped pass for both per-library opt-ins (they mirror each other).
    total, chunking_n, ocr_n = (
        await session.execute(
            select(
                func.count(),
                func.count().filter(Library.chunking_enabled.is_(True)),
                func.count().filter(Library.ocr_enabled.is_(True)),
            ).select_from(Library)
        )
    ).one()

    budget_gb = round(s.thumbnail_budget_bytes_effective() / 1024**3, 2)

    features = [
        {
            "key": "semantic",
            "title": "Semantic / hybrid search",
            "enabled": s.semantic_enabled,
            "scope": "env",
            "env": "FILEARR_SEMANTIC_ENABLED",
            "detail": (
                "Vector embeddings + hybrid ranking on top of keyword search. "
                "Enabling loads a local ONNX embedding model in the worker "
                "(~490 MB RSS); run 'Semantic-embedding backfill' afterwards to "
                "embed the existing catalog."
            ),
        },
        {
            "key": "chunking",
            "title": "RAG document chunking",
            "enabled": chunking_n > 0,
            "scope": "libraries",
            "env": None,
            "count": chunking_n,
            "total": total,
            "detail": (
                "Splits text-bearing documents into retrievable passages. "
                "Per-library toggle in each library's settings (Admin -> edit "
                "library); it feeds 'Chunk documents for RAG' and the passages "
                "search index, both of which no-op while no library opts in."
            ),
        },
        {
            "key": "ocr",
            "title": "OCR for scanned documents",
            "enabled": ocr_n > 0,
            "scope": "libraries",
            "env": None,
            "count": ocr_n,
            "total": total,
            "detail": (
                "Extracts text from image-only PDFs and scanned pages so they "
                "become searchable. Per-library opt-in (Admin -> edit library), "
                "mirroring the chunking toggle; needs tesseract, which is "
                "bundled in the container image."
            ),
        },
        {
            "key": "content_sniff",
            "title": "Content sniffing (extensionless files)",
            "enabled": s.content_sniff_enabled,
            "scope": "env",
            "env": "FILEARR_CONTENT_SNIFF_ENABLED",
            "detail": (
                "Magic-sniffs extensionless files still classified 'other' and "
                "reclassifies them by detected content type (libmagic). "
                "Enabling makes the 'Content-sniff extensionless files' task do "
                "real work, one bounded batch per run."
            ),
        },
        {
            "key": "agents",
            "title": "Distributed agents",
            "enabled": s.agents_enabled,
            "scope": "env",
            "env": "FILEARR_AGENTS_ENABLED",
            "detail": (
                "Remote scanner agents that index storage this server cannot "
                "reach, reporting back over mTLS. Enabling exposes the agent "
                "enrollment/poll API and the Admin -> Agents panel."
            ),
        },
        {
            "key": "auth",
            "title": "API authentication",
            "enabled": s.auth_enabled,
            "scope": "env",
            "env": "FILEARR_AUTH_ENABLED",
            "detail": (
                "Bearer API keys with read/write/admin scopes on every endpoint. "
                "Disabling leaves the whole API open to anyone who can reach it "
                "— intended for local development only."
            ),
        },
        {
            "key": "update_check_auto",
            "title": "Automatic update checks",
            "enabled": s.update_check_auto,
            "scope": "env",
            "env": "FILEARR_UPDATE_CHECK_AUTO",
            "detail": (
                "Lets the Updates card refresh a stale cache by contacting "
                "GitHub on its own. The manual 'Check now' button always works; "
                "this only controls the automatic refresh."
            ),
        },
        {
            "key": "log_db",
            "title": "Log recording to the database",
            "enabled": s.log_db_enabled,
            "scope": "env",
            "env": "FILEARR_LOG_DB_ENABLED",
            "detail": (
                "Persists application log records to Postgres so the console's "
                "Logs panel can show them. Disabling leaves logs on stdout only "
                "(container logs) and empties that panel."
            ),
        },
        {
            "key": "thumbnail_budget",
            "title": "Thumbnail cache budget",
            "enabled": True,
            "scope": "env",
            "env": "FILEARR_THUMBNAIL_BUDGET_GB",
            "value_gb": budget_gb,
            "detail": (
                "Advisory storage ceiling for the generated-thumbnail cache; "
                "crossing it warns rather than blocking generation (the "
                "per-file byte caps are the hard guard). 0 disables the alarm."
            ),
        },
    ]
    return {"features": features}


# --- About page: the whole running stack, version by version ----------------
#
# WHY (user request, 2026-08-10): the console had nowhere to answer "which
# versions is this deployment actually running". Every number below is read
# from the LIVE process, the LIVE database or the LIVE service — never from a
# pin in pyproject.toml, which states intent rather than fact and disagrees
# with reality exactly when it matters. Anything undeterminable is reported as
# null with a reason. Assembly lives in ``filearr.about``; this is the HTTP
# layer plus the one genuinely async section (the service probes).


@router.get(
    "/system/about",
    dependencies=[Depends(require_scope("read"))],
)
async def about_view(session: AsyncSession = Depends(get_session)) -> dict:
    """The complete running build stack (read scope).

    Sections: ``application`` (version, deploy build stamp, licence, Python and
    platform), ``services`` (Meilisearch/PostgreSQL probed live, Procrastinate
    and SQLAlchemy from package metadata), ``python_packages`` (every DIRECT
    dependency with its INSTALLED version and a documentation link),
    ``host_tools`` (ffprobe/ffmpeg/exiftool/tesseract/poppler on this machine,
    each with central's ``verdict`` against the published minimum version —
    the same judgement the fleet console applies to an agent's tools),
    ``agents`` (fleet ``agent_version`` histogram; null when agents are off) and
    ``embedding`` (the configured HF model, whether it is cached here, and the
    commit sha of the cached revision).

    Makes NO outbound network calls: it links to upstream projects, never
    fetches from them, so the page renders identically on an air-gapped box.
    Each service is probed independently and degrades to ``version: null`` plus
    an ``error`` reason — "Meilisearch: unreachable" IS the useful answer, and a
    down projection must never turn this page into a 500.

    SCOPE TRADEOFF, stated honestly: ``read`` rather than ``admin``. This is a
    version fingerprint, and a fingerprint helps an attacker who already has a
    read key match this deployment against a CVE list. It is nonetheless read
    scope because (a) an AGPL §13 deployment already publishes its Corresponding
    Source and its app version via ``/version`` and the footer, so the marginal
    disclosure is the dependency patch levels, and (b) the page's whole purpose
    is to be reachable by whoever is looking at a broken instance, which is
    routinely someone without an admin key. Deployments that disagree can gate
    ``read`` itself — which is the control that actually decides who sees this."""
    from starlette.concurrency import run_in_threadpool

    from filearr import about

    stamp = await run_in_threadpool(_read_stamp)
    # One hop to the threadpool for the whole blocking half (dependency metadata
    # scan, host-tool subprocesses, HF cache stat) rather than one per section.
    payload = await run_in_threadpool(about.sync_sections, stamp)
    payload["services"] = await about.services_section(session)
    payload["agents"] = await about.agent_fleet(session)
    # BK-T1: the key-fingerprint guard's live verdict, hung off ``application``
    # because it is a fact about THIS deployment's identity, next to the build
    # stamp an operator is already here to read. Fingerprints only — the values
    # are sha256(secret)[:16] and can never be walked back to a key (see
    # filearr.keyguard). Evaluated live rather than served from the startup
    # cache so an operator who fixes the key and reloads sees it clear without
    # restarting the container.
    from filearr import keyguard

    payload["application"]["key_fingerprints"] = await keyguard.check_all(session)
    return payload


# --- Jobs-page maintenance schedules (registry: filearr.maintenance) --------


@router.get(
    "/system/maintenance",
    dependencies=[Depends(require_scope("read"))],
)
async def maintenance_view(session: AsyncSession = Depends(get_session)) -> dict:
    """Every registered maintenance task with its description (tooltip), the
    EFFECTIVE schedule (operator override else registry default), next
    occurrence, and last-run status from job history. Registry order."""
    from filearr.maintenance import maintenance_status

    return {"tasks": await maintenance_status(session)}


class MaintenancePatch(BaseModel):
    """Body for PATCH /system/maintenance/{key}. ``cron`` present-and-null
    resets to the registry default; absent leaves the schedule untouched."""

    cron: str | None = None
    enabled: bool | None = None


@router.patch(
    "/system/maintenance/{key}",
    dependencies=[Depends(require_scope("admin"))],
)
async def maintenance_update(
    key: str, body: MaintenancePatch, session: AsyncSession = Depends(get_session)
) -> dict:
    """Override an editable maintenance task's cron and/or enable/disable it
    (admin). 404 unknown key; 409 for fixed-cadence infrastructure tasks; 422
    invalid cron. Takes effect on the next scheduler tick (≤1 minute) — no
    worker restart. Returns the task's refreshed status row."""
    from fastapi import HTTPException

    from filearr.maintenance import MAINT_TASKS, maintenance_status, set_schedule
    from filearr.schedule import InvalidCronError, validate_cron

    spec = MAINT_TASKS.get(key)
    if spec is None:
        raise HTTPException(404, detail=f"unknown maintenance task: {key}")
    if not spec.editable:
        raise HTTPException(409, detail=f"{key} has a fixed schedule (infrastructure task)")
    set_cron = "cron" in body.model_fields_set
    if set_cron and body.cron is not None:
        try:
            validate_cron(body.cron)
        except InvalidCronError as exc:
            raise HTTPException(422, detail=str(exc)) from exc
    await set_schedule(
        session, key, cron=body.cron, set_cron=set_cron, enabled=body.enabled
    )
    rows = await maintenance_status(session)
    return next(r for r in rows if r["key"] == key)


@router.post(
    "/system/maintenance/{key}/run",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def maintenance_run(key: str) -> dict:
    """Trigger one maintenance task immediately (admin). 404 unknown key; 409
    when a run is already queued/executing (queueing lock) or the task is a
    non-triggerable minutely tick. Returns the deferred ``job_id``."""
    from fastapi import HTTPException

    from filearr.maintenance import MAINT_TASKS, AlreadyQueued, run_now

    spec = MAINT_TASKS.get(key)
    if spec is None:
        raise HTTPException(404, detail=f"unknown maintenance task: {key}")
    if not spec.runnable:
        raise HTTPException(409, detail=f"{key} runs every minute on its own — nothing to trigger")
    try:
        job_id = await run_now(key)
    except AlreadyQueued as exc:
        raise HTTPException(409, detail=f"a run is already queued: {exc}") from exc
    return {"job_id": job_id}


# --- BK-T3: in-app backup ---------------------------------------------------
# Three admin endpoints so a backup needs no shell: trigger, list, download.
# Every one of them repeats the same caveat the manifest and the Jobs page
# carry — an in-app bundle cannot include the host .env or the step-ca volume
# and is therefore not, alone, a disaster-recovery backup. That sentence is
# defined once (filearr.backup.INCOMPLETE_NOTE) and echoed, never re-worded.


@router.post(
    "/system/backup",
    status_code=202,
    dependencies=[Depends(require_scope("admin"))],
)
async def backup_trigger() -> dict:
    """Queue an in-app backup (admin). 409 when one is already queued/running.

    Returns the deferred ``job_id`` plus ``incomplete_note`` — the API answer
    states the limitation even for a caller that never reads the docs or the
    UI."""
    from fastapi import HTTPException

    from filearr.backup import INCOMPLETE_NOTE
    from filearr.maintenance import AlreadyQueued, run_now

    try:
        job_id = await run_now("backup_now")
    except AlreadyQueued as exc:
        raise HTTPException(409, detail=f"a backup is already queued: {exc}") from exc
    return {"job_id": job_id, "incomplete_note": INCOMPLETE_NOTE}


@router.get(
    "/system/backups",
    dependencies=[Depends(require_scope("admin"))],
)
async def backups_list() -> dict:
    """List in-app backup bundles, newest first (admin).

    Admin rather than read: a bundle's mere existence, size and item count
    describe the deployment's recovery posture, and the download beside it is
    the entire database."""
    from starlette.concurrency import run_in_threadpool

    from filearr import backup

    settings = get_settings()
    # os.walk over a handful of directories on the /config volume — cheap, but
    # /config can be a network mount, so it does not run on the event loop.
    bundles = await run_in_threadpool(backup.list_bundles, settings)
    return {
        "bundles": bundles,
        "dir": backup.backup_dir(settings),
        "keep": settings.backup_keep,
        "incomplete_note": backup.INCOMPLETE_NOTE,
    }


@router.get(
    "/system/backups/{name}",
    dependencies=[Depends(require_scope("admin"))],
)
async def backup_download(name: str, request: Request):
    """Stream one bundle's Postgres dump (admin).

    Mirrors the report-export download discipline (``api/exports.py``): the
    scope is re-checked at FETCH time rather than trusted from the trigger, no
    operator string ever reaches a filesystem path (``name`` must match the
    generated ``filearr-<UTC>`` pattern AND resolve inside the backup
    directory), and the download is audited UNCONDITIONALLY — regardless of
    ``FILEARR_AUDIT_READS`` — because pulling a full database dump is the most
    exfiltration-shaped action this API offers.

    404 for an unknown or malformed name; the two are deliberately
    indistinguishable so the endpoint is not a probe for what exists on disk."""
    from fastapi import HTTPException
    from fastapi.responses import FileResponse
    from starlette.concurrency import run_in_threadpool

    from filearr import audit, backup

    def _resolve() -> tuple[str, int] | None:
        # Resolution + stat together in ONE threadpool hop: /config can be a
        # network mount, and neither the realpath containment check nor the
        # size read belongs on the event loop.
        p = backup.bundle_path(get_settings(), name)
        if p is None or not os.path.exists(p):
            return None
        return p, os.path.getsize(p)

    found = await run_in_threadpool(_resolve)
    if found is None:
        raise HTTPException(404, "backup not found")
    path, size = found
    await audit.emit(
        audit.BACKUP_DOWNLOADED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"bundle": name, "file_size_bytes": size},
    )
    return FileResponse(
        path, media_type="application/octet-stream", filename=os.path.basename(path)
    )


class ClearFailedJobs(BaseModel):
    """Body for POST /system/jobs/clear-failed (FIX-8). Optional ``queue`` scopes
    the delete to one Procrastinate queue; omitted clears failed rows in every
    queue."""

    queue: str | None = Field(default=None, max_length=128)


@router.post(
    "/system/jobs/clear-failed",
    dependencies=[Depends(require_scope("admin"))],
)
async def clear_failed_jobs(
    body: ClearFailedJobs | None = None,
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Delete failed Procrastinate rows NOW (FIX-8, admin scope).

    The daily ``purge_job_history`` task ages terminal rows out on retention; this
    is the on-demand handle the "Clear failed history" button calls so an operator
    can wipe the accumulated failed list immediately instead of waiting. A single
    set-based ``DELETE ... WHERE status = 'failed'`` (optionally filtered to one
    ``queue``); only ``failed`` rows are touched — todo/doing/succeeded are never
    affected. Returns ``{deleted, queue}`` (``deleted`` = affected row count).
    No-op (``deleted=0``) when the procrastinate schema is absent (fresh DB) so
    the endpoint stays total."""
    queue = body.queue if body else None
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return {"deleted": 0, "queue": queue}
    sql = "DELETE FROM procrastinate_jobs WHERE status = 'failed'"
    params: dict = {}
    if queue is not None:
        sql += " AND queue_name = :queue"
        params["queue"] = queue
    result = await session.execute(text(sql), params)
    await session.commit()
    return {"deleted": result.rowcount or 0, "queue": queue}


class JobPriorityUpdate(BaseModel):
    """Body for POST /system/jobs/priority (UI-T14).

    ``queue`` is the Procrastinate queue name (scan/extract/index/embed/
    maintenance/alerts). ``priority`` is clamped to -100..100 (higher runs
    sooner). ``scope`` is currently only ``"pending"`` -- the adjustment applies
    to jobs still in ``todo`` (a job already ``doing`` is never preempted)."""

    queue: str = Field(min_length=1, max_length=128)
    priority: int = Field(ge=-100, le=100)
    scope: Literal["pending"] = "pending"


@router.post(
    "/system/jobs/priority",
    dependencies=[Depends(require_scope("admin"))],
)
async def set_job_priority(
    body: JobPriorityUpdate, session: AsyncSession = Depends(get_session)
) -> dict:
    """Re-prioritise a queue's PENDING jobs (UI-T14, admin scope).

    ``UPDATE procrastinate_jobs SET priority = :p WHERE status = 'todo' AND
    queue_name = :q``. Only ``todo`` jobs are touched -- a job already ``doing``
    keeps the priority it was fetched with (procrastinate never preempts a running
    job), so this reorders the BACKLOG, not in-flight work. The per-task-class
    DEFAULT priorities (applied at defer time) are unchanged; this is a one-shot
    bump of what is already queued. Returns ``{queue, priority, updated}`` where
    ``updated`` is the affected row count. No-op (``updated=0``) when the
    procrastinate schema is absent (fresh DB) so the endpoint stays total."""
    exists = (
        await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))
    ).scalar()
    if exists is None:
        return {"queue": body.queue, "priority": body.priority, "updated": 0}
    result = await session.execute(
        text(
            "UPDATE procrastinate_jobs SET priority = :p "
            "WHERE status = 'todo' AND queue_name = :q"
        ),
        {"p": body.priority, "q": body.queue},
    )
    await session.commit()
    return {
        "queue": body.queue,
        "priority": body.priority,
        "updated": result.rowcount or 0,
    }


@router.post(
    "/system/reclassify-extensions",
    dependencies=[Depends(require_scope("admin"))],
)
async def reclassify_extensions(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Recompute every active item's ``(file_category, file_group)`` from the CURRENT
    taxonomy and re-sync the changed docs (OPS-T4, admin scope; W8-B).

    Existing items keep the classification assigned at their last scan; an edit to
    the taxonomy (add/reparent an extension, add a group/category) only takes effect
    on rescan. This endpoint applies the current taxonomy in place WITHOUT a full
    filesystem rescan: it groups the taxonomy's ``ext -> (category, group)`` map by
    target and runs one set-based ``UPDATE ... WHERE extension IN (...)`` per target
    (extensions are stored bare + lowercased, matching ``taxonomy.detect``), then a
    final pass demotes anything whose extension is unmapped/NULL to
    ``(other, other)``. Sidecars are updated by the same extension rule the scan
    uses, so this stays consistent with a rescan.

    Every changed row is re-projected into Meilisearch via the normal incremental
    ``index_sync`` path, deferred in bounded ``RECLASSIFY_SYNC_BATCH``-sized jobs.
    Returns ``{changed, by_category}`` where ``by_category`` maps each destination
    ``file_category`` to how many rows moved INTO it."""
    from filearr import taxonomy_ops

    return await taxonomy_ops.reclassify_now(session)



#: Keyset batch size for the RBAC path_scope backfill (750k-row live catalogs).
RBAC_BACKFILL_BATCH = 1000


@router.post(
    "/system/rbac-backfill",
    dependencies=[Depends(require_scope("admin"))],
)
async def rbac_backfill(
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Stamp ``items.path_scope`` (the ltree RBAC scope key) for existing rows
    (P6-T2). New/moved items are stamped by the scanner going forward; this is
    the one-shot for a pre-existing catalog. Keyset-paginated by ``id`` in
    ``RBAC_BACKFILL_BATCH`` chunks with a commit per batch (bounded memory + a
    resumable, restart-safe pass over a 750k-row table). Idempotent: only rows
    whose ``path_scope`` is NULL are (re)computed, so a re-run resumes cheaply.

    Returns ``{"stamped": n}`` — the number of rows updated this call."""
    from filearr import rbac

    stamped = 0
    last_id: str | None = None
    while True:
        q = (
            select(Item.id, Item.library_id, Item.rel_path)
            .where(Item.path_scope.is_(None))
            .order_by(Item.id)
            .limit(RBAC_BACKFILL_BATCH)
        )
        if last_id is not None:
            q = q.where(Item.id > last_id)
        rows = (await session.execute(q)).all()
        if not rows:
            break
        for iid, lib_id, rel in rows:
            scope = rbac.path_to_ltree(rel, library_id=lib_id)
            await session.execute(
                update(Item).where(Item.id == iid).values(path_scope=scope)
            )
        last_id = rows[-1][0]
        stamped += len(rows)
        await session.commit()
    return {"stamped": stamped}
