"""P10-T1 — the ``agent_commands`` on-demand command primitive (central-side).

The queue through which central asks an agent to do one thing — ``stat_check`` /
``rehash_check`` (existence/freshness) or ``stage_upload`` (retrieve trigger) —
distinct from Phase-5's policy/replication channels (research §3.1, osquery
``distributed_interval`` precedent). This module is the *central* surface only:
the durable table, its lifecycle, and two auth planes. The agent runtime that
long-polls this queue is P5-T4; the retrieve data plane that consumes a
``stage_upload`` is P10-T4/T6/T13.

Two auth planes, both behind the ``FILEARR_AGENTS_ENABLED`` gate (404 when off,
same as the enrollment surface):

* **Operator/admin plane** — enqueue (``write``), list / get (``read``), cancel
  (``write``). RBAC ``download``-gating of creation is Wave 4 (P6-T4 / R2); the
  coarse ``write`` scope is the stand-in today, exactly as the transfer endpoints
  do. Enqueue + cancel emit ``security_events``; per-poll churn does NOT (noise).
* **Agent plane** — poll / ack / complete. Authenticated with the P5-T1 INTERIM
  agent-plane credential (the agent's bound ``cert_fingerprint`` as a bearer
  token — the only durable per-agent secret before mTLS). **mTLS replaces this in
  P5-T6**: the request identity becomes the verified client cert and this bearer
  check is removed. Documented interim, gated off by default.

Lifecycle + the TTL/redelivery sweep live in :mod:`filearr.agentsync`
(``command_state_machine`` / ``run_agent_command_sweep``); the periodic wrapper
is :func:`filearr.worker.expire_agent_commands`.
"""

from __future__ import annotations

import json
import logging
import secrets
import uuid
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agentsync, audit, maintmode, verify
from filearr.api.agents import require_agents_enabled
from filearr.config import get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, Item
from filearr.security import require_scope
from filearr.worker import defer_agent_associate, defer_index_sync

log = logging.getLogger(__name__)

router = APIRouter()

CommandKind = Literal[
    "stat_check",
    "rehash_check",
    "stage_upload",
    "inventory",
    "self_update",
    "suspend",
    "agent_maintenance",
    "reextract",
    # QH-T6. NOT a variant of ``rehash_check`` above — see the AgentCommand
    # CHECK-constraint comment in models.py. ``rehash_check`` verifies ONE item
    # and writes nothing; ``rehash_sweep`` migrates a whole size band of the
    # agent's index and rewrites the rows in it.
    "rehash_sweep",
]

# Kinds that target the AGENT itself rather than one of its items: item_id is
# absent for these (nullable since the self_update migration).
_AGENT_SCOPED_KINDS = {
    "self_update",
    "suspend",
    "agent_maintenance",
    "reextract",
    "rehash_sweep",
}


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class CommandEnqueueIn(BaseModel):
    kind: CommandKind
    # Required for item-scoped kinds; must be ABSENT for agent-scoped kinds
    # (self_update) — validated in the endpoint.
    item_id: uuid.UUID | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    # Optional per-command TTL override (seconds); clamped server-side.
    ttl_seconds: int | None = Field(default=None, ge=60)


class CommandOut(BaseModel):
    id: uuid.UUID
    agent_id: uuid.UUID
    kind: str
    item_id: uuid.UUID | None
    payload: dict[str, Any]
    status: str
    attempts: int
    created_at: datetime
    updated_at: datetime
    expires_at: datetime
    picked_up_at: datetime | None
    completed_at: datetime | None
    result: dict[str, Any] | None
    requested_by: uuid.UUID | None

    @classmethod
    def of(cls, c: AgentCommand) -> CommandOut:
        return cls(
            id=c.id,
            agent_id=c.agent_id,
            kind=c.kind,
            item_id=c.item_id,
            payload=c.payload or {},
            status=c.status,
            attempts=c.attempts,
            created_at=c.created_at,
            updated_at=c.updated_at,
            expires_at=c.expires_at,
            picked_up_at=c.picked_up_at,
            completed_at=c.completed_at,
            result=c.result,
            requested_by=c.requested_by,
        )


class PollIn(BaseModel):
    # How many commands to drain in one poll. Clamped to FILEARR_AGENT_COMMAND_POLL_MAX.
    # NOTE (P5-T4): this is a PLAIN poll — no server-side long-poll / hold-open
    # yet. The held-open long-poll rides P5-T4's poll/ETag machinery.
    max: int = Field(default=10, ge=1)
    # W6-D3: the additive capability advertisement (inventory collector vocabulary +
    # version). Absent (None) on an older agent build → the stored value is left
    # unchanged. Stored VERBATIM on the agent row (size-capped) so the UI can offer
    # only the collectors an agent supports.
    capabilities: dict[str, Any] | None = None
    # 2026-08-08: the compact self-reported health snapshot (uptime, outbox
    # backlog, index size, scan state). Same contract as capabilities: absent
    # on older builds → stored value untouched; stored VERBATIM, size-capped.
    health: dict[str, Any] | None = None
    # 2026-08-08: the running binary's version. The update-manifest poll was
    # the ONLY version-confirmation channel, and container images disable the
    # updater — central showed those agents' enrollment-era version forever.
    version: str | None = Field(default=None, max_length=256)


class CompleteIn(BaseModel):
    ok: bool = True
    result: dict[str, Any] | None = None


