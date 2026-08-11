"""Build/stack provenance for the console's About page (``GET /system/about``).

WHY this module exists (user request, 2026-08-10): "the central console
currently doesn't have an about page with information about the whole build
stack and versions it is running". The operator question behind that is always
the same one, and it is always asked mid-incident — *which* build is actually
running here, right now, on this box?

THE GOVERNING RULE: every number reported here is read from the LIVE process,
the LIVE database or the LIVE service. A pin in ``pyproject.toml`` states what
the image was *asked* to install; ``importlib.metadata.version()`` states what
it *did* install, and those two disagree exactly when it matters (a dirty layer
cache, a hand-patched container, a resolver picking a different build of a
range-pinned dependency). So pyproject is consulted for ONE thing only — the
NAMES of the direct dependencies, which is a question about intent and which no
runtime source answers as well — and every version beside each name comes from
the installed distribution's own metadata. Anything that cannot be determined
is reported as ``null`` with a reason; nothing here ever falls back to a pinned
number, because a plausible-looking wrong version is worse than an honest gap.

NO OUTBOUND NETWORK CALLS. This deployment is deliberately offline-capable (the
manual is baked into the image, Swagger UI is vendored, HF telemetry is off),
and an About page that hangs because the box has no route to the internet would
be useless precisely when it is needed. The page LINKS to upstream projects; it
never fetches from them. The Meilisearch and Postgres probes below are calls to
this stack's own services over the local network — the same peers every request
already talks to — not to the internet.

Cost discipline: the host-tool probes each cost a subprocess, so they are
resolved at most once per (tool, resolved path) per process and cached forever
afterwards. The dependency table is likewise a once-per-process computation.
Only the genuinely live parts — the service probes, the agent-fleet rollup and
the embedding-model cache inspection — are recomputed per request.
"""

from __future__ import annotations

import functools
import importlib.metadata as im
import logging
import os
import platform
import re
import shutil
import subprocess
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import __version__, toolversions
from filearr.config import get_settings

log = logging.getLogger(__name__)

#: Cap on any string we copy out of a third-party source (a tool's version
#: banner, an exception's text). These end up rendered in a browser and, unlike
#: our own strings, have no length contract: an ffmpeg build banner runs to
#: kilobytes and a driver traceback can be far worse.
_MAX_TEXT = 200

# --------------------------------------------------------------------------- #
# Application                                                                  #
# --------------------------------------------------------------------------- #


def application_section(build_stamp: str | None) -> dict:
    """Identity of the running application itself.

    ``build_stamp`` is passed in rather than read here because the endpoint
    already offloads that file read to a threadpool for ``/version``; sharing
    the one helper keeps the About page and the deploy verifier reading the
    same ground truth instead of two implementations that can drift."""
    s = get_settings()
    return {
        "app_version": __version__,
        "build_stamp": build_stamp,
        "source_url": s.source_url,
        # AGPL-3.0-or-later is the project licence, declared in
        # backend/pyproject.toml; §13 is why ``source_url`` sits next to it.
        "license": "AGPL-3.0-or-later",
        "license_url": "https://www.gnu.org/licenses/agpl-3.0.html",
        "python_version": platform.python_version(),
        "python_implementation": platform.python_implementation(),
        # ``platform.platform()`` is the human summary (kernel + libc); the
        # machine/system pair is what you actually compare against a wheel tag
        # when a native dependency misbehaves.
        "platform": platform.platform(),
        "system": platform.system(),
        "machine": platform.machine(),
    }


# --------------------------------------------------------------------------- #
# Services (live-probed)                                                       #
# --------------------------------------------------------------------------- #


def _short(text: str | None) -> str | None:
    """Collapse whitespace and cap length on untrusted third-party text."""
    if text is None:
        return None
    flat = " ".join(str(text).split())
    if not flat:
        return None
    return flat[:_MAX_TEXT]


