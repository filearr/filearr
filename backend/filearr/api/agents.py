"""Distributed-agent enrollment surface (Phase 5, P5-T1).

The central-side fleet trust root: minting single-use short-TTL enrollment
tokens, the register-FIRST handshake (R3 — server assigns the authoritative
``agents.id`` before any cert exists), the pending→active cert-fingerprint
binding seam, the fleet console list, and the application-layer revoke kill
switch (research §1.4). Every mutation is audited via ``security_events``.

Auth model (two distinct planes):

* **Operator/admin plane** — token mint/list/revoke, agent list/revoke —
  requires the ``admin`` scope (RBAC-enforced, consistent with every other
  admin surface). These are the console actions.
* **Agent plane** — ``/register`` and ``/certificate`` — is NOT API-key gated:
  the enrollment TOKEN (register) and the one-time enroll SECRET (certificate)
  ARE the credentials, exactly as an unenrolled machine has no API key yet. In
  v3 these ride the enrollment/mTLS channel; P5-T2 hardens ``/certificate``
  behind the freshly-minted client cert. Both still require the feature to be
  enabled.

The whole surface is gated by ``FILEARR_AGENTS_ENABLED`` (default off): a
single-node deploy that never enables agents sees a 404 here and no console
panel. The tables exist regardless (empty), so enabling later needs no
migration. Actual CA signing/renewal is the agent↔step-ca flow (P5-T2, gated by
the P5-T2a integration spike); this module only brokers identity + bootstrap
info.
"""

from __future__ import annotations

import base64
import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field, model_validator
from sqlalchemy import delete, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, audit, toolversions
from filearr import policy as policy_mod
from filearr.agentsync import EnrollmentError
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, EnrollmentToken, Item, Library, ScanRun
from filearr.search import delete_docs
from filearr.security import require_scope

router = APIRouter()


# --------------------------------------------------------------------------- #
# Feature gate                                                                 #
# --------------------------------------------------------------------------- #
def require_agents_enabled() -> None:
    """404 the whole surface unless ``FILEARR_AGENTS_ENABLED`` is set. Agents are
    a v3 opt-in; a single-node deploy is unaffected."""
    if not get_settings().agents_enabled:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "distributed agents disabled")


# Map an EnrollmentError.reason onto an HTTP status.
_AUTH_REASONS = {"unknown_token", "consumed", "expired", "bad_secret"}
_CONFLICT_REASONS = {"already_bound"}
_FORBIDDEN_REASONS = {"revoked", "not_active"}


def _enrollment_http(err: EnrollmentError) -> HTTPException:
    if err.reason in _AUTH_REASONS:
        return HTTPException(status.HTTP_401_UNAUTHORIZED, str(err))
    if err.reason in _CONFLICT_REASONS:
        return HTTPException(status.HTTP_409_CONFLICT, str(err))
    if err.reason in _FORBIDDEN_REASONS:
        return HTTPException(status.HTTP_403_FORBIDDEN, str(err))
    if err.reason in {"unknown_agent"}:
        return HTTPException(status.HTTP_404_NOT_FOUND, str(err))
    return HTTPException(status.HTTP_400_BAD_REQUEST, str(err))


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class TokenMintIn(BaseModel):
    rollout_group: str = Field(default="default", min_length=1, max_length=128)
    # Override the default TTL (minutes); clamped to a sane minutes-to-hours band.
    ttl_minutes: int | None = Field(default=None, ge=1, le=1440)


class TokenMintOut(BaseModel):
    """The ONLY response that ever carries the raw token — shown once."""

    token: str  # raw, show-once, never persisted
    token_hash: str
    rollout_group: str
    expires_at: datetime


class TokenOut(BaseModel):
    token_hash: str
    rollout_group: str
    expires_at: datetime
    consumed_at: datetime | None
    consumed_by: uuid.UUID | None
    created_at: datetime
    status: str  # active | consumed | expired


class RegisterIn(BaseModel):
    token: str = Field(min_length=1)
    hostname: str = Field(min_length=1, max_length=255)
    platform: str  # windows | macos | linux (validated in agentsync)
    name: str | None = Field(default=None, max_length=255)
    agent_version: str | None = Field(default=None, max_length=64)
    # W6-D2: config group by NAME (from the installer sidecar). Resolved to
    # config_group_id at register; an unknown name is fail-safe (NULL group +
    # a warning in the response), never a registration failure.
    config_group: str | None = Field(default=None, max_length=128)


class CaBootstrap(BaseModel):
    """Public pinning material the agent needs to reach the CA (never a secret)."""

    url: str
    fingerprint: str
    provisioner: str
    cert_ttl_hours: int


