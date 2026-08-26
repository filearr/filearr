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

from sqlalchemy import delete, select, tuple_
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.models import Agent, Item, Library, PermissionSnapshot

log = logging.getLogger(__name__)

COLLECTOR_KEY = "permissions"
# 2026-08-23: the agent's walker merges every collector's returned map FLAT
# into the entry, and the permissions collector (agent <= 1.5.3) returned its
# Record under the key ``record`` -- so no wire entry ever carried a
# ``permissions`` key and NOTHING was ever ingested (live: xenon, weeks of
# "permissions enabled", zero snapshots). Agents >= 1.5.4 emit ``permissions``;
# the legacy key stays accepted so an un-upgraded fleet starts working the
# moment central is upgraded.
LEGACY_COLLECTOR_KEY = "record"


def record_of(entry: dict[str, Any]) -> dict[str, Any] | None:
    """The permissions record carried by an inventory entry, or None. Accepts
    the current ``permissions`` key and the legacy ``record`` key; normalises a
    JSON ``null`` ACE list (Go marshals a nil slice as null -- a file whose
    every ACE was filtered out) to an empty list so it still counts as a
    record."""
    rec = entry.get(COLLECTOR_KEY)
    if not isinstance(rec, dict):
        rec = entry.get(LEGACY_COLLECTOR_KEY)
    if not isinstance(rec, dict) or "owner" not in rec:
        return None
    entries = rec.get("entries")
    if entries is None:
        rec = {**rec, "entries": []}
    elif not isinstance(entries, list):
        return None
    return rec


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


INGEST_CHUNK = 500


def _candidate(
    roots: list[tuple[str, uuid.UUID]], path: str
) -> tuple[uuid.UUID | None, str | None]:
    """(library_id, rel_path) an agent-local absolute path would have in the
    catalog: the longest library root that prefixes it. Windows paths compare
    case-insensitively on the root; rel_path keeps the agent's spelling (the
    catalog stores it verbatim). The library root itself is not an item
    (``(lib, None)``); a path under no root is ``(None, None)``."""
    np = _norm_sep(path)
    for root, lib_id in roots:
        if not root:
            continue
        if np == root or np.lower() == root.lower():
            return lib_id, None
        if np[: len(root)].lower() == root.lower() and np[len(root) : len(root) + 1] == "/":
            return lib_id, np[len(root) + 1 :]
    return None, None


async def _link_item(
    session: AsyncSession, roots: list[tuple[str, uuid.UUID]], path: str
) -> tuple[uuid.UUID | None, uuid.UUID | None]:
    """Best-effort (item_id, library_id) for ONE path (the single-entry form of
    the chunked lookup ``ingest_entries`` uses)."""
    lib_id, rel = _candidate(roots, path)
    if lib_id is None:
        return None, None
    if rel is None:
        return None, lib_id
    items = await _items_for(session, {(lib_id, rel)})
    return items.get((lib_id, rel)), lib_id


async def _items_for(
    session: AsyncSession, pairs: set[tuple[uuid.UUID, str]]
) -> dict[tuple[uuid.UUID, str], uuid.UUID]:
    """item id per (library_id, rel_path), one query per chunk."""
    if not pairs:
        return {}
    rows = (
        await session.execute(
            select(Item.library_id, Item.rel_path, Item.id).where(
                tuple_(Item.library_id, Item.rel_path).in_(list(pairs))
            )
        )
    ).all()
    return {(r[0], r[1]): r[2] for r in rows}


