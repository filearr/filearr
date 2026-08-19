"""W7-T6 (2026-08-19): fan the ``permissions`` collector's records out of an
inventory result into ``permission_snapshots`` rows.

Two entry points, both fail-soft (a malformed entry is skipped, never fails the
command completion / upload that carried it):

* :func:`ingest_entries` -- inline inventory results (``result.entries`` on the
  command completion) or already-parsed NDJSON lines;
* :func:`ingest_ndjson_gz` -- a stored upload blob.

Digest-gated: an entry whose normalized record hashes equal to the newest
stored snapshot for the same (agent, path) writes NOTHING (an unchanged
re-collection must not grow the table); ``retain`` oldest rows beyond the cap
per path are pruned. Best-effort ``item_id`` link: an agent-local absolute path
that maps to an agent library's root + rel_path.
"""

from __future__ import annotations

import gzip
import hashlib
import json
import logging
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.models import PermissionSnapshot

log = logging.getLogger(__name__)

COLLECTOR_KEY = "permissions"


def _digest(record: dict[str, Any]) -> str:
    core = {
        "owner": record.get("owner"),
        "group": record.get("group"),
        "entries": record.get("entries") or [],
        "fidelity": record.get("fidelity"),
        "posture": record.get("posture"),
    }
    return hashlib.sha256(
        json.dumps(core, sort_keys=True, separators=(",", ":"), default=str).encode()
    ).hexdigest()


def _principals(record: dict[str, Any]) -> list[str]:
    out: list[str] = []
    for p in (record.get("owner"), record.get("group")):
        if isinstance(p, dict) and p.get("id"):
            out.append(str(p["id"]))
    for e in record.get("entries") or []:
        p = (e or {}).get("principal") if isinstance(e, dict) else None
        if isinstance(p, dict) and p.get("id"):
            out.append(str(p["id"]))
    return sorted(set(out))


async def ingest_entries(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    command_id: uuid.UUID | None,
    entries: list[dict[str, Any]],
    retain: int | None = None,
) -> dict[str, int]:
    """Write snapshots for every entry carrying a ``permissions`` record.
    Returns ``{seen, written, unchanged, skipped}``."""
    keep = retain if retain is not None else int(get_settings().permission_snapshots_retain)
    seen = written = unchanged = skipped = 0
    now = datetime.now(UTC)
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rec = entry.get(COLLECTOR_KEY)
        if not isinstance(rec, dict) or not isinstance(rec.get("entries"), list):
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            skipped += 1
            continue
        seen += 1
        digest = _digest(rec)
        latest = (
            await session.execute(
                select(PermissionSnapshot.digest)
                .where(PermissionSnapshot.agent_id == agent_id, PermissionSnapshot.path == path)
                .order_by(PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if latest == digest:
            unchanged += 1
            continue
        collected = rec.get("collected_at")
        try:
            collected_at = (
                datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
                if collected
                else now
            )
        except ValueError:
            collected_at = now
        session.add(
            PermissionSnapshot(
                agent_id=agent_id,
                command_id=command_id,
                path=path,
                is_dir=bool(entry.get("is_dir") or (entry.get("stat") or {}).get("is_dir")),
                collected_at=collected_at,
                owner=rec.get("owner"),
                group_=rec.get("group"),
                aces=rec.get("entries") or [],
                posture=rec.get("posture"),
                fidelity=str(rec.get("fidelity") or "unavailable"),
                principals=_principals(rec),
                digest=digest,
            )
        )
        written += 1
        await session.flush()  # the new row takes part in the retention ranking
        # retention per (agent, path): keep the newest `keep` rows
        if keep > 0:
            old_ids = (
                await session.execute(
                    select(PermissionSnapshot.id)
                    .where(
                        PermissionSnapshot.agent_id == agent_id, PermissionSnapshot.path == path
                    )
                    .order_by(PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc())
                    .offset(keep)
                )
            ).scalars().all()
            if old_ids:
                await session.execute(
                    delete(PermissionSnapshot).where(PermissionSnapshot.id.in_(old_ids))
                )
    if written:
        await session.commit()
    return {"seen": seen, "written": written, "unchanged": unchanged, "skipped": skipped}


async def ingest_ndjson_gz(
    session: AsyncSession, *, agent_id: uuid.UUID, command_id: uuid.UUID, blob: bytes
) -> dict[str, int]:
    """Parse a stored inventory upload (gzip NDJSON, one entry per line) and
    ingest it. Lines that are not JSON objects are skipped."""
    entries: list[dict[str, Any]] = []
    try:
        text = gzip.decompress(blob).decode("utf-8", errors="replace")
    except Exception as exc:  # noqa: BLE001
        log.warning("permission ingest: cannot decompress inventory result: %s", exc)
        return {"seen": 0, "written": 0, "unchanged": 0, "skipped": 0}
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict) and COLLECTOR_KEY in obj:
            entries.append(obj)
    return await ingest_entries(session, agent_id=agent_id, command_id=command_id, entries=entries)
