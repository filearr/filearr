"""Central distribution of agent binaries (first-install path).

The Docker image cross-compiles the Go agent for every supported platform into
``/app/agent-dist`` (override: ``FILEARR_AGENT_DIST_DIR``). This router serves
those binaries plus generated install scripts so a NEW machine can be set up
with one command against its own central — no GitHub account, no container
registry, no manual download step:

    Linux/macOS:  curl -fsSL <central>/api/v1/agent-dist/install.sh | sh -s -- -t <token>
    Windows:      irm <central>/api/v1/agent-dist/install.ps1 -OutFile install-agent.ps1
                  .\\install-agent.ps1 -Token <token>

Deliberately UNAUTHENTICATED (behind the ``agents_enabled`` feature gate): the
binaries are public AGPL artifacts (the same bits live on ghcr.io), and the
P5-T7 release-artifact endpoint they complement requires an already-enrolled,
certificate-authenticated agent — useless for a machine that doesn't exist yet.
Enrollment itself still requires an operator-minted single-use token, which is
what actually gates joining the fleet.

sha256 digests are computed lazily and cached per (path, size, mtime) so the
first manifest hit pays the hash cost once per process, not per request.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from fastapi.responses import FileResponse, PlainTextResponse

from filearr.api.agents import require_agents_enabled
from filearr.config import Settings, get_settings

router = APIRouter()

# filearr-agent-<goos>-<goarch>[.exe] — the only filename shape ever served.
_DIST_FILENAME = re.compile(r"^filearr-agent-[a-z0-9]+-[a-z0-9]+(\.exe)?$")

# (resolved path, size, mtime_ns) -> hex digest. Bounded by the artifact count.
_sha_cache: dict[tuple[str, int, int], str] = {}


def _dist_root(settings: Settings) -> Path:
    return Path(settings.agent_dist_dir or "/app/agent-dist")


def _artifacts(root: Path) -> list[Path]:
    if not root.is_dir():
        return []
    return sorted(
        p for p in root.iterdir() if p.is_file() and _DIST_FILENAME.match(p.name)
    )


def _sha256(path: Path) -> str:
    st = path.stat()
    key = (str(path), st.st_size, st.st_mtime_ns)
    cached = _sha_cache.get(key)
    if cached is not None:
        return cached
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    digest = h.hexdigest()
    _sha_cache[key] = digest
    return digest


def _platform_of(filename: str) -> tuple[str, str]:
    stem = filename.removesuffix(".exe")
    _, _, rest = stem.partition("filearr-agent-")
    goos, _, goarch = rest.partition("-")
    return goos, goarch


def _version(root: Path) -> str:
    try:
        return (root / "VERSION").read_text(encoding="utf-8").strip() or "unknown"
    except OSError:
        return "unknown"


def _base_url(request: Request) -> str:
    """The central URL the scripts phone back to and the manifest links carry —
    the shared derivation in :func:`filearr.urls.public_base_url`
    (FILEARR_PUBLIC_BASE_URL -> X-Forwarded-* -> request.base_url; the raw
    request said http:// behind the TLS proxy and baked dead URLs into
    install.ps1 — live 2026-08-08)."""
    from filearr.urls import public_base_url

    return public_base_url(request)


@router.get(
    "/agent-dist",
    dependencies=[Depends(require_agents_enabled)],
)
async def dist_manifest(request: Request) -> dict[str, Any]:
    """Version + per-platform artifact list (filename, os/arch, size, sha256, url)."""
    settings = get_settings()
    root = _dist_root(settings)
    arts = _artifacts(root)
    if not arts:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND,
            "no agent distribution artifacts on this install "
            "(container images ship them; set FILEARR_AGENT_DIST_DIR otherwise)",
        )
    base = _base_url(request)
    out = []
    for p in arts:
        goos, goarch = _platform_of(p.name)
        out.append(
            {
                "filename": p.name,
                "os": goos,
                "arch": goarch,
                "size": p.stat().st_size,
                "sha256": _sha256(p),
                "url": f"{base}/api/v1/agent-dist/{p.name}",
            }
        )
    return {"version": _version(root), "artifacts": out}


_INSTALL_SH = r"""#!/bin/sh
# Filearr agent installer — served by your central at
#   __CENTRAL_URL__/api/v1/agent-dist/install.sh
# Usage:
#   curl -fsSL __CENTRAL_URL__/api/v1/agent-dist/install.sh | sh -s -- [-t TOKEN] [-n NAME] [-d]
#   -t  enrollment token (Admin -> Agents -> Mint token); writes filearr-agent.json
#       and runs the service install
#   -n  friendly agent name (defaults to the hostname)
#   -d  download only (skip the service install)
set -eu
BASE="__CENTRAL_URL__"
TOKEN=""
NAME=""
DL_ONLY=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) TOKEN="$2"; shift 2 ;;
    -n) NAME="$2"; shift 2 ;;
    -d) DL_ONLY=1; shift ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
