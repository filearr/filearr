"""Agent configuration groups + remote configuration + installer distribution
(Wave 6, W6-D2).

Three admin surfaces, all behind ``FILEARR_AGENTS_ENABLED`` (404 when off) and
``admin`` scope, all audited:

* **Config-group CRUD** — ``/agents/config-groups`` (list/create/get/update/
  delete). A group is a named, reusable remote-configuration bundle (typed
  ``settings`` — :mod:`filearr.agent_config`) assigned to many agents. NULL
  ``agents.config_group_id`` is the built-in default; a "default" group is NOT
  special-cased. Deleting a group with members lets the ON DELETE SET NULL FK
  fall them back to NULL (the audit records the member count).

* **Assignment** — ``PUT /agents/{id}/config-group`` sets/clears an agent's
  group (matches the agents API's dedicated-mutation convention — the agents
  surface uses POST/DELETE/PUT sub-resources, not a general PATCH).

* **Installer distribution** — ``POST /agents/installer-config`` mints an
  enrollment token (existing machinery) and returns the COMPLETE sidecar JSON the
  W6-D1 console agent consumes, plus token metadata + per-OS install hints. The
  UI (W6-D4) renders/downloads it (FROZEN response contract — see
  :class:`InstallerConfigOut`).

The remote-configuration DELIVERY half (merging a group's settings into the
agent policy doc under a new ``group`` section, with ETag invalidation on edit)
lives in :mod:`filearr.api.agent_policies` + :func:`agent_config.merge_group_into_policy`.
"""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, agentsync, audit
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentConfigGroup
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class ConfigGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    # Typed at the request boundary as a dict so a non-object body is a 422 at
    # parse time; agent_config.validate_settings runs the known-key gate after.
    settings: dict[str, Any] = Field(default_factory=dict)


class ConfigGroupUpdateIn(BaseModel):
    # All optional (PATCH-style partial update); an omitted field is left as-is.
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    settings: dict[str, Any] | None = None


class ConfigGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    settings: dict[str, Any]
    member_count: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def of(cls, row: AgentConfigGroup, member_count: int) -> ConfigGroupOut:
        return cls(
            id=row.id,
            name=row.name,
            description=row.description,
            settings=row.settings or {},
            member_count=member_count,
            created_at=row.created_at,
            updated_at=row.updated_at,
        )


class AssignConfigGroupIn(BaseModel):
    # NULL clears the assignment (fall back to built-in defaults).
    config_group_id: uuid.UUID | None = None


class InstallerConfigIn(BaseModel):
    central_url_override: str | None = Field(default=None, max_length=2048)
    agent_name: str | None = Field(default=None, max_length=255)
    config_group_id: uuid.UUID | None = None
    log_level: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class InstallerSidecar(BaseModel):
    """The COMPLETE sidecar the W6-D1 console agent consumes (written as
    ``filearr-agent.json``)."""

    central_url: str
    enrollment_token: str  # raw, show-once (rides the mint's show-once contract)
    agent_name: str | None
    config_group: str | None  # group NAME (matches register's string field)
    log_level: str | None


class InstallHint(BaseModel):
    windows: str
    linux: str
    macos: str


class InstallerConfigOut(BaseModel):
    """FROZEN CONTRACT for W6-D4. ``sidecar`` is the file the agent consumes;
    ``token_hash``/``expires_at`` let the UI show/revoke the token; ``install_hint``
    carries per-OS one-line install commands referencing the P5-T7 release-artifact
    download path + ``filearr-agent install --config filearr-agent.json``."""

    sidecar: InstallerSidecar
    token_hash: str
    expires_at: datetime
    install_hint: InstallHint


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
async def _member_count(session: AsyncSession, group_id: uuid.UUID) -> int:
    return (
        await session.execute(
            select(func.count()).select_from(Agent).where(
                Agent.config_group_id == group_id
            )
        )
    ).scalar_one()