class RegisterOut(BaseModel):
    agent_id: uuid.UUID
    rollout_group: str
    status: str  # 'pending' — awaiting cert binding
    # One-time secret the agent presents to POST /certificate after the CA signs.
    enroll_secret: str
    ca: CaBootstrap
    # P5-T2: scoped step-ca JWK one-time token the agent exchanges DIRECTLY with
    # the CA's /1.0/sign to obtain its client cert. NULL when the provisioner JWK
    # (FILEARR_CA_PROVISIONER_JWK) is unset/malformed — the agent registers fine
    # but cannot fetch a cert until the operator plumbs the key (re-issue via
    # POST /agents/{id}/ca-ott once configured).
    ca_ott: str | None = None
    # W6-D2: null on success; set to a human-readable string when a supplied
    # ``config_group`` name did not resolve (agent enrolled with NULL group).
    config_group_warning: str | None = None


class CertBindIn(BaseModel):
    enroll_secret: str = Field(min_length=1)
    cert_fingerprint: str = Field(min_length=1, max_length=256)


class CaOttOut(BaseModel):
    """Response of the operator re-issue endpoint: a fresh step-ca OTT plus the
    same CA bootstrap the agent pins. Never carries a secret beyond the OTT
    (a short-lived, single-use bearer)."""

    ca_ott: str
    ca: CaBootstrap


class AgentOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    platform: str
    rollout_group: str
    status: str
    cert_fingerprint: str | None
    last_contiguous_seq_no: int
    last_seen_at: datetime | None
    agent_version: str | None
    policy_version_applied: int | None
    revoked_at: datetime | None
    created_at: datetime
    # W6-D4: current config-group assignment (NULL = built-in defaults). Lets the
    # fleet console reflect + drive the inline group dropdown without an N+1.
    config_group_id: uuid.UUID | None
    # W6-D3: capability advertisement persisted from the agent's command poll
    # ({inventory_collectors, inventory_version}; NULL until the agent's first
    # post-W6 poll). The console offers only collectors an agent supports.
    capabilities: dict | None = None
    # 2026-08-11 minimum-version awareness: central's verdict on each host tool
    # this agent advertises — 'ok' | 'outdated' | 'unknown' | 'absent', keyed by
    # tool name (filearr.toolversions). Derived from `capabilities`, never stored:
    # the minimums are a central opinion that can be revised in a central release,
    # so recomputing them per response is what keeps a fleet of agents out of it.
    # `{}` for an agent that has never advertised anything — empty is honest,
    # seven 'absent' verdicts would state as fact something we never observed.
    tool_verdicts: dict[str, str] = {}
    # 2026-08-08 fleet health: the agent's self-reported snapshot (uptime,
    # outbox backlog, index size, scan state) + when it arrived, and CENTRAL's
    # observation of the transport its last authenticated request used
    # ('bearer' | 'mtls') — the honest answer to "is this agent on mTLS".
    health: dict | None = None
    health_at: datetime | None = None
    last_auth_mode: str | None = None
    # 2026-08-05 update surfacing (populated by the LIST endpoint only —
    # mutation endpoints return the defaults): the version this agent would be
    # offered (signed release or the central-baked dist version), whether that
    # makes an update available, and whether a self_update command is already
    # in flight. Drives the console's badge + per-agent update button.
    update_available: bool = False
    update_target: str | None = None
    update_pending: bool = False


class AgentPage(BaseModel):
    """Paginated registered-agents listing. A large environment can hold
    hundreds/thousands of agents, so ``GET /agents`` pages server-side
    (``total`` drives a real pager; ``items`` is the requested window)."""

    items: list[AgentOut]
    total: int
    limit: int
    offset: int


class AgentPatchIn(BaseModel):
    """The operator-mutable fields of an ENROLLED agent (2026-08-10).

    Until this existed, ``agents.rollout_group`` was written exactly once — at
    register, from the consumed enrollment token — and nothing else in the
    backend ever assigned it. That froze an agent's POLICY group for life
    (``policy.resolve_effective_policy`` resolves ``agent:<id>`` >
    ``group:<rollout_group>`` > ``global``), so the only way to move a machine
    from, say, ``desktops`` to ``filers`` was to re-enroll it. Meanwhile the
    grouping the console *could* edit — ``config_group_id`` — is a deliberately
    ORTHOGONAL dimension (see :class:`filearr.models.AgentConfigGroup`) that
    policy resolution never consults. Operators reasonably assumed the editable
    one was the one policies used; it is not. This endpoint closes that gap
    without merging the two concepts.

    Both fields are optional; omitting one (or sending ``null``, which is not
    distinguishable here and means the same thing — neither column is nullable)
    leaves it untouched. A body that changes nothing is a 422 rather than a
    silent no-op, so a typo'd field name cannot look like a successful edit."""

    # Same constraints as TokenMintIn.rollout_group — the enrollment path and the
    # re-assignment path must agree on what a group name may be, or an operator
    # could create via PATCH a group that a token could never mint into.
    rollout_group: str | None = Field(default=None, min_length=1, max_length=128)
    # The console display name. Mutable: identity is ``agents.id`` (R3) and the
    # hostname is the machine fact — ``name`` is decoration. One non-retroactive
    # caveat, documented rather than prevented: agent-owned library names bake in
    # ``agent.name`` at library-creation time (agentsync.ensure_agent_library), so
    # a rename here does not re-title libraries created before it.
    name: str | None = Field(default=None, min_length=1, max_length=255)

    @model_validator(mode="after")
    def _strip_and_require_one(self) -> AgentPatchIn:
        # Group names become policy scope_ids (``group:<name>``), which are
        # compared byte-for-byte in resolve_effective_policy. A stray copy-paste
        # space would silently resolve to a DIFFERENT (empty) policy document, so
        # normalise here rather than debug it later.
        if self.rollout_group is not None:
            self.rollout_group = self.rollout_group.strip()
            if not self.rollout_group:
                raise ValueError("rollout_group must not be blank")
        if self.name is not None:
            self.name = self.name.strip()
            if not self.name:
                raise ValueError("name must not be blank")
        if self.rollout_group is None and self.name is None:
            raise ValueError("supply at least one of: rollout_group, name")
        return self


