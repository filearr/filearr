"""Runtime-editable settings that override env defaults (2026-08-16).

Backed by the ``app_settings`` key/value table. This module owns the key
vocabulary, validation and the env fallback, and keeps a short process cache
so hot paths (every authenticated request reads the session timeouts) do not
pay a query each time. Mutations bump the generation; other replicas converge
within the TTL.

Keys today (all optional; absent = env default):

  ``session_inactivity_hours``  int  sliding idle window (env FILEARR_SESSION_INACTIVITY_HOURS)
  ``session_ttl_hours``         int  absolute session lifetime (env FILEARR_SESSION_TTL_HOURS)

Per-user overrides of the same two live on ``principals`` and win over these.
"""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.config import get_settings

KEY_SESSION_INACTIVITY_HOURS = "session_inactivity_hours"
KEY_SESSION_TTL_HOURS = "session_ttl_hours"
# GUI-editable auth-provider config blobs (2026-08-20). Each holds a dict of
# Settings-field overrides (secrets encrypted); filearr.authconfig owns their
# vocabulary + validation and overlays them onto the env Settings.
KEY_LDAP_CONFIG = "ldap_config"
KEY_LDAP_DIRECTORY_CONFIG = "ldap_directory_config"
KEY_OIDC_CONFIG = "oidc_config"
# Taxonomy-upkeep watermarks (2026-08-21, filearr.taxonomy_ops): the shipped-seed
# fingerprint last adopted, and the taxonomy version the catalogue was last
# reclassified under. They make the periodic upkeep task event-driven — the
# expensive passes run only when a deploy changed the seed / an edit bumped the
# version, never on a bare timer.
KEY_TAXONOMY_SEED_FINGERPRINT = "taxonomy_seed_fingerprint"
KEY_TAXONOMY_RECLASSIFIED_VERSION = "taxonomy_reclassified_version"
KNOWN_KEYS = frozenset(
    {
        KEY_SESSION_INACTIVITY_HOURS,
        KEY_SESSION_TTL_HOURS,
        KEY_LDAP_CONFIG,
        KEY_LDAP_DIRECTORY_CONFIG,
        KEY_OIDC_CONFIG,
        KEY_TAXONOMY_SEED_FINGERPRINT,
        KEY_TAXONOMY_RECLASSIFIED_VERSION,
    }
)

# Sane bands, in hours: 5 minutes .. 1 year. Below/above is a typo, not policy.
MIN_HOURS = 1 / 12
MAX_HOURS = 24 * 365

_TTL_SECONDS = 30.0
_generation = 0
_loaded_generation = -1
_loaded_until = 0.0
_values: dict[str, object] = {}


def bump_generation() -> None:
    global _generation
    _generation += 1


async def _ensure_loaded(session: AsyncSession, *, force: bool = False) -> None:
    global _loaded_generation, _loaded_until, _values
    now = time.monotonic()
    if not force and _loaded_generation == _generation and _loaded_until > now:
        return
    from filearr.models import AppSetting

    try:
        rows = (await session.execute(select(AppSetting))).scalars().all()
    except Exception:  # noqa: BLE001 - keep serving the last view / env defaults
        return
    _values = {r.key: (r.value or {}).get("v") for r in rows}
    _loaded_generation = _generation
    _loaded_until = now + _TTL_SECONDS


@dataclass(frozen=True)
class SessionTimeouts:
    inactivity_hours: float
    ttl_hours: float
    # Where each value came from: "env" | "global" | "user".
    inactivity_source: str
    ttl_source: str


def _num(v: object) -> float | None:
    try:
        f = float(v)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None
    if not (MIN_HOURS <= f <= MAX_HOURS):
        return None
    return f


async def get_value(session: AsyncSession, key: str) -> object | None:
    """The stored value for ``key`` (env-independent), or ``None`` when unset.
    Shares the process cache/generation with the session-timeout readers, so an
    auth-config blob is loaded at most once per TTL like everything else."""
    await _ensure_loaded(session)
    return _values.get(key)


async def global_session_timeouts(session: AsyncSession) -> SessionTimeouts:
    """The deployment-wide effective timeouts: ``app_settings`` override, else env."""
    await _ensure_loaded(session)
    s = get_settings()
    ina = _num(_values.get(KEY_SESSION_INACTIVITY_HOURS))
    ttl = _num(_values.get(KEY_SESSION_TTL_HOURS))
    return SessionTimeouts(
        inactivity_hours=ina if ina is not None else float(s.session_inactivity_hours),
        ttl_hours=ttl if ttl is not None else float(s.session_ttl_hours),
        inactivity_source="global" if ina is not None else "env",
        ttl_source="global" if ttl is not None else "env",
    )


async def effective_session_timeouts(
    session: AsyncSession,
    *,
    user_inactivity_hours: int | float | None,
    user_ttl_hours: int | float | None,
) -> SessionTimeouts:
    """Per-principal effective timeouts: the user's override wins over global."""
    g = await global_session_timeouts(session)
    ina = _num(user_inactivity_hours)
    ttl = _num(user_ttl_hours)
    return SessionTimeouts(
        inactivity_hours=ina if ina is not None else g.inactivity_hours,
        ttl_hours=ttl if ttl is not None else g.ttl_hours,
        inactivity_source="user" if ina is not None else g.inactivity_source,
        ttl_source="user" if ttl is not None else g.ttl_source,
    )


async def set_value(
    session: AsyncSession, key: str, value: object | None, *, updated_by: uuid.UUID | None
) -> None:
    """Upsert (or clear with ``None``) one setting. Validation is the caller's;
    this only knows the vocabulary."""
    if key not in KNOWN_KEYS:
        raise ValueError(f"unknown setting {key!r}")
    from filearr.models import AppSetting

    if value is None:
        row = await session.get(AppSetting, key)
        if row is not None:
            await session.delete(row)
    else:
        stmt = pg_insert(AppSetting).values(
            key=key, value={"v": value}, updated_at=datetime.now(UTC), updated_by=updated_by
        )
        stmt = stmt.on_conflict_do_update(
            index_elements=[AppSetting.key],
            set_={"value": {"v": value}, "updated_at": datetime.now(UTC), "updated_by": updated_by},
        )
        await session.execute(stmt)
    bump_generation()


def reset_for_tests() -> None:
    global _values, _loaded_generation, _loaded_until
    _values, _loaded_generation, _loaded_until = {}, -1, 0.0
