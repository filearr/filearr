"""Operator-initiated update check against the public GitHub repository.

Central compares what it is RUNNING against what the repo's main branch
carries, per component, and pulls the recent commit messages (this project's
changelog — there is no separate CHANGELOG file) so they can be reviewed in
the console:

* **central** — the deploy build stamp embeds its build time
  (``<hash12>-<YYYYmmddTHHMMSSZ>``), so "repository has commits newer than
  this build" is the honest signal for source-deployed instances. Container
  images carry no build stamp but their agent-dist bake is stamped
  ``<ver>-<git sha7>`` by CI, so a sha comparison against HEAD works there.
* **agent binaries** — the release part of the local agent-dist bake version
  vs ``agent/VERSION`` at HEAD (the canonical version file every build path
  reads).

PRIVACY: this module contacts GitHub (api.github.com + raw.githubusercontent
.com) and nothing else, sending nothing but the request itself. It runs ONLY
when an operator clicks "Check now" in the console — or, with the explicit
``FILEARR_UPDATE_CHECK_AUTO`` opt-in, when the cached result is stale. The
no-phone-home posture in the docs depends on this staying operator-initiated;
never wire it into an unconditional startup or periodic path.
"""

from __future__ import annotations

import asyncio
import logging
import re
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

log = logging.getLogger(__name__)

_TIMEOUT_S = 10.0
_CACHE_TTL = timedelta(hours=6)
_CHANGELOG_COUNT = 20
_BODY_CAP = 4_000
_UA = {"User-Agent": "filearr-update-check", "Accept": "application/vnd.github+json"}

# deploy build stamp: <content-hash12>-<YYYYmmddTHHMMSSZ>
_STAMP_RE = re.compile(r"^[0-9a-f]{12}-(\d{8}T\d{6}Z)$")
# CI dist stamp suffix: -<git sha7>
_SHA7_RE = re.compile(r"-([0-9a-f]{7})$")

_lock = asyncio.Lock()
_cached: dict[str, Any] | None = None


def _repo(settings) -> tuple[str, str] | None:
    """(owner, repo) parsed from ``source_url``; None when it isn't GitHub."""
    m = re.match(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$", settings.source_url)
    return (m.group(1), m.group(2)) if m else None


def _release_part(version: str) -> tuple[int, ...] | None:
    """Numeric release tuple of a version's part before any '-' ("1.5.0-abc"
    -> (1, 5, 0)); None when it isn't numeric dotted."""
    head = version.lstrip("vV").split("-", 1)[0].split("@", 1)[0]
    try:
        return tuple(int(p) for p in head.split("."))
    except ValueError:
        return None


def _stamp_time(stamp: str) -> datetime | None:
    m = _STAMP_RE.match(stamp or "")
    if not m:
        return None
    return datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC)


def _local_identity() -> tuple[str | None, str | None]:
    """(build_stamp, dist_version) of the running instance — both best-effort."""
    from filearr.api.agent_dist import _dist_root, _version
    from filearr.api.system import _read_stamp
    from filearr.config import get_settings

    stamp = _read_stamp()
    dist = None
    try:
        root = _dist_root(get_settings())
        if root.is_dir():
            dist = _version(root)
    except Exception:  # noqa: BLE001 - identity is best-effort
        pass
    return stamp, dist


def _central_row(
    stamp: str | None, dist: str | None, head_sha: str, head_date: datetime
) -> dict[str, Any]:
    running = stamp or dist or "unknown"
    latest = f"{head_sha[:7]} ({head_date.date().isoformat()})"
    built = _stamp_time(stamp) if stamp else None
    if built is not None:
        newer = head_date > built
        return {
            "component": "central",
            "running": running,
            "latest": latest,
            "update_available": newer,
            "detail": (
                f"repository has commits newer than this build ({built.isoformat()})"
                if newer
                else "no repository commits newer than this build"
            ),
        }
    sha7 = _SHA7_RE.search(dist or "")
    if sha7:
        current = head_sha.startswith(sha7.group(1))
        return {
            "component": "central",
            "running": running,
            "latest": latest,
            "update_available": not current,
            "detail": (
                "running build's commit is the repository head"
                if current
                else f"running build {sha7.group(1)}; repository head {head_sha[:7]}"
            ),
        }
    return {
        "component": "central",
        "running": running,
        "latest": latest,
        "update_available": None,
        "detail": "no build stamp on this install (dev checkout?) — cannot compare",
    }