class AgentPatchOut(AgentOut):
    """The patched agent PLUS the consequence of the patch.

    Changing ``rollout_group`` changes two unrelated things at once, and an
    operator cannot be expected to hold both in their head:

    * **which policy document the agent resolves** — ``policy_scope`` /
      ``policy_version`` are recomputed with
      :func:`policy.resolve_effective_policy` right after the write, so the
      response says ``group:filers`` (or ``global``, or ``none``) rather than
      leaving the caller to infer it;
    * **release-canary membership** — ``rollout_group`` doubles as the
      canary selector (``agent_updates._covers`` / ``FILEARR_AGENT_CANARY_GROUP``),
      so ``canary_releases`` reports whether this agent now receives
      un-promoted ``stage='canary'`` builds.

    Neither is a separate round-trip for the console, and both are cheap: one
    indexed query per scope level and a settings read."""

    # The scope string the agent will resolve on its NEXT policy poll —
    # "agent:<uuid>" | "group:<name>" | "global" | "none" (no document anywhere).
    policy_scope: str
    policy_version: int
    # True when this agent's rollout group is the configured canary group.
    canary_releases: bool


class RolloutGroupOut(BaseModel):
    """One distinct POLICY (rollout) group observed across enrolled agents.

    Exists so the console can show a group's real member count and offer the
    groups that actually exist. It cannot be derived client-side: ``GET /agents``
    pages server-side (≤200 rows), so counting the loaded window would under-
    report every fleet larger than a page."""

    name: str
    agent_count: int
    # Whether this group is the configured release-canary cohort. Surfaced next
    # to the count because naming your groups "desktops"/"filers" quietly leaves
    # the default canary group ("canary") matching nobody.
    canary: bool


class RolloutGroupsOut(BaseModel):
    """The policy-group roster plus the canary group's NAME.

    The name is part of the payload rather than something the console infers
    from the ``canary`` flag: when the configured canary group has no members
    (the common outcome of naming every group ``desktops``/``filers``) no row
    would carry the flag, and the console could not warn about it or say which
    group a move joins/leaves."""

    canary_group: str
    groups: list[RolloutGroupOut]


class HostToolMinimumOut(BaseModel):
    """One row of the published host-tool minimums (2026-08-11).

    The console pairs this with each agent's ``tool_verdicts`` to explain an
    amber chip: the version found (from the agent), the version wanted
    (``minimum_version``) and what is worse without it (``impact``). ``reason``
    is the justification for the number itself — shipped to the UI on purpose,
    because a threshold whose rationale lives only in a Python comment is a
    threshold operators cannot argue with, and one they will therefore work
    around rather than act on."""

    name: str
    #: ``None`` = Filearr publishes no minimum for this tool. Renders as
    #: "not judged", never as "fine".
    minimum_version: str | None
    impact: str | None
    reason: str | None


def _ca_bootstrap(settings) -> CaBootstrap:
    """The public CA pinning/bootstrap material handed to an agent (never a
    secret — the root fingerprint is a pin, not a credential)."""
    return CaBootstrap(
        url=settings.ca_url,
        fingerprint=settings.ca_fingerprint,
        provisioner=settings.ca_provisioner,
        cert_ttl_hours=settings.agent_cert_ttl_hours,
    )


def _token_out(row: EnrollmentToken, now: datetime) -> TokenOut:
    if row.consumed_at is not None:
        st = "consumed"
    elif row.expires_at <= now:
        st = "expired"
    else:
        st = "active"
    return TokenOut(
        token_hash=row.token_hash,
        rollout_group=row.rollout_group,
        expires_at=row.expires_at,
        consumed_at=row.consumed_at,
        consumed_by=row.consumed_by,
        created_at=row.created_at,
        status=st,
    )


