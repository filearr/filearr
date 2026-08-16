"""Role registry — the live definitions behind ``rbac.Role`` (2026-08-16).

Roles used to be a three-value enum. They are now rows in the ``roles`` table
(``models.RoleDef``): the three builtins are seeded and undeletable (their
permissions ARE editable), and operators may add custom roles. This module is
the process-wide cache the synchronous resolvers (``rbac.role_from_name``,
``authx.scopes_for_role``) read from, so a role lookup never needs an ``await``.

Freshness: ``ensure_loaded(session)`` is awaited by the session-cookie auth
gate (``security.resolve_session_principal``) — the choke point every role
resolution for an interactive request goes through — and refreshes when the
TTL lapsed or ``bump_generation`` was called by a role mutation. Before any
load (unit tests, workers) the builtins answer, so nothing here can fail
closed harder than the pre-table behaviour.

Invariants enforced by the API layer (``api/roles.py``), restated here because
they are what keeps an operator from locking themselves out:
  * a builtin cannot be deleted;
  * the builtin ``admin`` keeps the ``admin`` scope (bypass) forever;
  * a role in use by any principal cannot be deleted (also a DB FK).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import rbac
from filearr.rbac import Role

_TTL_SECONDS = 30.0
_generation = 0
_loaded_generation = -1
_loaded_until = 0.0
_roles: dict[str, Role] = {}


@dataclass(frozen=True)
class RoleInfo:
    """A role as the API presents it (adds the descriptive fields)."""

    role: Role
    display_name: str
    description: str
    builtin: bool


_info: dict[str, RoleInfo] = {}


def _builtins() -> dict[str, Role]:
    return {r.name: r for r in rbac.BUILTIN_ROLES}


def bump_generation() -> None:
    """Invalidate every process's cached view on the next ``ensure_loaded``
    (per-process counter; a multi-replica deployment converges within TTL)."""
    global _generation
    _generation += 1


def get(name: str | None) -> Role:
    """Resolve a role by name. Registry → builtin defaults → an EMPTY fail-closed
    role (no scopes, no ceiling, no bypass) for anything unknown."""
    if not name:
        return Role("", frozenset(), frozenset(), False)
    r = _roles.get(name)
    if r is not None:
        return r
    b = _builtins().get(name)
    if b is not None:
        return b
    return Role(name, frozenset(), frozenset(), False)


def all_roles() -> list[Role]:
    """Every known role (registry when loaded, else the builtins), builtins first."""
    src = _roles if _roles else _builtins()
    builtin_names = [r.name for r in rbac.BUILTIN_ROLES]
    ordered = [src[n] for n in builtin_names if n in src]
    ordered += sorted((r for n, r in src.items() if n not in builtin_names), key=lambda r: r.name)
    return ordered


def info(name: str) -> RoleInfo | None:
    return _info.get(name)


def all_info() -> list[RoleInfo]:
    return [
        _info.get(r.name) or RoleInfo(r, r.name, "", r.name in _builtins()) for r in all_roles()
    ]


def is_builtin(name: str) -> bool:
    return name in _builtins()


def _install(rows) -> None:
    global _roles, _info
    roles: dict[str, Role] = {}
    infos: dict[str, RoleInfo] = {}
    for row in rows:
        scopes = frozenset(row.scopes or [])
        role = Role(
            row.name,
            frozenset(a for a in (row.ceiling_actions or []) if a in rbac.ACTIONS),
            scopes,
            "admin" in scopes,
        )
        roles[row.name] = role
        infos[row.name] = RoleInfo(
            role, row.display_name or row.name, row.description or "", bool(row.builtin)
        )
    # The builtins are always present even if the table were somehow missing
    # a seed row (never lock the admin out because of a bad migration).
    for name, b in _builtins().items():
        roles.setdefault(name, b)
        infos.setdefault(name, RoleInfo(b, name, "", True))
    _roles, _info = roles, infos


async def ensure_loaded(session: AsyncSession, *, force: bool = False) -> None:
    """Load/refresh the registry from ``roles`` when stale (TTL or generation).
    Silent on any DB error: the previous view (or the builtins) keeps serving —
    a transient DB hiccup must not turn every request into a 403."""
    global _loaded_generation, _loaded_until
    now = time.monotonic()
    if not force and _loaded_generation == _generation and _loaded_until > now:
        return
    from filearr.models import RoleDef

    try:
        rows = (await session.execute(select(RoleDef))).scalars().all()
    except Exception:  # noqa: BLE001 - see docstring
        return
    _install(rows)
    _loaded_generation = _generation
    _loaded_until = now + _TTL_SECONDS


def reset_for_tests() -> None:
    global _roles, _info, _loaded_generation, _loaded_until
    _roles, _info, _loaded_generation, _loaded_until = {}, {}, -1, 0.0
