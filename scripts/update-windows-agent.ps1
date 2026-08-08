#requires -version 5
<#
.SYNOPSIS
  Update an installed Windows Filearr agent to the latest build the central
  serves: compare versions, download and sha256-verify the new binary, stop
  the "filearr-agent" service, swap the binary in place, and start it again.

.DESCRIPTION
  Complements the agent's built-in self-update channel — use this when the
  update must be operator-driven: key-pinned builds central won't offer the
  unsigned dist channel to, machines with self-update disabled, or simply
  "update it now from a shell". The agent's enrollment, data, index, and
  scan configuration are untouched; only the binary changes.

  Works against BOTH central authentication modes. The binary/manifest
  downloads come from agent-dist, central's deliberately-unauthenticated
  first-install surface, so no key is ever REQUIRED — but -ApiKey is
  accepted and sent on every request for deployments that front central
  with an authenticating proxy:

    Unauthenticated central (or plain authenticated central):
      .\update-windows-agent.ps1 -CentralUrl https://filearr.example.com

    Behind an authenticating proxy:
      .\update-windows-agent.ps1 -CentralUrl https://filearr.example.com -ApiKey <key>

  Run from an ELEVATED PowerShell (stopping/starting the service and writing
  under Program Files requires admin).

.PARAMETER Force
  Skip the version comparison and reinstall whatever central serves — e.g.
  to recover a corrupted binary of the same version.
#>
param(
  [Parameter(Mandatory = $true)] [string]$CentralUrl,
  [string]$ApiKey,
  [switch]$Force
)
$ErrorActionPreference = "Stop"
$base = $CentralUrl.TrimEnd("/")
$headers = @{}
if ($ApiKey) { $headers["Authorization"] = "Bearer $ApiKey" }

$bin = Join-Path $env:ProgramFiles "Filearr Agent\filearr-agent.exe"
if (-not (Test-Path $bin)) {
  throw "no installed agent at $bin - use provision-windows-agent.ps1 for a first install"
}

# --- what we run vs what central serves -------------------------------------
$running = ((& $bin --version) -join "") -replace "^filearr-agent\s+", ""
$manifest = Invoke-RestMethod "$base/api/v1/agent-dist" -Headers $headers
$offered = $manifest.version
Write-Host "running: $running"
Write-Host "central serves: $offered"
# Build stamps are exact-build identifiers, so "different" is the signal —
# the same string-inequality contract the self-update dist channel uses.
if (-not $Force -and $running -eq $offered) {
  Write-Host "already up to date - nothing to do (use -Force to reinstall anyway)"
  exit 0
}

# --- download + verify the offered binary -----------------------------------
$file = "filearr-agent-windows-amd64.exe"
$art = $manifest.artifacts | Where-Object { $_.os -eq "windows" -and $_.arch -eq "amd64" }
if (-not $art) { throw "central's agent-dist has no windows/amd64 artifact" }
$staged = Join-Path $env:TEMP "filearr-agent-update.exe"
Write-Host "downloading $($art.url)"
Invoke-WebRequest $art.url -Headers $headers -OutFile $staged -UseBasicParsing
$got = (Get-FileHash $staged -Algorithm SHA256).Hash.ToLower()
if ($got -ne $art.sha256.ToLower()) { throw "sha256 mismatch: expected $($art.sha256) got $got" }
Write-Host "verified sha256 $got"

# --- swap under a stopped service, keep the old binary as rollback ----------
Write-Host "stopping service filearr-agent"
Stop-Service filearr-agent
try {
  Copy-Item $bin "$bin.old" -Force          # manual rollback: copy back + start
  Copy-Item $staged $bin -Force
} finally {
  Write-Host "starting service filearr-agent"
  Start-Service filearr-agent
}
Remove-Item $staged -ErrorAction SilentlyContinue

$now = ((& $bin --version) -join "") -replace "^filearr-agent\s+", ""
if ($now -ne $offered) {
  Write-Warning "service restarted but binary reports '$now' (expected '$offered') - previous binary kept at $bin.old"
  exit 1
}
Write-Host "done: agent updated $running -> $now (previous binary kept at $bin.old)"
