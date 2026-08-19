"""Library diagnosis — the "why is this library red on the Libraries page?" report
(2026-08-16).

The Libraries page shows three failure signals per library: a FAILED last scan, an
extraction-Errors count, and failed jobs. Each has a different cause class and
a different fix, and until now the operator had to correlate scan stats, the
failing-items list, the failed-jobs table, the container log and the mount by
hand. ``diagnose_library`` gathers all of that in ONE read-only pass and turns
it into ordered VERDICTS — findings with a severity, a plain-language cause,
the evidence, and what to do — plus the raw sections for the curious.

Everything here is best-effort and read-only: a probe that fails becomes a
verdict, never an exception (the report must render precisely when things are
broken). Filesystem probes are bounded (one ``os.scandir`` capped at
``_SAMPLE_ENTRIES`` entries, ``_PROBE_TIMEOUT_S``) so a hung network mount
cannot hang the API worker.

Docs: docs-site/troubleshooting/library-failures.md — the verdict ``code``s
double as anchors there.
"""

# ruff: noqa: E501  -- operator-facing prose; wrapping it would hurt more than help
from __future__ import annotations

import asyncio
import os
import re
import time
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import errors as errors_mod
from filearr import schedule as schedule_mod
from filearr.config import get_settings
from filearr.models import Agent, Library, ScanRun

_SAMPLE_ENTRIES = 25
_PROBE_TIMEOUT_S = 8.0
_RECENT_RUNS = 6
_LOG_LINES = 25
_TOP_MESSAGES = 8
DOCS_PATH = "/docs/troubleshooting/library-failures/"


def _verdict(
    severity: str,
    code: str,
    title: str,
    detail: str,
    *,
    actions: list[str] | None = None,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "severity": severity,  # error | warn | info | ok
        "code": code,  # doc anchor
        "title": title,
        "detail": detail,
        "actions": actions or [],
        "evidence": evidence or {},
        "doc": f"{DOCS_PATH}#{code}",
    }


def _iso(dt: datetime | None) -> str | None:
    return dt.isoformat() if dt else None


