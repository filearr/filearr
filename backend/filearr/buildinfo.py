"""Identity of the running build — always answers, never ``None``.

The deploy script's ``.build-stamp`` file only exists on source-deployed
instances, so the old ``build_stamp`` field was ``null`` for container-image
installs and dev checkouts and the console could not say what was running.
This module resolves a build identity from the first source that has one:

1. ``deploy`` — the deploy script's stamp file (``<hash12>-<YYYYmmddTHHMMSSZ>``),
   still the ground truth for source deploys (the deploy verifier compares it).
2. ``image``  — ``FILEARR_BUILD_COMMIT`` / ``_REF`` / ``_DATE``, baked into the
   container by the Dockerfile from CI build args.
3. ``git``    — a git checkout's HEAD (dev runs from the repo).
4. ``runtime`` — nothing above: package version + the package's install time.
   Still a non-null id, flagged so the UI can say it is unidentified.

Resolved once per process (cached): every source is fixed for the life of the
process, and ``/version`` is polled.
"""

from __future__ import annotations

import os
import pathlib
import re
import subprocess
from datetime import UTC, datetime
from functools import lru_cache

from filearr import __version__

_PKG_DIR = pathlib.Path(__file__).resolve().parent
_STAMP_CANDIDATES = (
    pathlib.Path("/app/.build-stamp"),
    _PKG_DIR.parent / ".build-stamp",
)
_STAMP_TIME_RE = re.compile(r"-(\d{8}T\d{6}Z)$")
_SHA_RE = re.compile(r"^[0-9a-f]{7,40}$")


def read_stamp() -> str | None:
    """The deploy script's stamp, or None when this is not a source deploy."""
    for cand in _STAMP_CANDIDATES:
        try:
            return cand.read_text().strip() or None
        except OSError:
            continue
    return None


def _iso(dt: datetime | None) -> str | None:
    return dt.astimezone(UTC).isoformat().replace("+00:00", "Z") if dt else None


def _from_stamp(stamp: str) -> dict:
    m = _STAMP_TIME_RE.search(stamp)
    built = (
        datetime.strptime(m.group(1), "%Y%m%dT%H%M%SZ").replace(tzinfo=UTC) if m else None
    )
    return {"id": stamp, "commit": None, "ref": None, "built_at": _iso(built), "source": "deploy"}


def _from_env() -> dict | None:
    commit = os.environ.get("FILEARR_BUILD_COMMIT", "").strip().lower()
    if not _SHA_RE.match(commit):
        return None
    ref = os.environ.get("FILEARR_BUILD_REF", "").strip() or None
    built_at = os.environ.get("FILEARR_BUILD_DATE", "").strip() or None
    return {
        "id": f"{ref}-{commit[:7]}" if ref else commit[:7],
        "commit": commit,
        "ref": ref,
        "built_at": built_at,
        "source": "image",
    }


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args],
            cwd=_PKG_DIR,
            capture_output=True,
            text=True,
            timeout=3,
            check=True,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _from_git() -> dict | None:
    commit = _git("rev-parse", "HEAD")
    if not commit or not _SHA_RE.match(commit):
        return None
    ref = _git("rev-parse", "--abbrev-ref", "HEAD")
    if ref == "HEAD":  # detached
        ref = None
    dirty = bool(_git("status", "--porcelain", "--untracked-files=no"))
    return {
        "id": f"{commit[:7]}-dirty" if dirty else commit[:7],
        "commit": commit,
        "ref": ref,
        "built_at": _git("log", "-1", "--format=%cI"),
        "source": "git",
    }


def _fallback() -> dict:
    try:
        built = datetime.fromtimestamp((_PKG_DIR / "__init__.py").stat().st_mtime, UTC)
    except OSError:
        built = None
    return {
        "id": f"{__version__}+unidentified",
        "commit": None,
        "ref": None,
        "built_at": _iso(built),
        "source": "runtime",
    }


@lru_cache(maxsize=1)
def build_info() -> dict:
    """``{id, commit, ref, built_at, source}``; ``id`` and ``source`` are never None."""
    stamp = read_stamp()
    if stamp:
        return _from_stamp(stamp)
    return _from_env() or _from_git() or _fallback()