def _service(
    name: str,
    *,
    version: str | None = None,
    detail: str | None = None,
    url: str,
    error: str | None = None,
) -> dict:
    return {
        "name": name,
        "version": version,
        "detail": _short(detail),
        "url": url,
        "error": _short(error),
    }


def _reason(exc: BaseException) -> str:
    """A one-line, operator-readable reason for a failed probe.

    The exception TYPE is included deliberately: ``ConnectionRefusedError`` and
    ``AuthenticationError`` send an operator to completely different places, and
    several client libraries raise with an empty message."""
    text = str(exc).strip()
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


async def _meili_service() -> dict:
    """Meilisearch's own reported version, from the running engine.

    Reuses :func:`filearr.search.client` — the single place the app builds a
    Meili client from settings — so the About page is guaranteed to be probing
    the SAME engine every search hits, not a URL assembled a second time."""
    from filearr.search import client

    try:
        async with client() as c:
            v = await c.get_version()
        sha = (v.commit_sha or "")[:8]
        return _service(
            "Meilisearch",
            version=v.pkg_version,
            detail=f"commit {sha}" if sha else None,
            url="https://www.meilisearch.com/docs",
        )
    except Exception as exc:  # noqa: BLE001 — a down projection must not 500 the page
        # A failed probe is INFORMATION, not an error: "Meilisearch:
        # unreachable" on the About page is exactly what an operator needs to
        # see. Logged at debug because /stats already warns about Meili health.
        log.debug("about: Meilisearch version probe failed", exc_info=True)
        return _service(
            "Meilisearch",
            url="https://www.meilisearch.com/docs",
            error=f"unreachable — {_reason(exc)}",
        )


#: ``SELECT version()`` returns a paragraph ("PostgreSQL 18.4 (Debian
#: 18.4-1.pgdg13+1) on x86_64-pc-linux-gnu, compiled by gcc (Debian 12.2.0-14)
#: 12.2.0, 64-bit"). The number is the first token after the product name.
_PG_VERSION_RE = re.compile(r"PostgreSQL\s+([^\s,]+)")


def split_pg_version(raw: str) -> tuple[str | None, str | None]:
    """Split ``SELECT version()`` into (version, human detail).

    Pure so the several real-world shapes (Debian/Alpine builds, the embedded
    pgserver used by the test harness) can be pinned without a database. The
    detail keeps the distribution build string — which patch build is running
    matters for a "fixed in 18.4-2" advisory — but drops the compiler banner,
    which nobody has ever needed."""
    flat = " ".join(raw.split())
    m = _PG_VERSION_RE.search(flat)
    version = m.group(1) if m else None
    # " on <platform triple>" starts the part that is never actionable.
    detail = flat.split(" on ", 1)[0].strip() or None
    return version, _short(detail)


async def _postgres_service(session: AsyncSession) -> dict:
    from sqlalchemy import text as sql_text

    url = "https://www.postgresql.org/docs/"
    try:
        raw = (await session.execute(sql_text("SELECT version()"))).scalar_one()
        version, detail = split_pg_version(str(raw))
        return _service("PostgreSQL", version=version, detail=detail, url=url)
    except Exception as exc:  # noqa: BLE001 — see _meili_service
        log.debug("about: PostgreSQL version probe failed", exc_info=True)
        # The session is poisoned once a statement has failed inside it; roll
        # back so the sections that run after this one can still use it.
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001 — nothing useful to do if even that fails
            pass
        return _service("PostgreSQL", url=url, error=f"unreachable — {_reason(exc)}")


def _package_service(name: str, dist: str, url: str) -> dict:
    """A service whose version IS the installed client library's version.

    Procrastinate is a Postgres-native queue and SQLAlchemy is the ORM: neither
    is a separate process to interrogate, so the honest answer is the version of
    the code this process imported."""
    try:
        return _service(name, version=im.version(dist), url=url)
    except im.PackageNotFoundError as exc:
        return _service(name, url=url, error=_reason(exc))