def _json_len(obj: Any) -> int:
    return len(json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def _accept_sized(
    agent_id: uuid.UUID, field: str, body: Any, settings: Any
) -> bool:
    """Should this self-reported blob be stored? Logs the drop when it is not.

    ``capabilities`` and ``health`` are stored VERBATIM under independent size
    caps, and an oversize body is dropped while the poll still succeeds — that
    part is deliberate and stays: the command drain must never depend on an
    advertisement, and a hostile or buggy agent must not be able to bloat its
    row or fail its own command delivery.

    What changes (2026-08-11) is the SILENCE. Dropping without a word means an
    agent whose advertisement grew past the cap goes on polling happily forever
    while central serves a stale capability report, with nothing anywhere saying
    so — the console shows old tool versions, the operator debugs the agent, and
    the answer was in a size check that never spoke. A warning naming the agent,
    the field and the measured size turns that into a one-line diagnosis.

    WARNING rather than ERROR because nothing is broken: commands still flow,
    the previous advertisement still stands. The agent side has its own
    self-trimming budget (``capabilitiesBudget``, 12 KiB, in
    agent/internal/inventory/collector.go) precisely so this branch stays
    unreachable in normal operation — reaching it means that budget and this cap
    have drifted apart, which is a thing worth being told about.
    """
    if body is None:
        return False
    size = _json_len(body)
    limit = settings.agent_capabilities_max_bytes
    if size <= limit:
        return True
    log.warning(
        "agent %s: dropped oversize %s advertisement (%d bytes > %d limit); "
        "the stored value is unchanged and the poll still succeeded",
        agent_id,
        field,
        size,
        limit,
    )
    return False


# --------------------------------------------------------------------------- #
# Agent-plane auth (P5-T6: mTLS-header modes supersede the interim bearer)      #
# --------------------------------------------------------------------------- #
# Headers the Caddy ``agents.<domain>`` mTLS site stamps after it has VERIFIED
# the client cert against the step-ca root (require_and_verify). They are only
# trusted when X-Filearr-Proxy-Auth matches ``FILEARR_PROXY_SHARED_SECRET`` —
# i.e. the request demonstrably transited our own proxy, never a direct hit.
_HDR_PROXY_AUTH = "x-filearr-proxy-auth"  # shared secret (trust gate)
_HDR_AGENT_SAN = "x-filearr-agent-san"    # client cert first DNS SAN == agent_id
_HDR_AGENT_FP = "x-filearr-agent-fp"      # client cert fingerprint (secondary)


async def _authenticate_agent(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """Authenticate an agent-plane request per ``FILEARR_AGENT_AUTH_MODE``.

    * ``fingerprint`` (default) — the INTERIM P5-T1 scheme: the agent's bound
      ``cert_fingerprint`` as a bearer token.
    * ``mtls-header`` — trust ONLY the Caddy-forwarded, already-verified mTLS
      identity (SAN == agent_id), gated by the proxy shared secret; the bearer
      is refused.
    * ``both`` — mtls-header when the proxy-auth header is present (hard-fails on
      a bad secret/SAN), else the bearer path (migration window).

    401 for a missing/mismatched credential, 404 for an unknown agent, 403 for a
    revoked or still-pending (unbound) agent."""
    mode = get_settings().agent_auth_mode
    has_proxy_header = request.headers.get(_HDR_PROXY_AUTH) is not None
    if mode == "mtls-header" or (mode == "both" and has_proxy_header):
        return await _authenticate_agent_mtls(session, agent_id, request)
    return await _authenticate_agent_bearer(session, agent_id, request)


async def _authenticate_agent_bearer(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """INTERIM agent-plane auth (P5-T1): the agent presents its bound
    ``cert_fingerprint`` as a bearer token — the only durable per-agent secret
    before mTLS.

    401 for a missing/mismatched bearer, 404 for an unknown agent, 403 for a
    revoked or still-pending (unbound) agent — a pending agent has no fingerprint
    and so cannot use this plane at all."""
    auth = request.headers.get("authorization") or ""
    token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
    if not token:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "agent credential required")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent revoked")
    if not agent.cert_fingerprint:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent not active")
    if not secrets.compare_digest(token, agent.cert_fingerprint):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid agent credential")
    # Central-observed transport truth for the fleet console ("is this agent
    # actually on mTLS?"). Write only on change — rides the caller's commit.
    if agent.last_auth_mode != "bearer":
        agent.last_auth_mode = "bearer"
    return agent


async def _authenticate_agent_mtls(
    session: AsyncSession, agent_id: uuid.UUID, request: Request
) -> Agent:
    """P5-T6 mTLS-header auth: trust the Caddy-forwarded, already-verified client
    identity when (and only when) X-Filearr-Proxy-Auth matches the configured
    shared secret. Identity is the client cert's DNS SAN, which the enroll flow
    sets to ``str(agent_id)`` — renewal-PROOF (the SAN survives cert rotation, so
    the interim fingerprint-drift caveat does not apply). The bearer token is NOT
    consulted here — the weaker path is shut off in this mode.

    401 for a missing/wrong shared secret (bearer alone can't authenticate),
    403 for a SAN that does not match the path agent_id, 404 for an unknown
    agent, 403 for a revoked agent. A forwarded fingerprint that disagrees with
    the bound one UPDATES the binding (the proxy verified the cert; the stored
    row is the stale side after a renewal — see the drift fix below)."""
    secret = get_settings().proxy_shared_secret or ""
    provided = request.headers.get(_HDR_PROXY_AUTH) or ""
    # Fail closed when the secret is unconfigured: an empty configured secret must
    # never authenticate (else the whole plane is open).
    if not secret or not provided or not secrets.compare_digest(provided, secret):
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "proxy authentication required")
    san = request.headers.get(_HDR_AGENT_SAN) or ""
    if not san or san != str(agent_id):
        # A valid mTLS cert, but for a different agent than the URL path — the
        # caller is authenticated as someone else (authorization failure).
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent identity mismatch")
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "agent revoked")
    # Fingerprint self-heal (drift fix 2026-07-24): the shipped Caddyfile
    # forwards the fp header UNCONDITIONALLY, so after a renewal the old
    # "must agree or 403" rule locked every agent out — the docstring's
    # "skipped when either is absent" escape never fired. The header fp comes
    # from a cert Caddy ALREADY verified against the step-ca root for THIS SAN,
    # so on a mismatch the stored binding is the stale party: update it (keeps
    # fingerprint-mode bearer auth and the console current too). The /rebind
    # endpoint is the mode-agnostic cure; this is defence-in-depth for mtls.
    fp = request.headers.get(_HDR_AGENT_FP) or ""
    if agent.cert_fingerprint and fp and not secrets.compare_digest(fp, agent.cert_fingerprint):
        agent.cert_fingerprint = fp
        await session.commit()
    # Central-observed transport truth for the fleet console ("is this agent
    # actually on mTLS?"). Write only on change — rides the caller's commit.
    if agent.last_auth_mode != "mtls":
        agent.last_auth_mode = "mtls"
    return agent


async def _owned_command(
    session: AsyncSession, agent: Agent, command_id: uuid.UUID
) -> AgentCommand:
    """Load a command that MUST belong to ``agent`` — a wrong-agent id is a 404
    (never leak another agent's command existence)."""
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None or cmd.agent_id != agent.id:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    return cmd


