"""Roles as data (2026-08-16): list / create / edit / delete global roles, and a
comparison matrix so an operator can pick the right role for a person.

Rules (also mirrored by DB constraints where possible):
  * the three builtins (admin/user/viewer) cannot be deleted; their permissions
    CAN be edited, except that builtin ``admin`` must keep the ``admin`` scope
    (the bypass) — the one guard against an operator locking every admin out;
  * a role in use by any principal cannot be deleted (409; the FK RESTRICTs it
    too) — reassign the users first (``PATCH /auth/users/{id}``);
  * a role name is a slug ([a-z0-9_-], 2..32) and immutable after creation
    (rename = create + reassign + delete);
  * a permission change revokes NO sessions but bumps the role registry and the
    grant cache so it takes effect on the next request everywhere.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, grant_cache, rbac
from filearr import roles as roles_registry
from filearr.db import get_session
from filearr.models import Principal, RoleDef, User
from filearr.security import require_scope

router = APIRouter()

SCOPES = ("read", "write", "admin")
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,31}$")


class RoleOut(BaseModel):
    name: str
    display_name: str
    description: str
    builtin: bool
    scopes: list[str]
    ceiling_actions: list[str]
    bypass: bool  # derived: "admin" in scopes → skips path evaluation entirely
    users: int  # principals currently on this role
    updated_at: datetime | None = None


class RoleCreateIn(BaseModel):
    name: str = Field(min_length=2, max_length=32)
    display_name: str = Field(min_length=1, max_length=64)
    description: str = Field(default="", max_length=500)
    scopes: list[str] = Field(default_factory=lambda: ["read"])
    ceiling_actions: list[str] = Field(
        default_factory=lambda: ["search_metadata", "search_content"]
    )
    # Optional starting point: copy permissions from an existing role.
    clone_from: str | None = None


class RolePatchIn(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=64)
    description: str | None = Field(default=None, max_length=500)
    scopes: list[str] | None = None
    ceiling_actions: list[str] | None = None


class RoleCompareOut(BaseModel):
    """Everything the comparison view needs in one payload: the axes, and one
    row per role with a boolean per axis, plus who is on it."""

    scopes: list[str]
    actions: list[str]
    action_help: dict[str, str]
    scope_help: dict[str, str]
    roles: list[RoleOut]
    # role name -> {scope|action -> bool}
    matrix: dict[str, dict[str, bool]]
    users_by_role: dict[str, list[str]]


ACTION_HELP = {
    "search_metadata": (
        "See items (names, paths, extracted metadata) in search/browse within granted paths."
    ),
    "search_content": "Search inside extracted document text (body/OCR) within granted paths.",
    "download": "Download original files and generated exports/reports.",
    "upload": "Upload files (reserved for write-back; not enforced by any route yet).",
    "modify": "Move/rename files (reserved for write-back; not enforced by any route yet).",
    "delete": "Delete files (reserved for write-back; not enforced by any route yet).",
    "edit_metadata": "Edit user metadata (tags, ratings, custom fields) on items.",
    "manage_alerts": "Create and manage alert rules (reserved; not enforced by any route yet).",
}
SCOPE_HELP = {
    "read": "Read APIs: search, browse, stats, item detail, own sessions.",
    "write": "Mutating APIs: metadata edits, batch edits, scans, library settings.",
    "admin": (
        "Administration: users, roles, grants, groups, audit, system settings — "
        "and BYPASSES path grants entirely."
    ),
}


def _validate_perms(
    scopes: list[str] | None, actions: list[str] | None
) -> tuple[list[str], list[str]]:
    sc = sorted({s for s in (scopes or [])})
    bad = [s for s in sc if s not in SCOPES]
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown scope(s): {bad}")
    ac = sorted({a for a in (actions or [])})
    bad = [a for a in ac if a not in rbac.ACTIONS]
    if bad:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"unknown action(s): {bad}")
    # write implies read, admin implies write+read: keep the stored set explicit
    # so the matrix reads truthfully.
    if "admin" in sc:
        sc = ["admin", "read", "write"]
    elif "write" in sc and "read" not in sc:
        sc = ["read", "write"]
    return sorted(sc), ac


async def _user_counts(session: AsyncSession) -> dict[str, int]:
    rows = (
        await session.execute(
            select(Principal.global_role, func.count()).group_by(Principal.global_role)
        )
    ).all()
    return {r: int(n) for r, n in rows}


def _out(row: RoleDef, users: int) -> RoleOut:
    return RoleOut(
        name=row.name,
        display_name=row.display_name,
        description=row.description or "",
        builtin=bool(row.builtin),
        scopes=sorted(row.scopes or []),
        ceiling_actions=sorted(row.ceiling_actions or []),
        bypass="admin" in (row.scopes or []),
        users=users,
        updated_at=row.updated_at,
    )


async def _bump(session: AsyncSession) -> None:
    """Invalidate every replica (generation) and refresh THIS process now, so a
    change is visible in-process immediately, not on the next auth gate."""
    roles_registry.bump_generation()
    grant_cache.bump_generation()
    await roles_registry.ensure_loaded(session, force=True)


@router.get(
    "/rbac/roles", response_model=list[RoleOut], dependencies=[Depends(require_scope("admin"))]
)
async def list_roles(session: AsyncSession = Depends(get_session)) -> list[RoleOut]:
    counts = await _user_counts(session)
    rows = (await session.execute(select(RoleDef))).scalars().all()
    builtin_order = [r.name for r in rbac.BUILTIN_ROLES]
    rows.sort(
        key=lambda r: (builtin_order.index(r.name) if r.name in builtin_order else 99, r.name)
    )
    return [_out(r, counts.get(r.name, 0)) for r in rows]


@router.post(
    "/rbac/roles",
    response_model=RoleOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_role(
    payload: RoleCreateIn, request: Request, session: AsyncSession = Depends(get_session)
) -> RoleOut:
    name = payload.name.strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "role name must be 2-32 chars of a-z, 0-9, '_' or '-', starting with a letter or digit",
        )
    if await session.get(RoleDef, name) is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"role '{name}' already exists")
    scopes, actions = payload.scopes, payload.ceiling_actions
    if payload.clone_from:
        src = await session.get(RoleDef, payload.clone_from)
        if src is None:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"clone_from role '{payload.clone_from}' not found"
            )
        scopes, actions = list(src.scopes or []), list(src.ceiling_actions or [])
    scopes, actions = _validate_perms(scopes, actions)
    row = RoleDef(
        name=name,
        display_name=payload.display_name.strip(),
        description=payload.description.strip(),
        builtin=False,
        scopes=scopes,
        ceiling_actions=actions,
    )
    session.add(row)
    await session.commit()
    await _bump(session)
    await audit.emit(
        audit.ROLE_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"role": name, "scopes": scopes, "ceiling_actions": actions},
    )
    return _out(row, 0)


@router.patch(
    "/rbac/roles/{name}", response_model=RoleOut, dependencies=[Depends(require_scope("admin"))]
)
async def patch_role(
    name: str, payload: RolePatchIn, request: Request, session: AsyncSession = Depends(get_session)
) -> RoleOut:
    row = await session.get(RoleDef, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"role '{name}' not found")
    changed: dict[str, object] = {}
    if payload.display_name is not None:
        row.display_name = payload.display_name.strip()
        changed["display_name"] = row.display_name
    if payload.description is not None:
        row.description = payload.description.strip()
        changed["description"] = True
    if payload.scopes is not None or payload.ceiling_actions is not None:
        scopes, actions = _validate_perms(
            payload.scopes if payload.scopes is not None else list(row.scopes or []),
            payload.ceiling_actions
            if payload.ceiling_actions is not None
            else list(row.ceiling_actions or []),
        )
        if row.builtin and row.name == "admin" and "admin" not in scopes:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                "the builtin 'admin' role must keep the 'admin' scope "
                "(otherwise nobody can administer this instance)",
            )
        row.scopes = scopes
        row.ceiling_actions = actions
        changed["scopes"] = scopes
        changed["ceiling_actions"] = actions
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await _bump(session)
    counts = await _user_counts(session)
    if changed:
        await audit.emit(
            audit.ROLE_UPDATED,
            request=request,
            principal_id=audit.actor_id(request),
            details={"role": name, **changed},
        )
    return _out(row, counts.get(name, 0))


@router.delete(
    "/rbac/roles/{name}", status_code=204, dependencies=[Depends(require_scope("admin"))]
)
async def delete_role(name: str, request: Request, session: AsyncSession = Depends(get_session)):
    row = await session.get(RoleDef, name)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"role '{name}' not found")
    if row.builtin or roles_registry.is_builtin(name):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"'{name}' is a builtin role and cannot be deleted"
        )
    counts = await _user_counts(session)
    if counts.get(name, 0):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            f"{counts[name]} user(s) still have role '{name}' — reassign them first",
        )
    await session.delete(row)
    await session.commit()
    await _bump(session)
    await audit.emit(
        audit.ROLE_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"role": name},
    )
    from fastapi import Response

    return Response(status_code=204)


@router.get(
    "/rbac/roles/compare",
    response_model=RoleCompareOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def compare_roles(session: AsyncSession = Depends(get_session)) -> RoleCompareOut:
    """One payload for the 'which role should this person get?' view: every
    role × every scope and action, plus the usernames on each role."""
    roles = await list_roles(session)
    matrix: dict[str, dict[str, bool]] = {}
    for r in roles:
        cells = {f"scope:{s}": (s in r.scopes) for s in SCOPES}
        cells.update({f"action:{a}": (a in r.ceiling_actions) for a in sorted(rbac.ACTIONS)})
        matrix[r.name] = cells
    rows = (
        await session.execute(
            select(Principal.global_role, User.username)
            .join(User, User.principal_id == Principal.id)
            .order_by(User.username)
        )
    ).all()
    by_role: dict[str, list[str]] = {r.name: [] for r in roles}
    for role, uname in rows:
        by_role.setdefault(role, []).append(uname)
    return RoleCompareOut(
        scopes=list(SCOPES),
        actions=sorted(rbac.ACTIONS),
        action_help=ACTION_HELP,
        scope_help=SCOPE_HELP,
        roles=roles,
        matrix=matrix,
        users_by_role=by_role,
    )
