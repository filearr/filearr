"""Operator-defined exclusion preset bundles (P2-T7, 2026-08-19).

The ``preset_bundles`` table holds CUSTOM bundles (and a mirror row per builtin,
kept in sync from code at startup so the table lists everything and ``version``
can later feed agent distribution). This module loads the custom rows into
``filearr.presets.CUSTOM_PRESET_BUNDLES`` -- the live half of the
``PRESET_BUNDLES`` mapping every consumer reads -- with the same TTL +
generation discipline as ``filearr.roles``: API mutations bump the generation
and refresh eagerly; the worker refreshes at the start of every scan (one cheap
query per scan) so a bundle created in the console reaches the next walk.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import presets as presets_mod
from filearr.presets import BUILTIN_PRESET_BUNDLES, CUSTOM_PRESET_BUNDLES, PresetBundle

_TTL_SECONDS = 30.0
_generation = 0
_loaded_generation = -1
_loaded_until = 0.0


def bump_generation() -> None:
    global _generation
    _generation += 1


def _install(rows) -> None:
    custom: dict[str, PresetBundle] = {}
    for row in rows:
        if row.is_builtin or row.name in BUILTIN_PRESET_BUNDLES:
            continue  # builtins come from code, never from the table
        custom[row.name] = PresetBundle(
            label=row.label,
            exclude=tuple(row.exclude or []),
            default_enabled=bool(row.default_enabled),
            caveat=row.caveat,
        )
    CUSTOM_PRESET_BUNDLES.clear()
    CUSTOM_PRESET_BUNDLES.update(custom)


async def ensure_loaded(session: AsyncSession, *, force: bool = False) -> None:
    """Refresh the custom-bundle view when stale. Silent on DB error / missing
    table (pre-migration): the previous view keeps serving."""
    global _loaded_generation, _loaded_until
    now = time.monotonic()
    if not force and _loaded_generation == _generation and _loaded_until > now:
        return
    from filearr.models import PresetBundleRow

    try:
        rows = (await session.execute(select(PresetBundleRow))).scalars().all()
    except Exception:  # noqa: BLE001 - see docstring
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return
    _install(rows)
    _loaded_generation = _generation
    _loaded_until = now + _TTL_SECONDS


async def seed_builtins(session: AsyncSession) -> int:
    """Mirror the code builtins into the table (insert or refresh rows with
    ``is_builtin=true``); NEVER overwrite a custom row that happens to share a
    name a builtin later acquires (it is skipped and left as the operator's).
    Returns the number of rows written. Idempotent."""
    from filearr.models import PresetBundleRow

    rows = {
        r.name: r for r in (await session.execute(select(PresetBundleRow))).scalars().all()
    }
    written = 0
    now = datetime.now(UTC)
    for name, b in BUILTIN_PRESET_BUNDLES.items():
        row = rows.get(name)
        if row is None:
            session.add(
                PresetBundleRow(
                    name=name, label=b.label, exclude=list(b.exclude),
                    default_enabled=b.default_enabled, caveat=b.caveat,
                    is_builtin=True, version=1,
                )
            )
            written += 1
        elif not row.is_builtin:
            continue  # operator's custom bundle predates this builtin name: keep it
        elif (
            row.label != b.label
            or list(row.exclude or []) != list(b.exclude)
            or bool(row.default_enabled) != b.default_enabled
            or row.caveat != b.caveat
        ):
            row.label, row.exclude = b.label, list(b.exclude)
            row.default_enabled, row.caveat = b.default_enabled, b.caveat
            row.version = (row.version or 1) + 1
            row.updated_at = now
            written += 1
    if written:
        await session.commit()
    return written


def reset_for_tests() -> None:
    global _loaded_generation, _loaded_until
    CUSTOM_PRESET_BUNDLES.clear()
    _loaded_generation, _loaded_until = -1, 0.0
    _ = presets_mod  # keep the import meaningful for readers