# --------------------------------------------------------------------------- #
# Operator/admin plane — enqueue / list / get / cancel                         #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/commands",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def enqueue_command(
    agent_id: uuid.UUID,
    body: CommandEnqueueIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Enqueue one command for an agent (P10-T1).

    ``write`` scope is the coarse gate today; **Wave 4 (P6-T4 / R2)** additionally
    evaluates the path-scoped RBAC ``download`` action BEFORE the row is created
    (``stat_check`` needs only ``search_metadata``; ``rehash_check`` /
    ``stage_upload`` need ``download``) — authorization stops the costly side
    effect, it does not clean up after it. Enqueue is audited unconditionally."""
    settings = get_settings()
    if _json_len(body.payload) > settings.agent_command_payload_max_bytes:
        raise HTTPException(
            413, "payload too large"
        )
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent revoked")
    if body.kind in _AGENT_SCOPED_KINDS:
        if body.item_id is not None:
            raise HTTPException(422, f"{body.kind} is agent-scoped; item_id must be absent")
    else:
        if body.item_id is None:
            raise HTTPException(422, f"{body.kind} requires item_id")
        item = (
            await session.execute(select(Item).where(Item.id == body.item_id))
        ).scalar_one_or_none()
        if item is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "no such item")

    ttl = body.ttl_seconds or settings.agent_command_ttl_seconds
    ttl = max(60, min(ttl, settings.agent_command_ttl_max_seconds))
    now = datetime.now(UTC)
    cmd = AgentCommand(
        agent_id=agent_id,
        kind=body.kind,
        item_id=body.item_id,
        payload=body.payload,
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=ttl),
        requested_by=_actor_uuid(request),
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": body.kind,
            "item_id": str(body.item_id) if body.item_id else None,
            "ttl_seconds": ttl,
        },
    )
    return CommandOut.of(cmd)


class SuspendIn(BaseModel):
    suspended: bool


async def _live_agent(session: AsyncSession, agent_id: uuid.UUID) -> Agent:
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent revoked")
    return agent


def _enqueue_agent_scoped(
    agent_id: uuid.UUID,
    kind: str,
    payload: dict[str, Any],
    request: Request,
    *,
    ttl_seconds: int | None = None,
) -> AgentCommand:
    """Build one agent-scoped command row.

    ``ttl_seconds`` overrides the global default for the kinds whose RUNTIME —
    not just their pickup delay — can outlast it. That distinction matters
    because :func:`filearr.agentsync.sweep_decision` expires on ``expires_at``
    unconditionally: TTL outranks the lease, so a command whose agent is
    faithfully heartbeating is still marked ``expired`` the moment its window
    lapses. For a command that finishes in seconds the TTL is purely "how long
    we will wait for an offline agent to come back"; for a long sweep it is also
    a ceiling on the work itself. Clamped to the same maximum an operator-
    supplied TTL is clamped to, so this can never outlive the sweep's own bound.
    """
    settings = get_settings()
    ttl = settings.agent_command_ttl_seconds
    if ttl_seconds is not None:
        ttl = min(max(ttl_seconds, ttl), settings.agent_command_ttl_max_seconds)
    now = datetime.now(UTC)
    return AgentCommand(
        agent_id=agent_id,
        kind=kind,
        item_id=None,
        payload=payload,
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=ttl),
        requested_by=_actor_uuid(request),
    )


@router.post(
    "/agents/{agent_id}/suspend",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def suspend_agent(
    agent_id: uuid.UUID,
    body: SuspendIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Queue a ``suspend`` command: the agent pauses (or resumes) its own scan
    scheduling and replication push, persists the state across restarts, and
    keeps polling commands + reporting health (``health.suspended`` is the
    applied truth the console badges). A still-``pending`` suspend command is
    COLLAPSED — its payload is overwritten with the latest desire, so rapid
    toggling never queues a contradictory backlog."""
    await _live_agent(session, agent_id)
    now = datetime.now(UTC)
    pending = (
        await session.execute(
            select(AgentCommand)
            .where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.kind == "suspend",
                AgentCommand.status == "pending",
            )
            .with_for_update(skip_locked=True)
        )
    ).scalars().first()
    if pending is not None:
        pending.payload = {"suspended": body.suspended}
        pending.updated_at = now
        cmd = pending
    else:
        cmd = _enqueue_agent_scoped(
            agent_id, "suspend", {"suspended": body.suspended}, request
        )
        session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": "suspend",
            "suspended": body.suspended,
        },
    )
    return CommandOut.of(cmd)