async def services_section(session: AsyncSession) -> list[dict]:
    """Every backing service, probed live, each degrading independently.

    One service being down never removes the others from the table and never
    fails the request — the row simply carries ``version: null`` plus the
    reason."""
    return [
        await _postgres_service(session),
        await _meili_service(),
        _package_service(
            "Procrastinate",
            "procrastinate",
            "https://procrastinate.readthedocs.io/",
        ),
        _package_service("SQLAlchemy", "sqlalchemy", "https://docs.sqlalchemy.org"),
    ]


# --------------------------------------------------------------------------- #
# Python dependencies                                                          #
# --------------------------------------------------------------------------- #

#: Project-URL labels worth linking, best first. Documentation beats the
#: repository because the person reading this table wants "what does this
#: version do", and the docs site for every one of these projects links back to
#: its own source anyway. Matched case-insensitively: PyPI metadata capitalises
#: these labels inconsistently ("homepage" vs "Homepage" vs "Home-page").
_URL_LABEL_PREFERENCE = (
    "documentation",
    "homepage",
    "source",
    "repository",
    "source code",
    "code",
)

#: FALLBACK ONLY — consulted when a distribution's own metadata carries no
#: usable link. It is deliberately not a URL per dependency: the metadata is
#: authoritative and self-maintaining, and duplicating ~40 URLs here would rot
#: silently. These specific projects publish wheels whose metadata has, in at
#: least one released build, carried no ``Project-URL`` at all — only a
#: ``Home-page`` that some build backends drop.
_CURATED_URLS = {
    "python-magic": "https://github.com/ahupp/python-magic",
    "defusedxml": "https://github.com/tiran/defusedxml",
    "xxhash": "https://github.com/ifduyue/python-xxhash",
    "soundfile": "https://github.com/bastibe/python-soundfile",
    "ldap3": "https://ldap3.readthedocs.io/",
    "xlsxwriter": "https://xlsxwriter.readthedocs.io/",
    "onnxruntime": "https://onnxruntime.ai/docs/",
    "huggingface-hub": "https://huggingface.co/docs/huggingface_hub",
}

#: Strips the version specifier, extras and environment marker off a PEP 508
#: requirement string, leaving the bare distribution name.
_REQ_NAME_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")


def requirement_name(spec: str) -> str | None:
    """``"sqlalchemy[asyncio]==2.0.51"`` -> ``"sqlalchemy"``. Pure/testable."""
    m = _REQ_NAME_RE.match(spec)
    return m.group(1) if m else None


def _normalize_url(url: str | None) -> str | None:
    """Tidy a metadata URL for display without changing where it points.

    Two real shapes need it: a ``.git`` clone URL (cronsim's ``Repository``) and
    a plain-``http`` GitHub link (python-magic's ``Home-page``), which GitHub
    redirects to https anyway — following that redirect in the browser costs a
    round trip and looks like an insecure link in the meantime."""
    if not url:
        return None
    u = url.strip()
    if not u.startswith(("http://", "https://")):
        return None
    if u.startswith("http://github.com/"):
        u = "https://" + u[len("http://") :]
    if u.endswith(".git"):
        u = u[: -len(".git")]
    return u


def _metadata_url(dist: str) -> str | None:
    """The best link a distribution's own metadata offers, or None."""
    try:
        md = im.metadata(dist)
    except im.PackageNotFoundError:
        return None
    entries: dict[str, str] = {}
    for raw in md.get_all("Project-URL") or []:
        label, _, value = str(raw).partition(",")
        if value:
            entries.setdefault(label.strip().lower(), value.strip())
    for label in _URL_LABEL_PREFERENCE:
        if (hit := _normalize_url(entries.get(label))) is not None:
            return hit
    # ``Home-page`` is the pre-PEP-621 field; still the only link some wheels
    # carry (sqlalchemy, onnxruntime, huggingface-hub today).
    if (hit := _normalize_url(md.get("Home-page"))) is not None:
        return hit
    # Any remaining Project-URL beats nothing at all.
    for value in entries.values():
        if (hit := _normalize_url(value)) is not None:
            return hit
    return None