def _agent_out(a: Agent) -> AgentOut:
    return AgentOut(
        id=a.id,
        name=a.name,
        hostname=a.hostname,
        platform=a.platform,
        rollout_group=a.rollout_group,
        status=agentsync.agent_status(a),
        cert_fingerprint=a.cert_fingerprint,
        last_contiguous_seq_no=a.last_contiguous_seq_no,
        last_seen_at=a.last_seen_at,
        agent_version=a.agent_version,
        policy_version_applied=a.policy_version_applied,
        revoked_at=a.revoked_at,
        created_at=a.created_at,
        config_group_id=a.config_group_id,
        capabilities=a.capabilities,
        tool_verdicts=toolversions.capability_verdicts(a.capabilities),
        health=a.health,
        health_at=a.health_at,
        last_auth_mode=a.last_auth_mode,
    )


# --------------------------------------------------------------------------- #
# Enrollment tokens (admin)                                                    #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/enrollment-tokens",
    response_model=TokenMintOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def mint_token(
    body: TokenMintIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> TokenMintOut:
    settings = get_settings()
    ttl_minutes = body.ttl_minutes or settings.enrollment_token_ttl_minutes
    raw, row = await agentsync.mint_enrollment_token(
        session,
        rollout_group=body.rollout_group,
        ttl_seconds=ttl_minutes * 60,
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_TOKEN_MINTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "token_hash": row.token_hash,  # scrubbed key name is fine; not the raw
            "rollout_group": row.rollout_group,
            "ttl_minutes": ttl_minutes,
        },
    )
    return TokenMintOut(
        token=raw,
        token_hash=row.token_hash,
        rollout_group=row.rollout_group,
        expires_at=row.expires_at,
    )


