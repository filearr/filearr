"""Agent configuration groups — the single admin surface for fleet configuration
(W6-D2, unified in P13: ``archive/docs/design-config-group-unification.md``).

Everything here is behind ``FILEARR_AGENTS_ENABLED`` (404 when off) and the
``admin`` scope, and every mutation is audited by id/scope only — never by
document body (the settings can name an operator's private directory trees).

* **Group CRUD + versioned history** — ``/agents/config-groups``. A group is a
  named, priority-ordered bundle of TWO documents (``settings``, validated by
  :mod:`filearr.agent_config`; ``policy``, validated by :mod:`filearr.policy`).
  Every document edit publishes an immutable snapshot; ``/history`` lists them
  and ``/rollback`` copies an old one FORWARD as a new version.
* **Phased rollouts** — ``/agents/config-rollouts``. A publish can go live
  immediately or walk a group's members through ≤5 percent/delay tiers. The
  worker's minute tick (``filearr.worker.advance_config_rollouts``) does the
  promoting; this surface creates, lists, promotes-now and cancels.
* **Membership** — ``PUT /agents/{agent_id}/config-groups`` replaces an agent's
  whole group set. The permanent **Global** group is implicit and is rejected
  here (including it would imply it could be removed).
* **Effective configuration** — ``GET /agents/{agent_id}/effective-config``: the
  merged document one agent would receive right now, with per-key provenance.
* **Installer distribution** — ``POST /agents/installer-config`` mints an
  enrollment token carrying the groups the new agent joins and returns the
  COMPLETE sidecar JSON the console agent consumes.

The agent-facing DELIVERY half (the frozen ``GET /agents/{id}/policy`` wire
document) lives in :mod:`filearr.api.agent_policies`.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, agentsync, audit
from filearr import policy as policy_mod
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import (
    Agent,
    AgentConfigGroup,
    AgentConfigGroupMember,
    AgentConfigGroupVersion,
    AgentConfigRollout,
)
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class RolloutRequestIn(BaseModel):
    """The optional ``rollout`` block on a publish: phase the new version in
    instead of switching every member at once.

    ``tiers`` stays an untyped list here on purpose — :func:`agent_config.validate_tiers`
    owns the shape (ascending percents, last == 100, ≤5 entries) and produces
    error text that says WHICH rule was broken; a pydantic sub-model would
    intercept the easy half of that and report it in a different vocabulary."""

    tiers: list[dict[str, Any]] = Field(default_factory=list)
    #: NULL = start at the next minute tick.
    starts_at: datetime | None = None


class ConfigGroupIn(BaseModel):
    name: str = Field(min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    # Both documents are typed as dicts at the request boundary so a non-object
    # body is a 422 at parse time; the per-section validators run after.
    settings: dict[str, Any] = Field(default_factory=dict)
    policy: dict[str, Any] = Field(default_factory=dict)
    #: Ascending merge order; later (higher) wins per key. Global is 0.
    priority: int = Field(default=100, ge=0, le=1_000_000)


class ConfigGroupUpdateIn(BaseModel):
    # All optional (PATCH-style partial update); an omitted field is left as-is.
    name: str | None = Field(default=None, min_length=1, max_length=128)
    description: str | None = Field(default=None, max_length=1024)
    priority: int | None = Field(default=None, ge=0, le=1_000_000)
    settings: dict[str, Any] | None = None
    policy: dict[str, Any] | None = None
    #: Free-text note stored on the published snapshot ("why did this change?").
    note: str | None = Field(default=None, max_length=1024)
    rollout: RolloutRequestIn | None = None


class RollbackIn(BaseModel):
    version: int = Field(ge=1)
    note: str | None = Field(default=None, max_length=1024)


class ActiveRolloutOut(BaseModel):
    """The live rollout summary embedded in a group row."""

    id: uuid.UUID
    status: str
    current_tier: int
    tiers: list[dict[str, Any]]
    target_version: int
    starts_at: datetime | None
    started_at: datetime | None
    tier_started_at: datetime | None


class ConfigGroupOut(BaseModel):
    id: uuid.UUID
    name: str
    description: str | None
    settings: dict[str, Any]
    policy: dict[str, Any]
    priority: int
    is_system: bool
    current_version: int
    #: Explicit members. For **Global** this is the total agent count — its
    #: membership is implicit, so a literal count of join rows would say 0 for
    #: the group every agent is in.
    member_count: int
    active_rollout: ActiveRolloutOut | None
    created_at: datetime
    updated_at: datetime


class ConfigVersionOut(BaseModel):
    #: The GLOBAL generation counter — this is the number delivered to agents.
    seq: int
    #: The per-group counter shown in the console and targeted by rollback.
    version: int
    settings: dict[str, Any]
    policy: dict[str, Any]
    actor: str | None
    note: str | None
    created_at: datetime


class ConfigGroupDetailOut(ConfigGroupOut):
    """One group plus its most recent snapshots (cap 20 — the console's history
    panel pages the rest through ``/history``)."""

    versions: list[ConfigVersionOut]


class RolloutOut(BaseModel):
    id: uuid.UUID
    group_id: uuid.UUID
    group_name: str
    target_version: int
    tiers: list[dict[str, Any]]
    status: str
    current_tier: int
    #: The percentage covered right now — ``0`` until a tier activates.
    covered_percent: int
    #: When the tick will advance to the next tier (``None`` when not running,
    #: or when the current tier is the last one).
    next_promotion_at: datetime | None
    starts_at: datetime | None
    started_at: datetime | None
    tier_started_at: datetime | None
    finished_at: datetime | None
    actor: str | None
    created_at: datetime


class MembershipIn(BaseModel):
    group_ids: list[uuid.UUID] = Field(default_factory=list)


class MembershipOut(BaseModel):
    agent_id: uuid.UUID
    #: Explicit memberships only (Global is implicit and never listed here).
    group_ids: list[uuid.UUID]
    groups: list[dict[str, Any]]


class EffectiveConfigOut(BaseModel):
    """What one agent would receive on its next poll, plus everything needed to
    explain it. Read-only — it never stamps ``last_seen_at`` and emits no ETag."""

    agent_id: uuid.UUID
    #: The merged wire document. The server-injected ``taxonomy_version`` is
    #: deliberately ABSENT: it is computed per response, never operator-authored,
    #: and including it would invite an operator to "set" a key no edit controls.
    document: dict[str, Any]
    generation: int
    hash: str
    #: Contributing groups in MERGE ORDER (ascending priority, later wins).
    groups: list[dict[str, Any]]
    #: ``"<section>.<key>" -> {group_id, group_name, version}``.
    provenance: dict[str, dict[str, Any]]
    #: The generation the agent last echoed back via ``?applied=``. Lower than
    #: ``generation`` means "published, not yet confirmed".
    confirmed_generation: int | None
    last_seen_at: datetime | None


class InstallerConfigIn(BaseModel):
    central_url_override: str | None = Field(default=None, max_length=2048)
    agent_name: str | None = Field(default=None, max_length=255)
    #: Groups the freshly-enrolled agent joins. Global is implicit.
    config_group_ids: list[uuid.UUID] = Field(default_factory=list)
    log_level: str | None = None
    ttl_seconds: int | None = Field(default=None, ge=60, le=86400)


class InstallerSidecar(BaseModel):
    """The COMPLETE sidecar the W6-D1 console agent consumes (written as
    ``filearr-agent.json``)."""

    central_url: str
    enrollment_token: str  # raw, show-once (rides the mint's show-once contract)
    agent_name: str | None
    #: P13: the full list of groups to join. Membership is ALSO recorded on the
    #: minted token, so central applies it at register whatever the agent sends —
    #: the sidecar copy is informational + forward-looking.
    config_group_names: list[str]
    #: The first group name, kept because shipped agent binaries parse exactly
    #: this key from the sidecar and pass it to ``/agents/register``. Dropping it
    #: would silently strip the group from every install done with a current
    #: binary. ``null`` when no groups were selected.
    config_group: str | None
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
def _validate_settings_or_422(settings: Any) -> None:
    try:
        agent_config.validate_settings(settings)
    except agent_config.GroupSettingsValidationError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err


def _validate_policy_or_422(policy: Any) -> None:
    settings = get_settings()
    if policy_mod.policy_json_len(policy) > settings.agent_policy_max_bytes:
        raise HTTPException(status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "policy too large")
    try:
        policy_mod.validate_policy(policy)
    except policy_mod.PolicyValidationError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err


def _validate_tiers_or_422(tiers: Any) -> list[dict[str, int]]:
    try:
        return agent_config.validate_tiers(tiers)
    except agent_config.RolloutValidationError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err


async def _member_counts(session: AsyncSession) -> dict[uuid.UUID, int]:
    """Explicit member count per group — one grouped query, no N+1."""
    return dict(
        (
            await session.execute(
                select(AgentConfigGroupMember.group_id, func.count()).group_by(
                    AgentConfigGroupMember.group_id
                )
            )
        ).all()
    )


async def _agent_total(session: AsyncSession) -> int:
    return (await session.execute(select(func.count()).select_from(Agent))).scalar_one()


async def _live_rollout(
    session: AsyncSession, group_id: uuid.UUID
) -> AgentConfigRollout | None:
    return (
        await session.execute(
            select(AgentConfigRollout).where(
                AgentConfigRollout.group_id == group_id,
                AgentConfigRollout.status.in_(("scheduled", "running")),
            )
        )
    ).scalars().first()


def _active_rollout_out(rollout: AgentConfigRollout | None) -> ActiveRolloutOut | None:
    if rollout is None:
        return None
    return ActiveRolloutOut(
        id=rollout.id,
        status=rollout.status,
        current_tier=rollout.current_tier,
        tiers=list(rollout.tiers or []),
        target_version=rollout.target_version,
        starts_at=rollout.starts_at,
        started_at=rollout.started_at,
        tier_started_at=rollout.tier_started_at,
    )


def _group_out(
    row: AgentConfigGroup, member_count: int, rollout: AgentConfigRollout | None
) -> ConfigGroupOut:
    return ConfigGroupOut(
        id=row.id,
        name=row.name,
        description=row.description,
        settings=row.settings or {},
        policy=row.policy or {},
        priority=row.priority,
        is_system=row.is_system,
        current_version=row.current_version,
        member_count=member_count,
        active_rollout=_active_rollout_out(rollout),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def _version_out(row: AgentConfigGroupVersion) -> ConfigVersionOut:
    return ConfigVersionOut(
        seq=row.seq,
        version=row.version,
        settings=row.settings or {},
        policy=row.policy or {},
        actor=row.actor,
        note=row.note,
        created_at=row.created_at,
    )


def _next_promotion_at(rollout: AgentConfigRollout) -> datetime | None:
    """When the tick will advance this rollout to its next tier.

    Derived rather than stored: the schedule is entirely a function of
    ``tier_started_at`` plus the next tier's declared delay, and a stored copy
    would need re-computing on every promote-now anyway."""
    if rollout.status != "running" or rollout.tier_started_at is None:
        return None
    tiers = rollout.tiers or []
    nxt = rollout.current_tier + 1
    if nxt >= len(tiers):
        return None
    delay = tiers[nxt].get("delay_minutes", 0)
    return rollout.tier_started_at + timedelta(minutes=int(delay or 0))


def _rollout_out(rollout: AgentConfigRollout, group_name: str) -> RolloutOut:
    return RolloutOut(
        id=rollout.id,
        group_id=rollout.group_id,
        group_name=group_name,
        target_version=rollout.target_version,
        tiers=list(rollout.tiers or []),
        status=rollout.status,
        current_tier=rollout.current_tier,
        covered_percent=agent_config.rollout_active_percent(rollout) or 0,
        next_promotion_at=_next_promotion_at(rollout),
        starts_at=rollout.starts_at,
        started_at=rollout.started_at,
        tier_started_at=rollout.tier_started_at,
        finished_at=rollout.finished_at,
        actor=rollout.actor,
        created_at=rollout.created_at,
    )


async def _publish_version(
    session: AsyncSession,
    group: AgentConfigGroup,
    *,
    actor: str | None,
    note: str | None,
) -> AgentConfigGroupVersion:
    """Snapshot the group's CURRENT documents as version ``max+1``.

    Forward-only, exactly as the old policy history was: nothing ever mutates a
    stored snapshot, so "what was this group running on Tuesday" always has an
    answer. The caller decides whether the new version goes live immediately
    (``current_version``) or behind a rollout."""
    prior = (
        await session.execute(
            select(func.max(AgentConfigGroupVersion.version)).where(
                AgentConfigGroupVersion.group_id == group.id
            )
        )
    ).scalar()
    row = AgentConfigGroupVersion(
        group_id=group.id,
        version=(prior or 0) + 1,
        settings=group.settings or {},
        policy=group.policy or {},
        actor=actor,
        note=note,
    )
    session.add(row)
    await session.flush()
    return row


# --------------------------------------------------------------------------- #
# Inventory-collector catalogue (unchanged by P13)                             #
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


# --------------------------------------------------------------------------- #
# Rollouts (declared BEFORE /agents/config-groups/{group_id} — literal paths    #
# must win the match, same reason /agents/summary sits where it does)           #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/config-rollouts",
    response_model=list[RolloutOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_config_rollouts(
    session: AsyncSession = Depends(get_session),
    status_filter: str | None = Query(default=None, alias="status"),
    limit: int = Query(default=50),
) -> list[RolloutOut]:
    """Rollouts, newest first. ``?status=`` filters (``scheduled`` | ``running``
    | ``completed`` | ``cancelled``); the default returns only the LIVE ones,
    because a rollouts panel showing every rollout ever run is a history view
    nobody asked for."""
    limit = max(1, min(limit, 200))
    q = select(AgentConfigRollout, AgentConfigGroup.name).join(
        AgentConfigGroup, AgentConfigGroup.id == AgentConfigRollout.group_id
    )
    if status_filter:
        q = q.where(AgentConfigRollout.status == status_filter)
    else:
        q = q.where(AgentConfigRollout.status.in_(("scheduled", "running")))
    q = q.order_by(AgentConfigRollout.created_at.desc()).limit(limit)
    return [_rollout_out(r, name) for r, name in (await session.execute(q)).all()]


async def _get_rollout(
    session: AsyncSession, rollout_id: uuid.UUID
) -> tuple[AgentConfigRollout, AgentConfigGroup]:
    rollout = await session.get(AgentConfigRollout, rollout_id)
    if rollout is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such rollout")
    group = await session.get(AgentConfigGroup, rollout.group_id)
    if group is None:  # pragma: no cover — FK CASCADE makes this unreachable
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    return rollout, group


@router.post(
    "/agents/config-rollouts/{rollout_id}/cancel",
    response_model=RolloutOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def cancel_config_rollout(
    rollout_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RolloutOut:
    """Stop a scheduled/running rollout. 409 if it already finished.

    **The group's ``current_version`` is unchanged**, which means agents already
    covered by an active tier FALL BACK to it on their next poll (within one
    poll interval, ~60 s). That is the intended behaviour — cancelling a rollout
    means "stop shipping this version", not "freeze it half-shipped" — and it is
    why cancel is a safe panic button. To keep the new version instead, promote
    the rollout to completion rather than cancelling it."""
    rollout, group = await _get_rollout(session, rollout_id)
    if rollout.status not in ("scheduled", "running"):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"rollout is already {rollout.status}"
        )
    rollout.status = "cancelled"
    rollout.finished_at = datetime.now(UTC)
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_ROLLOUT_CANCELLED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "rollout_id": str(rollout.id),
            "group_id": str(group.id),
            "group_name": group.name,
            "target_version": rollout.target_version,
            "tier": rollout.current_tier,
        },
    )
    return _rollout_out(rollout, group.name)


@router.post(
    "/agents/config-rollouts/{rollout_id}/promote",
    response_model=RolloutOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def promote_config_rollout(
    rollout_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RolloutOut:
    """Advance a RUNNING rollout to its next tier immediately, skipping the
    remaining delay (409 when it is not running — a scheduled rollout has no
    tier to advance from, and a finished one has nowhere to go).

    Promoting the LAST tier completes the rollout: the group's
    ``current_version`` moves to ``target_version`` and the rollout is done.
    This is the same transition the worker tick performs, called by hand by an
    operator who has watched the first tier and does not want to wait."""
    rollout, group = await _get_rollout(session, rollout_id)
    if rollout.status != "running":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"rollout is {rollout.status}, not running"
        )
    now = datetime.now(UTC)
    tiers = rollout.tiers or []
    if rollout.current_tier + 1 < len(tiers):
        rollout.current_tier += 1
        rollout.tier_started_at = now
    if rollout.current_tier >= len(tiers) - 1:
        rollout.status = "completed"
        rollout.finished_at = now
        group.current_version = rollout.target_version
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_ROLLOUT_PROMOTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "rollout_id": str(rollout.id),
            "group_id": str(group.id),
            "group_name": group.name,
            "tier": rollout.current_tier,
            "status": rollout.status,
        },
    )
    return _rollout_out(rollout, group.name)


# --------------------------------------------------------------------------- #
# Config-group CRUD                                                            #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/config-groups",
    response_model=list[ConfigGroupOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_config_groups(
    session: AsyncSession = Depends(get_session),
) -> list[ConfigGroupOut]:
    """Every config group in MERGE ORDER (ascending priority, then name) with its
    member count and live rollout — the same order resolution applies them in, so
    the table reads top-to-bottom as "what overrides what"."""
    rows = list(
        (
            await session.execute(
                select(AgentConfigGroup).order_by(
                    AgentConfigGroup.priority, AgentConfigGroup.name
                )
            )
        ).scalars()
    )
    counts = await _member_counts(session)
    total_agents = await _agent_total(session)
    live = {
        r.group_id: r
        for r in (
            await session.execute(
                select(AgentConfigRollout).where(
                    AgentConfigRollout.status.in_(("scheduled", "running"))
                )
            )
        ).scalars()
    }
    return [
        _group_out(
            r,
            total_agents if r.is_system else counts.get(r.id, 0),
            live.get(r.id),
        )
        for r in rows
    ]


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
    """Create a config group and publish its version 1.

    Both documents are validated (422 on an unknown ``settings`` key / bad preset
    / bad regex / bad cron; 422 on a policy bound violation; 413 on an oversize
    policy) and stored verbatim. A duplicate ``name`` is a 409."""
    _validate_settings_or_422(body.settings)
    _validate_policy_or_422(body.policy)
    row = AgentConfigGroup(
        name=body.name,
        description=body.description,
        settings=body.settings,
        policy=body.policy,
        priority=body.priority,
        is_system=False,
        current_version=1,
    )
    session.add(row)
    try:
        await session.flush()
        snapshot = await _publish_version(
            session, row, actor=audit.actor_id(request), note="created"
        )
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
        details={
            "group_id": str(row.id),
            "name": row.name,
            "priority": row.priority,
            "generation": snapshot.seq,
        },
    )
    return _group_out(row, 0, None)


@router.get(
    "/agents/config-groups/{group_id}",
    response_model=ConfigGroupDetailOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def get_config_group(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupDetailOut:
    """One group's full documents plus its 20 most recent snapshots."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    counts = await _member_counts(session)
    members = (
        await _agent_total(session) if row.is_system else counts.get(row.id, 0)
    )
    versions = list(
        (
            await session.execute(
                select(AgentConfigGroupVersion)
                .where(AgentConfigGroupVersion.group_id == group_id)
                .order_by(AgentConfigGroupVersion.version.desc())
                .limit(20)
            )
        ).scalars()
    )
    base = _group_out(row, members, await _live_rollout(session, row.id))
    return ConfigGroupDetailOut(
        **base.model_dump(), versions=[_version_out(v) for v in versions]
    )


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
    """Partial update, and the ONE place a new version is published.

    ``settings`` / ``policy`` REPLACE their section wholesale when supplied
    (authoring is replace; LAYERING is what happens across groups — deep-merging
    within a group as well would leave an operator unable to UNSET a key). Any
    document change snapshots version N+1:

    * **without** ``rollout`` — ``current_version`` moves to N+1 and every member
      picks it up on its next poll;
    * **with** ``rollout`` — ``current_version`` stays put and a rollout row
      hands N+1 only to the agents inside the active tier. 409 if a live rollout
      already exists for this group (two overlapping rollouts would each define a
      different "active version" for the same agent). 422 if the tiers are
      malformed, or if a rollout is requested without a document change (there
      would be nothing to roll out).

    **Global** (``is_system``) rejects a ``name`` or ``priority`` change with 409
    — it is the permanent bottom layer and both properties are load-bearing. Its
    documents are fully editable and rollout-able, which is the whole point of
    having a fleet-wide baseline.

    A duplicate ``name`` is a 409. Nothing is pushed: agents pick the change up
    on their next poll (the ETag changes, so it is a 200, not a 304)."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    if row.is_system and (body.name is not None or body.priority is not None):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the Global group's name and priority are fixed — every agent is a "
            "member and it is always the lowest-priority layer",
        )

    document_changed = body.settings is not None or body.policy is not None
    if body.rollout is not None and not document_changed:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "a rollout needs a settings/policy change to roll out",
        )

    if body.name is not None:
        row.name = body.name
    if body.description is not None:
        row.description = body.description
    if body.priority is not None:
        row.priority = body.priority
    if body.settings is not None:
        _validate_settings_or_422(body.settings)
        row.settings = body.settings
    if body.policy is not None:
        _validate_policy_or_422(body.policy)
        row.policy = body.policy

    rollout: AgentConfigRollout | None = None
    snapshot: AgentConfigGroupVersion | None = None
    tiers: list[dict[str, int]] = []
    if document_changed:
        if body.rollout is not None:
            tiers = _validate_tiers_or_422(body.rollout.tiers)
            if await _live_rollout(session, row.id) is not None:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "this group already has a live rollout — cancel or complete "
                    "it before starting another",
                )
        snapshot = await _publish_version(
            session, row, actor=audit.actor_id(request), note=body.note
        )
        if body.rollout is None:
            row.current_version = snapshot.version
        else:
            rollout = AgentConfigRollout(
                group_id=row.id,
                target_version=snapshot.version,
                tiers=tiers,
                status="scheduled",
                current_tier=-1,
                starts_at=body.rollout.starts_at,
                actor=audit.actor_id(request),
            )
            session.add(rollout)

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
        details={"group_id": str(row.id), "name": row.name, "priority": row.priority},
    )
    if snapshot is not None:
        await audit.emit(
            audit.AGENT_CONFIG_VERSION_PUBLISHED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "group_id": str(row.id),
                "name": row.name,
                "version": snapshot.version,
                "generation": snapshot.seq,
                "immediate": rollout is None,
            },
        )
    if rollout is not None:
        await audit.emit(
            audit.AGENT_CONFIG_ROLLOUT_CREATED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "rollout_id": str(rollout.id),
                "group_id": str(row.id),
                "group_name": row.name,
                "target_version": rollout.target_version,
                "tiers": len(rollout.tiers or []),
            },
        )
    counts = await _member_counts(session)
    members = await _agent_total(session) if row.is_system else counts.get(row.id, 0)
    return _group_out(row, members, await _live_rollout(session, row.id))


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
    """Delete a config group. 409 for **Global** (``is_system``) — every agent is
    a member and its removal would leave the fleet with no baseline layer.

    Membership rows, version snapshots and any live rollout cascade away with the
    row; the audit records how many agents lost the group. Members are never left
    unconfigured: they keep Global plus whatever other groups they are in."""
    row = await session.get(AgentConfigGroup, group_id)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    if row.is_system:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "the Global group is permanent — every agent is a member of it",
        )
    counts = await _member_counts(session)
    members = counts.get(row.id, 0)
    name = row.name
    await session.delete(row)
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_DELETED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"group_id": str(group_id), "name": name, "members_reset": members},
    )


@router.get(
    "/agents/config-groups/{group_id}/history",
    response_model=list[ConfigVersionOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def config_group_history(
    group_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
    before: int | None = Query(default=None),
    limit: int = Query(default=100),
) -> list[ConfigVersionOut]:
    """Version history for one group, newest-first (keyset by ``version`` desc;
    ``before`` = the last version of the previous page). Cap 100."""
    if await session.get(AgentConfigGroup, group_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    limit = max(1, min(limit, 100))
    q = select(AgentConfigGroupVersion).where(
        AgentConfigGroupVersion.group_id == group_id
    )
    if before is not None:
        q = q.where(AgentConfigGroupVersion.version < before)
    q = q.order_by(AgentConfigGroupVersion.version.desc()).limit(limit)
    return [_version_out(r) for r in (await session.execute(q)).scalars()]


@router.post(
    "/agents/config-groups/{group_id}/rollback",
    response_model=ConfigGroupOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def rollback_config_group(
    group_id: uuid.UUID,
    body: RollbackIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ConfigGroupOut:
    """Republish an old snapshot as a NEW version, live immediately.

    Forward-only: the history is never rewound, it grows. 404 for an unknown
    group or version.

    **No rollout option, deliberately.** Rollback is what an operator reaches for
    when a config is actively breaking machines; making them schedule a phased
    return to the known-good document would be the wrong default at exactly the
    wrong moment. For the same reason any LIVE rollout on this group is
    cancelled: leaving it running would keep handing the bad version to the
    covered slice while the rest recovered."""
    group = await session.get(AgentConfigGroup, group_id)
    if group is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such config group")
    target = (
        await session.execute(
            select(AgentConfigGroupVersion).where(
                AgentConfigGroupVersion.group_id == group_id,
                AgentConfigGroupVersion.version == body.version,
            )
        )
    ).scalars().first()
    if target is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such version")

    group.settings = target.settings or {}
    group.policy = target.policy or {}
    live = await _live_rollout(session, group.id)
    if live is not None:
        live.status = "cancelled"
        live.finished_at = datetime.now(UTC)
    snapshot = await _publish_version(
        session,
        group,
        actor=audit.actor_id(request),
        note=body.note or f"rollback to version {body.version}",
    )
    group.current_version = snapshot.version
    await session.commit()
    await session.refresh(group)
    await audit.emit(
        audit.AGENT_CONFIG_VERSION_PUBLISHED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "group_id": str(group.id),
            "name": group.name,
            "version": snapshot.version,
            "generation": snapshot.seq,
            "immediate": True,
            "rollback_of": body.version,
        },
    )
    if live is not None:
        await audit.emit(
            audit.AGENT_CONFIG_ROLLOUT_CANCELLED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "rollout_id": str(live.id),
                "group_id": str(group.id),
                "group_name": group.name,
                "reason": "superseded by rollback",
            },
        )
    counts = await _member_counts(session)
    members = (
        await _agent_total(session) if group.is_system else counts.get(group.id, 0)
    )
    return _group_out(group, members, None)


# --------------------------------------------------------------------------- #
# Membership + effective configuration (per agent)                             #
# --------------------------------------------------------------------------- #
@router.put(
    "/agents/{agent_id}/config-groups",
    response_model=MembershipOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def set_agent_config_groups(
    agent_id: uuid.UUID,
    body: MembershipIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> MembershipOut:
    """Replace an agent's ENTIRE explicit group membership (PUT semantics — an
    empty list removes every group).

    404 for an unknown agent or an unknown group id. **Global is implicit**: it
    is never stored as a membership row, so passing its id is a 400 rather than a
    silent no-op — accepting it would imply it could also be OMITTED, which it
    cannot.

    Nothing is pushed. The agent's next poll resolves the new group set; the
    document hash rides the ETag, so a membership change alone (which moves no
    version number) still forces a 200 rather than a 304."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")

    wanted = list(dict.fromkeys(body.group_ids))  # de-dupe, keep order
    groups: list[AgentConfigGroup] = []
    for gid in wanted:
        group = await session.get(AgentConfigGroup, gid)
        if group is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, f"no such config group: {gid}")
        if group.is_system:
            raise HTTPException(
                status.HTTP_400_BAD_REQUEST,
                "the Global group is implicit — every agent is already a member "
                "and it cannot be added or removed",
            )
        groups.append(group)

    old = set(
        (
            await session.execute(
                select(AgentConfigGroupMember.group_id).where(
                    AgentConfigGroupMember.agent_id == agent_id
                )
            )
        ).scalars()
    )
    await session.execute(
        delete(AgentConfigGroupMember).where(
            AgentConfigGroupMember.agent_id == agent_id
        )
    )
    for group in groups:
        session.add(AgentConfigGroupMember(agent_id=agent_id, group_id=group.id))
    await session.commit()
    await audit.emit(
        audit.AGENT_CONFIG_GROUP_ASSIGNED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "agent_id": str(agent_id),
            "old_group_ids": sorted(str(g) for g in old),
            "new_group_ids": [str(g.id) for g in groups],
        },
    )
    return MembershipOut(
        agent_id=agent_id,
        group_ids=[g.id for g in groups],
        groups=[
            {"id": str(g.id), "name": g.name, "priority": g.priority} for g in groups
        ],
    )


