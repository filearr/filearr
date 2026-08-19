"""Exclusion preset bundles (P2-T5 read side; P2-T7 custom bundles, 2026-08-19).

Builtins come from code (``filearr.presets.BUILTIN_PRESET_BUNDLES``) and are
fork-not-mutate: ``POST /presets/{name}/fork`` copies one under a new name.
Custom bundles are stored in ``preset_bundles`` and merged into the same live
``PRESET_BUNDLES`` mapping every walk reads (``filearr.preset_registry``).
"""

from __future__ import annotations

import re
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import audit, preset_registry
from filearr.db import get_session
from filearr.models import Library, PresetBundleRow
from filearr.presets import (
    BUILTIN_PRESET_BUNDLES,
    EXTENSION_GROUPS,
    PRESET_BUNDLES,
    PresetBundle,
    is_builtin_preset,
)
from filearr.schemas import ExtensionGroupOut, PresetOut, PresetsResponse
from filearr.security import require_scope

router = APIRouter()

_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{1,63}$")


def _preset_out(name: str, bundle: PresetBundle) -> PresetOut:
    return PresetOut(
        name=name,
        label=bundle.label,
        patterns=list(bundle.exclude),
        default_enabled=bundle.default_enabled,
        caveat=bundle.caveat,
        builtin=is_builtin_preset(name),
    )


@router.get("", response_model=PresetsResponse, dependencies=[Depends(require_scope("read"))])
async def list_presets(session: AsyncSession = Depends(get_session)) -> PresetsResponse:
    """All preset bundles (builtin + custom) + extension groups."""
    await preset_registry.ensure_loaded(session)
    return PresetsResponse(
        presets=[_preset_out(name, b) for name, b in PRESET_BUNDLES.items()],
        extension_groups=[
            ExtensionGroupOut(
                name=name,
                label=g.label,
                file_category=g.file_category,
                extensions=list(g.extensions),
            )
            for name, g in EXTENSION_GROUPS.items()
        ],
    )


@router.get(
    "/{name}", response_model=PresetOut, dependencies=[Depends(require_scope("read"))]
)
async def get_preset_detail(name: str, session: AsyncSession = Depends(get_session)) -> PresetOut:
    """A single preset bundle by name; 404 if unknown."""
    await preset_registry.ensure_loaded(session)
    bundle = PRESET_BUNDLES.get(name)
    if bundle is None:
        raise HTTPException(404, "preset not found")
    return _preset_out(name, bundle)