def _validate_settings_or_422(settings: Any) -> None:
    try:
        agent_config.validate_settings(settings)
    except agent_config.GroupSettingsValidationError as err:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)
        ) from err


# --------------------------------------------------------------------------- #
# Config-group CRUD (admin)                                                    #
# --------------------------------------------------------------------------- #
class CollectorOut(BaseModel):
    """One inventory collector the console can offer as a checkbox."""

    name: str
    label: str
    description: str
    platforms: list[str]
    cost: str
    #: True when this name came from the shipped catalogue; False when it was
    #: only discovered from an agent's advertisement (a newer build's collector
    #: this central release has no prose for). The console still offers it — it
    #: just cannot explain it.
    described: bool
    #: How many enrolled agents advertise supporting it. 0 with described=True
    #: means "we know what this is, but nothing in your fleet reports it" —
    #: usually a platform mismatch, occasionally an agent too old to advertise.
    advertised_by: int


@router.get(
    "/agents/inventory-collectors",
    response_model=list[CollectorOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_inventory_collectors(
    session: AsyncSession = Depends(get_session),
) -> list[CollectorOut]:
    """The inventory-collector vocabulary, for rendering a checkbox list.

    Why this exists: ``InventoryConfig.collectors`` is stored as FREE strings and
    that is deliberate — a newer agent build can register a collector this
    central release has never heard of, and central refusing it would make the
    server the thing that blocks an agent upgrade. The cost of that flexibility
    landed entirely on the operator, who got a comma-separated text box and had
    to know the vocabulary from the agent's Go source. This endpoint pays that
    cost back without giving up the flexibility.

    The result is a UNION of two sources, and the ``described`` / ``advertised_by``
    flags say which:

    * the shipped catalogue (:data:`filearr.agent_config.COLLECTOR_CATALOGUE`) —
      names we can explain, with platform and cost;
    * every distinct name the enrolled fleet advertises in
      ``capabilities.inventory_collectors`` — so a collector added by a newer
      agent appears in the console the moment one agent reports it, with no
      central release required.

    Storage and validation are unchanged: this is a UI catalogue, never a
    whitelist. A stored name that appears in neither source must still round-trip
    (the console preserves unknown entries the same way the policy editor
    preserves unknown keys — dropping them would be silent data loss)."""
    rows = (
        await session.execute(
            select(Agent.capabilities).where(Agent.capabilities.is_not(None))
        )
    ).scalars().all()

    advertised: dict[str, int] = {}
    for caps in rows:
        if not isinstance(caps, dict):
            continue
        names = caps.get("inventory_collectors")
        if not isinstance(names, list):
            continue
        # A hostile/buggy agent could advertise anything; count only sane strings
        # and let the caps size cap upstream bound the rest.
        for n in {x for x in names if isinstance(x, str) and x}:
            advertised[n] = advertised.get(n, 0) + 1

    out: list[CollectorOut] = [
        CollectorOut(
            name=c["name"],
            label=c["label"],
            description=c["description"],
            platforms=list(c["platforms"]),
            cost=c["cost"],
            described=True,
            advertised_by=advertised.get(c["name"], 0),
        )
        for c in agent_config.COLLECTOR_CATALOGUE
    ]
    for name in sorted(set(advertised) - agent_config.KNOWN_COLLECTORS):
        out.append(
            CollectorOut(
                name=name,
                label=name,
                description=(
                    "Reported by an agent in this fleet but not described by this "
                    "Filearr release — probably a newer agent build. It can be "
                    "enabled; consult that agent's documentation for what it collects."
                ),
                platforms=[],
                cost="unknown",
                described=False,
                advertised_by=advertised[name],
            )
        )
    return out


@router.get(
    "/agents/config-groups",
    response_model=list[ConfigGroupOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_config_groups(
    session: AsyncSession = Depends(get_session),
) -> list[ConfigGroupOut]:
    """Every config group (newest first) with its current member count."""
    rows = (
        await session.execute(
            select(AgentConfigGroup).order_by(AgentConfigGroup.created_at.desc())
        )
    ).scalars().all()
    # One grouped count query, then attribute per group (avoids N+1).
    counts = dict(
        (
            await session.execute(
                select(Agent.config_group_id, func.count())
                .where(Agent.config_group_id.is_not(None))
                .group_by(Agent.config_group_id)
            )
        ).all()
    )
    return [ConfigGroupOut.of(r, counts.get(r.id, 0)) for r in rows]


@router.post(
    "/agents/config-groups",
    response_model=ConfigGroupOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def create_config_group(
    body: ConfigGroupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    """Create a config group. ``settings`` is validated (422 on unknown top-level
    key / bad preset / bad regex / bad cron / oversize) and stored verbatim. A
    duplicate ``name`` is a 409 (``name`` is UNIQUE)."""
    _validate_settings_or_422(body.settings)
    row = AgentConfigGroup(
        name=body.name,
        description=body.description,
        settings=body.settings,
    )
    session.add(row)
    try:
        await session.commit()
    except IntegrityError as err:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"config group name already exists: {body.name!r}"
        ) from err
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(row.id), "name": row.name},
    )
    return ConfigGroupOut.of(row, 0)


@router.get(
    "/agents/config-groups/{group_id}",
    response_model=ConfigGroupOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def get_config_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    return ConfigGroupOut.of(row, await _member_count(session, row.id))


@router.patch(
    "/agents/config-groups/{group_id}",
    response_model=ConfigGroupOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def update_config_group(
    group_id: uuid.UUID,
    body: ConfigGroupUpdateIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    """Partial update. A supplied ``settings`` is re-validated (422) and REPLACES
    the stored object (settings are not deep-merged — an edit is authored whole).
    A duplicate ``name`` is a 409. The edit bumps ``updated_at``, which invalidates
    every member agent's cached policy (the ETag folds in the group tag)."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.settings is not None:
        _validate_settings_or_422(body.settings)
        row.settings = body.settings
    try:
        await session.commit()
    except IntegrityError as err:
        await session.rollback()
        raise HTTPException(
            status.HTTP_409_CONFLICT, "config group name already exists"
        ) from err
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_UPDATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(row.id), "name": row.name},
    )
    return ConfigGroupOut.of(row, await _member_count(session, row.id))


@router.delete(
    "/agents/config-groups/{group_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def delete_config_group(
    group_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete a config group. Members (if any) fall back to NULL via the ON DELETE
    SET NULL FK (built-in defaults); the audit records how many did. Always
    allowed — a group is never load-bearing enough to block deletion."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    members = await _member_count(session, row.id)
    name = row.name
    await session.delete(row)
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(group_id), "name": name, "members_reset": members},
    )


# --------------------------------------------------------------------------- #
# Assignment (admin) — PUT /agents/{id}/config-group                           #
# --------------------------------------------------------------------------- #
@router.put(
    "/agents/{agent_id}/config-group",
    response_model=ConfigGroupOut | None,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def assign_config_group(
    agent_id: uuid.UUID,
    body: AssignConfigGroupIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut | None:
    """Assign (or clear, with ``config_group_id: null``) an agent's config group.
    404 for an unknown agent or an unknown target group. Returns the newly-assigned
    group (or ``null`` when cleared). Audited (old → new group)."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    old = agent.config_group_id
    group: AgentConfigGroup | None = None
    if body.config_group_id is not None:
        group = await session.get(AgentConfigGroup, body.config_group_id)
        if group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    agent.config_group_id = body.config_group_id
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_ASSIGNED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "agent_id": str(agent_id),
            "old_group_id": str(old) if old else None,
            "new_group_id": str(body.config_group_id) if body.config_group_id else None,
        },
    )
    if group is None:
        return None
    return ConfigGroupOut.of(group, await _member_count(session, group.id))


# --------------------------------------------------------------------------- #
# Console installer distribution (admin) — POST /agents/installer-config       #
# --------------------------------------------------------------------------- #
def _install_hint(central_url: str) -> InstallHint:
    """Per-OS one-line install commands against the UNAUTHENTICATED first-install
    distribution surface (``/api/v1/agent-dist``: central-baked binaries +
    sha256-verifying install scripts). The scripts pick the platform binary,
    verify its digest, and run ``filearr-agent install --config
    filearr-agent.json`` — the operator saves the sidecar from this response
    next to the script (or passes the token as a flag instead).

    (The former hint pointed at the P5-T7 release-artifact path, which is
    agent-certificate-authenticated — unusable by a machine that isn't enrolled
    yet.)"""
    base = central_url.rstrip("/")
    dist = f"{base}/api/v1/agent-dist"
    return InstallHint(
        windows=(
            f"irm {dist}/install.ps1 -OutFile install-agent.ps1; "
            ".\\install-agent.ps1   # elevated shell; add -Token <token> "
            "if filearr-agent.json is not beside it"
        ),
        linux=(
            f"curl -fsSL {dist}/install.sh | sh   "
            "# add: -s -- -t <token> if filearr-agent.json is not in the cwd"
        ),
        macos=(
            f"curl -fsSL {dist}/install.sh | sh   "
            "# add: -s -- -t <token> if filearr-agent.json is not in the cwd"
        ),
    )


@router.post(
    "/agents/installer-config",
    response_model=InstallerConfigOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def issue_installer_config(
    body: InstallerConfigIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> InstallerConfigOut:
    """Mint an enrollment token (existing machinery) and return the COMPLETE
    sidecar the W6-D1 console agent consumes, plus token metadata + per-OS install
    hints (FROZEN contract, :class:`InstallerConfigOut`).

    ``central_url`` = ``central_url_override`` or the request base URL.
    ``config_group_id`` (if given) must exist (422 otherwise) and is emitted in the
    sidecar by NAME (the agent later presents it to ``/agents/register``).
    ``log_level`` (if given) must be a known level (422). Audited by token hash +
    config group (NEVER the raw token)."""
    settings = get_settings()

    if body.log_level is not None and body.log_level not in agent_config.LOG_LEVELS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"log_level must be one of {list(agent_config.LOG_LEVELS)}",
        )

    group_name: str | None = None
    if body.config_group_id is not None:
        group = await session.get(AgentConfigGroup, body.config_group_id)
        if group is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, "no such config group"
            )
        group_name = group.name

    from filearr.urls import public_base_url

    central_url = (
        body.central_url_override.rstrip("/")
        if body.central_url_override
        # Shared outward-URL derivation (FILEARR_PUBLIC_BASE_URL ->
        # X-Forwarded-* -> request.base_url): the raw request.base_url said
        # http:// behind the TLS proxy, minting sidecars that dialed a dead
        # scheme/port (same class as the 2026-08-08 install.ps1 failure).
        else public_base_url(request)
    )

    ttl_seconds = body.ttl_seconds or (settings.enrollment_token_ttl_minutes * 60)
    raw, tok = await agentsync.mint_enrollment_token(
        session, rollout_group="default", ttl_seconds=ttl_seconds
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_INSTALLER_CONFIG_ISSUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "token_hash": tok.token_hash,
            "config_group": group_name,
            "agent_name": body.agent_name,
        },
    )
    return InstallerConfigOut(
        sidecar=InstallerSidecar(
            central_url=central_url,
            enrollment_token=raw,
            agent_name=body.agent_name,
            config_group=group_name,
            log_level=body.log_level,
        ),
        token_hash=tok.token_hash,
        expires_at=tok.expires_at,
        install_hint=_install_hint(central_url),
    )