@router.post(
    "/agents/{agent_id}/maintenance",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def run_agent_maintenance(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Queue an ``agent_maintenance`` command: the agent compacts its local
    SQLite index (VACUUM + WAL checkpoint), prunes replicated-and-acknowledged
    outbox rows past retention, and sweeps stale temp/download files from its
    data dir — reporting what it reclaimed in the command result. 409 while one
    is already queued or running (it is a whole-agent operation)."""
    await _live_agent(session, agent_id)
    in_flight = (
        await session.execute(
            select(AgentCommand.id).where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.kind == "agent_maintenance",
                AgentCommand.status.in_(("pending", "picked_up")),
            )
        )
    ).first()
    if in_flight is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "agent maintenance already queued or running"
        )
    cmd = _enqueue_agent_scoped(agent_id, "agent_maintenance", {}, request)
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": "agent_maintenance",
        },
    )
    return CommandOut.of(cmd)


# A sweep bound, not a page size: the agent stops after this many candidate items
# and leaves its cursor where it stopped, so the next command resumes there. The
# ceiling only exists to reject nonsense (a stray 1e18 from a fat-fingered
# operator or a buggy caller) — a genuine "sweep everything" is expressed by
# OMITTING the knob, which is also the default. Ten million is comfortably past
# the largest catalogue we have observed (~1.09M items on the live LXC).
REEXTRACT_MAX_ITEMS_CEILING = 10_000_000

# TTL for a queued sweep. Unlike every other agent-scoped command, this one's TTL
# has to cover the RUN, not just the wait for pickup (see _enqueue_agent_scoped),
# and a sweep over a million files that runs ffprobe/tesseract per candidate is a
# multi-hour job. 24h is the settings clamp (agent_command_ttl_max_seconds), i.e.
# the longest window the server is willing to hold any command open, and past it
# an operator should be using max_items to chunk the work rather than asking for
# one unbounded run.
REEXTRACT_TTL_SECONDS = 86_400


class ReextractIn(BaseModel):
    # Both knobs are optional and both are simply forwarded in the payload: the
    # agent owns every other decision (which items qualify, in what order, with
    # which extractors) from its own cached policy + local index state. Central
    # holds no cursor and must not pretend to.
    force: bool = False
    # Validated as a positive bound with a ceiling; anything else is a 422 rather
    # than a silent clamp. Enqueue is a fire-and-forget operator action whose
    # effect is not visible for minutes — normalising 0 to "all items" or
    # 10**18 to the ceiling would hand back a 201 for a request the operator did
    # not make. (The TTL clamp in enqueue_command is the opposite case: a
    # machine-set knob with a server-owned safe range.)
    max_items: int | None = Field(default=None, ge=1, le=REEXTRACT_MAX_ITEMS_CEILING)


@router.post(
    "/agents/{agent_id}/reextract",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def reextract_agent(
    agent_id: uuid.UUID,
    # Defaulted so a bare POST works like the sibling ``/maintenance`` action —
    # "sweep everything, honour the fingerprint" is the overwhelmingly common
    # request and should not require a body. Never mutated, so the shared
    # instance is safe.
    request: Request,
    body: ReextractIn = ReextractIn(),
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Queue a ``reextract`` command: the agent sweeps its EXISTING local index,
    re-runs the extraction pass over items that never got one, and re-emits them
    through the normal replication path (extraction parity phase 3).

    Why the command exists: extraction runs inside the agent's scan, over the
    files that scan reports as new or changed. That is the right default — a
    steady-state rescan of an unchanged tree costs nothing — but it leaves a
    permanent gap. An item catalogued before ``extract_enabled`` was turned on,
    or before its host gained ffprobe/exiftool/poppler/tesseract, is never
    enriched, because nothing about that file will ever change again. Only an
    explicit sweep closes it.

    The sweep is RESUMABLE and IDEMPOTENT per extraction configuration: the
    agent keeps a cursor across command invocations (a run interrupted by a
    restart, a suspend, or ``max_items`` continues where it stopped) and
    fingerprints the configuration that would produce the metadata — extractor
    schema, the extraction policy knobs, and which host tools actually resolved.
    A repeat sweep at an unchanged fingerprint short-circuits, so re-firing this
    endpoint after a config change is real work while re-firing it for no reason
    is nearly free. Installing exiftool changes the fingerprint, which is exactly
    the "my agent gained a capability" case this exists for.

    ``force`` is the operator's escape hatch for the remaining case: re-sweep at
    an UNCHANGED configuration (a partial run whose failures are worth retrying,
    or a host whose tools changed in a way the fingerprint cannot see).

    409 while a sweep is already queued or running for this agent — the
    ``agent_maintenance`` guard, not the ``suspend`` collapse. Collapsing is
    right for a *desired state* (the last write of "suspended: true/false" wins
    and a superseded one was never worth delivering); a sweep is a *job* with
    per-agent cursor state, and two of them would fight over that cursor and
    double-emit the items they raced on. The knobs also don't merge: silently
    overwriting a pending ``max_items: 1000`` with an unbounded run is not what
    either operator asked for. Invariant 2 still holds throughout — the sweep
    only ever refreshes extracted ``metadata``; ``user_metadata`` edits are
    untouched, and no file CONTENT leaves the agent."""
    await _live_agent(session, agent_id)
    in_flight = (
        await session.execute(
            select(AgentCommand.id).where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.kind == "reextract",
                AgentCommand.status.in_(("pending", "picked_up")),
            )
        )
    ).first()
    if in_flight is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "reextract sweep already queued or running"
        )
    payload: dict[str, Any] = {"force": body.force, "max_items": body.max_items}
    # A sweep is the one command whose RUNTIME is measured in hours: it re-reads
    # and re-parses every file the agent already holds. The 1h default TTL would
    # expire it mid-run — TTL outranks the lease in ``sweep_decision``, so even a
    # faithfully heartbeating agent would have its command marked ``expired``,
    # the console's in-flight badge would clear while the sweep was still going,
    # and the history would report a failure for work that actually completed.
    # None of the sweep's OUTPUT is lost when that happens (the events are
    # already replicated and the cursor is durable), but the report is a lie, so
    # the row gets the long window instead.
    cmd = _enqueue_agent_scoped(
        agent_id, "reextract", payload, request, ttl_seconds=REEXTRACT_TTL_SECONDS
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": "reextract",
            "force": body.force,
            "max_items": body.max_items,
        },
    )
    return CommandOut.of(cmd)


# --------------------------------------------------------------------------- #
# QH-T6 — the agent-side quick_hash migration sweep                            #
# --------------------------------------------------------------------------- #
# Same bound and the same reasoning as REEXTRACT_MAX_ITEMS_CEILING: a rejection
# for nonsense, not a policy. "Sweep everything" is expressed by OMITTING the
# knob. In practice the default band holds ~99k items fleet-wide, so a single
# unbounded command covers an entire agent and this only matters for the widened
# opt-in backfill.
REHASH_MAX_ITEMS_CEILING = 10_000_000

# The DEFECT BAND, inclusive at both ends, mirroring rehash.DefaultMinSize /
# DefaultMaxSize in the Go agent. Duplicated rather than shared because there is
# no shared vocabulary between the two runtimes for this, and pinned here as the
# API's documented default so an operator reading /api/docs sees the band without
# reading Go.
#
# 65537 and not 1: the pre-QH-T1 code's unconditional read(65536) truncated
# naturally at EOF, so a file of 65536 bytes or fewer had its ENTIRE content
# hashed and its stored quick_hash is already correct. 131072 and not higher:
# above that the tail branch fired then and fires now, so those digests are
# unchanged by the fix.
REHASH_DEFAULT_MIN_SIZE = 65_537
REHASH_DEFAULT_MAX_SIZE = 131_072

# A hard ceiling on the band, not a default. Even the widest legitimate run (the
# QH-T2 parity backfill, granting content_hash to the ~1.03M files below the
# band) stops at 131072 — above it nothing about hashing changed. Accepting an
# arbitrary max_size would let one console click ask an agent to re-read its
# entire library over SMB, which is not a migration, it is an outage. An
# operator who genuinely wants that has ``force`` on the scan side.
REHASH_MAX_SIZE_CEILING = 131_072