def dependency_url(dist: str) -> str:
    """Resolve a documentation/repository link for a distribution.

    Metadata first (self-maintaining, correct for the version actually
    installed), then the small curated fallback, then the project's PyPI page —
    which is never wrong, always exists, and links onward to whatever the
    project does publish."""
    return (
        _metadata_url(dist)
        or _CURATED_URLS.get(dist.lower())
        or f"https://pypi.org/project/{dist}/"
    )


def _pyproject_path() -> Path:
    """``backend/pyproject.toml`` in a dev checkout, ``/app/pyproject.toml`` in
    the image (the Dockerfile copies it to the WORKDIR before installing)."""
    return Path(__file__).resolve().parents[1] / "pyproject.toml"


def _declared_dependencies() -> tuple[list[str], list[str]]:
    """The DIRECT dependency names: (required, optional).

    pyproject is the source for names only — see the module docstring. When it
    is absent (an unusual packaging of the app), the installed distribution's
    own ``Requires-Dist`` is the fallback, which answers the same question from
    the runtime side."""
    try:
        import tomllib

        with open(_pyproject_path(), "rb") as fh:
            doc = tomllib.load(fh)
        project = doc.get("project", {})
        required = [
            n for spec in project.get("dependencies", []) if (n := requirement_name(spec))
        ]
        optional = [
            n
            for group in project.get("optional-dependencies", {}).values()
            for spec in group
            if (n := requirement_name(spec))
        ]
        if required:
            return required, optional
    except (OSError, ValueError):
        log.debug("about: pyproject.toml unreadable; falling back to dist metadata")
    try:
        return [
            n
            for spec in (im.requires("filearr") or [])
            # A requirement carrying an ``extra ==`` marker belongs to an extra,
            # not to the base install.
            if "extra ==" not in spec and (n := requirement_name(spec))
        ], []
    except im.PackageNotFoundError:
        return [], []


@functools.lru_cache(maxsize=1)
def python_dependencies() -> list[dict]:
    """The direct Python dependencies with their RUNNING versions and links.

    ``version`` is ``None`` for a declared dependency that is not importable
    here — a genuinely broken install, and one worth seeing rather than hiding.
    Optional-extra packages appear only when actually installed (``optional:
    true``), so the table answers "is the apprise extra present on this box?"
    without inventing a row for an extra nobody enabled.

    Cached for the process: installed versions cannot change under a running
    interpreter, and the metadata scan touches every dependency's dist-info."""
    required, optional = _declared_dependencies()
    rows: list[dict] = []
    for name in required:
        try:
            version: str | None = im.version(name)
        except im.PackageNotFoundError:
            version = None
        rows.append(
            {
                "name": name,
                "version": version,
                "url": dependency_url(name),
                "optional": False,
            }
        )
    for name in optional:
        try:
            version = im.version(name)
        except im.PackageNotFoundError:
            continue  # extra not installed: no row, nothing to report
        rows.append(
            {"name": name, "version": version, "url": dependency_url(name), "optional": True}
        )
    rows.sort(key=lambda r: r["name"].lower())
    return rows


# --------------------------------------------------------------------------- #
# Extraction host tools                                                        #
# --------------------------------------------------------------------------- #
#
# This mirrors the Go agent's agent/internal/inventory/toolversions.go so a
# central row and an agent row mean the same thing. The three properties worth
# restating here, because getting any of them wrong fails silently:
#
#  1. The version ARGUMENT is per-tool. ffmpeg/ffprobe answer `-version`,
#     tesseract wants `--version`, exiftool wants `-ver` (its `-version` is
#     something else entirely), and the poppler trio answer `-v`.
#  2. The version may arrive on stdout OR stderr — the poppler tools print to
#     stderr — and a NON-ZERO EXIT IS NOT A FAILURE here: several of these exit
#     1 immediately after printing their version quite happily. The output
#     decides, never the exit status.
#  3. The probe costs a subprocess, so it is cached per (tool, resolved path).