@router.get(
    "/agents/enrollment-tokens",
    response_model=list[TokenOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_tokens(
    session: AsyncSession = Depends(get_session),
) -> list[TokenOut]:
    now = datetime.now(UTC)
    rows = (
        await session.execute(
            select(EnrollmentToken).order_by(EnrollmentToken.created_at.desc())
        )
    ).scalars().all()
    return [_token_out(r, now) for r in rows]


@router.delete(
    "/agents/enrollment-tokens/{token_hash}",
    status_code=status.HTTP_204_NO_CONTENT,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def revoke_token(
    token_hash: str,
    request: Request,
    force: bool = False,
    session: AsyncSession = Depends(get_session),
) -> None:
    """Delete an enrollment token row. An UNconsumed token deletes freely (it is
    a live credential being retired). A CONSUMED token is spent and harmless, but
    its row carries the ``consumed_by`` audit link — deleting it needs
    ``?force=true``, and the audit event captures that link before the row goes
    (so the trail survives the cleanup)."""
    row = await session.get(EnrollmentToken, token_hash)
    if row is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such token")
    if row.consumed_at is not None and not force:
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "token already consumed; delete with ?force=true to clean it up",
        )
    consumed_by = str(row.consumed_by) if row.consumed_by else None
    await session.execute(
        delete(EnrollmentToken).where(EnrollmentToken.token_hash == token_hash)
    )
    await session.commit()
    await audit.emit(
        audit.AGENT_TOKEN_REVOKED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"token_hash": token_hash, "forced": force, "consumed_by": consumed_by},
    )


# --------------------------------------------------------------------------- #
# Register-first handshake (agent plane — token/secret authed, NOT API key)    #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/register",
    response_model=RegisterOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled)],
)
async def register(
    body: RegisterIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> RegisterOut:
    """R3: consume the enrollment token, assign the authoritative server-side
    ``agent_id``, and return it plus CA bootstrap info. The agent is created
    PENDING (no cert yet); it then CSRs against step-ca with the returned id in
    its CN/SAN and binds the fingerprint via ``/certificate``."""
    try:
        agent, raw_secret, config_group_warning = await agentsync.register_agent(
            session,
            raw_token=body.token,
            hostname=body.hostname,
            platform=body.platform,
            name=body.name,
            agent_version=body.agent_version,
            config_group=body.config_group,
        )
    except EnrollmentError as err:
        await session.rollback()
        # Audit the failed attempt (no secrets; the raw token never lands here).
        await audit.emit(
            audit.AGENT_REGISTERED,
            request=request,
            details={"outcome": "rejected", "reason": err.reason},
        )
        raise _enrollment_http(err) from err
    await session.commit()
    settings = get_settings()
    await audit.emit(
        audit.AGENT_REGISTERED,
        request=request,
        details={
            "outcome": "ok",
            "agent_id": str(agent.id),
            "hostname": agent.hostname,
            "platform": agent.platform,
            "rollout_group": agent.rollout_group,
        },
    )
    # Mint the scoped step-ca OTT (fail-safe: null when the provisioner JWK is
    # unset/malformed — registration already succeeded). Audit the mint by jti
    # only; the token itself never touches the log.
    ca_ott, jti = agentsync.try_mint_ca_ott(agent.id, settings)
    if ca_ott is not None:
        await audit.emit(
            audit.AGENT_CA_OTT_MINTED,
            request=request,
            details={"agent_id": str(agent.id), "jti": jti, "via": "register"},
        )
    return RegisterOut(
        agent_id=agent.id,
        rollout_group=agent.rollout_group,
        status="pending",
        enroll_secret=raw_secret,
        ca=_ca_bootstrap(settings),
        ca_ott=ca_ott,
        config_group_warning=config_group_warning,
    )


@router.post(
    "/agents/{agent_id}/certificate",
    response_model=AgentOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def bind_certificate(
    agent_id: uuid.UUID,
    body: CertBindIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    """Bind the CA-issued cert fingerprint (pending→active). Gated by the
    one-time enroll secret from register (P5-T2 hardens this behind mTLS)."""
    try:
        agent = await agentsync.bind_agent_certificate(
            session,
            agent_id=agent_id,
            raw_secret=body.enroll_secret,
            cert_fingerprint=body.cert_fingerprint,
        )
    except EnrollmentError as err:
        await session.rollback()
        raise _enrollment_http(err) from err
    await session.commit()
    await audit.emit(
        audit.AGENT_CERT_BOUND,
        request=request,
        details={"agent_id": str(agent_id), "cert_fingerprint": body.cert_fingerprint},
    )
    return _agent_out(agent)


class RebindIn(BaseModel):
    """Proof-of-possession cert rotation (see filearr.agentcert). The chain is
    leaf-first PEM (leaf + step-ca intermediate); the signature is base64 ASN.1
    ECDSA over ``agentcert.canonical_payload(agent_id, timestamp, leaf_fp)``."""

    cert_chain_pem: str = Field(min_length=1, max_length=32768)
    timestamp: int
    signature: str = Field(min_length=1, max_length=2048)


@router.post(
    "/agents/{agent_id}/rebind",
    response_model=AgentOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def rebind_certificate(
    agent_id: uuid.UUID,
    body: RebindIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    """Rotate an active agent's bound cert fingerprint after a renewal
    (credential-drift fix 2026-07-24: renewal used to silently invalidate the
    fingerprint-mode bearer ~32h after enroll, and 403-lock mtls-header mode).

    Auth is proof-of-possession, NOT the bearer (which is exactly what has
    drifted when this is needed): the presented chain must verify to the
    pinned step-ca root, name this agent in its SAN, and the caller must sign
    a fresh timestamped payload with the leaf key. Mode-agnostic — it keeps
    the stored fingerprint current for every ``agent_auth_mode``."""
    from filearr import agentcert

    settings = get_settings()
    try:
        sig = base64.b64decode(body.signature, validate=True)
    except Exception as exc:
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST, "signature is not valid base64"
        ) from exc
    try:
        root = await agentcert.get_ca_root(settings)
        fingerprint = agentcert.verify_rebind(
            chain_pem=body.cert_chain_pem,
            agent_id=str(agent_id),
            timestamp=body.timestamp,
            signature=sig,
            root=root,
            max_skew_s=settings.agent_rebind_max_skew_seconds,
        )
    except agentcert.RebindError as err:
        if err.reason == "ca_root_unavailable":
            raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE, str(err)) from err
        if err.reason == "san_mismatch":
            # A valid CA-issued cert for a DIFFERENT agent — identity confusion,
            # not a credential failure.
            raise HTTPException(status.HTTP_403_FORBIDDEN, str(err)) from err
        if err.reason == "bad_request":
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(err)) from err
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(err)) from err

    try:
        agent, changed = await agentsync.rebind_agent_certificate(
            session, agent_id=agent_id, cert_fingerprint=fingerprint
        )
    except EnrollmentError as err:
        await session.rollback()
        raise _enrollment_http(err) from err
    await session.commit()
    if changed:
        await audit.emit(
            audit.AGENT_CERT_REBOUND,
            request=request,
            details={"agent_id": str(agent_id), "cert_fingerprint": fingerprint},
        )
    return _agent_out(agent)


# --------------------------------------------------------------------------- #
# CA one-time token re-issue (admin) — re-enrollment recovery path             #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/ca-ott",
    response_model=CaOttOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def reissue_ca_ott(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CaOttOut:
    """Operator-driven re-issue of a scoped step-ca OTT for an existing agent
    (spike's re-enrollment gap: a long-offline agent past its cert TTL, or one
    that registered before the provisioner JWK was configured). A PENDING or
    ACTIVE agent gets a fresh OTT; a REVOKED agent is refused (409). Audited by
    jti (never the token). 503 when the provisioner JWK is not configured — the
    endpoint's sole purpose is to mint, so there is no fail-safe null here."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent is revoked")
    settings = get_settings()
    ca_ott, jti = agentsync.try_mint_ca_ott(agent.id, settings)
    if ca_ott is None:
        raise HTTPException(
            status.HTTP_503_SERVICE_UNAVAILABLE,
            "CA provisioner JWK not configured (cannot mint OTT)",
        )
    await audit.emit(
        audit.AGENT_CA_OTT_MINTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"agent_id": str(agent_id), "jti": jti, "via": "reissue"},
    )
    return CaOttOut(ca_ott=ca_ott, ca=_ca_bootstrap(settings))


# --------------------------------------------------------------------------- #
# Fleet console (admin)                                                        #
# --------------------------------------------------------------------------- #
class AgentFleetSummary(BaseModel):
    """W6-D4 status header tallies. ``connected``/``disconnected`` split the
    *active* (cert-bound, not revoked) agents by liveness against
    ``agent_online_threshold_seconds``; ``pending``/``revoked`` come straight from
    lifecycle status. ``total`` == connected + disconnected + pending + revoked."""

    total: int
    connected: int
    disconnected: int
    pending: int
    revoked: int


@router.get(
    "/agents/summary",
    response_model=AgentFleetSummary,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def agent_fleet_summary(
    session: AsyncSession = Depends(get_session),
) -> AgentFleetSummary:
    """Fleet status counts for the W6-D4 header, in ONE conditional-aggregation
    query (Postgres ``count(*) FILTER (WHERE …)`` — no N+1). Lifecycle mirrors
    :func:`agentsync.agent_status`: revoked (``revoked_at`` set) > active
    (``cert_fingerprint`` bound) > pending. An *active* agent is CONNECTED when it
    was last seen within ``agent_online_threshold_seconds``; otherwise (older, or
    never seen) DISCONNECTED."""
    settings = get_settings()
    cutoff = datetime.now(UTC) - timedelta(
        seconds=settings.agent_online_threshold_seconds
    )
    # Lifecycle predicates.
    is_revoked = Agent.revoked_at.is_not(None)
    is_pending = Agent.revoked_at.is_(None) & Agent.cert_fingerprint.is_(None)
    is_active = Agent.revoked_at.is_(None) & Agent.cert_fingerprint.is_not(None)
    is_fresh = Agent.last_seen_at.is_not(None) & (Agent.last_seen_at >= cutoff)
    row = (
        await session.execute(
            select(
                func.count().label("total"),
                func.count().filter(is_revoked).label("revoked"),
                func.count().filter(is_pending).label("pending"),
                func.count().filter(is_active & is_fresh).label("connected"),
                func.count()
                .filter(
                    is_active
                    & or_(Agent.last_seen_at.is_(None), Agent.last_seen_at < cutoff)
                )
                .label("disconnected"),
            )
        )
    ).one()
    return AgentFleetSummary(
        total=row.total,
        connected=row.connected,
        disconnected=row.disconnected,
        pending=row.pending,
        revoked=row.revoked,
    )


@router.get(
    "/agents/rollout-groups",
    response_model=RolloutGroupsOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_rollout_groups(
    session: AsyncSession = Depends(get_session),
) -> RolloutGroupsOut:
    """Every distinct POLICY (rollout) group across enrolled agents, with its
    member count — one GROUP BY, no N+1, no page-window skew.

    Declared BEFORE any ``/agents/{agent_id}`` route so the literal path wins the
    match (same reason ``/agents/summary`` sits where it does). Revoked agents are
    counted too: a revoked row keeps its group and still shows in the fleet table,
    so hiding it here would make the count disagree with the list.

    The configured canary group is ALWAYS present in ``groups`` — with
    ``agent_count: 0`` when nothing is in it. That zero is the point: the default
    canary group is ``"canary"``, so an operator who names their groups
    ``desktops``/``filers`` has silently left canary releases reaching nobody, and
    the console can only say so if the row exists."""
    settings = get_settings()
    counts = dict(
        (
            await session.execute(
                select(Agent.rollout_group, func.count()).group_by(
                    Agent.rollout_group
                )
            )
        ).all()
    )
    counts.setdefault(settings.agent_canary_group, 0)
    return RolloutGroupsOut(
        canary_group=settings.agent_canary_group,
        groups=[
            RolloutGroupOut(
                name=name,
                agent_count=counts[name],
                canary=(name == settings.agent_canary_group),
            )
            for name in sorted(counts)
        ],
    )


@router.get(
    "/agents/host-tool-minimums",
    response_model=list[HostToolMinimumOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_host_tool_minimums() -> list[HostToolMinimumOut]:
    """The minimum recommended version of each extraction host tool, with why.

    Static data (:data:`filearr.toolversions.HOST_TOOL_MINIMUMS`) — no database,
    no agent state. The per-agent judgement already rides every agent row as
    ``tool_verdicts``; this endpoint carries the PROSE the console needs to
    explain a verdict: the number, one sentence on what degrades below it, and
    the justification behind the number.

    Sent separately rather than inline on each agent because it is identical for
    every row — repeating four paragraphs of reasoning across a 200-agent page
    would be most of the payload. Declared BEFORE ``/agents/{agent_id}`` so the
    literal path wins the match (same reason ``/agents/summary`` sits where it
    does).

    A tool with ``minimum_version: null`` is one Filearr publishes no opinion
    about; the console must render it as unjudged, not as fine."""
    return [
        HostToolMinimumOut(
            name=e["name"],
            minimum_version=e["minimum"],
            impact=e["impact"],
            reason=e["reason"],
        )
        for e in toolversions.HOST_TOOL_MINIMUMS
    ]


@router.patch(
    "/agents/{agent_id}",
    response_model=AgentPatchOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def update_agent(
    agent_id: uuid.UUID,
    body: AgentPatchIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> AgentPatchOut:
    """Re-assign an enrolled agent's POLICY (rollout) group and/or rename it.

    **This is the only writer of ``agents.rollout_group`` after registration.**
    Before it existed the column was set once from the enrollment token and never
    again, which froze an agent's policy group permanently — operators worked
    around it by re-enrolling the machine. That is no longer necessary.

    **Two consequences, both deliberate, both reported in the response.**
    ``rollout_group`` has two jobs and this endpoint does not try to split them
    (the model documents the separation on purpose — see
    :class:`filearr.models.AgentConfigGroup`):

    1. It is the middle POLICY scope. ``policy.resolve_effective_policy`` walks
       ``agent:<id>`` > ``group:<rollout_group>`` > ``global`` and the winner
       supplies the WHOLE document — there is NO key merging — so moving an agent
       into a group that has a ``group:`` document replaces its entire effective
       policy with that document, not just the keys that differ. The recomputed
       ``policy_scope``/``policy_version`` in the response is the honest answer to
       "what does it get now".
    2. It is the release-canary selector (``agent_updates._covers`` against
       ``FILEARR_AGENT_CANARY_GROUP``, default ``"canary"``). Moving an agent out
       of the canary group stops offering it un-promoted builds; moving one in
       starts. ``canary_releases`` reports which side it landed on.

    **Timing:** nothing is pushed. The agent picks the new policy up on its next
    poll (``poll_interval_seconds``, default ~60 s) — the policy ETag is derived
    from ``<scope>/<version>``, so a scope change alone invalidates its cache and
    forces a 200 rather than a 304. No restart, no command queue.

    404 unknown agent; 409 revoked (a denylisted agent receives no policy at all,
    so re-grouping it would be a lie); 422 on a blank/oversized name or a body
    that changes nothing. Audited as ``agent_updated`` with old→new values."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent is revoked")

    changes: dict[str, dict[str, str]] = {}
    if body.rollout_group is not None and body.rollout_group != agent.rollout_group:
        changes["rollout_group"] = {"old": agent.rollout_group, "new": body.rollout_group}
        agent.rollout_group = body.rollout_group
    if body.name is not None and body.name != agent.name:
        changes["name"] = {"old": agent.name, "new": body.name}
        agent.name = body.name
    if changes:
        await session.commit()
        await audit.emit(
            audit.AGENT_UPDATED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "agent_id": str(agent_id),
                "hostname": agent.hostname,
                "changes": changes,
            },
        )

    # Resolve AFTER the commit so the reported scope is what the agent's next
    # poll will actually see, not a pre-write guess.
    scope, version, _policy = await policy_mod.resolve_effective_policy(session, agent)
    settings = get_settings()
    out = AgentPatchOut(
        **_agent_out(agent).model_dump(),
        policy_scope=scope,
        policy_version=version,
        canary_releases=agent.rollout_group == settings.agent_canary_group,
    )
    return out


