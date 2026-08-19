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
that maps to an agent library's root + rel_path (so the library filter and RBAC
scoping of the permission reports work for catalogued paths).

W7-T9 (2026-08-19): when a NEW snapshot replaces a previous one for the same
(agent, path), the two are diffed (``permissions.diff_records``) after the batch
commit and a ``permission_changed`` alert event is filed for the "System:
permission change" rule (``alerts.ops.emit_permission_change``) — best-effort,
never failing the ingest.
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
from filearr.models import Agent, Item, Library, PermissionSnapshot

log = logging.getLogger(__name__)

COLLECTOR_KEY = "permissions"


def _norm_sep(p: str) -> str:
    return p.replace("\\", "/")


async def _agent_roots(session: AsyncSession, agent_id: uuid.UUID) -> list[tuple[str, uuid.UUID]]:
    """(normalized agent-side root, library_id) for every library this agent
    replicates -- longest root first so the most specific library wins."""
    rows = (
        await session.execute(
            select(Library.root_path, Library.id).where(Library.source_agent_id == agent_id)
        )
    ).all()
    roots = [(_norm_sep(r[0]).rstrip("/"), r[1]) for r in rows if r[0]]
    roots.sort(key=lambda t: len(t[0]), reverse=True)
    return roots


async def _link_item(
    session: AsyncSession, roots: list[tuple[str, uuid.UUID]], path: str
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Best-effort (item_id, library_id) for an agent-local absolute path: the
    longest library root that prefixes it, then the item at that rel_path.
    Windows paths compare case-insensitively on the root; rel_path is looked up
    verbatim (the catalog stores the agent's spelling)."""
    np = _norm_sep(path)
    for root, lib_id in roots:
        if not root:
            continue
        cand = np
        if cand == root or cand.lower() == root.lower():
            return None, lib_id  # the library root itself is not an item
        if cand[: len(root)].lower() == root.lower() and cand[len(root) : len(root) + 1] == "/":
            rel = cand[len(root) + 1 :]
            item_id = (
                await session.execute(
                    select(Item.id).where(Item.library_id == lib_id, Item.rel_path == rel).limit(1)
                )
            ).scalar_one_or_none()
            return item_id, lib_id
    return None, None


async def _emit_change_alert(
    session: AsyncSession,
    *,
    agent_id: uuid.UUID,
    agent_name: str | None,
    path: str,
    item_id: uuid.UUID | None,
    library_id: uuid.UUID | None,
    prev: PermissionSnapshot,
    rec: dict[str, Any],
    digest: str,
) -> None:
    """W7-T9: diff the new record against the previous snapshot and file a
    ``permission_changed`` alert event when anything material moved. Wrapped:
    an alert-layer fault must never fail the ingest."""
    try:
        from filearr.alerts.ops import emit_permission_change
        from filearr.permissions import diff_records, record_from_wire, summarize_diff

        old = record_from_wire(
            owner=prev.owner,
            group=prev.group_,
            aces=prev.aces,
            fidelity=prev.fidelity,
            posture=prev.posture,
        )
        new = record_from_wire(
            owner=rec.get("owner"),
            group=rec.get("group"),
            aces=rec.get("entries"),
            fidelity=rec.get("fidelity"),
            posture=rec.get("posture"),
        )
        diff = diff_records(old, new)
        if diff.is_empty:
            return  # fidelity/posture-only change: recorded, not alerted
        await emit_permission_change(
            session,
            agent_id=str(agent_id),
            agent_name=agent_name or str(agent_id),
            path=path,
            item_id=item_id,
            library_id=library_id,
            summary=summarize_diff(diff),
            added=len(diff.added),
            removed=len(diff.removed),
            modified=len(diff.modified),
            owner_changed=diff.owner_changed or diff.group_changed,
            digest=digest,
        )
    except Exception as exc:  # noqa: BLE001 - alerting is best-effort
        log.warning("permission change alert skipped for %s: %s", path, exc)


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
    roots: list[tuple[str, uuid.UUID]] | None = None
    agent_name: str | None = None
    # (path, item_id, library_id, previous row, record, digest) for the change
    # alerts -- emitted after the batch commit so the events never outrun rows.
    changed: list[tuple[str, uuid.UUID | None, uuid.UUID | None, PermissionSnapshot, dict, str]]
    changed = []
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
        prev = (
            await session.execute(
                select(PermissionSnapshot)
                .where(PermissionSnapshot.agent_id == agent_id, PermissionSnapshot.path == path)
                .order_by(PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if prev is not None and prev.digest == digest:
            unchanged += 1
            continue
        if roots is None:
            roots = await _agent_roots(session, agent_id)
            agent_name = (
                await session.execute(select(Agent.name).where(Agent.id == agent_id))
            ).scalar_one_or_none()
        item_id, library_id = await _link_item(session, roots, path)
        collected = rec.get("collected_at")
        try:
            collected_at = (
                datetime.fromisoformat(str(collected).replace("Z", "+00:00")) if collected else now
            )
        except ValueError:
            collected_at = now
        if prev is not None:
            # detach the previous row's values before retention may delete it
            session.expunge(prev)
            changed.append((path, item_id, library_id, prev, rec, digest))
        session.add(
            PermissionSnapshot(
                agent_id=agent_id,
                command_id=command_id,
                item_id=item_id,
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
                (
                    await session.execute(
                        select(PermissionSnapshot.id)
                        .where(
                            PermissionSnapshot.agent_id == agent_id, PermissionSnapshot.path == path
                        )
                        .order_by(
                            PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc()
                        )
                        .offset(keep)
                    )
                )
                .scalars()
                .all()
            )
            if old_ids:
                await session.execute(
                    delete(PermissionSnapshot).where(PermissionSnapshot.id.in_(old_ids))
                )
    if written:
        await session.commit()
    for path, item_id, library_id, prev, rec, digest in changed:
        await _emit_change_alert(
            session,
            agent_id=agent_id,
            agent_name=agent_name,
            path=path,
            item_id=item_id,
            library_id=library_id,
            prev=prev,
            rec=rec,
            digest=digest,
        )
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