# --------------------------------------------------------------------------- #
# Probes                                                                       #
# --------------------------------------------------------------------------- #
def _probe_path_sync(root: str) -> dict[str, Any]:
    out: dict[str, Any] = {
        "root_path": root,
        "exists": False,
        "is_dir": False,
        "readable": False,
        "listing_ms": None,
        "sample": [],
        "entries_seen": 0,
        "error": None,
        "network": None,
        "fstype": None,
        "empty": None,
    }
    try:
        out["exists"] = os.path.exists(root)
        out["is_dir"] = os.path.isdir(root)
        if not out["is_dir"]:
            return out
        out["readable"] = os.access(root, os.R_OK | os.X_OK)
        t0 = time.monotonic()
        n = 0
        sample: list[dict[str, Any]] = []
        with os.scandir(root) as it:
            for e in it:
                n += 1
                if len(sample) < _SAMPLE_ENTRIES:
                    sample.append({"name": e.name, "dir": e.is_dir(follow_symlinks=False)})
                if n >= 2000:
                    break
        out["listing_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["entries_seen"] = n
        out["sample"] = sample
        out["empty"] = n == 0
    except Exception as exc:  # noqa: BLE001 - the probe's failure IS the finding
        out["error"] = f"{type(exc).__name__}: {exc}"
    try:
        out["network"] = schedule_mod.is_network_path(root)
        mi = open("/proc/self/mountinfo", encoding="utf-8").read()
        mounts = (
            schedule_mod._parse_mountinfo(mi) if hasattr(schedule_mod, "_parse_mountinfo") else []
        )
        fs = schedule_mod._containing_fstype(root, mounts) if mounts else None
        out["fstype"] = fs
    except Exception:  # noqa: BLE001
        pass
    return out


async def _probe_path(root: str) -> dict[str, Any]:
    try:
        return await asyncio.wait_for(asyncio.to_thread(_probe_path_sync, root), _PROBE_TIMEOUT_S)
    except TimeoutError:
        return {
            "root_path": root,
            "exists": None,
            "is_dir": None,
            "readable": None,
            "listing_ms": None,
            "sample": [],
            "entries_seen": 0,
            "error": f"probe timed out after {_PROBE_TIMEOUT_S:.0f}s (hung mount?)",
            "network": None,
            "fstype": None,
            "empty": None,
            "timeout": True,
        }


async def _recent_runs(session: AsyncSession, library_id) -> list[dict[str, Any]]:
    rows = (
        (
            await session.execute(
                select(ScanRun)
                .where(ScanRun.library_id == library_id)
                .order_by(ScanRun.started_at.desc())
                .limit(_RECENT_RUNS)
            )
        )
        .scalars()
        .all()
    )
    out = []
    for r in rows:
        stats = dict(r.stats or {})
        dur = None
        if r.finished_at and r.started_at:
            dur = round((r.finished_at - r.started_at).total_seconds(), 1)
        out.append(
            {
                "id": str(r.id),
                "status": r.status,
                "started_at": _iso(r.started_at),
                "finished_at": _iso(r.finished_at),
                "duration_s": dur,
                "rel_path": getattr(r, "rel_path", None),
                "error": errors_mod.sanitize_error(stats.get("error"))
                if stats.get("error")
                else None,
                "stats": {k: v for k, v in stats.items() if k != "error"},
            }
        )
    return out


async def _extract_error_summary(session: AsyncSession, library_id) -> dict[str, Any]:
    count = await errors_mod.extract_error_count(session, str(library_id))
    by_kind: dict[str, int] = {}
    top: list[dict[str, Any]] = []
    if count:
        rows = await session.execute(
            text(
                "SELECT coalesce(metadata ->> '_extract_error_kind','error') AS kind, count(*) "
                "FROM items WHERE library_id = :lib AND status='active' "
                "AND metadata ? '_extract_error' GROUP BY 1"
            ),
            {"lib": str(library_id)},
        )
        by_kind = {r[0]: int(r[1]) for r in rows.all()}
        rows = await session.execute(
            text(
                "SELECT left(metadata ->> '_extract_error', 160) AS msg, count(*) AS n "
                "FROM items WHERE library_id = :lib AND status='active' "
                "AND metadata ? '_extract_error' GROUP BY 1 ORDER BY n DESC LIMIT :lim"
            ),
            {"lib": str(library_id), "lim": _TOP_MESSAGES},
        )
        top = [{"message": errors_mod.sanitize_error(r[0]), "count": int(r[1])} for r in rows.all()]
    return {"count": count, "by_kind": by_kind, "top_messages": top}


async def _failed_jobs_for(session: AsyncSession, library_id) -> list[dict[str, Any]]:
    """Failed Procrastinate jobs whose args mention this library (scan/extract
    jobs carry ``library_id`` or an ``item_id`` — item ids are not resolvable
    cheaply here, so this is the library-scoped subset only)."""
    try:
        has = (await session.execute(text("SELECT to_regclass('procrastinate_jobs')"))).scalar()
        if not has:
            return []
        rows = await session.execute(
            text(
                "SELECT j.id::text AS id, j.task_name AS task, j.queue_name AS queue, "
                "j.attempts, j.scheduled_at, "
                "(SELECT je.message FROM job_errors je WHERE je.job_id = j.id "
                " ORDER BY je.created_at DESC LIMIT 1) AS error "
                "FROM procrastinate_jobs j "
                "WHERE j.status = 'failed' AND j.args ->> 'library_id' = :lib "
                "ORDER BY j.id DESC LIMIT 10"
            ),
            {"lib": str(library_id)},
        )
    except Exception:  # noqa: BLE001 - job_errors may not exist on old DBs
        return []
    return [
        {
            "id": r.id,
            "task": r.task,
            "queue": r.queue,
            "attempts": r.attempts,
            "scheduled_at": _iso(r.scheduled_at),
            "error": errors_mod.sanitize_error(r.error) if r.error else None,
        }
        for r in rows.all()
    ]


async def _recent_logs(session: AsyncSession, library_id, name: str) -> list[dict[str, Any]]:
    """WARNING+ app_logs lines that mention the library id or name (best-effort)."""
    try:
        has = (await session.execute(text("SELECT to_regclass('app_logs')"))).scalar()
        if not has:
            return []
        rows = await session.execute(
            text(
                "SELECT ts, source, level, logger, left(message, 400) AS message "
                "FROM app_logs WHERE levelno >= 30 AND "
                "(message ILIKE :idpat OR message ILIKE :namepat) "
                "ORDER BY ts DESC LIMIT :lim"
            ),
            {"idpat": f"%{library_id}%", "namepat": f"%{name}%", "lim": _LOG_LINES},
        )
    except Exception:  # noqa: BLE001
        return []
    return [
        {
            "ts": _iso(r.ts),
            "source": r.source,
            "level": r.level,
            "logger": r.logger,
            "message": r.message,
        }
        for r in rows.all()
    ]


async def _agent_status(session: AsyncSession, agent_id) -> dict[str, Any] | None:
    if agent_id is None:
        return None
    a = await session.get(Agent, agent_id)
    if a is None:
        return {"id": str(agent_id), "missing": True}
    now = datetime.now(UTC)
    seen = a.last_seen_at
    stale_s = (now - seen).total_seconds() if seen else None
    return {
        "id": str(a.id),
        "name": a.name,
        "hostname": a.hostname,
        "platform": a.platform,
        "last_seen_at": _iso(seen),
        "stale_seconds": stale_s,
        "revoked": a.revoked_at is not None,
        "online": stale_s is not None and stale_s < 300 and a.revoked_at is None,
    }


# --------------------------------------------------------------------------- #
# Verdicts                                                                     #
# --------------------------------------------------------------------------- #
_PERM_RE = re.compile(r"permission denied|EACCES|access is denied", re.I)
_MISSING_RE = re.compile(r"no such file|ENOENT|not found|does not exist", re.I)
_IO_RE = re.compile(
    r"input/output error|EIO|stale file handle|transport endpoint|host is down|ETIMEDOUT|timed out",
    re.I,
)


def _verdicts(
    lib: Library, path: dict, runs: list[dict], errs: dict, jobs: list[dict], agent: dict | None
) -> list[dict]:
    v: list[dict] = []
    agent_owned = lib.source_agent_id is not None
    last = runs[0] if runs else None

    # --- agent-owned libraries: central never opens the files --------------
    if agent_owned:
        if agent is None or agent.get("missing"):
            v.append(
                _verdict(
                    "error",
                    "agent-missing",
                    "Owning agent no longer exists",
                    "This library's files live on a remote agent that has been deleted; nothing can scan or serve it.",
                    actions=["Re-enrol the agent and re-add the library, or delete this library."],
                )
            )
        elif agent.get("revoked"):
            v.append(
                _verdict(
                    "error",
                    "agent-revoked",
                    "Owning agent is revoked",
                    "A revoked agent cannot report; scans and metadata for this library are frozen.",
                    actions=["Un-revoke or re-enrol the agent under Admin → Agents."],
                )
            )
        elif not agent.get("online"):
            v.append(
                _verdict(
                    "warn",
                    "agent-offline",
                    "Owning agent is offline",
                    f"Last contact {int((agent.get('stale_seconds') or 0) // 60)} min ago. Scans run ON the agent; central only receives results.",
                    actions=[
                        "Check the agent service on the remote host (filearr-agent service status) and its log.",
                        "Confirm the agent can reach central (policy/command polls in its log).",
                    ],
                    evidence={"last_seen_at": agent.get("last_seen_at")},
                )
            )
        else:
            v.append(
                _verdict(
                    "ok",
                    "agent-online",
                    "Owning agent is online",
                    "Central-side path probes do not apply — the path below is the agent's, not this container's.",
                )
            )

    # --- path health (central-owned libraries) --------------------------------
    if not agent_owned:
        if path.get("timeout"):
            v.append(
                _verdict(
                    "error",
                    "path-hung",
                    "Root path did not answer",
                    f"Listing {lib.root_path} timed out after {_PROBE_TIMEOUT_S:.0f}s — the classic sign of a hung SMB/NFS/rclone mount inside the container.",
                    actions=[
                        "Check the mount from the host (ls the same path); remount if it hangs there too.",
                        "For rclone/FUSE mounts, restart the mount service; the container needs bind propagation rslave to see a remount.",
                    ],
                    evidence={"error": path.get("error")},
                )
            )
        elif path.get("exists") is False:
            v.append(
                _verdict(
                    "error",
                    "path-missing",
                    "Root path does not exist inside the container",
                    f"{lib.root_path} is not present in the filearr container. Every file will be tombstoned as missing on the next scan (nothing is deleted).",
                    actions=[
                        "Docker tab → filearr → Edit: confirm the Media path mapping covers this directory.",
                        "If the share was renamed/moved, edit the library's root path (Edit → Root path) rather than re-adding it — identity is (library, rel_path).",
                    ],
                    evidence={k: path.get(k) for k in ("root_path", "error")},
                )
            )
        elif path.get("is_dir") is False:
            v.append(
                _verdict(
                    "error",
                    "path-not-dir",
                    "Root path is not a directory",
                    f"{lib.root_path} exists but is a file (or a broken mount point).",
                    actions=["Fix the mount / choose the directory above."],
                )
            )
        elif path.get("readable") is False or (
            path.get("error") and _PERM_RE.search(path["error"] or "")
        ):
            v.append(
                _verdict(
                    "error",
                    "path-permission",
                    "Root path is not readable by the container user",
                    "The filearr process (PUID/PGID, default 99:100) cannot read the directory.",
                    actions=[
                        "Unraid: run Docker Safe New Permissions on the share, or match PUID/PGID to the share owner.",
                        "Compose/Proxmox: chown/chmod the mount, or set FILEARR PUID/PGID to the owning uid:gid.",
                    ],
                    evidence={"error": path.get("error")},
                )
            )
        elif path.get("error"):
            sev = "error"
            code = "path-io"
            title = "Root path returned an I/O error"
            if _IO_RE.search(path["error"]):
                title = "Root path returned a transport/I-O error (mount is unhealthy)"
            v.append(
                _verdict(
                    sev,
                    code,
                    title,
                    path["error"],
                    actions=[
                        "Check the underlying mount (dmesg / rclone log / SMB server).",
                        "Re-run a scan once the mount is healthy; tombstoned files come back automatically.",
                    ],
                )
            )
        else:
            if path.get("empty"):
                v.append(
                    _verdict(
                        "warn",
                        "path-empty",
                        "Root path is EMPTY",
                        "The directory lists zero entries. A scan will tombstone every item as missing (nothing is deleted). Typical when a mount is not mounted yet: the empty mount point is what you are looking at.",
                        actions=[
                            "Confirm the share is actually mounted at this path (host: mount | grep <path>).",
                            "Wait for the mount before scanning; the recycle-bin retention protects tombstoned items meanwhile.",
                        ],
                    )
                )
            else:
                ms = path.get("listing_ms")
                slow = ms is not None and ms > 2000
                v.append(
                    _verdict(
                        "warn" if slow else "ok",
                        "path-ok",
                        "Root path is reachable" + (" but SLOW" if slow else ""),
                        f"Listed {path.get('entries_seen')} entr{'y' if path.get('entries_seen') == 1 else 'ies'} in {ms} ms"
                        + (
                            f" over a network filesystem ({path.get('fstype') or 'network'})"
                            if path.get("network")
                            else ""
                        )
                        + (
                            ". A first listing over 2 s usually means a struggling SMB/rclone mount; scans will be slow and may time out."
                            if slow
                            else "."
                        ),
                        evidence={
                            "listing_ms": ms,
                            "network": path.get("network"),
                            "fstype": path.get("fstype"),
                        },
                    )
                )
        if lib.watch_mode and path.get("network"):
            v.append(
                _verdict(
                    "warn",
                    "watch-on-network",
                    "Watch mode on a network mount",
                    "inotify is unreliable over SMB/NFS/FUSE — changes may never be seen. Use polling scans (a schedule) instead.",
                    actions=["Edit the library: turn watch mode off and set a scan schedule."],
                )
            )

    # --- last scan --------------------------------------------------------------
    if last is None:
        v.append(
            _verdict(
                "info",
                "never-scanned",
                "Never scanned",
                "No scan has run for this library yet.",
                actions=["Click Scan on the Libraries page."],
            )
        )
    else:
        st = last["status"]
        err = last.get("error") or ""
        if st == "failed":
            cause = "The scan crashed and was marked failed (never left 'running')."
            actions = [
                "Read the error below; the failed-jobs and log sections carry the traceback if the worker recorded one.",
                "Fix the cause, then Scan again — a scan is idempotent.",
            ]
            code = "scan-failed"
            if _PERM_RE.search(err):
                cause = "The scan hit a permission error partway through the tree."
                code = "scan-failed-permission"
            elif _IO_RE.search(err):
                cause = "The scan hit an I/O / transport error — the mount went away mid-walk."
                code = "scan-failed-io"
            elif _MISSING_RE.search(err):
                cause = "The scan lost its root or a directory mid-walk (mount unmounted?)."
                code = "scan-failed-missing"
            v.append(
                _verdict(
                    "error",
                    code,
                    "Last scan FAILED",
                    cause + (f" Error: {err}" if err else ""),
                    actions=actions,
                    evidence={
                        "scan_id": last["id"],
                        "started_at": last["started_at"],
                        "stats": last["stats"],
                    },
                )
            )
        elif st in ("stopped", "cancelled"):
            v.append(
                _verdict(
                    "warn",
                    "scan-stopped",
                    f"Last scan was {st}",
                    "Someone stopped/cancelled it (or the worker was restarted mid-scan and the crash handler marked it). Items seen so far are kept; nothing was tombstoned.",
                    actions=["Run the scan again to complete the walk."],
                )
            )
        elif st == "running":
            started = last.get("started_at")
            v.append(
                _verdict(
                    "info",
                    "scan-running",
                    "A scan is running",
                    f"Started {started}. If it has been running far longer than usual and the rate is 0, the worker may be stuck on a hung mount.",
                    actions=[
                        "Watch the live rate on the Libraries page; use Stop, then re-scan, if it sits at 0/s."
                    ],
                )
            )
        else:
            stats = last.get("stats") or {}
            missing = int(stats.get("missing") or 0)
            seen = int(stats.get("seen") or 0)
            if seen == 0 and missing > 0:
                v.append(
                    _verdict(
                        "error",
                        "scan-tombstoned-all",
                        "Last scan saw ZERO files and tombstoned everything",
                        f"{missing} items marked missing. Almost always an unmounted share at scan time — the tree looked empty. Nothing was deleted; the next successful scan restores them.",
                        actions=["Fix the mount (see the path verdict), then Scan."],
                        evidence=stats,
                    )
                )
            elif missing and seen and missing > seen:
                v.append(
                    _verdict(
                        "warn",
                        "scan-many-missing",
                        "Last scan tombstoned more than it saw",
                        f"seen {seen}, missing {missing}. A subtree may have gone away (renamed folder, unmounted sub-share).",
                        actions=[
                            "Check the folders that disappeared; a rename is best handled by editing the library root or moving files back."
                        ],
                        evidence=stats,
                    )
                )
            else:
                v.append(
                    _verdict(
                        "ok",
                        "scan-ok",
                        "Last scan finished",
                        f"seen {seen}, new {stats.get('new', 0)}, changed {stats.get('changed', 0)}, missing {missing}, excluded {stats.get('excluded', 0)}.",
                        evidence=stats,
                    )
                )
        # crash-loop detection: several failed runs in a row
        failed_streak = 0
        for r in runs:
            if r["status"] == "failed":
                failed_streak += 1
            else:
                break
        if failed_streak >= 2:
            v.append(
                _verdict(
                    "error",
                    "scan-crash-loop",
                    f"{failed_streak} consecutive failed scans",
                    "The same failure is recurring; the schedule will keep hitting it. Do not just re-run — read the error.",
                    evidence={
                        "recent": [
                            {
                                "status": r["status"],
                                "started_at": r["started_at"],
                                "error": r["error"],
                            }
                            for r in runs
                        ]
                    },
                )
            )

    # --- extraction errors ------------------------------------------------------
    if errs.get("count"):
        bk = errs.get("by_kind") or {}
        parts = []
        if bk.get("dependency"):
            parts.append(
                _verdict(
                    "error",
                    "extract-dependency",
                    f"{bk['dependency']} items failed on a MISSING TOOL",
                    "Extraction needs a binary that is not in the container (ffprobe/exiftool/pdftotext/tesseract...). This is a deployment problem, not a file problem: every affected file will fail the same way until the tool exists.",
                    actions=[
                        "Check Admin → Features / the About page for detected tools; install the missing one or use the image that bundles it.",
                        "Then 'Retry extracts' on the library.",
                    ],
                )
            )
        if bk.get("corrupt"):
            parts.append(
                _verdict(
                    "warn",
                    "extract-corrupt",
                    f"{bk['corrupt']} items are corrupt or unreadable files",
                    "The parser rejected the file itself (truncated download, bad tags, encrypted PDF). Expected on real libraries; the item is still indexed by name/path.",
                    actions=[
                        "Open the failing-items list to see which; replace or ignore. Retry only if you fixed the files."
                    ],
                )
            )
        if bk.get("guard"):
            parts.append(
                _verdict(
                    "info",
                    "extract-guard",
                    f"{bk['guard']} items were skipped by a safety ceiling",
                    "A deliberate limit (file too large, too many pages, decode budget) stopped extraction. Not an error to fix unless you raise the ceiling.",
                    actions=[
                        "See the extraction limits in Settings/Features if you want these processed."
                    ],
                )
            )
        other = errs["count"] - sum(bk.get(k, 0) for k in ("dependency", "corrupt", "guard"))
        if other > 0:
            parts.append(
                _verdict(
                    "warn",
                    "extract-error",
                    f"{other} items failed extraction with an I/O or unexpected error",
                    "Usually a read that failed mid-way (network mount hiccup) — a retry often clears them.",
                    actions=[
                        "'Retry extracts' on the library after confirming the mount is healthy.",
                        "If the same messages recur, the top-messages list below is what to search the logs for.",
                    ],
                )
            )
        v.extend(parts)
    else:
        v.append(
            _verdict(
                "ok", "extract-ok", "No extraction errors", "Every active item extracted cleanly."
            )
        )

    # --- failed jobs --------------------------------------------------------------
    if jobs:
        v.append(
            _verdict(
                "warn",
                "jobs-failed",
                f"{len(jobs)} failed background job(s) reference this library",
                "Jobs that failed after exhausting retries. Their recorded error (if the worker captured one) is listed in the Jobs section.",
                actions=[
                    "Read the error; scan/extract jobs are safe to re-trigger via Scan / Retry extracts.",
                    "Clear the history from the Admin page once understood.",
                ],
            )
        )

    # --- schedule / enabled -------------------------------------------------------
    if not lib.enabled:
        v.append(
            _verdict(
                "info",
                "disabled",
                "Library is disabled",
                "Scheduled scans skip it; manual scans still work.",
            )
        )
    order = {"error": 0, "warn": 1, "info": 2, "ok": 3}
    v.sort(key=lambda x: order.get(x["severity"], 9))
    return v


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #
async def diagnose_library(session: AsyncSession, lib: Library) -> dict[str, Any]:
    agent = await _agent_status(session, lib.source_agent_id)
    path = (
        await _probe_path(lib.root_path)
        if lib.source_agent_id is None
        else {"root_path": lib.root_path, "skipped": "agent-owned"}
    )
    runs = await _recent_runs(session, lib.id)
    errs = await _extract_error_summary(session, lib.id)
    jobs = await _failed_jobs_for(session, lib.id)
    logs = await _recent_logs(session, lib.id, lib.name)
    settings = get_settings()
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "docs_url": DOCS_PATH,
        "library": {
            "id": str(lib.id),
            "name": lib.name,
            "root_path": lib.root_path,
            "enabled": lib.enabled,
            "watch_mode": lib.watch_mode,
            "scan_cron": lib.scan_cron,
            "hash_policy": lib.hash_policy,
            "source_agent_id": str(lib.source_agent_id) if lib.source_agent_id else None,
            "native_prefix": lib.native_prefix,
            "share_prefix": lib.share_prefix,
        },
        "verdicts": _verdicts(lib, path, runs, errs, jobs, agent),
        "path": path,
        "scans": runs,
        "extract_errors": errs,
        "failed_jobs": jobs,
        "agent": agent,
        "logs": logs,
        "context": {
            "recycle_retention_days": getattr(settings, "recycle_retention_days", None),
            "worker_concurrency": getattr(settings, "worker_concurrency", None),
        },
    }