def _agent_row(dist: str | None, remote_version: str | None) -> dict[str, Any]:
    running = dist or "none baked"
    if not remote_version:
        return {
            "component": "agent binaries",
            "running": running,
            "latest": "unknown",
            "update_available": None,
            "detail": "agent/VERSION not readable at repository head",
        }
    local_rel = _release_part(dist or "")
    remote_rel = _release_part(remote_version)
    if local_rel is None or remote_rel is None:
        return {
            "component": "agent binaries",
            "running": running,
            "latest": remote_version,
            "update_available": None,
            "detail": "running bake carries no comparable version stamp",
        }
    newer = remote_rel > local_rel
    return {
        "component": "agent binaries",
        "running": running,
        "latest": remote_version,
        "update_available": newer,
        "detail": (
            f"agent/VERSION at head is {remote_version}"
            if newer
            else "agent bake matches the canonical agent/VERSION"
        ),
    }


async def _fetch_github(settings) -> tuple[list[dict], str | None]:
    """(commits newest-first, agent/VERSION at head). Raises on network/API
    failure — the caller shapes the error."""
    owner, repo = _repo(settings)  # type: ignore[misc]
    api = f"https://api.github.com/repos/{owner}/{repo}"
    raw = f"https://raw.githubusercontent.com/{owner}/{repo}/main/agent/VERSION"
    async with httpx.AsyncClient(timeout=_TIMEOUT_S, headers=_UA) as client:
        commits_resp, ver_resp = await asyncio.gather(
            client.get(f"{api}/commits", params={"per_page": _CHANGELOG_COUNT}),
            client.get(raw),
        )
    commits_resp.raise_for_status()
    remote_version = ver_resp.text.strip() if ver_resp.status_code == 200 else None
    commits = []
    for c in commits_resp.json():
        msg = (c.get("commit") or {}).get("message") or ""
        subject, _, body = msg.partition("\n")
        commits.append(
            {
                # full sha here; check() truncates for display AFTER the
                # head-sha comparison has used the full value
                "sha": c.get("sha") or "",
                "date": ((c.get("commit") or {}).get("committer") or {}).get("date"),
                "subject": subject.strip(),
                "body": body.strip()[:_BODY_CAP],
            }
        )
    return commits, remote_version


async def check(*, force: bool = False) -> dict[str, Any]:
    """Run (or serve the cached) update check. Contacts GitHub only when
    ``force`` or the cache is stale/absent."""
    global _cached
    from filearr.config import get_settings

    settings = get_settings()
    now = datetime.now(UTC)
    async with _lock:
        if (
            not force
            and _cached is not None
            and now - datetime.fromisoformat(_cached["checked_at"]) < _CACHE_TTL
        ):
            return _cached
        if _repo(settings) is None:
            return {
                "checked_at": now.isoformat(),
                "source": settings.source_url,
                "error": "source_url is not a GitHub repository — nothing to check against",
                "components": [],
                "changelog": [],
            }
        try:
            commits, remote_version = await _fetch_github(settings)
        except Exception as exc:  # noqa: BLE001 - offline box must degrade politely
            log.info("update check failed: %s", exc)
            return {
                "checked_at": now.isoformat(),
                "source": settings.source_url,
                "error": f"could not reach GitHub: {exc.__class__.__name__}",
                "components": [],
                "changelog": [],
            }
        if not commits:
            return {
                "checked_at": now.isoformat(),
                "source": settings.source_url,
                "error": "repository has no commits",
                "components": [],
                "changelog": [],
            }
        head = commits[0]
        head_date = datetime.fromisoformat((head["date"] or "").replace("Z", "+00:00"))
        stamp, dist = _local_identity()
        central = _central_row(stamp, dist, head["sha"], head_date)
        built = _stamp_time(stamp) if stamp else None
        for c in commits:
            c_date = datetime.fromisoformat((c["date"] or "").replace("Z", "+00:00"))
            # is_new: this commit postdates the running build (only decidable
            # for stamp-carrying installs; null elsewhere).
            c["is_new"] = (c_date > built) if built is not None else None
            c["sha"] = c["sha"][:7]
        result = {
            "checked_at": now.isoformat(),
            "source": settings.source_url,
            "components": [
                central,
                _agent_row(dist, remote_version),
            ],
            "changelog": commits,
        }
        _cached = result
        return result


def cached() -> dict[str, Any] | None:
    """The last result without any network activity (None = never checked)."""
    return _cached
