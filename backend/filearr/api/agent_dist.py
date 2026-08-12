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
#   -T  skip the extraction host tools (ffmpeg/poppler/exiftool/tesseract)
#   -F  reinstall those tools even when already present (upgrades a copy the
#       package manager does not manage — e.g. a hand-dropped old tesseract)
set -eu
BASE="__CENTRAL_URL__"
TOKEN=""
NAME=""
DL_ONLY=""
SKIP_TOOLS=""
FORCE_TOOLS=""
while [ $# -gt 0 ]; do
  case "$1" in
    -t) TOKEN="$2"; shift 2 ;;
    -n) NAME="$2"; shift 2 ;;
    -d) DL_ONLY=1; shift ;;
    -T|--skip-tools) SKIP_TOOLS=1; shift ;;
    -F|--force-tools) FORCE_TOOLS=1; shift ;;
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

# --- extraction host tools (default ON, -T to skip) --------------------------
#
# These are what let the agent read CONTENT rather than just cataloguing names:
# ffprobe for the media probe, poppler for PDFs, exiftool for camera metadata,
# tesseract for OCR. Extraction itself stays off until a policy enables it, so
# installing them is preparation, not activation.
#
# Deliberate design: we install through the PLATFORM PACKAGE MANAGER and nothing
# else. The alternative — fetching zips of third-party binaries and unpacking
# them — would mean this script downloads and installs arbitrary executables,
# and would put the burden of signature/hash verification on us for every tool
# on every platform. Package managers already do exactly that verification, from
# the distro's own signed repositories. When no package manager is available we
# PRINT the instructions and carry on rather than inventing a download path;
# a missing optional tool is a degraded capability, never a failed install.
install_tools() {
  missing=""
  for t in ffprobe pdftotext exiftool tesseract; do
    command -v "$t" >/dev/null 2>&1 || missing="$missing $t"
  done
  # -F reinstalls everything even when present. The case it exists for: a tool
  # installed by hand (an unpacked tarball, an old vendor build) that the
  # package manager does not know about and therefore never upgrades — a
  # hand-installed tesseract 4.x reads scans materially worse than 5.x and is
  # invisible to `apt upgrade`.
  if [ -n "$FORCE_TOOLS" ]; then
    missing=" ffprobe pdftotext exiftool tesseract (forced)"
  fi
  if [ -z "$missing" ]; then
    echo "extraction tools: already present"
    return 0
  fi
  echo "extraction tools: missing$missing — installing via the system package manager"
  # Package NAMES differ from binary names: ffprobe ships in ffmpeg, pdftotext in
  # poppler-utils, and exiftool is perl-image-exiftool almost everywhere.
  if command -v apt-get >/dev/null 2>&1; then
    $SUDO apt-get update -qq \
      && $SUDO apt-get install -y ffmpeg poppler-utils libimage-exiftool-perl tesseract-ocr
  elif command -v dnf >/dev/null 2>&1; then
    $SUDO dnf install -y ffmpeg poppler-utils perl-Image-ExifTool tesseract
  elif command -v zypper >/dev/null 2>&1; then
    $SUDO zypper --non-interactive install ffmpeg poppler-tools exiftool tesseract-ocr
  elif command -v pacman >/dev/null 2>&1; then
    pac="ffmpeg poppler perl-image-exiftool tesseract tesseract-data-eng"
    # shellcheck disable=SC2086  # deliberate word splitting: a package list
    $SUDO pacman -S --noconfirm --needed $pac
  elif command -v apk >/dev/null 2>&1; then
    $SUDO apk add --no-cache ffmpeg poppler-utils exiftool tesseract-ocr tesseract-ocr-data-eng
  elif command -v brew >/dev/null 2>&1; then
    # Homebrew refuses to run as root, so this deliberately does NOT use $SUDO.
    brew install ffmpeg poppler exiftool tesseract
  else
    echo "  no supported package manager found — install these yourself:" >&2
    echo "    ffmpeg (ffprobe) · poppler-utils · exiftool · tesseract" >&2
    return 0
  fi
}
if [ -n "$SKIP_TOOLS" ]; then
  echo "skipping extraction tools (-T); the agent will report them as absent"
