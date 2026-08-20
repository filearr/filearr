"""AD/LDAP directory objects + sync trigger (LDAP-T1, 2026-08-20).

Browse the synced directory, see reconciliation health (how many agent-pushed
SIDs resolve to a named object), and trigger a sync on demand. Admin scope. The
sync itself runs as the ``sync_directory`` maintenance task (scheduled +
here-triggered); this router never blocks on LDAP I/O."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, or_, select, text
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import DirectoryObject
from filearr.security import require_scope

router = APIRouter()


def _obj(r: DirectoryObject) -> dict:
    return {
        "object_guid": r.object_guid,
        "object_sid": r.object_sid,
        "sam_account_name": r.sam_account_name,
        "display_name": r.display_name,
        "user_principal_name": r.user_principal_name,
        "distinguished_name": r.distinguished_name,
        "kind": r.kind,
        "domain": r.domain,
        "member_of_sids": list(r.member_of_sids or []),
        "disabled": r.disabled,
        "deleted": r.deleted_at is not None,
        "last_synced_at": r.last_synced_at.isoformat() if r.last_synced_at else None,
    }


@router.get("/objects", dependencies=[Depends(require_scope("admin"))])
async def list_objects(
    q: str | None = Query(
        default=None, description="substring over sAMAccountName / displayName / SID"
    ),
    kind: str | None = Query(default=None, pattern="^(user|group|computer|other)$"),
    include_deleted: bool = False,
    limit: int = Query(default=100, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    session: AsyncSession = Depends(get_session),
) -> dict:
    """Paginated directory browse (admin). Filter by kind and a free substring."""
    stmt = select(DirectoryObject)
    if not include_deleted:
        stmt = stmt.where(DirectoryObject.deleted_at.is_(None))
    if kind:
        stmt = stmt.where(DirectoryObject.kind == kind)
    if q:
        like = f"%{q.strip()}%"
        stmt = stmt.where(
            or_(
                DirectoryObject.sam_account_name.ilike(like),
                DirectoryObject.display_name.ilike(like),
                DirectoryObject.object_sid.ilike(like),
            )
        )
    total = (
        await session.execute(select(func.count()).select_from(stmt.subquery()))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                stmt.order_by(DirectoryObject.sam_account_name, DirectoryObject.object_guid)
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    return {
        "objects": [_obj(r) for r in rows],
        "total": int(total),
        "limit": limit,
        "offset": offset,
    }


@router.get("/status", dependencies=[Depends(require_scope("admin"))])
async def directory_status(session: AsyncSession = Depends(get_session)) -> dict:
    """Reconciliation health: directory size, last sync, and how many distinct
    SIDs referenced by permission snapshots do vs do NOT resolve to a directory
    object (the unresolved ones are the gap an operator should investigate —
    missing base DN, a foreign domain, or a since-deleted account)."""
    settings = get_settings()
    counts = dict(
        (
            await session.execute(
                select(DirectoryObject.kind, func.count())
                .where(DirectoryObject.deleted_at.is_(None))
                .group_by(DirectoryObject.kind)
            )
        ).all()
    )
    last_sync = (
        await session.execute(select(func.max(DirectoryObject.last_synced_at)))
    ).scalar_one_or_none()
    # SIDs present in snapshots, split by whether a live directory row matches.
    has_snapshots = (
        await session.execute(text("SELECT to_regclass('permission_snapshots')"))
    ).scalar()
    resolved = unresolved = 0
    if has_snapshots is not None:
        sid_rows = (
            await session.execute(
                text(
                    "SELECT DISTINCT v FROM ("
                    "  SELECT ace->'principal'->>'id' AS v "
                    "    FROM permission_snapshots, jsonb_array_elements(aces) AS ace "
                    "  UNION SELECT owner->>'id' FROM permission_snapshots "
                    '  UNION SELECT "group"->>\'id\' FROM permission_snapshots'
                    ") t WHERE v LIKE 'S-%'"
                )
            )
        ).all()
        sids = [r[0] for r in sid_rows]
        if sids:
            live = {
                s
                for (s,) in (
                    await session.execute(
                        select(DirectoryObject.object_sid).where(
                            DirectoryObject.object_sid.in_(sids),
                            DirectoryObject.deleted_at.is_(None),
                        )
                    )
                ).all()
            }
            resolved = sum(1 for s in sids if s in live)
            unresolved = len(sids) - resolved
    return {
        "enabled": settings.ldap_directory_sync_enabled,
        "users": int(counts.get("user", 0)),
        "groups": int(counts.get("group", 0)),
        "computers": int(counts.get("computer", 0)),
        "other": int(counts.get("other", 0)),
        "last_synced_at": last_sync.isoformat() if last_sync else None,
        "snapshot_sids_resolved": resolved,
        "snapshot_sids_unresolved": unresolved,
    }


@router.post("/sync", status_code=202, dependencies=[Depends(require_scope("admin"))])
async def trigger_sync(session: AsyncSession = Depends(get_session)) -> dict:
    """Queue a directory sync now (the same task the daily schedule runs).

    422 when directory sync is not enabled/configured, so an admin gets a clear
    signal instead of a silently-skipped job. At most one sync is queued at a
    time (queueing lock); a duplicate trigger reports ``already_queued``."""
    from procrastinate.exceptions import AlreadyEnqueued

    from filearr.worker import open_pool_if_needed, proc_app

    settings = get_settings()
    if not settings.ldap_directory_sync_enabled:
        raise HTTPException(
            422,
            "directory sync is disabled — set FILEARR_LDAP_DIRECTORY_SYNC_ENABLED "
            "and the ldap_* / service-bind config first",
        )
    async with open_pool_if_needed():
        try:
            job = await proc_app.configure_task(
                "filearr.worker.sync_directory",
                queue="maintenance",
                queueing_lock="sync-directory",
            ).defer_async(timestamp=0)
        except AlreadyEnqueued:
            return {"job_id": None, "already_queued": True}
    return {"job_id": job}