done

os=$(uname -s | tr '[:upper:]' '[:lower:]')
case "$os" in
  linux|darwin) ;;
  *) echo "unsupported OS: $os (use the Windows script or a container)" >&2; exit 1 ;;
esac
arch=$(uname -m)
case "$arch" in
  x86_64|amd64) arch=amd64 ;;
  aarch64|arm64) arch=arm64 ;;
  *) echo "unsupported arch: $arch" >&2; exit 1 ;;
esac
file="filearr-agent-$os-$arch"

echo "downloading $BASE/api/v1/agent-dist/$file"
curl -fsSL "$BASE/api/v1/agent-dist/$file" -o filearr-agent
chmod +x filearr-agent

want=$(curl -fsSL "$BASE/api/v1/agent-dist/$file.sha256")
if command -v sha256sum >/dev/null 2>&1; then
  got=$(sha256sum filearr-agent | awk '{print $1}')
else
  got=$(shasum -a 256 filearr-agent | awk '{print $1}')
fi
if [ "$want" != "$got" ]; then
  echo "sha256 mismatch: expected $want got $got" >&2
  exit 1
fi
echo "verified sha256 $got"

if [ -n "$TOKEN" ]; then
  umask 077
  if [ -n "$NAME" ]; then
    printf '{"central_url": "%s", "enrollment_token": "%s", "agent_name": "%s"}\n' \
      "$BASE" "$TOKEN" "$NAME" > filearr-agent.json
  else
    printf '{"central_url": "%s", "enrollment_token": "%s"}\n' \
      "$BASE" "$TOKEN" > filearr-agent.json
  fi
fi

if [ -n "$DL_ONLY" ]; then
  echo "downloaded ./filearr-agent"
  exit 0
fi
if [ ! -f filearr-agent.json ]; then
  echo "downloaded ./filearr-agent — no token (-t) and no filearr-agent.json here."
  echo "next: mint a token in Admin -> Agents, then either re-run with -t <token> or:"
  echo "  ./filearr-agent enroll -central $BASE -token <token> && sudo ./filearr-agent install"
  exit 0
fi

SUDO=""
if [ "$(id -u)" != "0" ]; then
  SUDO="sudo"
  echo "service install needs root — using sudo"
fi
$SUDO ./filearr-agent install --config filearr-agent.json
"""

_INSTALL_PS1 = r"""#requires -version 5
# Filearr agent installer — served by your central at
#   __CENTRAL_URL__/api/v1/agent-dist/install.ps1
# Usage (elevated PowerShell — the installer creates a Windows service):
#   irm __CENTRAL_URL__/api/v1/agent-dist/install.ps1 -OutFile install-agent.ps1
#   .\install-agent.ps1 -Token <enrollment token> [-Name <friendly name>]
param(
  [string]$Token,
  [string]$Name,
  [switch]$DownloadOnly
)
$ErrorActionPreference = "Stop"
$base = "__CENTRAL_URL__"
$file = "filearr-agent-windows-amd64.exe"

Write-Host "downloading $base/api/v1/agent-dist/$file"
Invoke-WebRequest "$base/api/v1/agent-dist/$file" -OutFile filearr-agent.exe -UseBasicParsing

$want = (Invoke-WebRequest "$base/api/v1/agent-dist/$file.sha256" -UseBasicParsing).Content.Trim()
$got = (Get-FileHash filearr-agent.exe -Algorithm SHA256).Hash.ToLower()
if ($want -ne $got) { throw "sha256 mismatch: expected $want got $got" }
Write-Host "verified sha256 $got"

