"""P5-T7 — signed agent update manifest distribution (central-side).

The central half of the agent self-updater. Central STORES and SERVES signed
release manifests + artifact binaries but is UNTRUSTED for update integrity
(research §8): it cannot re-sign a manifest (the Ed25519 private key never
reaches central), so the agent verifies the signature against its build-time
pinned public key. A compromised central therefore cannot push a wrongly-signed
binary — the worst it can do is withhold or corrupt a download, which the
agent's sha256 check catches.

Two planes, both behind ``FILEARR_AGENTS_ENABLED`` (404 when off):

* **Operator/admin plane** (``admin`` scope): upload a release (the signed
  manifest, then each artifact binary) and list releases with the per-agent
  confirmed-version rollup ("which version has each agent confirmed").

  HISTORY (P13, 2026-08-11): uploads used to land as ``stage='canary'``, visible
  only to agents in the configured canary rollout group, and needed an explicit
  promote to reach the fleet. That staging died with ``rollout_group``. Every
  release is now fleet-visible on upload; the brake is the per-group
  ``auto_update`` policy key (enforced server-side on the manifest poll) and the
  targeting tool is a per-agent ``self_update`` command. Configuration gets the
  phased treatment instead — see ``agent_config_rollouts``; attaching binary
  releases to that same tier engine is on the roadmap.
* **Agent plane** (``_authenticate_agent`` reused from ``api.agent_commands`` —
  interim bearer / mTLS-header per ``FILEARR_AGENT_AUTH_MODE``): fetch the newest
  covering manifest for THIS agent, and download an artifact by filename (served
  ONLY when listed in the stored manifest — no path traversal).

Upload is TWO-PHASE (no multipart dependency, friendlier to large binaries):
``POST /agent-releases`` registers the signed manifest; ``PUT
/agent-releases/{version}/artifacts/{filename}`` streams each binary (verified
against the manifest sha256/size). A release is only OFFERED / PROMOTABLE once
every manifest artifact is present on disk.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, Response, status
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import agent_config, audit, update_gate
from filearr.api import agent_dist
from filearr.api.agent_commands import _authenticate_agent
from filearr.api.agents import require_agents_enabled
from filearr.config import Settings, get_settings
from filearr.db import get_session
from filearr.models import Agent, AgentCommand, AgentRelease, AgentReleaseRollout
from filearr.security import require_scope

router = APIRouter()

# A version/filename that will become a filesystem path component must be a plain
# token — no separators, no traversal. Both are operator/manifest-controlled, but
# validated defensively regardless (the download endpoint's primary defence is
# the "must be in the manifest" check; this is belt-and-braces).
_SAFE_VERSION = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,127}$")
_SAFE_FILENAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._+-]{0,255}$")


# --------------------------------------------------------------------------- #
# Release storage helpers                                                      #
# --------------------------------------------------------------------------- #
def _releases_root(settings: Settings) -> Path:
    base = settings.agent_releases_dir or f"{settings.config_dir}/agent-releases"
    return Path(base)


def _release_dir(settings: Settings, version: str) -> Path:
    return _releases_root(settings) / version


def _artifact_path(settings: Settings, version: str, filename: str) -> Path:
    """Resolve a release artifact path, refusing any traversal. ``version`` and
    ``filename`` MUST already have passed the safe-token check."""
    root = _release_dir(settings, version).resolve()
    target = (root / filename).resolve()
    if root != target.parent:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid artifact path")
    return target


def _manifest_artifacts(manifest: dict[str, Any]) -> list[dict[str, Any]]:
    arts = manifest.get("artifacts")
    return arts if isinstance(arts, list) else []


def _release_ready(settings: Settings, rel: AgentRelease) -> bool:
    """True when every artifact named in the stored manifest exists on disk — the
    precondition for OFFERING the release to an agent or PROMOTING it."""
    arts = _manifest_artifacts(rel.manifest)
    if not arts:
        return False
    for a in arts:
        name = a.get("url")
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name):
            return False
        if not _artifact_path(settings, rel.version, name).exists():
            return False
    return True


def _version_newer(candidate: str, current: str) -> bool:
    """Semver-ish "is candidate strictly newer than current". Mirrors the Go
    ``update.CompareVersions`` rule byte-for-byte (documented there): strip a
    leading v, drop build metadata after '+', split release/prerelease on the
    first '-', compare release components numerically (missing == 0, non-numeric
    falls back to string), and rank a prerelease BELOW the equivalent release."""
    return _compare_versions(candidate, current) > 0


def _compare_versions(a: str, b: str) -> int:
    ar, ap = _split_version(a)
    br, bp = _split_version(b)
    c = _compare_release(ar, br)
    if c != 0:
        return c
    if ap == "" and bp == "":
        return 0
    if ap == "":
        return 1
    if bp == "":
        return -1
    return (ap > bp) - (ap < bp)


def _split_version(v: str) -> tuple[str, str]:
    v = (v or "").strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    v = v.split("+", 1)[0]
    if "-" in v:
        rel, pre = v.split("-", 1)
        return rel, pre
    return v, ""


def _compare_release(a: str, b: str) -> int:
    ap = a.split(".")
    bp = b.split(".")
    for i in range(max(len(ap), len(bp))):
        as_ = ap[i] if i < len(ap) else "0"
        bs_ = bp[i] if i < len(bp) else "0"
        if as_.isdigit() and bs_.isdigit():
            an, bn = int(as_), int(bs_)
            if an != bn:
                return (an > bn) - (an < bn)
        elif as_ != bs_:
            return (as_ > bs_) - (as_ < bs_)
    return 0


# --------------------------------------------------------------------------- #
# Schemas                                                                      #
# --------------------------------------------------------------------------- #
class ReleaseOut(BaseModel):
    id: uuid.UUID
    version: str
    created_at: datetime
    artifacts: list[dict[str, Any]]
    ready: bool
    confirmed_count: int

    @classmethod
    def of(cls, rel: AgentRelease, ready: bool, confirmed: int) -> ReleaseOut:
        return cls(
            id=rel.id,
            version=rel.version,
            created_at=rel.created_at,
            artifacts=_manifest_artifacts(rel.manifest),
            ready=ready,
            confirmed_count=confirmed,
        )


class AgentVersionOut(BaseModel):
    id: uuid.UUID
    name: str
    hostname: str
    agent_version: str | None
    last_seen_at: datetime | None


class ReleaseListOut(BaseModel):
    releases: list[ReleaseOut]
    agents: list[AgentVersionOut]


# --------------------------------------------------------------------------- #
# Operator/admin plane                                                         #
# --------------------------------------------------------------------------- #
@router.post(
    "/agent-releases",
    response_model=ReleaseOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def register_release(
    manifest: dict[str, Any],
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseOut:
    """Register a SIGNED release manifest (phase 1 of upload). The manifest is
    stored VERBATIM (including its ``signature``); central never validates the
    signature (it holds no key). Artifacts are uploaded next via PUT, and the
    release is only OFFERED once every artifact it names is on disk. A duplicate
    version is a 409 (releases are immutable — re-cut a new version rather than
    mutating one)."""
    version = manifest.get("version")
    if not isinstance(version, str) or not _SAFE_VERSION.match(version):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest version missing or invalid")
    if not manifest.get("signature"):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest is unsigned")
    arts = _manifest_artifacts(manifest)
    if not arts:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "manifest lists no artifacts")
    for a in arts:
        name = a.get("url")
        if not isinstance(name, str) or not _SAFE_FILENAME.match(name):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, f"invalid artifact filename: {name!r}")
        if not isinstance(a.get("sha256"), str):
            raise HTTPException(status.HTTP_400_BAD_REQUEST, "artifact missing sha256")

    existing = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if existing is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, f"release {version} already exists")

    settings = get_settings()
    _release_dir(settings, version).mkdir(parents=True, exist_ok=True)

    rel = AgentRelease(version=version, manifest=manifest)
    session.add(rel)
    await session.commit()
    await audit.emit(
        audit.AGENT_RELEASE_UPLOADED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"version": version, "artifacts": len(arts)},
    )
    return ReleaseOut.of(rel, _release_ready(settings, rel), 0)


@router.put(
    "/agent-releases/{version}/artifacts/{filename}",
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def upload_artifact(
    version: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> dict[str, Any]:
    """Stream one release artifact binary (phase 2). The raw request body is the
    file. The filename MUST be listed in the release's manifest; the streamed
    bytes are verified against the manifest's sha256 + size (mismatch → 400 and
    the partial file is removed). Idempotent (re-upload overwrites)."""
    if not _SAFE_VERSION.match(version) or not _SAFE_FILENAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid version or filename")
    rel = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if rel is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    art = next(
        (a for a in _manifest_artifacts(rel.manifest) if a.get("url") == filename), None
    )
    if art is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "filename not in manifest")

    settings = get_settings()
    dest = _artifact_path(settings, version, filename)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    max_bytes = settings.agent_update_max_artifact_bytes
    h = hashlib.sha256()
    size = 0
    try:
        with tmp.open("wb") as fh:
            async for chunk in request.stream():
                size += len(chunk)
                if size > max_bytes:
                    raise HTTPException(
                        status.HTTP_413_REQUEST_ENTITY_TOO_LARGE, "artifact too large"
                    )
                h.update(chunk)
                fh.write(chunk)
    except HTTPException:
        tmp.unlink(missing_ok=True)
        raise
    got = h.hexdigest()
    want = str(art.get("sha256", "")).lower()
    declared = art.get("size")
    if got != want or (isinstance(declared, int) and declared != size):
        tmp.unlink(missing_ok=True)
        raise HTTPException(
            status.HTTP_400_BAD_REQUEST,
            f"artifact does not match manifest (sha256 got {got}, want {want}; size {size})",
        )
    tmp.replace(dest)
    return {"version": version, "filename": filename, "size": size, "sha256": got}


@router.get(
    "/agent-releases",
    response_model=ReleaseListOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("read"))],
)
async def list_releases(
    session: AsyncSession = Depends(get_session),
) -> ReleaseListOut:
    """List releases (newest first) with each release's confirmed-agent count, and
    the per-agent running-version rollup (§6.3: "which version has each agent
    confirmed"). ``agent_version`` is what each agent last reported running via
    its manifest poll — the confirmed-version signal."""
    settings = get_settings()
    releases = (
        await session.execute(select(AgentRelease).order_by(AgentRelease.created_at.desc()))
    ).scalars().all()
    agents = (
        await session.execute(select(Agent).where(Agent.revoked_at.is_(None)))
    ).scalars().all()

    counts: dict[str, int] = {}
    for ag in agents:
        if ag.agent_version:
            counts[ag.agent_version] = counts.get(ag.agent_version, 0) + 1

    return ReleaseListOut(
        releases=[
            ReleaseOut.of(r, _release_ready(settings, r), counts.get(r.version, 0))
            for r in releases
        ],
        agents=[
            AgentVersionOut(
                id=ag.id,
                name=ag.name,
                hostname=ag.hostname,
                agent_version=ag.agent_version,
                last_seen_at=ag.last_seen_at,
            )
            for ag in agents
        ],
    )


async def _confirmed_count(session: AsyncSession, version: str) -> int:
    agents = (
        await session.execute(
            select(Agent).where(Agent.revoked_at.is_(None), Agent.agent_version == version)
        )
    ).scalars().all()
    return len(agents)


# --------------------------------------------------------------------------- #
# Agent plane                                                                  #
# --------------------------------------------------------------------------- #
# A "clean" release-tag version whose ordering CompareVersions understands
# (v1.2.3 / 1.2.3-rc1). Anything else — branch@sha builds like "main-1a2b3c4" —
# has NO defined ordering: for those, "differs from current" is the only
# meaningful update signal (string equality means "the exact current build").
_CLEAN_VERSION = re.compile(r"^[vV]?\d+(\.\d+)*([-+].*)?$")
#: Pre-release part of a CI build stamp ("1.5.0-a8396e8"): a short git sha, which
#: carries NO ordering. Mirrors the Go ``update.shaStampRe``.
_SHA_STAMP = re.compile(r"^[0-9a-f]{7,40}$")


def _should_offer(candidate: str, current: str) -> bool:
    """Mirror of the Go ``update.ShouldApply``: semver ordering when both sides
    are clean release tags, plain inequality otherwise -- and plain inequality
    too when the release parts are equal and either side is sha-stamped
    (live 2026-08-18: 1.5.0-a8396e8 was 'older' than 1.5.0-fe31b85, so the new
    central build was never offered)."""
    if not current:
        return True
    if _CLEAN_VERSION.match(candidate) and _CLEAN_VERSION.match(current):
        cr, cp = _split_version(candidate)
        rr, rp = _split_version(current)
        if _compare_release(cr, rr) == 0 and (_SHA_STAMP.match(cp) or _SHA_STAMP.match(rp)):
            return candidate != current
        return _version_newer(candidate, current)
    return candidate != current


def _dist_manifest_for(settings: Settings, current: str) -> dict[str, Any] | None:
    """UNSIGNED update manifest derived from the central-baked agent-dist
    binaries (the "published central console version"). Returns None when there
    is no dist bake, the version is not a safe URL token, or it does not differ
    from ``current``. Served through the SAME per-release artifact download path
    (the dist bake acts as a virtual release), so the Go client's
    filename-only URL resolution is untouched."""
    root = agent_dist._dist_root(settings)
    arts = agent_dist._artifacts(root)
    version = agent_dist._version(root)
    if not arts or version == "unknown" or not _SAFE_VERSION.match(version):
        return None
    if not _should_offer(version, current):
        return None
    goos_to_platform = {"windows": "windows", "linux": "linux", "darwin": "macos"}
    entries: list[dict[str, Any]] = []
    for p in arts:
        goos, goarch = agent_dist._platform_of(p.name)
        platform = goos_to_platform.get(goos)
        if platform is None:
            continue
        entries.append(
            {
                "platform": platform,
                "arch": goarch,
                "sha256": agent_dist._sha256(p),
                "size": p.stat().st_size,
                "url": p.name,
            }
        )
    if not entries:
        return None
    try:
        created = datetime.fromtimestamp((root / "VERSION").stat().st_mtime, tz=UTC)
    except OSError:
        created = datetime.now(UTC)
    return {
        "version": version,
        "created_at": created.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "artifacts": entries,
        # No signature: only unpinned agent builds accept this channel (their
        # trust root is the authenticated TLS channel + sha256, same as their
        # original install via the agent-dist install scripts). A key-pinned
        # build refuses unsigned manifests (fail-closed) and signals
        # key_pinned=true on its poll so central does not even offer this.
    }


async def _pending_self_update(session: AsyncSession, agent_id: uuid.UUID) -> bool:
    row = (
        await session.execute(
            select(AgentCommand.id)
            .where(
                AgentCommand.agent_id == agent_id,
                AgentCommand.kind == "self_update",
                AgentCommand.status.in_(("pending", "picked_up")),
            )
            .limit(1)
        )
    ).scalar_one_or_none()
    return row is not None


async def resolve_update_target(
    session: AsyncSession,
    settings: Settings,
    agent: Agent,
    releases: list[AgentRelease] | None = None,
) -> str | None:
    """The version this agent WOULD be offered (newest ready signed release,
    else the differing agent-dist bake), or None when up to date.
    Shared by the console fields (update_available/update_target) and the
    self-update trigger endpoint — one definition of "update available".
    ``releases`` lets a paging caller preload the (small) release list once."""
    current = agent.agent_version or ""
    if releases is None:
        releases = list(
            (
                await session.execute(
                    select(AgentRelease).order_by(AgentRelease.created_at.desc())
                )
            ).scalars()
        )
    for rel in releases:
        if current and not _version_newer(rel.version, current):
            break  # newest release is not newer -> signed channel done
        if _release_ready(settings, rel):
            return rel.version
    dist = _dist_manifest_for(settings, current)
    if dist is not None:
        return str(dist["version"])
    return None


@router.get(
    "/agents/{agent_id}/update-manifest",
    dependencies=[Depends(require_agents_enabled)],
)
async def get_update_manifest(
    agent_id: uuid.UUID,
    request: Request,
    current: str = "",
    key_pinned: bool = False,
    session: AsyncSession = Depends(get_session),
) -> Response:
    """Return the newest covering release strictly newer than the agent's reported
    ``current`` version, as the stored signed manifest (the agent verifies the
    signature) — falling back to an UNSIGNED manifest derived from the
    central-baked agent-dist binaries when no signed release applies but the
    published central version differs (skipped when the agent reports
    ``key_pinned=true``: a pinned build would refuse unsigned bits anyway).
    204 when up to date / nothing covers this agent / updates are gated off.

    Gate (2026-08-05): an update is OFFERED only when the agent's effective
    policy allows ``auto_update`` (absent = true) OR an operator-triggered
    ``self_update`` command is in flight for this agent (the click IS the
    authorization). The gate sits server-side so every agent build honors it.

    Reporting ``current`` here is ALSO the §6.3 confirmed-version signal: the
    agent's running version is recorded on ``agents.agent_version`` (+ a
    ``last_seen_at`` refresh) on every poll — a running, polling agent has by
    definition booted that version. The stamp happens BEFORE the gate: a
    gated-off agent still reports its version."""
    agent = await _authenticate_agent(session, agent_id, request)
    settings = get_settings()

    # Record the confirmed running version + liveness (agent is demonstrably up).
    now = datetime.now(UTC)
    if current:
        agent.agent_version = current[:256]
    agent.last_seen_at = now
    await session.commit()

    # Containerized agents (the `container` capability from the command poll)
    # update by image pull: the console FLAGS the newer build, but central
    # never offers them a manifest — a binary swapped inside a container dies
    # on the next recreate. (The shipped agent image also disables its updater
    # via FILEARR_AGENT_SELF_UPDATE=false; this is the server-side half.)
    if (agent.capabilities or {}).get("container"):
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    effective = await agent_config.resolve_effective_config(session, agent)
    # auto_update (whether) + update_not_before / update_window (when): all
    # three are the same server-side gate, all bypassed by an operator's
    # queued self_update (the click is the authorization).
    clicked = await _pending_self_update(session, agent_id)
    if update_gate.hold_reason(effective.document) and not clicked:
        return Response(status_code=status.HTTP_204_NO_CONTENT)
    # Phased release rollouts (roadmap §23): a version with a LIVE rollout is
    # offered only to agents inside the active tier's bucket range. The click
    # bypasses this too -- the operator targeted THIS agent by hand.
    live = {} if clicked else await live_release_rollouts(session)

    def _covered(version: str) -> bool:
        r = live.get(version)
        return r is None or agent_config.rollout_covers(r, agent.id)

    releases = (
        await session.execute(select(AgentRelease).order_by(AgentRelease.created_at.desc()))
    ).scalars().all()
    for rel in releases:  # newest first
        if current and not _version_newer(rel.version, current):
            # The newest release is not newer than what we run — the
            # signed channel is done (the dist fallback below may still differ).
            break
        if not _release_ready(settings, rel):
            # Manifest registered but artifacts not fully uploaded — do not offer
            # a manifest whose download would 404. Skip to older covering ones.
            continue
        if not _covered(rel.version):
            continue  # phased: this agent's bucket is not in the active tier yet
        return Response(content=_manifest_json(rel.manifest), media_type="application/json")

    if not key_pinned:
        dist = _dist_manifest_for(settings, current)
        if dist is not None and _covered(str(dist["version"])):
            return Response(content=_manifest_json(dist), media_type="application/json")
    return Response(status_code=status.HTTP_204_NO_CONTENT)


async def live_release_rollouts(session: AsyncSession) -> dict[str, AgentReleaseRollout]:
    """``{release_version: rollout}`` for every scheduled/running release
    rollout (one live per version, enforced by the partial unique index)."""
    rows = (
        await session.execute(
            select(AgentReleaseRollout).where(
                AgentReleaseRollout.status.in_(("scheduled", "running"))
            )
        )
    ).scalars()
    return {r.release_version: r for r in rows}


def release_rollout_hold(rollout: AgentReleaseRollout | None, agent_id: uuid.UUID) -> str | None:
    """Why a live rollout is NOT offering ``rollout.release_version`` to this
    agent right now, or None when it is (or there is no rollout)."""
    if rollout is None:
        return None
    if agent_config.rollout_covers(rollout, agent_id):
        return None
    pct = agent_config.rollout_active_percent(rollout)
    if rollout.status == "scheduled":
        when = "the next tick"
        if rollout.starts_at:
            when = rollout.starts_at.isoformat(timespec="minutes")
        return f"phased rollout of {rollout.release_version} is scheduled to start at {when}"
    if pct is None:
        return f"phased rollout of {rollout.release_version} has not activated its first tier yet"
    return (
        f"phased rollout of {rollout.release_version} covers {pct}% of the fleet; "
        f"this agent's bucket ({agent_config.agent_bucket(agent_id)}) is not in it yet"
    )


def _manifest_json(manifest: dict[str, Any]) -> bytes:
    import json

    return json.dumps(manifest, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


@router.get(
    "/agents/{agent_id}/releases/{version}/artifacts/{filename}",
    dependencies=[Depends(require_agents_enabled)],
)
async def download_artifact(
    agent_id: uuid.UUID,
    version: str,
    filename: str,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> FileResponse:
    """Stream a release artifact to an authenticated agent. The filename MUST be
    listed in that release's stored manifest (no path traversal — the filename is
    validated against the manifest, then resolved and confirmed to sit directly
    in the release dir)."""
    agent = await _authenticate_agent(session, agent_id, request)
    _ = agent  # authenticated; any enrolled agent may download any covering release
    if not _SAFE_VERSION.match(version) or not _SAFE_FILENAME.match(filename):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid version or filename")
    rel = (
        await session.execute(select(AgentRelease).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    settings = get_settings()
    if rel is None:
        # The agent-dist bake is a VIRTUAL release (unsigned dist-fallback
        # manifests reference it by its baked version): serve its files through
        # this same authenticated path when the version matches.
        root = agent_dist._dist_root(settings)
        if agent_dist._version(root) == version:
            target = (root.resolve() / filename)
            if (
                any(p.name == filename for p in agent_dist._artifacts(root))
                and target.parent == root.resolve()
                and target.is_file()
            ):
                return FileResponse(
                    str(target), media_type="application/octet-stream", filename=filename
                )
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release")
    if not any(a.get("url") == filename for a in _manifest_artifacts(rel.manifest)):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "filename not in manifest")
    path = _artifact_path(settings, version, filename)
    if not path.exists():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "artifact not uploaded")
    return FileResponse(
        str(path), media_type="application/octet-stream", filename=filename
    )


# --------------------------------------------------------------------------- #
# Operator plane — per-agent update trigger (console button, 2026-08-05)       #
# --------------------------------------------------------------------------- #
class SelfUpdateOut(BaseModel):
    command_id: uuid.UUID
    agent_id: uuid.UUID
    target: str
    expires_at: datetime


@router.post(
    "/agents/{agent_id}/self-update",
    response_model=SelfUpdateOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("write"))],
)
async def trigger_self_update(
    agent_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> SelfUpdateOut:
    """Queue a ``self_update`` command for one agent — applied at its next
    command check-in (default 60s poll), regardless of its ``auto_update``
    policy (the operator's click IS the authorization; the update-manifest
    gate honors an in-flight command). 409 when no update is available or one
    is already queued; the button in the console mirrors both conditions."""
    settings = get_settings()
    agent = await session.get(Agent, agent_id)
    if agent is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such agent")
    if agent.revoked_at is not None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent revoked")
    if (agent.capabilities or {}).get("container"):
        raise HTTPException(
            status.HTTP_409_CONFLICT,
            "containerized agent — it updates by pulling a new agent image, "
            "not via self-update",
        )
    target = await resolve_update_target(session, settings, agent)
    if target is None:
        raise HTTPException(status.HTTP_409_CONFLICT, "agent is already up to date")
    if await _pending_self_update(session, agent_id):
        raise HTTPException(status.HTTP_409_CONFLICT, "an update is already queued")

    now = datetime.now(UTC)
    cmd = AgentCommand(
        agent_id=agent_id,
        kind="self_update",
        item_id=None,
        payload={"target": target},
        status="pending",
        created_at=now,
        updated_at=now,
        expires_at=now + timedelta(seconds=settings.agent_command_ttl_seconds),
    )
    session.add(cmd)
    await session.commit()
    await audit.emit(
        audit.AGENT_UPDATE_TRIGGERED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "command_id": str(cmd.id),
            "agent_id": str(agent_id),
            "target": target,
            "current": agent.agent_version,
        },
    )
    return SelfUpdateOut(
        command_id=cmd.id, agent_id=agent_id, target=target, expires_at=cmd.expires_at
    )


# --------------------------------------------------------------------------- #
# Phased release rollouts (roadmap §23, 2026-08-19)                             #
# --------------------------------------------------------------------------- #
class ReleaseRolloutIn(BaseModel):
    """Same shape as a config rollout: ``tiers`` (validated by
    ``agent_config.validate_tiers``: 1..5 entries, ascending percents, last is
    100) and an optional ``starts_at`` (NULL = the next minute tick)."""

    tiers: list[dict[str, Any]] = Field(default_factory=list)
    starts_at: datetime | None = None


class ReleaseRolloutOut(BaseModel):
    id: uuid.UUID
    release_version: str
    tiers: list[dict[str, Any]]
    status: str
    current_tier: int
    covered_percent: int
    next_promotion_at: datetime | None
    starts_at: datetime | None
    started_at: datetime | None
    tier_started_at: datetime | None
    finished_at: datetime | None
    actor: str | None
    created_at: datetime


def _release_rollout_out(r: AgentReleaseRollout) -> ReleaseRolloutOut:
    nxt_at = None
    if r.status == "running" and r.tier_started_at is not None:
        tiers = r.tiers or []
        nxt = r.current_tier + 1
        if nxt < len(tiers):
            nxt_at = r.tier_started_at + timedelta(
                minutes=int(tiers[nxt].get("delay_minutes", 0) or 0)
            )
    return ReleaseRolloutOut(
        id=r.id,
        release_version=r.release_version,
        tiers=list(r.tiers or []),
        status=r.status,
        current_tier=r.current_tier,
        covered_percent=agent_config.rollout_active_percent(r) or 0,
        next_promotion_at=nxt_at,
        starts_at=r.starts_at,
        started_at=r.started_at,
        tier_started_at=r.tier_started_at,
        finished_at=r.finished_at,
        actor=r.actor,
        created_at=r.created_at,
    )


async def _known_offerable_version(session: AsyncSession, settings: Settings, version: str) -> bool:
    """A rollout can target a REGISTERED signed release or the central-baked
    agent-dist version -- anything else is a typo, refuse it."""
    rel = (
        await session.execute(select(AgentRelease.id).where(AgentRelease.version == version))
    ).scalar_one_or_none()
    if rel is not None:
        return True
    root = agent_dist._dist_root(settings)
    return bool(agent_dist._artifacts(root)) and agent_dist._version(root) == version


@router.post(
    "/agent-releases/{version}/rollouts",
    response_model=ReleaseRolloutOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def create_release_rollout(
    version: str,
    body: ReleaseRolloutIn,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseRolloutOut:
    """Start (or schedule) a phased rollout of ``version``: while it is live the
    update-manifest poll offers that version only to agents whose stable
    hash bucket is inside the active tier. ``auto_update`` / the update window /
    ``update_not_before`` still gate first; the per-agent update action still
    bypasses everything. 404 for a version central cannot offer, 409 when a
    rollout of that version is already live, 422 for a bad tier list."""
    settings = get_settings()
    if not _SAFE_VERSION.match(version):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "invalid version")
    if not await _known_offerable_version(session, settings, version):
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            f"{version!r} is neither a registered release nor the central-baked agent version",
        )
    try:
        tiers = agent_config.validate_tiers(body.tiers)
    except agent_config.RolloutValidationError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err)) from err
    live = await live_release_rollouts(session)
    if version in live:
        raise HTTPException(
            status.HTTP_409_CONFLICT, f"a rollout of {version} is already {live[version].status}"
        )
    row = AgentReleaseRollout(
        release_version=version,
        tiers=tiers,
        starts_at=body.starts_at,
        actor=audit.actor_id(request),
    )
    session.add(row)
    await session.commit()
    await session.refresh(row)
    await audit.emit(
        audit.AGENT_RELEASE_ROLLOUT_CREATED,
        request=request,
        principal_id=audit.actor_id(request),
        details={"rollout_id": str(row.id), "release_version": version, "tiers": tiers},
    )
    return _release_rollout_out(row)


@router.get(
    "/agent-release-rollouts",
    response_model=list[ReleaseRolloutOut],
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def list_release_rollouts(
    session: AsyncSession = Depends(get_session),
    status_filter: str | None = None,
    limit: int = 50,
) -> list[ReleaseRolloutOut]:
    """Release rollouts, newest first; default = only the live ones."""
    limit = max(1, min(limit, 200))
    q = select(AgentReleaseRollout)
    if status_filter:
        q = q.where(AgentReleaseRollout.status == status_filter)
    else:
        q = q.where(AgentReleaseRollout.status.in_(("scheduled", "running")))
    q = q.order_by(AgentReleaseRollout.created_at.desc()).limit(limit)
    return [_release_rollout_out(r) for r in (await session.execute(q)).scalars()]


async def _get_release_rollout(session: AsyncSession, rollout_id: uuid.UUID) -> AgentReleaseRollout:
    r = await session.get(AgentReleaseRollout, rollout_id)
    if r is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such release rollout")
    return r


@router.post(
    "/agent-release-rollouts/{rollout_id}/cancel",
    response_model=ReleaseRolloutOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def cancel_release_rollout(
    rollout_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseRolloutOut:
    """Stop offering the version to agents not yet covered. **This does not
    roll anything back**: an agent that already swapped the binary in keeps
    running it (central cannot un-swap an executable; the agent's own
    boot-counter rollback is the only un-install path). 409 if finished."""
    r = await _get_release_rollout(session, rollout_id)
    if r.status not in ("scheduled", "running"):
        raise HTTPException(status.HTTP_409_CONFLICT, f"rollout is already {r.status}")
    r.status = "cancelled"
    r.finished_at = datetime.now(UTC)
    await session.commit()
    await audit.emit(
        audit.AGENT_RELEASE_ROLLOUT_CANCELLED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "rollout_id": str(r.id), "release_version": r.release_version, "tier": r.current_tier,
        },
    )
    return _release_rollout_out(r)


@router.post(
    "/agent-release-rollouts/{rollout_id}/promote",
    response_model=ReleaseRolloutOut,
    dependencies=[Depends(require_agents_enabled), Depends(require_scope("admin"))],
)
async def promote_release_rollout(
    rollout_id: uuid.UUID,
    request: Request,
    session: AsyncSession = Depends(get_session),
) -> ReleaseRolloutOut:
    """Advance a RUNNING rollout to its next tier now (the last tier completes
    it -- offered fleet-wide). 409 when not running."""
    r = await _get_release_rollout(session, rollout_id)
    if r.status != "running":
        raise HTTPException(status.HTTP_409_CONFLICT, f"rollout is {r.status}, not running")
    now = datetime.now(UTC)
    tiers = r.tiers or []
    if r.current_tier + 1 < len(tiers):
        r.current_tier += 1
        r.tier_started_at = now
    if r.current_tier >= len(tiers) - 1:
        r.status = "completed"
        r.finished_at = now
    await session.commit()
    await audit.emit(
        audit.AGENT_RELEASE_ROLLOUT_PROMOTED,
        request=request,
        principal_id=audit.actor_id(request),
        details={
            "rollout_id": str(r.id), "release_version": r.release_version,
            "tier": r.current_tier, "status": r.status,
        },
    )
    return _release_rollout_out(r)