class PresetIn(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    label: str = Field(min_length=1, max_length=120)
    patterns: list[str] = Field(default_factory=list, max_length=500)
    caveat: str | None = Field(default=None, max_length=500)


class PresetPatchIn(BaseModel):
    label: str | None = Field(default=None, min_length=1, max_length=120)
    patterns: list[str] | None = Field(default=None, max_length=500)
    caveat: str | None = Field(default=None, max_length=500)


class ForkIn(BaseModel):
    name: str = Field(min_length=2, max_length=64)
    label: str | None = Field(default=None, max_length=120)


def _clean_patterns(patterns: list[str]) -> list[str]:
    out: list[str] = []
    for p in patterns:
        p = p.strip()
        if not p or p.startswith("#"):
            continue
        if len(p) > 512:
            raise HTTPException(422, "a pattern is longer than 512 characters")
        out.append(p)
    if not out:
        raise HTTPException(422, "a bundle needs at least one exclude pattern")
    # gitignore syntax check via pathspec (the same engine the walk uses)
    from pathspec import GitIgnoreSpec

    try:
        GitIgnoreSpec.from_lines(out)
    except Exception as exc:  # noqa: BLE001
        raise HTTPException(422, f"invalid gitignore pattern: {exc}") from exc
    return out


def _valid_name(name: str) -> str:
    name = name.strip().lower()
    if not _NAME_RE.match(name):
        raise HTTPException(
            422, "name must be 2-64 chars of a-z, 0-9, '-' or '_' (starting alphanumeric)"
        )
    return name


async def _refresh(session: AsyncSession) -> None:
    preset_registry.bump_generation()
    await preset_registry.ensure_loaded(session, force=True)


@router.post(
    "", response_model=PresetOut, status_code=201, dependencies=[Depends(require_scope("admin"))]
)
async def create_preset(
    body: PresetIn, request: Request, session: AsyncSession = Depends(get_session)
) -> PresetOut:
    """Create a custom bundle. 409 for a name that exists (builtin or custom)."""
    name = _valid_name(body.name)
    await preset_registry.ensure_loaded(session, force=True)
    if name in PRESET_BUNDLES or name in BUILTIN_PRESET_BUNDLES:
        raise HTTPException(409, f"a preset named {name!r} already exists")
    patterns = _clean_patterns(body.patterns)
    session.add(
        PresetBundleRow(
            name=name, label=body.label.strip(), exclude=patterns,
            default_enabled=False, caveat=(body.caveat or None), is_builtin=False, version=1,
        )
    )
    await session.commit()
    await _refresh(session)
    await audit.emit(
        audit.PRESET_CREATED, request=request, details={"name": name, "patterns": len(patterns)}
    )
    return _preset_out(name, PRESET_BUNDLES[name])


@router.post(
    "/{name}/fork", response_model=PresetOut, status_code=201,
    dependencies=[Depends(require_scope("admin"))],
)
async def fork_preset(
    name: str, body: ForkIn, request: Request, session: AsyncSession = Depends(get_session)
) -> PresetOut:
    """Copy a bundle (builtin or custom) under a NEW name as an editable custom
    bundle -- the way to 'edit' a builtin."""
    await preset_registry.ensure_loaded(session, force=True)
    src = PRESET_BUNDLES.get(name)
    if src is None:
        raise HTTPException(404, "preset not found")
    new = _valid_name(body.name)
    if new in PRESET_BUNDLES:
        raise HTTPException(409, f"a preset named {new!r} already exists")
    session.add(
        PresetBundleRow(
            name=new, label=(body.label or f"{src.label} (copy)").strip(),
            exclude=list(src.exclude), default_enabled=False, caveat=src.caveat,
            is_builtin=False, version=1,
        )
    )
    await session.commit()
    await _refresh(session)
    await audit.emit(
        audit.PRESET_CREATED, request=request, details={"name": new, "forked_from": name}
    )
    return _preset_out(new, PRESET_BUNDLES[new])


@router.patch("/{name}", response_model=PresetOut, dependencies=[Depends(require_scope("admin"))])
async def patch_preset(
    name: str, body: PresetPatchIn, request: Request, session: AsyncSession = Depends(get_session)
) -> PresetOut:
    """Edit a CUSTOM bundle (409 for a builtin: fork it instead)."""
    if is_builtin_preset(name):
        raise HTTPException(409, "builtin presets are read-only -- fork it under a new name")
    row = await session.get(PresetBundleRow, name)
    if row is None or row.is_builtin:
        raise HTTPException(404, "preset not found")
    if body.label is not None:
        row.label = body.label.strip()
    if body.patterns is not None:
        row.exclude = _clean_patterns(body.patterns)
    if "caveat" in body.model_fields_set:
        row.caveat = body.caveat or None
    row.version = (row.version or 1) + 1
    row.updated_at = datetime.now(UTC)
    await session.commit()
    await _refresh(session)
    await audit.emit(audit.PRESET_UPDATED, request=request, details={"name": name})
    return _preset_out(name, PRESET_BUNDLES[name])


@router.delete("/{name}", status_code=204, dependencies=[Depends(require_scope("admin"))])
async def delete_preset(
    name: str, request: Request, session: AsyncSession = Depends(get_session)
) -> None:
    """Delete a CUSTOM bundle. 409 for builtins and for a bundle any library
    still enables (turn it off there first -- the walk would otherwise silently
    stop excluding what the operator thought it did)."""
    if is_builtin_preset(name):
        raise HTTPException(409, "builtin presets cannot be deleted")
    row = await session.get(PresetBundleRow, name)
    if row is None or row.is_builtin:
        raise HTTPException(404, "preset not found")
    users = (
        await session.execute(
            select(Library.name).where(Library.enabled_presets.any(name)).limit(5)
        )
    ).scalars().all()
    if users:
        raise HTTPException(
            409,
            f"preset {name!r} is enabled on library(ies): {', '.join(users)} "
            "-- disable it there first",
        )
    await session.delete(row)
    await session.commit()
    await _refresh(session)
    await audit.emit(audit.PRESET_DELETED, request=request, details={"name": name})