if ($Token) {
  $sidecar = [ordered]@{ central_url = $base; enrollment_token = $Token }
  if ($Name) { $sidecar.agent_name = $Name }
  $sidecar | ConvertTo-Json | Set-Content -Encoding utf8 filearr-agent.json
}

if ($DownloadOnly) { Write-Host "downloaded .\filearr-agent.exe"; exit 0 }
if (-not (Test-Path filearr-agent.json)) {
  Write-Host "downloaded .\filearr-agent.exe - no -Token given and no filearr-agent.json here."
  Write-Host "next: mint a token in Admin -> Agents, then re-run with -Token <token>."
  exit 0
}

# The installer stops/creates the Windows service and copies the binary into
# Program Files — it will fail without an elevated shell.
.\filearr-agent.exe install --config filearr-agent.json
"""


@router.get(
    "/agent-dist/install.sh",
    dependencies=[Depends(require_agents_enabled)],
    response_class=PlainTextResponse,
)
async def install_sh(request: Request) -> PlainTextResponse:
    """POSIX install script templated with THIS central's URL."""
    return PlainTextResponse(
        _INSTALL_SH.replace("__CENTRAL_URL__", _base_url(request)),
        media_type="text/x-shellscript; charset=utf-8",
    )


@router.get(
    "/agent-dist/install.ps1",
    dependencies=[Depends(require_agents_enabled)],
    response_class=PlainTextResponse,
)
async def install_ps1(request: Request) -> PlainTextResponse:
    """Windows install script templated with THIS central's URL."""
    return PlainTextResponse(
        _INSTALL_PS1.replace("__CENTRAL_URL__", _base_url(request)),
        media_type="text/plain; charset=utf-8",
    )


# The unified Windows lifecycle script (provision / update / reconfigure — it
# auto-detects). Shipped as a FILE (image: /app/agentscripts; dev checkouts
# fall back to the repo scripts/ dir) so the repo copy stays the single source
# of truth; serving bakes THIS central's URL into the -CentralUrl default.
# Registered BEFORE the {filename} catch-all below or it would never match.
_MANAGE_SCRIPT = "manage-windows-agent.ps1"


def _manage_script_path() -> Path | None:
    for cand in (
        Path("/app/agentscripts") / _MANAGE_SCRIPT,
        Path(__file__).resolve().parents[3] / "scripts" / _MANAGE_SCRIPT,
    ):
        if cand.is_file():
            return cand
    return None


@router.get(
    "/agent-dist/manage-windows-agent.ps1",
    dependencies=[Depends(require_agents_enabled)],
    response_class=PlainTextResponse,
)
async def manage_windows_agent_ps1(request: Request) -> PlainTextResponse:
    """The one-script Windows agent lifecycle tool (provision + update +
    reconfigure), templated with THIS central's URL."""
    path = _manage_script_path()
    if path is None:
        raise HTTPException(
            status.HTTP_404_NOT_FOUND, "management script not shipped on this install"
        )
    return PlainTextResponse(
        path.read_text(encoding="utf-8").replace("__CENTRAL_URL__", _base_url(request)),
        media_type="text/plain; charset=utf-8",
    )


@router.get(
    "/agent-dist/{filename}",
    dependencies=[Depends(require_agents_enabled)],
)
async def download_dist(filename: str):
    """Stream one platform binary; ``<filename>.sha256`` returns its hex digest
    (what the install scripts verify against)."""
    want_digest = filename.endswith(".sha256")
    if want_digest:
        filename = filename.removesuffix(".sha256")
    if not _DIST_FILENAME.match(filename):
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such artifact")
    root = _dist_root(get_settings()).resolve()
    target = (root / filename).resolve()
    # Belt-and-braces: the regex already forbids separators/traversal.
    if target.parent != root or not target.is_file():
        raise HTTPException(status.HTTP_404_NOT_FOUND, "no such artifact")
    if want_digest:
        return PlainTextResponse(_sha256(target) + "\n")
    return FileResponse(
        str(target), media_type="application/octet-stream", filename=filename
    )
