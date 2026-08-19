"""Admin management of ordinary API keys (Bearer tokens): mint / list / revoke.

These are the ``read`` / ``write`` / ``admin`` scoped keys documented in
security.md for ``*arr``-style integrations and scripts. Until 2026-08-17 the
only UI that minted rows in ``api_keys`` was the LLM-key panel (``llm_role``
set, coarse scope pinned to ``read``); a plain key had to be inserted by hand.

Ordinary keys are the rows with ``llm_role IS NULL``. The LLM facade refuses
them (it requires a role); the main API honours their ``scopes`` exactly like
before — nothing about verification changes here, only issuance.

The full key material is returned exactly once at mint time.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, security
from filearr.db import get_session
from filearr.models import ApiKey, Principal, ServiceAccount
from filearr.security import generate_key, require_scope

router = APIRouter()

VALID_SCOPES = ("read", "write", "admin")

SCOPE_HELP = {
    "read": "Search, browse, download thumbnails, read monitoring views.",
    "write": "Everything in read, plus metadata edits, scans, exports, and "
    "library administration that is not admin-only.",
    "admin": "Full access — implies read and write. Users, roles, keys, "
    "settings, maintenance. Treat like the admin password.",
}


class MintRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    scopes: list[str] = Field(default_factory=lambda: ["read"], min_length=1)
    expires_days: int | None = Field(default=None, ge=1, le=3650)
    #: P6-T10: the OWNING service account. Required -- there are no orphan keys.
    service_account_id: uuidlib.UUID

    @field_validator("scopes")
    @classmethod
    def _valid_scopes(cls, v: list[str]) -> list[str]:
        out: list[str] = []
        for s in v:
            s = s.strip().lower()
            if s not in VALID_SCOPES:
                raise ValueError(f"unknown scope {s!r}; valid: {', '.join(VALID_SCOPES)}")
            if s not in out:
                out.append(s)
        # Canonical order for display.
        return [s for s in VALID_SCOPES if s in out]


def _row(k: ApiKey, owner_name: str | None = None) -> dict:
    now = datetime.now(UTC)
    return {
        "id": str(k.id),
        "name": k.name,
        "service_account_id": str(k.service_account_id) if k.service_account_id else None,
        "service_account": owner_name,
        "prefix": k.prefix,
        "scopes": list(k.scopes or []),
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
        "expired": bool(k.expires_at and k.expires_at < now),
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/scopes", dependencies=[Depends(require_scope("admin"))])
async def list_scopes() -> dict:
    return {"scopes": [{"name": s, "description": SCOPE_HELP[s]} for s in VALID_SCOPES]}


@router.get("", dependencies=[Depends(require_scope("admin"))])
async def list_api_keys(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        await session.execute(
            select(ApiKey, ServiceAccount.name)
            .outerjoin(ServiceAccount, ServiceAccount.principal_id == ApiKey.service_account_id)
            .where(ApiKey.llm_role.is_(None))
            .order_by(ApiKey.created_at.desc())
        )
    ).all()
    return {"keys": [_row(k, owner) for k, owner in rows]}


@router.post("", status_code=201, dependencies=[Depends(require_scope("admin"))])
async def mint_api_key(
    body: MintRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    # 2026-08-20 privilege ceiling: a caller may only mint keys at the same or
    # less access than they hold (defense in depth — today the endpoint is
    # admin-gated, but the rule must survive any future loosening, and it stops
    # an admin-scoped KEY from being laundered into broader grants if the
    # vocabulary ever grows).
    granted = await security.caller_scopes(request, session)
    security.require_grant_ceiling(security.expand_scopes(body.scopes), granted, "an API key")
    owner = await session.get(ServiceAccount, body.service_account_id)
    if owner is None:
        raise HTTPException(404, "service account not found")
    owner_principal = await session.get(Principal, body.service_account_id)
    if owner_principal is not None and owner_principal.disabled_at is not None:
        raise HTTPException(409, "service account is disabled -- enable it first")
    full, prefix, key_hash = generate_key()
    row = ApiKey(
        name=body.name.strip(),
        prefix=prefix,
        key_hash=key_hash,
        scopes=body.scopes,
        service_account_id=body.service_account_id,
        expires_at=(
            datetime.now(UTC) + timedelta(days=body.expires_days)
            if body.expires_days
            else None
        ),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.emit(
        audit.API_KEY_MINTED,
        request=request,
        details={
            "key_id": str(row.id), "name": row.name, "scopes": row.scopes,
            "service_account_id": str(body.service_account_id),
        },
    )
    out = _row(row, owner.name)
    out["key"] = full  # shown exactly once
    return out


@router.delete("/{key_id}", status_code=204, dependencies=[Depends(require_scope("admin"))])
async def revoke_api_key(
    key_id: uuidlib.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        delete(ApiKey).where(ApiKey.id == key_id, ApiKey.llm_role.is_(None))
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "no such API key")
    await audit.emit(audit.API_KEY_REVOKED, request=request, details={"key_id": str(key_id)})