_TOOL_VERSION_ARGS = {
    "ffmpeg": "-version",
    "ffprobe": "-version",
    "tesseract": "--version",
    "exiftool": "-ver",
    "pdfinfo": "-v",
    "pdftotext": "-v",
    "pdftoppm": "-v",
}

#: Bounds one probe. These are local processes that print a line and exit, so a
#: few seconds is already generous; the ceiling exists for the pathological case
#: (a binary on a dead network mount) where the honest answer is "version
#: unknown" rather than a hung page.
_TOOL_PROBE_TIMEOUT_S = 3.0

#: What each tool is FOR, and where its documentation lives. Curated by
#: necessity — a host binary has no metadata to introspect.
_TOOL_INFO = {
    "ffprobe": ("Video/audio stream metadata", "https://ffmpeg.org/ffprobe.html"),
    "ffmpeg": ("Thumbnail poster frames", "https://ffmpeg.org/ffmpeg.html"),
    "exiftool": ("Image/EXIF metadata", "https://exiftool.org/"),
    "tesseract": ("OCR for scanned documents", "https://tesseract-ocr.github.io/"),
    "pdfinfo": ("PDF document properties", "https://poppler.freedesktop.org/"),
    "pdftotext": ("PDF text extraction", "https://poppler.freedesktop.org/"),
    "pdftoppm": ("PDF page rasterisation (OCR)", "https://poppler.freedesktop.org/"),
}


def parse_tool_version(name: str, out: str) -> str | None:
    """Extract the version token from a tool's version banner.

    Pure, so every tool's real output shape is pinned by tests without any of
    them being installed. The three shapes actually seen::

        ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 …  -> after "version"
        tesseract 5.3.4                                          -> second token
        12.76                                                    -> the whole line
    """
    line = next((ln.strip() for ln in out.splitlines() if ln.strip()), "")
    if not line:
        return None
    fields = line.split()
    if not fields:
        return None
    # "<something> version <token> …" — taking the token AFTER the word is what
    # keeps the vendor prefix out of the value.
    for i, f in enumerate(fields):
        if f.lower() == "version" and i + 1 < len(fields):
            return _clean_version(fields[i + 1])
    # "tesseract 5.3.4" — the tool names itself, then the version.
    if len(fields) >= 2 and fields[0].lower() == name.lower():
        return _clean_version(fields[1])
    # A bare version on its own line (exiftool's -ver). Guarded on a leading
    # digit so a usage banner is never stored as a version.
    if len(fields) == 1 and fields[0][:1].isdigit():
        return _clean_version(fields[0])
    return None


def _clean_version(token: str) -> str | None:
    """Strip control characters and cap the length — untrusted tool output."""
    cleaned = "".join(ch for ch in token if ch.isprintable()).strip()
    return cleaned[:64] or None


@functools.cache
def _probe_tool_version(name: str, path: str) -> str | None:
    """Run one tool's version argument. Cached per (tool, resolved path)."""
    arg = _TOOL_VERSION_ARGS.get(name)
    if arg is None:
        return None
    try:
        proc = subprocess.run(  # noqa: S603 — argv list, no shell, fixed argument
            [path, arg],
            capture_output=True,
            text=True,
            timeout=_TOOL_PROBE_TIMEOUT_S,
            check=False,  # a non-zero exit is not evidence; see the section note
        )
    except (OSError, subprocess.SubprocessError):
        log.debug("about: version probe failed for %s", name, exc_info=True)
        return None
    out = proc.stdout if proc.stdout.strip() else proc.stderr
    return parse_tool_version(name, out)


def _configured_tool_paths() -> dict[str, str]:
    """Each tool's configured command, honouring the per-tool path overrides.

    pdfinfo/pdftotext have no override setting (nothing in the app invokes them
    through a configurable path), so they are looked up by bare name — which is
    still the right answer for "is poppler installed in this image"."""
    s = get_settings()
    return {
        "ffprobe": s.ffprobe_path,
        "ffmpeg": s.ffmpeg_path,
        "exiftool": s.exiftool_path,
        "tesseract": s.ocr_tesseract_path,
        "pdfinfo": "pdfinfo",
        "pdftotext": "pdftotext",
        "pdftoppm": s.ocr_pdftoppm_path,
    }