@router.get(
    "/agents",
    response_model=AgentPage,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_agents(
    limit: int = 50,
    offset: int = 0,
    session: AsyncSession = Depends(get_session),
) -> AgentPage:
    """Registered agents, newest first, PAGINATED — a fleet can reach hundreds
    or thousands of agents, so the console never loads the whole table.
    ``limit`` is capped server-side; the fleet-status tallies stay on the
    dedicated one-query ``/agents/summary``."""
    limit = max(1, min(limit, 200))
    offset = max(0, offset)
    total = (
        await session.execute(select(func.count()).select_from(Agent))
    ).scalar_one()
    rows = (
        (
            await session.execute(
                select(Agent)
                .order_by(Agent.created_at.desc())
                .limit(limit)
                .offset(offset)
            )
        )
        .scalars()
        .all()
    )
    # Update surfacing (2026-08-05): one shared "what would this agent be
    # offered" resolution per row (releases are read once inside; the page is
    # <=200 rows) + one grouped query for in-flight self_update commands.
    # Deferred import: agent_updates imports require_agents_enabled from here.
    from filearr.api.agent_updates import resolve_update_target
    from filearr.models import AgentCommand, AgentRelease

    settings = get_settings()
    releases = list(
        (
            await session.execute(
                select(AgentRelease).order_by(AgentRelease.created_at.desc())
            )
        ).scalars()
    )
    pending_ids = {
        row
        for row in (
            await session.execute(
                select(AgentCommand.agent_id).where(
                    AgentCommand.kind == "self_update",
                    AgentCommand.status.in_(("pending", "picked_up")),
                )
            )
        ).scalars()
    }
    items: list[AgentOut] = []
    for a in rows:
        out = _agent_out(a)
        if a.revoked_at is None:
            target = await resolve_update_target(session, settings, a, releases)
            out.update_target = target
            out.update_available = target is not None
            out.update_pending = a.id in pending_ids
        items.append(out)
    return AgentPage(items=items, total=total, limit=limit, offset=offset)


@router.delete(
    "/agents/{agent_id}",
    response_model=AgentOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def revoke_agent(
    agent_id: uuid.UUID,
    request: Request,
    purge: bool = False,
    delete_libraries: bool = False,
    session: AsyncSession = Depends(get_session),
) -> AgentOut:
    """Application-layer kill switch (research §1.4): stamp ``revoked_at`` so the
    agent is denylisted on every replication/config request regardless of whether
    its short-lived cert is still cryptographically valid. NOT a hard delete —
    the row (and its future replication history) is retained. Idempotent.

    ``?purge=true`` HARD-deletes the row instead — the cleanup path for failed
    enrollments (pending rows) and decommissioned machines with no data
    footprint. Refused (409) while any library or item still references the
    agent: replicated data must keep its provenance, so a data-owning agent can
    only be revoked (or its libraries removed first). Cascades wipe the agent's
    commands/transfers/ledger/reconcile rows; ``libraries.source_agent_id`` and
    ``enrollment_tokens.consumed_by`` are SET NULL by their FKs."""
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if purge:
        # ?delete_libraries=true (user request 2026-07-27): decommissioning an
        # agent used to demand deleting each of its libraries by hand (each
        # with a typed-name confirmation) before the purge would proceed. This
        # flag removes the agent's libraries in the same action — same
        # semantics as the library DELETE endpoint (FK cascade removes items/
        # scan_runs/scan_paths/item_versions; the Meili projection is pruned
        # by explicit id AFTER the commit), same running-scan refusal. Only
        # meaningful with purge (a revoke keeps data by definition).
        deleted_libraries = 0
        removed_item_ids: list[str] = []
        if delete_libraries:
            libs = (
                await session.execute(
                    select(Library).where(Library.source_agent_id == agent_id)
                )
            ).scalars().all()
            if libs:
                running = (
                    await session.execute(
                        select(func.count())
                        .select_from(ScanRun)
                        .where(
                            ScanRun.library_id.in_([lib.id for lib in libs]),
                            ScanRun.status.in_(("running", "stopping")),
                        )
                    )
                ).scalar_one()
                if running:
                    raise HTTPException(
                        status.HTTP_409_CONFLICT,
                        "a scan is running for one of this agent's libraries; "
                        "wait for it to finish first",
                    )
                lib_ids = [lib.id for lib in libs]
                removed_item_ids.extend(
                    str(i)
                    for i in (
                        await session.execute(
                            select(Item.id).where(Item.library_id.in_(lib_ids))
                        )
                    ).scalars()
                )
                # Core DELETE (not session.delete): the ORM would try to NULL
                # the children's FKs; the DB's ON DELETE CASCADE is the
                # authority here, exactly like the library DELETE endpoint.
                await session.execute(
                    delete(Library).where(Library.id.in_(lib_ids))
                )
                deleted_libraries = len(libs)
                await session.flush()
        lib_count = (
            await session.execute(
                select(func.count()).select_from(Library).where(
                    Library.source_agent_id == agent_id
                )
            )
        ).scalar_one()
        # items.source_agent_id carries no FK (provenance column) — guard in code
        # so a purge can never leave dangling ownership references.
        item_count = (
            await session.execute(
                select(func.count()).select_from(Item).where(
                    Item.source_agent_id == agent_id
                )
            )
        ).scalar_one()
        if lib_count or item_count:
            raise HTTPException(
                status.HTTP_409_CONFLICT,
                f"agent owns replicated data ({lib_count} library(ies), "
                f"{item_count} item(s)) — revoke it instead, delete with "
                f"delete_libraries=true to remove them in one action, or "
                "libraries first",
            )
        snapshot = _agent_out(agent)
        await session.delete(agent)
        await session.commit()
        # Meili prune AFTER the commit (same discipline as the library DELETE:
        # a failed commit must never remove still-live docs), explicit ids only.
        for start in range(0, len(removed_item_ids), 10_000):
            await delete_docs(removed_item_ids[start : start + 10_000])
        await audit.emit(
            audit.AGENT_DELETED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "agent_id": str(agent_id),
                "hostname": agent.hostname,
                "status": snapshot.status,
                "deleted_libraries": deleted_libraries,
                "deleted_items": len(removed_item_ids),
            },
        )
        return snapshot
    if agent.revoked_at is None:
        agent.revoked_at = datetime.now(UTC)
        await session.commit()
        await audit.emit(
            audit.AGENT_REVOKED,
            request=request,
            principal_id=audit.actor_id(request),
            details={"agent_id": str(agent_id), "hostname": agent.hostname},
        )
    return _agent_out(agent)