else
  # Never let an optional tool take the install down with it: extraction is
  # opt-in, and an agent with no tools is a perfectly valid inventory agent.
  install_tools     || echo "extraction tools: install failed — continuing without them" >&2
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
  [switch]$DownloadOnly,
  # Extraction host tools are installed by DEFAULT via winget; -SkipTools opts out.
  [switch]$SkipTools,
  # Reinstall them even when present — upgrades a hand-installed copy that
  # winget does not manage and therefore never upgrades.
  [switch]$ForceTools
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

# --- extraction host tools (default ON, -SkipTools to opt out) ---------------
#
# ffprobe (media probe), poppler (PDF), exiftool (camera metadata) and tesseract
# (OCR) are what let the agent read CONTENT rather than only cataloguing names.
# Extraction stays off until a policy enables it, so this is preparation.
#
# winget ONLY, deliberately. The alternative is downloading third-party zips and
# unpacking them, which would make this script an installer of arbitrary
# executables and put hash/signature verification on us for four tools. winget
# validates package manifests against publisher hashes already. No winget (older
# Windows 10, or a locked-down image) means we PRINT the links and continue —
# a missing optional tool is a degraded capability, never a failed install.
#
# MACHINE SCOPE THROUGHOUT. The agent runs as a LocalSystem service, and
# `winget install` without a scope flag defaults to USER scope: the shims and
# payloads land in the installing operator's %LOCALAPPDATA%, while LocalSystem's
# profile is under System32\config\systemprofile. That combination silently
# produced agents reporting exiftool and poppler absent right after this script
# "installed" them (2026-08-11).
#
# The agent also IGNORES user-profile installs on purpose — executing a binary
# out of a user-writable directory as SYSTEM is a local privilege-escalation
# path — so this script never probes a profile either. Machine-visible or
# missing; there is no third answer.

# Directories a LocalSystem service can actually read: the MACHINE PATH (NOT
# $env:PATH, which is the operator's machine+user merge) plus the same machine
# locations the agent probes.
#
# The well-known half is Program Files ONLY (tightened 2026-08-11, matching
# agent/internal/inventory/wellknown_windows.go). %ProgramData% and C:\ both
# let Authenticated Users create subdirectories, so a well-known path under
# them that does not exist yet can be created and filled by a non-admin and
# then executed by SYSTEM. Chocolatey and Scoop installs are still found: both
# put their shim directory on the MACHINE PATH, which is read above.
function Get-ServiceVisibleDirs {
  $dirs = @()
  $machinePath = [Environment]::GetEnvironmentVariable("Path", "Machine")
  if ($machinePath) { $dirs += ($machinePath -split ";" | Where-Object { $_ }) }
  $dirs += @(
    "$env:ProgramFiles\Tesseract-OCR",
    "${env:ProgramFiles(x86)}\Tesseract-OCR",
    "$env:ProgramFiles\ExifTool",
    "$env:ProgramFiles\ffmpeg\bin",
    "$env:ProgramFiles\WinGet\Links"
  )
  # Versioned payloads: poppler's own layout, and winget PORTABLE packages,
  # which unpack to Packages\<Id>_<hash>\<inner-versioned-dir>\... (both depths,
  # since some packages have no inner directory). Get-Item, not Get-ChildItem:
  # we want the matching directories themselves, not their contents.
  foreach ($g in @("$env:ProgramFiles\poppler*\Library\bin",
                   "$env:ProgramFiles\WinGet\Packages\*\*\Library\bin",
                   "$env:ProgramFiles\WinGet\Packages\*\Library\bin",
                   "$env:ProgramFiles\WinGet\Packages\*\*\bin",
                   "$env:ProgramFiles\WinGet\Packages\*\bin")) {
    $dirs += (Get-Item $g -ErrorAction SilentlyContinue |
              Where-Object { $_.PSIsContainer } | ForEach-Object { $_.FullName })
  }
  return ($dirs | Where-Object { $_ })
}

function Test-ServiceVisible([string]$cmd, [string[]]$dirs) {
  foreach ($d in $dirs) {
    if (Test-Path (Join-Path $d ($cmd + ".exe"))) { return $true }
  }
  return $false
}

function Test-Elevated {
  $id = [Security.Principal.WindowsIdentity]::GetCurrent()
  return ([Security.Principal.WindowsPrincipal]$id).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
}

