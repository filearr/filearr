"""Admin management of LLM API keys (M1): mint / list / revoke.

Lives on the MAIN v1 API (admin scope) — the facade itself never mints
keys. The full key material is returned exactly once at mint time.
"""

from __future__ import annotations

import uuid as uuidlib
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit
from filearr.db import get_session
from filearr.llm import DEFAULT_RATE_LIMIT, LLM_ROLES
from filearr.models import ApiKey
from filearr.security import generate_key, require_scope

router = APIRouter()


class MintRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    role: str
    path_scope: str | None = None
    libraries: list[uuidlib.UUID] | None = None
    content_access: bool | None = None
    reveal_paths: bool | None = None
    rate_limit: int | None = Field(default=None, ge=1, le=6000)
    expires_days: int | None = Field(default=None, ge=1, le=3650)


def _key_row(k: ApiKey) -> dict:
    role = LLM_ROLES.get(k.llm_role or "")
    return {
        "id": str(k.id),
        "name": k.name,
        "prefix": k.prefix,
        "role": k.llm_role,
        "role_description": role.description if role else None,
        "path_scope": k.path_scope,
        "libraries": [str(x) for x in (k.libraries or [])] or None,
        "content_access": (
            k.content_access if k.content_access is not None
            else (role.content_access if role else False)
        ),
        "reveal_paths": (
            k.reveal_paths if k.reveal_paths is not None
            else (role.reveal_paths if role else True)
        ),
        "rate_limit": k.rate_limit or DEFAULT_RATE_LIMIT,
        "expires_at": k.expires_at.isoformat() if k.expires_at else None,
        "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
        "created_at": k.created_at.isoformat() if k.created_at else None,
    }


@router.get("/roles", dependencies=[Depends(require_scope("admin"))])
async def list_roles() -> dict:
    return {
        "roles": [
            {
                "name": r.name,
                "description": r.description,
                "tools": list(r.tools),
                "content_access": r.content_access,
                "reveal_paths": r.reveal_paths,
            }
            for r in LLM_ROLES.values()
        ]
    }


@router.get("", dependencies=[Depends(require_scope("admin"))])
async def list_llm_keys(session: AsyncSession = Depends(get_session)) -> dict:
    rows = (
        (
            await session.execute(
                select(ApiKey)
                .where(ApiKey.llm_role.is_not(None))
                .order_by(ApiKey.created_at.desc())
            )
        )
        .scalars()
        .all()
    )
    return {"keys": [_key_row(k) for k in rows]}


@router.post("", status_code=201, dependencies=[Depends(require_scope("admin"))])
async def mint_llm_key(
    body: MintRequest,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict:
    if body.role not in LLM_ROLES:
        raise HTTPException(
            422, f"unknown role {body.role!r}; valid: {', '.join(LLM_ROLES)}"
        )
    full, prefix, key_hash = generate_key()
    row = ApiKey(
        name=body.name,
        prefix=prefix,
        key_hash=key_hash,
        scopes=["read"],  # facade access only; coarse scope stays read
        llm_role=body.role,
        path_scope=body.path_scope,
        libraries=body.libraries,
        content_access=body.content_access,
        reveal_paths=body.reveal_paths,
        rate_limit=body.rate_limit,
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
        "LLM_KEY_MINTED",
        request=request,
        details={"key_id": str(row.id), "name": row.name, "role": body.role},
    )
    out = _key_row(row)
    out["key"] = full  # shown exactly once
    return out


@router.delete("/{key_id}", status_code=204, dependencies=[Depends(require_scope("admin"))])
async def revoke_llm_key(
    key_id: uuidlib.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    result = await session.execute(
        delete(ApiKey).where(ApiKey.id == key_id, ApiKey.llm_role.is_not(None))
    )
    await session.commit()
    if result.rowcount == 0:
        raise HTTPException(404, "no such LLM key")
    await audit.emit(
        "LLM_KEY_REVOKED", request=request, details={"key_id": str(key_id)}
    )
