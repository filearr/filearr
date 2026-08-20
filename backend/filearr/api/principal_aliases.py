"""W7-T8 (2026-08-20): principal-alias administration.

Maps raw permission-principal identifiers (an agent-local ``local:<host>:1000``,
a SID, a bare uid) onto one canonical cross-host identity with an optional
display name. Applied report-side (permissions reports join through the table);
snapshots stay verbatim. Admin scope; tiny table (hundreds at most)."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import PrincipalAlias
from filearr.security import require_scope

router = APIRouter()

_MAX_LEN = 256


class AliasIn(BaseModel):
    alias: str = Field(min_length=1, max_length=_MAX_LEN)
    canonical: str = Field(min_length=1, max_length=_MAX_LEN)
    display: str | None = Field(default=None, max_length=_MAX_LEN)


@router.get("", dependencies=[Depends(require_scope("admin"))])
async def list_aliases(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        (await session.execute(select(PrincipalAlias).order_by(PrincipalAlias.canonical)))
        .scalars()
        .all()
    )
    return {
        "aliases": [
            {
                "alias": r.alias,
                "canonical": r.canonical,
                "display": r.display,
                # 'manual' (this API) or 'ldap' (the directory sync). An 'ldap'
                # row is overwritten by the next sync unless a manual PUT with
                # ?force=true claims it (source flips to 'manual', pinning it).
                "source": r.source,
                "created_at": r.created_at.isoformat(),
            }
            for r in rows
        ]
    }


@router.put("", dependencies=[Depends(require_scope("admin"))], status_code=200)
async def upsert_aliases(
    body: list[AliasIn],
    force: bool = Query(
        default=False,
        description="overwrite an alias currently owned by the directory sync "
        "(source='ldap'); the row's source flips to 'manual' and future syncs "
        "leave it alone",
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Upsert one or many MANUAL mappings (idempotent — re-PUT to edit).

    A manual PUT never silently overwrites an LDAP-synced alias: the conflict
    update is gated on ``source = 'manual'`` unless ``?force=true`` (which claims
    the row as manual so the sync stops touching it). ``skipped`` counts rows
    left untouched because they belong to the directory sync."""
    if len(body) > 500:
        raise HTTPException(422, "at most 500 aliases per request")
    upserted = skipped = 0
    for row in body:
        set_ = {
            "canonical": row.canonical.strip(),
            "display": (row.display or "").strip() or None,
            "source": "manual",
        }
        stmt = pg_insert(PrincipalAlias).values(
            alias=row.alias.strip(),
            canonical=row.canonical.strip(),
            display=(row.display or "").strip() or None,
            source="manual",
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[PrincipalAlias.alias],
            set_=set_,
            # Only claim the row when it is already manual, unless forced.
            where=None if force else (PrincipalAlias.source == "manual"),
        )
        result = await session.execute(stmt)
        if result.rowcount:
            upserted += 1
        else:
            skipped += 1
    await session.commit()
    return {"upserted": upserted, "skipped": skipped}


@router.delete("/{alias:path}", dependencies=[Depends(require_scope("admin"))], status_code=204)
async def delete_alias(alias: str, session: AsyncSession = Depends(get_session)) -> None:
    result = await session.execute(delete(PrincipalAlias).where(PrincipalAlias.alias == alias))
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "no such alias")


# --------------------------------------------------------------------------- #
# W7-T10 (2026-08-20): effective-access inspection                             #
# --------------------------------------------------------------------------- #


perm_router = APIRouter()


@perm_router.get(
    "/permissions/effective-access", dependencies=[Depends(require_scope("admin"))]
)
async def effective_access_endpoint(
    agent_id: str,
    path: str,
    principal: list[str] | None = Query(default=None),
    expand_groups: bool = Query(
        default=True,
        description="expand the supplied SIDs with their AD group memberships "
        "from the synced directory (nested groups included), so a grant to a "
        "group attributes to its members; set false to evaluate only the "
        "identities you passed",
    ),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """What can these principal identities actually DO on this path?

    Loads the NEWEST stored permission snapshot for (agent, path) and runs the
    pure §3.5 evaluator (``permissions.effective_access``): ordered
    deny-before-allow over the ACEs, POSIX owner/group/other class selection,
    and local ∩ share layer intersection. ``principal`` is repeatable and
    should carry every identity the caller answers to (uid, SID, names). With
    ``expand_groups`` (default on) the AD group closure is added automatically
    from the synced directory (LDAP-T1) — otherwise group membership is the
    CALLER's job and nothing is guessed."""
    import uuid as _uuid

    from filearr.ldap_directory import expand_principals_with_groups
    from filearr.models import PermissionSnapshot
    from filearr.permissions import effective_access, record_from_wire

    try:
        aid = _uuid.UUID(agent_id)
    except ValueError as exc:
        raise HTTPException(422, "agent_id must be a UUID") from exc
    principals = {p for p in (principal or []) if p.strip()}
    if not principals:
        raise HTTPException(422, "at least one principal identity is required")
    supplied = sorted(principals)
    if expand_groups:
        principals = await expand_principals_with_groups(session, principals)
    snap = (
        await session.execute(
            select(PermissionSnapshot)
            .where(PermissionSnapshot.agent_id == aid, PermissionSnapshot.path == path)
            .order_by(PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if snap is None:
        raise HTTPException(404, "no permission snapshot for that agent/path")
    record = record_from_wire(
        owner=snap.owner,
        group=snap.group_,
        aces=snap.aces,
        fidelity=snap.fidelity,
        posture=snap.posture,
    )
    result = effective_access(record, principals)
    return {
        "agent_id": agent_id,
        "path": path,
        "principals": supplied,
        "principals_effective": sorted(principals),
        "group_expansion": expand_groups,
        "collected_at": snap.collected_at.isoformat(),
        "fidelity": snap.fidelity,
        "verbs": sorted(result.verbs),
        "denied": sorted(result.denied),
        "by_source": {k: sorted(v) for k, v in result.by_source.items()},
        "matched_aces": result.matched,
    }