function Install-ExtractionTools {
  $wanted = @(
    @{ Cmd = "ffprobe"; Id = "Gyan.FFmpeg"; Name = "ffmpeg (ffprobe)"
       Link = "https://www.gyan.dev/ffmpeg/builds/" },
    @{ Cmd = "pdftotext"; Id = "oschwartz10612.Poppler"; Name = "poppler-utils"
       Link = "https://github.com/oschwartz10612/poppler-windows/releases" },
    @{ Cmd = "exiftool"; Id = "OliverBetz.ExifTool"; Name = "exiftool"
       Link = "https://exiftool.org/  (rename exiftool(-k).exe to exiftool.exe)" },
    @{ Cmd = "tesseract"; Id = "UB-Mannheim.TesseractOCR"; Name = "tesseract"
       Link = "https://github.com/UB-Mannheim/tesseract/wiki" }
  )
  # "Already installed" is asked exactly the way the SERVICE will ask it. Using
  # Get-Command here would answer "can the operator run this?", which is how a
  # user-scope install passes as present forever and never gets repaired.
  $visibleDirs = Get-ServiceVisibleDirs

  $missing = @()
  foreach ($t in $wanted) {
    $found = Test-ServiceVisible $t.Cmd $visibleDirs
    if (-not $found -or $ForceTools) { $missing += $t }
  }
  if ($missing.Count -eq 0) { Write-Host "extraction tools: already present machine-wide"; return }

  if (-not (Get-Command winget -ErrorAction SilentlyContinue)) {
    Write-Warning ("winget not available - install these yourself MACHINE-WIDE " +
      "(under $env:ProgramFiles, never a per-user directory: the service runs as " +
      "LocalSystem and ignores user-profile installs), then restart the service:")
    foreach ($t in $wanted) { Write-Host ("  {0,-10} {1}" -f $t.Cmd, $t.Link) }
    return
  }
  if (-not (Test-Elevated)) {
    Write-Warning ("not elevated - 'winget --scope machine' requires admin. Re-run " +
      "this from an elevated PowerShell, or the tools land in YOUR profile, where " +
      "the agent ignores them.")
  }
  $installedAny = $false
  foreach ($t in $missing) {
    Write-Host "extraction tools: installing $($t.Name) via winget (machine scope)"
    # --accept-*-agreements keeps this non-interactive; a failure is reported and
    # skipped, never fatal.
    $wa = @("install", "--id", $t.Id, "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity")
    # --force is what makes -ForceTools mean anything: without it winget no-ops
    # on an id it already considers installed.
    if ($ForceTools) { $wa += "--force" }
    & winget @wa --scope machine 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $installedAny = $true; continue }
    # Not every package honours machine scope (some installer types and some
    # portables are user-only). Retry once WITHOUT the flag so the tool at least
    # exists, and be explicit that the agent still will not use it.
    & winget @wa 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "  $($t.Name) failed (winget exit $LASTEXITCODE) - reported absent"
    } else {
      Write-Warning ("  $($t.Name) refused machine scope and installed for THIS " +
        "USER only - the agent IGNORES user-profile installs (LocalSystem must not " +
        "execute from a user-writable directory) and will report it absent. " +
        "Install it machine-wide by hand: $($t.Link)")
    }
  }
  if ($installedAny) {
    # Said without looking anywhere: a pre-existing user-scope copy is left in
    # place (uninstalling someone else's software is not this script's job) and
    # may still win in the operator's own terminal.
    Write-Host ("extraction tools: note - a copy installed for your user is left " +
      "alone and may still win in YOUR terminal, so 'Get-Command <tool>' can show " +
      "a different path than the agent uses.")
  }
  Write-Host ("extraction tools: done. The 'filearr-agent.exe install' step below " +
    "starts the service, and a service reads the MACHINE environment at start - " +
    "that is when these become visible to it.")
}

if ($SkipTools) {
  Write-Host "skipping extraction tools (-SkipTools); the agent will report them as absent"
} else {
  # $ErrorActionPreference is Stop for the install itself; tools must not inherit
  # that, because an optional capability failing is not an install failure.
  try { $ErrorActionPreference = "Continue"; Install-ExtractionTools }
  catch { Write-Warning "extraction tools: $_ - continuing" }
  finally { $ErrorActionPreference = "Stop" }
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
