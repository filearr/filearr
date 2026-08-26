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

#: Canonical ids / names that mean "everyone" on their platform (kept for the
#: principal-list projection; the exposure tiers below are what decide
#: "world-readable").
WORLD_IDS: frozenset[str] = frozenset(
    {"s-1-1-0", "s-1-5-11", "everyone", "authenticated users", "other", "world"}
)

# 2026-08-26 exposure tiers (user report: a file the share granted "Everyone
# Read" but whose NTFS ACL only named Authenticated Users showed as world-
# readable, while Windows' own Effective Access denied ANONYMOUS LOGON). Two
# distinct questions, answered separately:
#   * ANONYMOUS  -- can a caller with NO credential read it? Only principals
#     that include anonymous: Everyone (S-1-1-0), ANONYMOUS LOGON (S-1-5-7),
#     POSIX other/world, a NULL DACL.
#   * AUTHENTICATED -- can any logged-in account read it? The above plus
#     Authenticated Users (S-1-5-11).
# And two LAYERS: the object's own ACL ("local": NTFS / POSIX / macOS) and, when
# the collector saw one, the SMB share's ACL ("share"). Access through a share
# is capped by BOTH (Windows evaluates share ∩ file), so the effective verdict
# is the AND of the layers; the layers are also compared, because the common
# misconfiguration is exactly a share that is wider (or narrower) than the
# files behind it.
ANONYMOUS_IDS: frozenset[str] = frozenset(
    {"s-1-1-0", "everyone", "other", "world", "s-1-5-7", "anonymous logon", "anonymous"}
)
AUTHENTICATED_IDS: frozenset[str] = ANONYMOUS_IDS | frozenset({"s-1-5-11", "authenticated users"})
_READ_VERBS: frozenset[str] = frozenset({"read", "full", "full_control"})

EXPOSURE_ANONYMOUS = "anonymous"
EXPOSURE_AUTHENTICATED = "authenticated"
EXPOSURE_RESTRICTED = "restricted"
EXPOSURES: tuple[str, ...] = (EXPOSURE_ANONYMOUS, EXPOSURE_AUTHENTICATED, EXPOSURE_RESTRICTED)

EMPTY_SUMMARY: dict[str, Any] = {
    "perm_principals": [],
    "perm_world": False,
    "perm_owner": None,
    "perm_exposure": None,
    "perm_share_mismatch": False,
    "layers": None,
    "share_names": [],
}


def _tier_reads(aces: list[dict], ids: frozenset[str]) -> bool:
    """Whether the principals in ``ids`` end up with READ on this layer, walking
    the ACEs in stored order with deny-before-allow (NTFS canonical order: a
    deny seen first removes the right for good; an allow seen first keeps it).
    Default ACL entries (POSIX ``dir_default``) template children, not this
    object, and are skipped."""
    allowed = denied = False
    for ace in aces:
        if not isinstance(ace, dict):
            continue
        if str(ace.get("scope") or "this").lower() == "dir_default":
            continue
        names = {n.lower() for n in _names(ace.get("principal"))}
        if not (names & ids):
            continue
        verbs = {str(v).lower() for v in (ace.get("verbs") or [])}
        if not (verbs & _READ_VERBS):
            continue
        if str(ace.get("type") or "allow").lower() == "deny":
            if not allowed:
                denied = True
        elif not denied:
            allowed = True
    return allowed and not denied


def _layer(aces: list[dict]) -> dict[str, bool]:
    return {
        "anonymous": _tier_reads(aces, ANONYMOUS_IDS),
        "authenticated": _tier_reads(aces, AUTHENTICATED_IDS),
    }


def exposure_layers(aces: list[dict] | None) -> dict[str, Any]:
    """Per-layer and effective read exposure for one ACE list.

    Returns ``{"local": {...}, "share": {...} | None, "effective": {...},
    "share_mismatch": bool, "share_names": [...], "exposure": tier}`` where
    each ``{...}`` is ``{"anonymous": bool, "authenticated": bool}``. With no
    share-source ACEs the effective verdict IS the local one; with them it is
    the AND of both layers, and ``share_mismatch`` says the two disagree on
    either tier (the reconciliation report lists exactly those files)."""
    rows = [a for a in (aces or []) if isinstance(a, dict)]
    local = [a for a in rows if str(a.get("source") or "local") != "share"]
    share = [a for a in rows if str(a.get("source") or "") == "share"]
    lv = _layer(local)
    sv = _layer(share) if share else None
    if sv is None:
        eff = dict(lv)
    else:
        eff = {k: bool(lv[k] and sv[k]) for k in lv}
    exposure = (
        EXPOSURE_ANONYMOUS
        if eff["anonymous"]
        else EXPOSURE_AUTHENTICATED
        if eff["authenticated"]
        else EXPOSURE_RESTRICTED
    )
    names = sorted({str(a.get("share")) for a in share if a.get("share")})
    return {
        "local": lv,
        "share": sv,
        "effective": eff,
        "share_mismatch": bool(sv is not None and sv != lv),
        "share_names": names,
        "exposure": exposure,
    }


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
    layers = exposure_layers(snapshot.aces or [])
    del world  # superseded by the tiered, layer-reconciled verdict below
    return {
        "perm_principals": principals,
        # "World-readable" now means the EFFECTIVE anonymous tier: through the
        # share when share ACEs were collected, else the object's own ACL. A
        # file only Authenticated Users can read is NOT world-readable.
        "perm_world": bool(layers["effective"]["anonymous"]),
        "perm_owner": owner_names[-1] if owner_names else None,
        "perm_exposure": layers["exposure"],
        "perm_share_mismatch": layers["share_mismatch"],
        "layers": {
            "local": layers["local"],
            "share": layers["share"],
            "effective": layers["effective"],
        },
        "share_names": layers["share_names"],
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