async def _newest_snapshots(
    session: AsyncSession, agent_id: uuid.UUID, paths: list[str]
) -> dict[str, PermissionSnapshot]:
    """Newest stored snapshot per path for this agent (the digest gate and the
    change-alert baseline), one DISTINCT ON query per chunk."""
    if not paths:
        return {}
    rows = (
        await session.execute(
            select(PermissionSnapshot)
            .distinct(PermissionSnapshot.path)
            .where(PermissionSnapshot.agent_id == agent_id, PermissionSnapshot.path.in_(paths))
            .order_by(
                PermissionSnapshot.path,
                PermissionSnapshot.collected_at.desc(),
                PermissionSnapshot.id.desc(),
            )
        )
    ).scalars()
    return {r.path: r for r in rows}


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
    Returns ``{seen, written, unchanged, skipped}``.

    2026-08-25: chunked. The original loop issued two SELECTs per entry (the
    previous snapshot, then the item link) -- ~200k round-trips for a 100k-file
    drive, minutes inside the upload request. Each chunk now prefetches the
    newest snapshot per path and the catalog items for every candidate
    (library, rel_path) in two queries, and commits per chunk so a crash
    mid-blob keeps what landed.
    """
    keep = retain if retain is not None else int(get_settings().permission_snapshots_retain)
    seen = written = unchanged = skipped = 0
    now = datetime.now(UTC)
    roots = await _agent_roots(session, agent_id)
    agent_name = (
        await session.execute(select(Agent.name).where(Agent.id == agent_id))
    ).scalar_one_or_none()
    # (path, item_id, library_id, previous row, record, digest) for the change
    # alerts -- emitted after the commits so the events never outrun rows.
    changed: list[tuple[str, uuid.UUID | None, uuid.UUID | None, PermissionSnapshot, dict, str]]
    changed = []
    touched_items: list[uuid.UUID] = []

    # Normalise first: only well-formed (path, record) pairs reach the DB.
    work: list[tuple[str, dict[str, Any], dict[str, Any]]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        rec = record_of(entry)
        if rec is None:
            continue
        path = entry.get("path")
        if not isinstance(path, str) or not path:
            skipped += 1
            continue
        seen += 1
        work.append((path, rec, entry))

    for start in range(0, len(work), INGEST_CHUNK):
        chunk = work[start : start + INGEST_CHUNK]
        paths = [w[0] for w in chunk]
        prev_by_path = await _newest_snapshots(session, agent_id, paths)
        # Resolve every chunk path to its (library, rel) candidate up front so
        # the item lookup is one query.
        cands: dict[str, tuple[uuid.UUID | None, str | None]] = {}
        for path in paths:
            cands[path] = _candidate(roots, path)
        pairs = {
            (lib, rel) for lib, rel in cands.values() if lib is not None and rel is not None
        }
        items = await _items_for(session, pairs)
        wrote_this_chunk = 0
        for path, rec, entry in chunk:
            digest = _digest(rec)
            prev = prev_by_path.get(path)
            if prev is not None and prev.digest == digest:
                unchanged += 1
                continue
            lib_id, rel = cands[path]
            item_id = items.get((lib_id, rel)) if lib_id is not None and rel is not None else None
            if item_id is not None:
                touched_items.append(item_id)
            collected = rec.get("collected_at")
            try:
                collected_at = (
                    datetime.fromisoformat(str(collected).replace("Z", "+00:00"))
                    if collected
                    else now
                )
            except ValueError:
                collected_at = now
            if prev is not None:
                # detach the previous row's values before retention may delete it
                session.expunge(prev)
                changed.append((path, item_id, lib_id, prev, rec, digest))
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
            wrote_this_chunk += 1
            await session.flush()  # the new row takes part in the retention ranking
            # retention per (agent, path): keep the newest `keep` rows
            if keep > 0:
                old_ids = (
                    (
                        await session.execute(
                            select(PermissionSnapshot.id)
                            .where(
                                PermissionSnapshot.agent_id == agent_id,
                                PermissionSnapshot.path == path,
                            )
                            .order_by(
                                PermissionSnapshot.collected_at.desc(),
                                PermissionSnapshot.id.desc(),
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
        if wrote_this_chunk:
            await session.commit()
    # 2026-08-23: the search projection carries who-can-read / world-readable /
    # owner per item (permission_projection), so a new snapshot must refresh
    # the linked items' docs. Best-effort, after the commit (invariant 5).
    linked = [str(iid) for iid in touched_items]
    if linked:
        try:
            from filearr import worker as _worker

            for i in range(0, len(linked), 500):
                await _worker.defer_index_sync(linked[i : i + 500])
        except Exception:  # noqa: BLE001 — projection refresh must never fail ingest
            log.warning("permission ingest: index_sync defer failed", exc_info=True)
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
        if isinstance(obj, dict) and (COLLECTOR_KEY in obj or LEGACY_COLLECTOR_KEY in obj):
            entries.append(obj)
    return await ingest_entries(session, agent_id=agent_id, command_id=command_id, entries=entries)