def host_tools() -> list[dict]:
    """The extraction host tools ON THIS (central) machine: present or absent,
    the version where the tool will state one, and how that version measures up.

    Row shape:
    ``{name, purpose, present, version, path, url, minimum_version, verdict, impact}``.

    ``present`` with ``version: null`` is a real and distinct state — the binary
    resolved but would not say which build it is — and the UI renders it as
    "installed, version unknown" rather than a blank.

    ``verdict`` (``ok``/``outdated``/``unknown``/``absent``) is computed by
    :func:`filearr.toolversions.tool_verdict`, the SAME function that judges an
    agent's advertised tools. Sharing it is the point: central's own poppler and
    a remote agent's poppler must be called old by the same rule, or the two
    pages contradict each other and neither gets believed.

    Not cached as a whole (``shutil.which`` is a cheap path scan and an operator
    who just installed exiftool should see it without a restart); the expensive
    half, the subprocess, is cached per resolved path."""
    rows = []
    for name, configured in _configured_tool_paths().items():
        purpose, url = _TOOL_INFO[name]
        resolved = shutil.which(configured)
        version = _probe_tool_version(name, resolved) if resolved else None
        rows.append(
            {
                "name": name,
                "purpose": purpose,
                "present": resolved is not None,
                "version": version,
                "path": resolved,
                "url": url,
                "minimum_version": toolversions.minimum_version(name),
                "verdict": toolversions.tool_verdict(name, resolved is not None, version),
                "impact": toolversions.minimum_impact(name),
            }
        )
    return rows


# --------------------------------------------------------------------------- #
# Agent fleet                                                                  #
# --------------------------------------------------------------------------- #


async def agent_fleet(session: AsyncSession) -> dict | None:
    """Distinct ``agent_version`` values across the fleet, with a count each.

    A fleet mid-rollout is the case this exists for: three agents on 0.4.1 and
    one still on 0.3.9 is a one-glance answer to "did the rollout finish", and
    the alternative is scrolling the Agents page counting rows. ``None`` when
    the agent feature is disabled — there is no fleet to describe, and an empty
    table would read as "all your agents vanished".

    An agent that has never reported a version (enrolled but not yet polled)
    buckets under ``version: null``, which the UI renders as "unknown"."""
    from filearr.models import Agent

    if not get_settings().agents_enabled:
        return None
    try:
        rows = (
            await session.execute(
                select(Agent.agent_version, func.count())
                .group_by(Agent.agent_version)
                .order_by(func.count().desc())
            )
        ).all()
    except Exception as exc:  # noqa: BLE001 — pre-migration DB must not 500 the page
        log.debug("about: agent fleet rollup failed", exc_info=True)
        try:
            await session.rollback()
        except Exception:  # noqa: BLE001
            pass
        return {"total": 0, "versions": [], "error": _short(_reason(exc))}
    versions = [{"version": v, "count": int(n)} for v, n in rows]
    return {
        "total": sum(v["count"] for v in versions),
        "versions": versions,
        "error": None,
    }


# --------------------------------------------------------------------------- #
# Embedding model                                                              #
# --------------------------------------------------------------------------- #

#: huggingface_hub lays a cached repo out as
#: ``{cache}/models--{org}--{name}/snapshots/{commit_sha}/{filename}``, where
#: the file is a symlink (or, on Windows without symlink privilege, a copy) of a
#: content-addressed blob. The SNAPSHOT DIRECTORY NAME IS THE COMMIT SHA of the
#: revision on disk — far better provenance than any date, because it names the
#: exact upstream commit these weights came from and can be opened directly on
#: the Hub.
_HF_SNAPSHOTS = "snapshots"


