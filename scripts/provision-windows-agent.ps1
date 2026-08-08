#requires -version 5
<#
.SYNOPSIS
  Provision a Windows machine as a Filearr agent in one step: mint an
  enrollment token through the central API, download and sha256-verify the
  latest agent binary from central's agent-dist surface, install it as the
  auto-start Windows service "filearr-agent", and (optionally) configure the
  location(s) it scans.

.DESCRIPTION
  Works against BOTH central authentication modes:

    Unauthenticated central (FILEARR_AUTH_ENABLED=false) — no key needed:
      .\provision-windows-agent.ps1 -CentralUrl https://filearr.example.com `
          -ScanRoot D:\media

    Authenticated central — token minting is an ADMIN operation, so pass an
    admin-scope API key (Settings -> API keys). Downloads themselves need no
    key: agent-dist is central's deliberately-unauthenticated first-install
    surface.
      .\provision-windows-agent.ps1 -CentralUrl https://filearr.example.com `
          -ApiKey <admin key> -ScanRoot D:\media -ScanRoot E:\photos

  Run from an ELEVATED PowerShell: registering the service and writing under
  Program Files / ProgramData requires admin. Re-running is safe — the
  installer upgrades in place and a fresh token is minted each run (tokens
  are single-use; unused ones expire after -TokenTtlMinutes).

.PARAMETER ScanRoot
  Location the agent scans; repeat the switch for several roots
  (-ScanRoot D:\media -ScanRoot E:\photos). Written to scan.json in the
  service data dir; omit to configure scanning later from the agent's local
  web UI or by editing scan.json.
#>
param(
  [Parameter(Mandatory = $true)] [string]$CentralUrl,
  [string]$ApiKey,
  [string]$Name = $env:COMPUTERNAME,
  [string[]]$ScanRoot,
  [string]$RolloutGroup = "default",
  [ValidateRange(1, 1440)] [int]$TokenTtlMinutes = 60
)
$ErrorActionPreference = "Stop"
$base = $CentralUrl.TrimEnd("/")
$headers = @{}
if ($ApiKey) { $headers["Authorization"] = "Bearer $ApiKey" }

# --- 1. mint a single-use enrollment token via the API ----------------------
$mintBody = @{ rollout_group = $RolloutGroup; ttl_minutes = $TokenTtlMinutes } | ConvertTo-Json
try {
  $mint = Invoke-RestMethod -Method Post -Uri "$base/api/v1/agents/enrollment-tokens" `
    -Headers $headers -ContentType "application/json" -Body $mintBody
} catch {
  $status = $_.Exception.Response.StatusCode.value__
  if ($status -in 401, 403) {
    throw "token mint rejected ($status): this central has authentication enabled - pass -ApiKey with an ADMIN-scope key"
  }
  throw
}
Write-Host "minted enrollment token (rollout group '$($mint.rollout_group)', expires $($mint.expires_at))"

# --- 2. download + verify the latest agent binary ---------------------------
$file = "filearr-agent-windows-amd64.exe"
$staged = Join-Path $env:TEMP "filearr-agent-provision.exe"
Write-Host "downloading $base/api/v1/agent-dist/$file"
Invoke-WebRequest "$base/api/v1/agent-dist/$file" -Headers $headers -OutFile $staged -UseBasicParsing
$want = (Invoke-WebRequest "$base/api/v1/agent-dist/$file.sha256" -Headers $headers -UseBasicParsing).Content.Trim().ToLower()
$got = (Get-FileHash $staged -Algorithm SHA256).Hash.ToLower()
if ($want -ne $got) { throw "sha256 mismatch: expected $want got $got" }
Write-Host "verified sha256 $got"

# --- 3. sidecar config the installer consumes -------------------------------
# The token is single-use: the agent blanks it in the installed copy once
# enrollment succeeds, so nothing secret lingers on disk.
$sidecar = Join-Path $env:TEMP "filearr-agent.json"
[ordered]@{ central_url = $base; enrollment_token = $mint.token; agent_name = $Name } |
  ConvertTo-Json | Set-Content -Encoding utf8 $sidecar

# --- 4. install as a Windows service (enrolls, registers, starts) -----------
& $staged install --config $sidecar
if ($LASTEXITCODE -ne 0) { throw "service install failed (exit $LASTEXITCODE)" }
Remove-Item $sidecar, $staged -ErrorAction SilentlyContinue

# --- 5. scan locations -> scan.json in the service data dir -----------------
if ($ScanRoot) {
  foreach ($r in $ScanRoot) {
    if (-not (Test-Path $r)) { Write-Warning "scan root does not exist (yet): $r" }
  }
  $dataDir = Join-Path $env:ProgramData "Filearr Agent"
  $scanPath = Join-Path $dataDir "scan.json"
  # Merge into an existing scan.json (re-provisioning must not drop
  # presets/globs/category filters someone configured) — only roots change.
  $scan = [ordered]@{}
  if (Test-Path $scanPath) {
    (Get-Content $scanPath -Raw | ConvertFrom-Json).PSObject.Properties |
      ForEach-Object { $scan[$_.Name] = $_.Value }
  }
  $scan["roots"] = @($ScanRoot)
  $scan | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $scanPath
  $bin = Join-Path $env:ProgramFiles "Filearr Agent\filearr-agent.exe"
  & $bin service restart
  if ($LASTEXITCODE -ne 0) { throw "service restart failed (exit $LASTEXITCODE)" }
  Write-Host "scan roots configured: $($ScanRoot -join ', ')"
}

Write-Host "done: agent '$Name' enrolled against $base, running as service 'filearr-agent'"
Write-Host "it appears under Admin -> Agents on central within a minute"