# TTL for a queued sweep. Identical reasoning to REEXTRACT_TTL_SECONDS and the
# same 24h settings clamp (agent_command_ttl_max_seconds): this command's TTL has
# to cover the RUN, not just the wait for pickup, because ``sweep_decision``
# ranks TTL above the lease — so even a faithfully heartbeating agent would have
# a multi-hour sweep marked ``expired`` mid-run under the 1h default, clearing
# the console's in-flight badge and recording a failure for work that completed.
REHASH_TTL_SECONDS = 86_400


class RehashSweepIn(BaseModel):
    """Body for ``POST /agents/{agent_id}/rehash-sweep``. Every knob is optional;
    all four are forwarded in the payload and the agent owns every other decision
    (which rows qualify, in what order, at what cursor) from its own local index
    state. Central holds no cursor and must not pretend to."""

    #: Re-sweep at an unchanged scheme and band — the escape hatch for a run
    #: whose failures are worth retrying. Safe to press: the sweep emits only on
    #: change, so a forced run over already-corrected rows verifies them and
    #: writes nothing.
    force: bool = False
    #: Validated as a positive bound with a ceiling; anything else is a 422
    #: rather than a silent clamp, for the same reason ``ReextractIn`` gives —
    #: enqueue is fire-and-forget and normalising a request the operator did not
    #: make into a 201 is worse than refusing it.
    max_items: int | None = Field(default=None, ge=1, le=REHASH_MAX_ITEMS_CEILING)
    #: Inclusive band edges. ``None`` means the defect band above. Both are
    #: bounded by REHASH_MAX_SIZE_CEILING and cross-validated below; a widened
    #: band is a deliberate, separate, opt-in run (the QH-T2 content_hash
    #: backfill), never the default, because it is ~10x the I/O for a different
    #: benefit.
    min_size: int | None = Field(default=None, ge=1, le=REHASH_MAX_SIZE_CEILING)
    max_size: int | None = Field(default=None, ge=1, le=REHASH_MAX_SIZE_CEILING)