def hf_repo_dirname(repo: str) -> str:
    """``"Qdrant/bge-small-en-v1.5-onnx-Q"`` -> ``"models--Qdrant--bge-small-en-v1.5-onnx-Q"``."""
    return "models--" + repo.replace("/", "--")


def _inspect_hf_cache(cache_dir: str, repo: str, filename: str) -> dict:
    """Locate the cached model file and read its revision, size and mtime.

    Returns ``{downloaded, revision, size, downloaded_at, path}``. Every field
    but ``downloaded`` is ``None`` when the model has never been fetched, which
    is the DEFAULT state (semantic search is opt-in) and must read as normal
    rather than broken.

    When more than one snapshot is cached — an upgrade that pulled a new
    revision without the old one being pruned — the most recently modified one
    is reported, because that is the one a fresh load would resolve to."""
    blank = {
        "downloaded": False,
        "revision": None,
        "size": None,
        "downloaded_at": None,
        "path": None,
    }
    snapshots = Path(cache_dir) / hf_repo_dirname(repo) / _HF_SNAPSHOTS
    try:
        candidates = sorted(p for p in snapshots.iterdir() if p.is_dir())
    except OSError:
        return blank

    best: tuple[float, Path, os.stat_result] | None = None
    for snap in candidates:
        target = snap / filename
        try:
            st = target.stat()  # follows the symlink through to the blob
        except OSError:
            continue
        if best is None or st.st_mtime > best[0]:
            best = (st.st_mtime, target, st)
    if best is None:
        return blank

    _, target, st = best
    return {
        "downloaded": True,
        # The snapshot directory IS the commit sha of the revision on disk.
        "revision": target.parent.name,
        "size": st.st_size,
        "downloaded_at": datetime.fromtimestamp(st.st_mtime, tz=UTC).isoformat(),
        "path": str(target),
    }


def embedding_model() -> dict:
    """The semantic-search embedding model: what is configured, and what is
    actually on this disk.

    ON DATES, EXPLICITLY: ``downloaded_at`` is the mtime of the cached blob —
    WHEN THIS MACHINE DOWNLOADED THE FILE, not when the model was published.
    The publication date is not knowable here without asking the Hub, and this
    page makes no outbound calls, so it is not guessed. ``revision`` is the
    honest provenance answer instead: it is the upstream commit these exact
    weights came from, and ``revision_url`` opens that tree on the Hub."""
    s = get_settings()
    cache = _inspect_hf_cache(s.embed_model_cache, s.embed_model_repo, s.embed_model_file)
    revision = cache["revision"]
    repo = s.embed_model_repo
    return {
        # Semantic search is off by default; the model is only fetched on first
        # use by the embed worker. Both facts are reported separately because
        # "enabled but never downloaded" (just switched on, no embed job has run
        # yet) and "disabled but cached" (switched off again) are both real.
        "enabled": s.semantic_enabled,
        "name": s.embed_model,
        "repo": repo,
        "file": s.embed_model_file,
        "dimensions": s.embed_dim,
        "cache_dir": s.embed_model_cache,
        "downloaded": cache["downloaded"],
        "revision": revision,
        "size": cache["size"],
        # Local download time — see the docstring. Named to make that hard to
        # misread on the wire as well as in the UI.
        "downloaded_at": cache["downloaded_at"],
        "path": cache["path"],
        "model_url": f"https://huggingface.co/{repo}",
        "revision_url": (
            f"https://huggingface.co/{repo}/tree/{revision}" if revision else None
        ),
        "license_note": (
            "Runs entirely locally via onnxruntime — file contents are never "
            "sent anywhere."
        ),
    }


# --------------------------------------------------------------------------- #
# Assembly                                                                     #
# --------------------------------------------------------------------------- #


def sync_sections(build_stamp: str | None) -> dict[str, Any]:
    """The blocking half — subprocesses and filesystem stats — in one call so
    the endpoint offloads exactly once to a threadpool."""
    return {
        "application": application_section(build_stamp),
        "python_packages": python_dependencies(),
        "host_tools": host_tools(),
        "embedding": embedding_model(),
    }