@router.get(
    "/agents/{agent_id}/effective-config",
    response_model=EffectiveConfigOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def agent_effective_config(
    agent_id: uuid.UUID,
    session: AsyncSession = Depends(get_session),
) -> EffectiveConfigOut:
    """The ADMIN view of one agent's effective configuration.

    Byte-identical to what ``GET /agents/{id}/policy`` would deliver, minus the
    server-injected ``taxonomy_version`` (see :class:`EffectiveConfigOut`), plus
    the ordered contributor list and per-key provenance the console renders as
    source badges. Read-only: it stamps nothing and emits no ETag, so an operator
    inspecting an agent never perturbs its liveness signals.

    ``generation`` vs ``confirmed_generation`` is the "published vs enforced"
    pair: the second is what the agent last echoed via ``?applied=``, so a lower
    value means the change has been published but the agent has not confirmed it
    (it may be offline, or simply between polls)."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "agent not found")
    effective = await agent_config.resolve_effective_config(session, agent)
    return EffectiveConfigOut(
        agent_id=agent.id,
        document=effective.document,
        generation=effective.generation,
        hash=effective.hash,
        groups=[
            {
                "id": str(c.group_id),
                "name": c.name,
                "priority": c.priority,
                "is_system": c.is_system,
                "version_used": c.version_used,
                "via_rollout": c.via_rollout,
            }
            for c in effective.contributors
        ],
        provenance=effective.provenance,
        confirmed_generation=agent.config_generation_applied,
        last_seen_at=agent.last_seen_at,
    )


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
    """Mint an enrollment token and return the COMPLETE sidecar the console agent
    consumes, plus token metadata + per-OS install hints (FROZEN contract,
    :class:`InstallerConfigOut`).

    ``central_url`` = ``central_url_override`` or the derived outward URL.
    ``config_group_ids`` (if given) must all exist (422 otherwise); the group
    NAMES are recorded on the minted TOKEN, so central applies the membership at
    register regardless of what the enrolling binary sends — which is what lets a
    multi-group install work with agent binaries that only know how to echo a
    single ``config_group`` string back. ``log_level`` (if given) must be a known
    level (422). Audited by token hash + group names (NEVER the raw token)."""
    settings = get_settings()

    if body.log_level is not None and body.log_level not in agent_config.LOG_LEVELS:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"log_level must be one of {list(agent_config.LOG_LEVELS)}",
        )

    group_names: list[str] = []
    for gid in body.config_group_ids:
        group = await session.get(AgentConfigGroup, gid)
        if group is None:
            raise HTTPException(
                status.HTTP_422_UNPROCESSABLE_ENTITY, f"no such config group: {gid}"
            )
        if group.is_system:
            # Global is implicit; naming it would suggest it were optional.
            continue
        group_names.append(group.name)

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
        session, config_group_names=group_names, ttl_seconds=ttl_seconds
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_INSTALLER_CONFIG_ISSUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "token_hash": tok.token_hash,
            "config_groups": group_names,
            "agent_name": body.agent_name,
        },
    )
    return InstallerConfigOut(
        sidecar=InstallerSidecar(
            central_url=central_url,
            enrollment_token=raw,
            agent_name=body.agent_name,
            config_group_names=group_names,
            config_group=group_names[0] if group_names else None,
            log_level=body.log_level,
        ),
        token_hash=tok.token_hash,
        expires_at=tok.expires_at,
        install_hint=_install_hint(central_url),
    )
