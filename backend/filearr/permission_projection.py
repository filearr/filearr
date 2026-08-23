"""Permission facts projected onto the search index (2026-08-23).

W7 stores a normalized ACL snapshot per agent-collected path
(``permission_snapshots``); until now it was only reachable through the
permission REPORTS and the per-item detail view had nothing. This module turns
the newest snapshot of an item into three small, filterable search fields so
``/search`` can answer "what can <principal> reach?" and "what is world-
readable?" without a report:

* ``perm_principals`` — every principal with an ALLOW entry (plus the owner),
  as BOTH the canonical id (SID / uid) and the resolved display name, minus any
  principal that also carries a DENY entry. Facet-searchable (type-ahead).
* ``perm_world``     — True when one of the world principals (Everyone /
  S-1-1-0, POSIX "other"/"world", Authenticated Users) has an allow entry.
* ``perm_owner``     — the owner's display name or id.

The projection is DERIVED (the snapshot table is the truth), so a later
``rebuild_index`` reproduces it exactly; :func:`permission_summary_map` is the
one batch lookup every ``build_doc`` call site uses. Items without a snapshot
(central-scanned libraries — permissions are collected by agents only) project
``[]`` / ``False`` / ``None`` so the filters stay meaningful across the catalog.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import select

from filearr.models import PermissionSnapshot

#: Canonical ids / names that mean "everyone" on their platform.
WORLD_IDS: frozenset[str] = frozenset(
    {"s-1-1-0", "s-1-5-11", "everyone", "authenticated users", "other", "world"}
)

EMPTY_SUMMARY: dict[str, Any] = {"perm_principals": [], "perm_world": False, "perm_owner": None}


def _names(p: dict | None) -> list[str]:
    """The identifiers a principal is addressable by: canonical id, raw source
    identifier and display name (deduped, order kept)."""
    if not isinstance(p, dict):
        return []
    out: list[str] = []
    for key in ("canonical_id", "id", "source_identifier", "display", "name"):
        v = p.get(key)
        if isinstance(v, str) and v.strip() and v not in out:
            out.append(v.strip())
    return out


def _is_world(p: dict | None) -> bool:
    return any(n.lower() in WORLD_IDS for n in _names(p))


def summary_for_snapshot(snapshot: PermissionSnapshot | None) -> dict[str, Any]:
    """Reduce one snapshot to the three projected fields."""
    if snapshot is None:
        return dict(EMPTY_SUMMARY)
    allow: list[str] = []
    denied: set[str] = set()
    world = False
    for ace in snapshot.aces or []:
        if not isinstance(ace, dict):
            continue
        p = ace.get("principal")
        names = _names(p)
        if not names:
            continue
        ace_type = str(ace.get("type") or "allow").lower()
        scope = str(ace.get("scope") or "this").lower()
        if scope == "dir_default":
            continue  # POSIX default ACL: who CHILDREN inherit, not access now
        if ace_type == "deny":
            denied.update(names)
            continue
        for n in names:
            if n not in allow:
                allow.append(n)
        if _is_world(p):
            world = True
    owner_names = _names(snapshot.owner)
    for n in owner_names:
        if n not in allow:
            allow.append(n)
    principals = [n for n in allow if n not in denied]
    return {
        "perm_principals": principals,
        "perm_world": bool(world),
        "perm_owner": owner_names[-1] if owner_names else None,
    }


async def permission_summary_map(session: Any, items: list[Any]) -> dict[Any, dict[str, Any]]:
    """``{item_id: summary}`` for the NEWEST snapshot of every item in ``items``
    that has one (single DISTINCT ON query). Items without a snapshot are
    absent — callers fall back to :data:`EMPTY_SUMMARY` via ``build_doc``."""
    ids = [i.id for i in items if getattr(i, "id", None) is not None]
    if not ids:
        return {}
    # Newest row per item: DISTINCT ON (ids are uuidv7 — time-ordered but
    # Postgres has no max(uuid) — so order explicitly by collected_at, id).
    stmt = (
        select(PermissionSnapshot)
        .distinct(PermissionSnapshot.item_id)
        .where(PermissionSnapshot.item_id.in_(ids))
        .order_by(
            PermissionSnapshot.item_id,
            PermissionSnapshot.collected_at.desc(),
            PermissionSnapshot.id.desc(),
        )
    )
    rows = (await session.execute(stmt)).scalars().all()
    return {r.item_id: summary_for_snapshot(r) for r in rows}