@router.post(
    "/agents/{agent_id}/rehash-sweep",
    response_model=CommandOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def rehash_sweep_agent(
    agent_id: uuid.UUID,
    # Defaulted so a bare POST works like the sibling ``/maintenance`` action —
    # "sweep the defect band" is the overwhelmingly common request and should not
    # require a body. Never mutated, so the shared instance is safe.
    request: Request,
    body: RehashSweepIn = RehashSweepIn(),
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Queue a ``rehash_sweep`` command: the agent re-reads every file in its
    index inside a size band, recomputes both hashes under the post-QH-T1 rules,
    and re-emits through the normal replication path the rows whose stored value
    was wrong (QH-T6).

    **This is not ``rehash_check``.** That kind is item-scoped, verifies ONE
    file, and writes nothing anywhere. This one is agent-scoped, runs for hours,
    and rewrites rows in the agent's local index.

    Why the command exists: until 2026-07-18 both hashers read a fixed 64 KiB
    head and appended the tail only above 131072 bytes, so a file in the
    65537..131072 band had its middle and its tail silently UNhashed — two
    different files whose first 64 KiB coincided produced the same
    ``quick_hash``. QH-T1 fixed the hashers, but a fix to a hasher does not fix
    stored values: the agent's scan re-hashes a file only when its size or mtime
    moved (or its ``quick_hash`` is empty), so a stable file in that band keeps
    its wrong hash forever, because nothing about it will ever change again.

    Central cannot repair those rows on the agent's behalf. It does not host the
    files, and ``agentsync.apply_batch`` never writes ``policy_version`` for
    agent-owned rows, so the QH-T4 ``rehash_small_files`` sweep — which converged
    central's own catalogue — cannot even distinguish a stale agent hash from a
    correct one. The agent is the sole writer for those rows and this is the only
    mechanism that reaches them.

    **Operator-triggered, never automatic.** An agent that upgrades does not
    start re-reading its library on its own: a fleet-wide unprompted I/O storm is
    exactly the thing an operator needs to schedule. The agent instead REPORTS
    its migration state in the health block it attaches to every command poll,
    and the console surfaces it on the per-agent About panel.

    The sweep is RESUMABLE and IDEMPOTENT per (hash scheme, band): the agent
    keeps a cursor across command invocations — a run interrupted by a restart, a
    suspend, or ``max_items`` continues where it stopped — and fingerprints the
    rules it ran under, so a repeat command at an unchanged fingerprint
    short-circuits. It also emits ONLY ON CHANGE: a row whose recomputed hashes
    match its stored ones is counted ``verified``, produces no write and no
    replication event, and costs central nothing. That matters because every
    applied batch defers a Meilisearch sync job.

    409 while a sweep is already queued or running for this agent — the
    ``agent_maintenance``/``reextract`` guard, not the ``suspend`` collapse.
    Collapsing is right for a *desired state*; a sweep is a *job* with per-agent
    cursor state, and two of them would fight over that cursor and double-emit
    the rows they raced on. The band knobs do not merge either.

    Invariant 2 holds throughout: the sweep touches only the identity hash
    fields. It never attaches an ``extracted`` payload, which is precisely what
    keeps a ~99k-row hash correction from cascading into a fleet-wide
    re-extraction (``apply_batch`` merges ``metadata_`` only when ``extracted``
    is present). No file CONTENT leaves the agent.
    """
    await _live_agent(session, agent_id)

    min_size = body.min_size if body.min_size is not None else REHASH_DEFAULT_MIN_SIZE
    max_size = body.max_size if body.max_size is not None else REHASH_DEFAULT_MAX_SIZE
    if min_size > max_size:
        # 422 rather than a swap or a clamp: an inverted band selects zero rows,
        # and the agent would then stamp that fingerprint FINISHED — permanently
        # short-circuiting the real sweep at that band until someone forces it.
        # (The agent refuses it independently too; this is the friendly half.)
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            f"invalid size band: min_size ({min_size}) must be <= max_size ({max_size})",
        )

    in_flight = (
        await session.execute(
            select(AgentCommand.id).where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.kind == "rehash_sweep",
                AgentCommand.status.in_(("pending", "picked_up")),
            )
        )
    ).first()
    if in_flight is not None:
        raise HTTPException(
            status.HTTP_409_CONFLICT, "rehash sweep already queued or running"
        )

    payload: dict[str, Any] = {
        "force": body.force,
        "max_items": body.max_items,
        # Resolved, not passed through as None: the agent defaults a missing knob
        # to the same numbers, but the FINGERPRINT is built from whatever it ends
        # up using, so sending the explicit band keeps the command row a faithful
        # record of what was actually asked for.
        "min_size": min_size,
        "max_size": max_size,
    }
    cmd = _enqueue_agent_scoped(
        agent_id, "rehash_sweep", payload, request, ttl_seconds=REHASH_TTL_SECONDS
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_ENQUEUED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "kind": "rehash_sweep",
            "force": body.force,
            "max_items": body.max_items,
            "min_size": min_size,
            "max_size": max_size,
        },
    )
    return CommandOut.of(cmd)


@router.get(
    "/agent-commands",
    response_model=list[CommandOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def list_commands(
    session: AsyncSession = Depends(get_session),
    agent_id: uuid.UUID | None = None,
    state: str | None = None,
    kind: str | None = None,
    before: uuid.UUID | None = None,
    limit: int = 50,
) -> list[CommandOut]:
    """List commands newest-first with keyset pagination (``before`` = the last id
    of the previous page; ``id`` is uuidv7 → time-ordered). Filter by ``agent_id``
    / ``state`` / ``kind``."""
    limit = max(1, min(limit, 200))
    q = select(AgentCommand).order_by(AgentCommand.id.desc()).limit(limit)
    if agent_id is not None:
        q = q.where(AgentCommand.agent_id == agent_id)
    if state is not None:
        q = q.where(AgentCommand.status == state)
    if kind is not None:
        q = q.where(AgentCommand.kind == kind)
    if before is not None:
        q = q.where(AgentCommand.id < before)
    rows = (await session.execute(q)).scalars().all()
    return [CommandOut.of(c) for c in rows]


@router.get(
    "/agent-commands/{command_id}",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def get_command(
    command_id: uuid.UUID, session: AsyncSession = Depends(get_session)
) -> CommandOut:
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    return CommandOut.of(cmd)


@router.post(
    "/agent-commands/{command_id}/cancel",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def cancel_command(
    command_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Cancel a PRE-TERMINAL command (409 if already terminal). Audited."""
    cmd = await session.get(AgentCommand, command_id)
    if cmd is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such command")
    if agentsync.command_is_terminal(cmd.status):
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command already {cmd.status}"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(cmd.status, "cancel")
    cmd.completed_at = now
    cmd.updated_at = now
    await session.commit()
    await audit.emit(
        audit.AGENT_COMMAND_CANCELLED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"command_id": str(command_id), "agent_id": str(cmd.agent_id)},
    )
    return CommandOut.of(cmd)


# --------------------------------------------------------------------------- #
# Agent plane — poll / ack / complete (interim bearer; mTLS in P5-T6)          #
# --------------------------------------------------------------------------- #
@router.post(
    "/agents/{agent_id}/commands/poll",
    response_model=list[CommandOut],
    dependencies=[Depends(require_agents_enabled)],
)
async def poll_commands(
    agent_id: uuid.UUID,
    body: PollIn,
    request: Request,
    response: Response,
    session: AsyncSession = Depends(get_session),
) -> list[CommandOut]:
    """Drain up to ``max`` pending, not-yet-expired commands FIFO, delivering each
    (``pending`` → ``picked_up``; ``attempts`` incremented, lease clock started).
    ``FOR UPDATE SKIP LOCKED`` so concurrent polls/sweeps never block. A poll also
    refreshes ``agents.last_seen_at`` (the agent is demonstrably alive). Plain
    poll — no long-poll hold-open yet (P5-T4).

    Maintenance advertisement: the response body is a bare command array (frozen
    wire shape), so global maintenance mode rides the ``X-Filearr-Maintenance``
    response header instead — ``1`` while active, absent otherwise. Agents that
    understand it pause their replication push (and keep scanning locally);
    older builds ignore the header and are throttled by the replication
    endpoint's 503 instead."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    if await maintmode.is_active(session):
        response.headers["X-Filearr-Maintenance"] = "1"
    want = min(body.max, settings.agent_command_poll_max)
    now = datetime.now(UTC)
    # W6-D3: persist the agent's advertised capabilities (additive; a poll without
    # them leaves the stored value untouched). Size-capped so a hostile/buggy body
    # cannot bloat the row; an oversize advertisement is dropped (never a poll
    # failure — the command drain must not depend on the advertisement).
    # ``capabilities_at`` stamps the poll that actually STORED it, which is why
    # it is set here and not beside health: the two caps are applied
    # independently, so a dropped capabilities body must not advance this clock.
    if _accept_sized(agent_id, "capabilities", body.capabilities, settings):
        agent.capabilities = body.capabilities
        agent.capabilities_at = now
    # Self-reported health rides the same poll under the same size cap; the
    # arrival stamp lets the console show "as of Xm ago" honestly.
    if _accept_sized(agent_id, "health", body.health, settings):
        agent.health = body.health
        agent.health_at = now
    # Version confirmation for updater-disabled agents (container image): the
    # same stamp the update-manifest poll performs, on the channel every
    # agent build actually uses.
    if body.version:
        agent.agent_version = body.version
    rows = (
        await session.execute(
            select(AgentCommand)
            .where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.status == "pending",
                AgentCommand.expires_at > now,
            )
            .order_by(AgentCommand.created_at.asc())
            .limit(want)
            .with_for_update(skip_locked=True)
        )
    ).scalars().all()
    for cmd in rows:
        cmd.status = agentsync.command_state_machine(cmd.status, "deliver")
        cmd.picked_up_at = now
        cmd.attempts += 1
        cmd.updated_at = now
    agent.last_seen_at = now
    await session.commit()
    return [CommandOut.of(c) for c in rows]


@router.post(
    "/agents/{agent_id}/commands/{command_id}/ack",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def ack_command(
    agent_id: uuid.UUID,
    command_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Lease heartbeat for an in-flight (``picked_up``) command: refresh the lease
    clock so a genuinely-working slow command is not reclaimed by the redelivery
    sweep. 409 if the command is not in-flight (already terminal / not delivered)."""
    agent = await _authenticate_agent(session, agent_id, request)
    cmd = await _owned_command(session, agent, command_id)
    if cmd.status != "picked_up":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command not in-flight (is {cmd.status})"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(cmd.status, "ack")
    cmd.picked_up_at = now  # refresh lease
    cmd.updated_at = now
    await session.commit()
    return CommandOut.of(cmd)


@router.post(
    "/agents/{agent_id}/commands/{command_id}/complete",
    response_model=CommandOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def complete_command(
    agent_id: uuid.UUID,
    command_id: uuid.UUID,
    body: CompleteIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> CommandOut:
    """Report a terminal result for a picked-up command (``done`` / ``failed``).

    Idempotent replay: re-completing an already-``done`` row is a no-op that
    returns the stored result (mirrors replication's at-least-once posture). A
    different terminal state (``failed`` / ``expired`` / ``cancelled``) or a
    never-delivered (``pending``) command is a 409. Result size is capped."""
    agent = await _authenticate_agent(session, agent_id, request)
    cmd = await _owned_command(session, agent, command_id)
    if cmd.status == "done":
        return CommandOut.of(cmd)  # idempotent success replay
    if cmd.status != "picked_up":
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"command not in-flight (is {cmd.status})"
        )
    if body.result is not None and (
        _json_len(body.result) > get_settings().agent_command_result_max_bytes
    ):
        raise HTTPException(
            413, "result too large"
        )
    now = datetime.now(UTC)
    cmd.status = agentsync.command_state_machine(
        cmd.status, "complete" if body.ok else "fail"
    )
    cmd.result = body.result
    # W7-T6: an inventory completion carrying inline entries -> permission
    # snapshots (only entries with a `permissions` record; fail-soft).
    if body.ok and cmd.kind == "inventory" and isinstance(body.result, dict):
        inline = body.result.get("entries")
        if isinstance(inline, list) and inline:
            try:
                from filearr import permission_ingest

                await permission_ingest.ingest_entries(
                    session, agent_id=agent.id, command_id=cmd.id, entries=inline
                )
            except Exception:  # noqa: BLE001 - never fail the completion
                log.exception("permission ingest (inline) failed for command %s", cmd.id)
    cmd.completed_at = now
    cmd.updated_at = now
    # P10-T3: reconcile a successful stat_check/rehash_check against the item IN
    # THE SAME transaction as the terminal status (the item mutation + command
    # completion are atomic). The follow-up alert + index_sync run AFTER commit
    # (invariant 5). A failed/unparseable/non-verify completion reconciles nothing.
    outcome = None
    if body.ok and cmd.kind in verify.VERIFY_KINDS and body.result is not None:
        outcome = await verify.reconcile_completion(session, cmd, body.result, now=now)
    await session.commit()
    if outcome is not None:
        await verify.finalize_completion(session, agent, outcome, now=now)
    # P10-T9 (R2): a completed rehash_check reads the file's full CONTENT on the
    # agent — a data-access event audited UNCONDITIONALLY (regardless of
    # FILEARR_AUDIT_READS), mirroring the transfer-download carve-out. Fired on the
    # terminal completion (done OR failed) so the read attempt is always recorded;
    # an idempotent replay of an already-``done`` command returned above and never
    # re-audits. A stat_check is a metadata-only existence probe — not audited.
    # The actor is the agent (no principal), so agent_id is recorded in details.
    if cmd.kind == "rehash_check":
        await audit.emit(
            audit.AGENT_VERIFY_COMPLETED,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "command_id": str(command_id),
                "agent_id": str(agent_id),
                "item_id": str(cmd.item_id),
                "kind": cmd.kind,
                "ok": body.ok,
                "mismatch": outcome.mismatch if outcome is not None else None,
                "differed": outcome.differed if outcome is not None else [],
            },
        )
    return CommandOut.of(cmd)


# --------------------------------------------------------------------------- #
# Agent plane — replication batch apply (P5-T4; interim bearer, mTLS in P5-T6)  #
# --------------------------------------------------------------------------- #
class ReplicationResult(BaseModel):
    """The apply outcome returned on a 200 (P5-T4). ``last_seq`` is the agent's
    new contiguous watermark (``agents.last_contiguous_seq_no``); ``noop_tombstones``
    is the R2 reconciliation metric (tombstones against already-purged rows)."""

    applied: int
    upserted: int
    tombstoned: int
    noop_tombstones: int
    libraries_created: int
    last_seq: int


@router.post(
    "/agents/{agent_id}/replication-batch",
    response_model=ReplicationResult,
    dependencies=[Depends(require_agents_enabled)],
)
async def apply_replication_batch(
    agent_id: uuid.UUID,
    body: agentsync.ReplicationBatch,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Apply one agent replication batch to central items + the replication ledger
    (P5-T4). Behind ``FILEARR_AGENTS_ENABLED`` (404 off), interim agent bearer
    auth (the bound ``cert_fingerprint``; mTLS replaces it in P5-T6).

    Flow: authenticate the agent → require the body ``agent_id`` to match the path
    (403 on mismatch) → cap on entries (413) → ``check_batch`` against
    ``agents.last_contiguous_seq_no``:

      * NOT a contiguous continuation → **409** ``{"reason", "expected_seq_no"}``
        (the frozen resend-from contract; the agent rewinds its outbox drain).
      * a clean continuation → :func:`agentsync.apply_batch` (one transaction:
        upserts, then tombstones, then the ledger + seq-watermark advance) → 200.

    The Meili projection for the touched item ids is deferred AFTER the commit
    (invariant 5). A poll-style ``last_seen_at`` refresh happens on both the 409
    and the applied path (the agent is demonstrably alive)."""
    agent = await _authenticate_agent(session, agent_id, request)
    # Never apply one agent's outbox under another's identity.
    if str(body.agent_id) != str(agent_id):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "batch agent_id mismatch")
    # Global maintenance mode: refuse the batch so the agent's outbox drain
    # backs off (its existing flush-failure exponential backoff handles the
    # cadence; new builds also pause proactively via the poll header). Nothing
    # is lost — the outbox is durable and resends from the same seq_no.
    if await maintmode.is_active(session):
        agent.last_seen_at = datetime.now(UTC)
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"reason": "maintenance"},
            headers={"Retry-After": str(maintmode.RETRY_AFTER_SECONDS)},
        )
    settings = get_settings()
    if len(body.entries) > settings.agent_replication_max_entries:
        raise HTTPException(413, "replication batch too large")
    verdict = agentsync.check_batch(body, agent.last_contiguous_seq_no)
    if not verdict.ok:
        # Resend-request: refresh liveness, hand back the seq to rewind to.
        agent.last_seen_at = datetime.now(UTC)
        await session.commit()
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={
                "reason": verdict.reason,
                "expected_seq_no": verdict.expected_seq_no,
            },
        )
    # apply_batch commits (items + ledger + last_contiguous_seq_no + last_seen).
    result = await agentsync.apply_batch(session, agent, body)
    item_ids = result.pop("item_ids", [])
    library_ids = result.pop("library_ids", [])
    if item_ids:
        await defer_index_sync(item_ids)  # invariant 5: AFTER commit
        # T3 parity: replication never sets sidecar_of — queue the debounced
        # per-library association pass (collapses across a scan's batch stream).
        await defer_agent_associate(library_ids)
    return ReplicationResult(**result)


# --------------------------------------------------------------------------- #
# Agent plane — full-manifest reconciliation sweep (P5-T5; interim bearer)      #
# --------------------------------------------------------------------------- #
class ReconcileStartIn(BaseModel):
    library_ref: str
    digest: str
    row_count: int = Field(ge=0)
    rebuilt: bool = False


class ReconcileStartOut(BaseModel):
    status: str  # "match" | "mismatch"
    session_id: str | None = None


class ReconcileRow(BaseModel):
    rel_path: str
    size: int
    mtime: float
    quick_hash: str | None = None
    content_hash: str | None = None


class ReconcileRowsIn(BaseModel):
    rows: list[ReconcileRow] = Field(default_factory=list)


class ReconcileRowsOut(BaseModel):
    staged: int


class ReconcileFinishIn(BaseModel):
    digest: str
    row_count: int = Field(ge=0)
    reset_seq: bool = False


class ReconcileResult(BaseModel):
    """The anti-join outcome on a 200 finish (P5-T5, ruling 3)."""

    status: str
    upserted: int
    tombstoned: int
    reactivated: int
    updated: int
    trashed_conflicts: int
    unchanged: int


_RECONCILE_COUNTERS = (
    "upserted",
    "tombstoned",
    "reactivated",
    "updated",
    "trashed_conflicts",
    "unchanged",
)


@router.post(
    "/agents/{agent_id}/reconcile/start",
    response_model=ReconcileStartOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_start_endpoint(
    agent_id: uuid.UUID,
    body: ReconcileStartIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReconcileStartOut:
    """Phase 1 of the full-manifest sweep (P5-T5). Compare the agent's whole-
    library digest to central's projection. Equal → ``match`` (watermark stamped;
    ``rebuilt`` resets the seq watermark). Otherwise open ONE live session for the
    agent (superseding any prior unfinished one) and return its ``session_id``.
    Interim agent bearer auth; behind ``FILEARR_AGENTS_ENABLED``."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    result = await agentsync.reconcile_start(
        session,
        agent,
        library_ref=body.library_ref,
        digest=body.digest,
        row_count=body.row_count,
        rebuilt=body.rebuilt,
        now=datetime.now(UTC),
        ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
    )
    if result["status"] == "match":
        await audit.emit(
            audit.AGENT_RECONCILED,
            request=request,
            principal_id=audit.actor_id(request),
            details={"agent_id": str(agent_id), "status": "match"},
        )
    return ReconcileStartOut(**result)


