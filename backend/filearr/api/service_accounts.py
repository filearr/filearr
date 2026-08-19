"""Service accounts (P6-T10, 2026-08-19): first-class non-human principals
that OWN api keys. Every key minted from the console belongs to exactly one;
pre-migration keys were backfilled under "Pre-existing keys". Disabling an
account refuses all its keys on the next request; deleting it revokes them
(FK CASCADE). Admin scope throughout.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.db import get_session
from filearr.models import ApiKey, Principal, ServiceAccount
from filearr.security import require_scope

router = APIRouter()


class ServiceAccountOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    disabled: bool
    created_at: datetime
    key_count: int
    llm_key_count: int
    last_used_at: datetime | None


class ServiceAccountIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)


class ServiceAccountPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=120)
    description: str | None = Field(default=None, max_length=500)
    disabled: bool | None = None


async def _rows(session: AsyncSession, only: uuid.UUID | None = None) -> list[ServiceAccountOut]:
    q = (
        select(
            ServiceAccount.principal_id,
            ServiceAccount.name,
            ServiceAccount.description,
            Principal.disabled_at,
            Principal.created_at,
            func.count(ApiKey.id).filter(ApiKey.llm_role.is_(None)).label("keys"),
            func.count(ApiKey.id).filter(ApiKey.llm_role.is_not(None)).label("llm_keys"),
            func.max(ApiKey.last_used_at).label("last_used"),
        )
        .join(Principal, Principal.id == ServiceAccount.principal_id)
        .outerjoin(ApiKey, ApiKey.service_account_id == ServiceAccount.principal_id)
        .group_by(
            ServiceAccount.principal_id, ServiceAccount.name, ServiceAccount.description,
            Principal.disabled_at, Principal.created_at,
        )
        .order_by(ServiceAccount.name)
    )
    if only is not None:
        q = q.where(ServiceAccount.principal_id == only)
    return [
        ServiceAccountOut(
            id=r.principal_id, name=r.name, description=r.description,
            disabled=r.disabled_at is not None, created_at=r.created_at,
            key_count=int(r.keys or 0), llm_key_count=int(r.llm_keys or 0),
            last_used_at=r.last_used,
        )
        for r in (await session.execute(q)).all()
    ]


@router.get("", dependencies=[Depends(require_scope("admin"))])
async def list_service_accounts(session: AsyncSession = Depends(get_session)) -> dict:
    return {"service_accounts": [r.model_dump() for r in await _rows(session)]}


@router.post(
    "", status_code=201, response_model=ServiceAccountOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def create_service_account(
    body: ServiceAccountIn, request: Request, session: AsyncSession = Depends(get_session)
) -> ServiceAccountOut:
    name = body.name.strip()
    dup = (
        await session.execute(
            select(ServiceAccount).where(func.lower(ServiceAccount.name) == name.lower())
        )
    ).scalar_one_or_none()
    if dup is not None:
        raise HTTPException(409, f"a service account named {name!r} already exists")
    principal = Principal(kind="service_account", global_role="viewer")
    session.add(principal)
    await session.flush()
    session.add(
        ServiceAccount(principal_id=principal.id, name=name, description=body.description or None)
    )
    await session.commit()
    await audit.emit(
        audit.SERVICE_ACCOUNT_CREATED, request=request,
        details={"service_account_id": str(principal.id), "name": name},
    )
    return (await _rows(session, principal.id))[0]


@router.patch(
    "/{account_id}",
    response_model=ServiceAccountOut,
    dependencies=[Depends(require_scope("admin"))],
)
async def patch_service_account(
    account_id: uuid.UUID, body: ServiceAccountPatch, request: Request,
    session: AsyncSession = Depends(get_session),
) -> ServiceAccountOut:
    sa = await session.get(ServiceAccount, account_id)
    if sa is None:
        raise HTTPException(404, "service account not found")
    principal = await session.get(Principal, account_id)
    changes: dict = {}
    if body.name is not None and body.name.strip() != sa.name:
        sa.name = body.name.strip()
        changes["name"] = sa.name
    if "description" in body.model_fields_set:
        sa.description = body.description or None
        changes["description"] = True
    if body.disabled is not None and principal is not None:
        principal.disabled_at = datetime.now(UTC) if body.disabled else None
        changes["disabled"] = body.disabled
    await session.commit()
    await audit.emit(
        audit.SERVICE_ACCOUNT_UPDATED, request=request,
        details={"service_account_id": str(account_id), **changes},
    )
    return (await _rows(session, account_id))[0]


@router.delete("/{account_id}", status_code=204, dependencies=[Depends(require_scope("admin"))])
async def delete_service_account(
    account_id: uuid.UUID, request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """Delete the account AND revoke every key it owns (FK CASCADE)."""
    principal = await session.get(Principal, account_id)
    if principal is None or principal.kind != "service_account":
        raise HTTPException(404, "service account not found")
    n = (
        await session.execute(
            select(func.count()).select_from(ApiKey).where(ApiKey.service_account_id == account_id)
        )
    ).scalar_one()
    await session.delete(principal)  # cascades to service_accounts + api_keys
    await session.commit()
    await audit.emit(
        audit.SERVICE_ACCOUNT_DELETED, request=request,
        details={"service_account_id": str(account_id), "keys_revoked": int(n)},
    )
