"""Agent-plane configuration delivery (P5-T6 poll surface; P13 resolution).

Two agent-authenticated GETs, both behind the ``FILEARR_AGENTS_ENABLED`` gate
(404 when off) and both using the interim agent bearer / mTLS-header seam
(:func:`agent_commands._authenticate_agent`):

* ``GET /agents/{agent_id}/policy`` — the agent's effective configuration,
  resolved by :func:`filearr.agent_config.resolve_effective_config` (Global plus
  the agent's member groups, ascending priority, per-key merge). Served with a
  strong ETag supporting ``If-None-Match`` → 304, and ``?applied=<generation>``
  stamps what the agent is actually running.
* ``GET /agents/{agent_id}/taxonomy`` — the compact, version-gated File
  Extension Similarity Taxonomy the agent resolves against locally (W8-E).

**The wire shape is FROZEN.** The response body is still
``{"scope", "version", "policy"}`` with merged policy keys at the top level of
``policy``, merged settings under ``policy.group``, the three lifted
local-surface keys and the injected ``taxonomy_version`` — exactly what
``agent/internal/config.Policy`` parses today. P13 changed only what those
values MEAN: ``scope`` is the literal ``"groups"`` (there is one resolution
scheme now, so the field carries no information and exists to keep old binaries
parsing) and ``version`` is the config GENERATION
(``max(agent_config_group_versions.seq)`` over the contributing snapshots)
rather than a per-scope policy version. The Go client keys its re-apply decision
on the ETag as well as the version, so the numbering change is safe across the
upgrade: the ETag changes shape, every agent sees a 200, and re-applies once.

The ADMIN plane that used to live here (``PUT/GET /agent-policies/*``, scope
grammar, per-scope history, the effective-policy view) is gone. Its replacement
is the config-group CRUD + history + rollback + effective-config surface in
:mod:`filearr.api.agent_config_groups`.

mTLS is the only integrity layer for the config channel (R4 — no payload signing
in v3; revisit trigger is phase-6 multi-author RBAC policy).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, Query, Request, Response, status
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, taxonomy
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.db import get_session

router = APIRouter()

#: The literal ``scope`` value every agent now receives. Kept in the payload
#: because the Go ``PolicyDoc`` persists and displays it; there is exactly one
#: resolution scheme, so it is a constant rather than a computed answer.
CONFIG_SCOPE = "groups"


def _etag_matches(if_none_match: str, etag: str) -> bool:
    """True if the ``If-None-Match`` header matches ``etag`` (comma list or ``*``)."""
    tokens = [t.strip() for t in if_none_match.split(",")]
    return "*" in tokens or etag in tokens


# --------------------------------------------------------------------------- #
# Agent plane — GET /agents/{id}/policy (bearer; ETag/If-None-Match)            #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/{agent_id}/policy",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_agent_policy(
    agent_id: uuid.UUID,
    request: Request,
    applied: int | None = Query(default=None),
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve ``agent``'s effective configuration, merged across its groups.

    ``200 {"scope":"groups","version":<generation>,"policy":{…}}`` with
    ``ETag: "groups/<generation>/h:<hash>/t:<taxonomy_version>"``; a matching
    ``If-None-Match`` → ``304`` (empty body, ETag still present).

    **Resolution** (:func:`filearr.agent_config.resolve_effective_config`): the
    permanent Global group plus every group this agent is a member of, applied in
    ascending ``priority`` — a later group overrides only the KEYS it sets, so an
    agent in four groups gets one document assembled key by key rather than
    whichever single document happened to be most specific. A group with a
    running phased rollout hands this agent the rollout's ``target_version``
    instead of ``current_version`` when its stable bucket falls inside the active
    tier.

    An agent never 404s here: with no groups at all it still resolves Global, and
    with no Global row (only reachable mid-migration) it gets an empty document
    rather than an error.

    Three independent things invalidate the cache and all three ride the ETag:
    any contributing group publishing a version (the generation), the merged
    content changing (the hash — which also catches a membership or priority
    edit that moves no version number), and a taxonomy edit.

    ``?applied=`` reports the generation the agent has fully applied; it stamps
    ``agents.config_generation_applied`` + ``last_seen_at`` (the agent is
    demonstrably alive, like the poll/replication endpoints)."""
    agent = await _authenticate_agent(session, agent_id, request)
    now = datetime.now(UTC)
    agent.last_seen_at = now
    if applied is not None:
        agent.config_generation_applied = applied
    await session.commit()

    effective = await agent_config.resolve_effective_config(session, agent)

    # W8-E: surface the central File Extension Similarity Taxonomy version so a
    # taxonomy edit invalidates the agent's policy cache. The version rides the
    # policy body (an additive, computed key the agent reads to version-gate its
    # compact-taxonomy fetch) AND the ETag (a bump forces a 200, not a 304, even
    # when the configuration itself is unchanged). Central always sets the
    # authoritative value — an operator-authored ``taxonomy_version`` key does
    # not win, and it is deliberately EXCLUDED from the document hash so the
    # content fingerprint stays a fingerprint of operator intent.
    tax_version = await taxonomy.current_version(session)
    doc = {**effective.document, "taxonomy_version": tax_version}
    etag = agent_config.config_etag(effective.generation, effective.hash, tax_version)

    inm = request.headers.get("if-none-match")
    if inm and _etag_matches(inm, etag):
        return Response(status_code=status.HTTP_304_NOT_MODIFIED, headers={"ETag": etag})
    return JSONResponse(
        {"scope": CONFIG_SCOPE, "version": effective.generation, "policy": doc},
        headers={"ETag": etag},
    )


# --------------------------------------------------------------------------- #
# Agent plane — GET /agents/{id}/taxonomy (bearer; compact resolution payload)  #
# --------------------------------------------------------------------------- #
@router.get(
    "/agents/{agent_id}/taxonomy",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_agent_taxonomy(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Serve the COMPACT File Extension Similarity Taxonomy an agent resolves
    against locally (W8-E) — NOT the admin ``GET /taxonomy`` tree.

    The agent fetches this VERSION-GATED: after a policy poll surfaces a newer
    ``taxonomy_version`` than the agent's cached snapshot, it pulls this payload
    once and persists it. Because it is version-gated, shipping the full
    ~1350-entry ``ext_to_group`` map each fetch is fine. The payload is exactly
    :meth:`filearr.taxonomy.Taxonomy.agent_payload` — flat lookup maps plus the
    ``primary_categories`` sidecar-parent set (the categories with an extractor).

    Agent-plane auth (the same interim cert-fingerprint bearer / mTLS-header seam
    as the policy + replication endpoints); 404 when the agents feature is off."""
    await _authenticate_agent(session, agent_id, request)
    payload = await taxonomy.agent_payload(session)
    return JSONResponse(payload)