@router.post(
    "/agents/{agent_id}/reconcile/{session_id}/rows",
    response_model=ReconcileRowsOut,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_rows_endpoint(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ReconcileRowsIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReconcileRowsOut:
    """Phase 2: page the agent's full manifest into staging (413 above
    ``FILEARR_AGENT_RECONCILE_PAGE_MAX``; 404 for an unknown/expired session). A
    re-sent page upserts (idempotent). Returns the running staged-row count."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    if len(body.rows) > settings.agent_reconcile_page_max:
        raise HTTPException(413, "reconcile page too large")
    try:
        staged = await agentsync.reconcile_stage_rows(
            session,
            agent,
            session_id=session_id,
            rows=body.rows,
            now=datetime.now(UTC),
            ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
        )
    except agentsync.ReconcileError as err:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    return ReconcileRowsOut(staged=staged)


@router.post(
    "/agents/{agent_id}/reconcile/{session_id}/finish",
    response_model=ReconcileResult,
    dependencies=[Depends(require_agents_enabled)],
)
async def reconcile_finish_endpoint(
    agent_id: uuid.UUID,
    session_id: uuid.UUID,
    body: ReconcileFinishIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
):
    """Phase 3: verify the staged manifest, run the anti-join in ONE transaction,
    stamp the watermark, drop the session. A staged digest/count that disagrees
    with the body → 409 ``{"reason":"digest_mismatch"}`` and the session is
    destroyed (the agent re-sweeps). 404 for an unknown/expired session. The Meili
    projection for touched ids is deferred AFTER the commit (invariant 5)."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()
    try:
        result = await agentsync.reconcile_finish(
            session,
            agent,
            session_id=session_id,
            digest=body.digest,
            row_count=body.row_count,
            reset_seq=body.reset_seq,
            now=datetime.now(UTC),
            ttl_seconds=settings.agent_reconcile_session_ttl_seconds,
        )
    except agentsync.ReconcileError as err:
        if err.reason == "digest_mismatch":
            return JSONResponse(
                status_code=status.HTTP_409_CONFLICT,
                content={"reason": "digest_mismatch"},
            )
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(err)) from err
    item_ids = result.pop("item_ids", [])
    library_ids = result.pop("library_ids", [])
    if item_ids:
        await defer_index_sync(item_ids)  # invariant 5: AFTER commit
        await defer_agent_associate(library_ids)
    await audit.emit(
        audit.AGENT_RECONCILED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "agent_id": str(agent_id),
            **{k: result[k] for k in _RECONCILE_COUNTERS},
        },
    )
    return ReconcileResult(**result)


def _actor_uuid(request: Request) -> uuid.UUID | None:
    aid = audit.actor_id(request)
    return uuid.UUID(aid) if aid else None
