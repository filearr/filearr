#requires -version 5
<#
.SYNOPSIS
  ONE script for the whole Windows agent lifecycle, driven by the central API.
  It auto-detects what the machine needs:

    * agent NOT installed  -> PROVISION: mint an enrollment token, download +
      sha256-verify the binary, install the auto-start "filearr-agent" service
      (enrolls non-interactively), then apply any -ScanRoot / -MtlsUrl config.
    * agent installed      -> UPDATE + RECONFIGURE: compare the running
      version against what central serves and swap the binary under a stopped
      service when they differ (or -Force); apply -ScanRoot / -MtlsUrl
      changes in the same window. With neither an update nor config changes
      it exits without touching anything.

.DESCRIPTION
  Download it PRE-CONFIGURED from your central (the URL is baked in):

    irm https://filearr.example.com/api/v1/agent-dist/manage-windows-agent.ps1 `
        -OutFile manage-windows-agent.ps1
    .\manage-windows-agent.ps1 -ScanRoot D:\media          # elevated shell

  Or run the repository copy against any central by passing -CentralUrl.

  Works against BOTH central auth modes: with FILEARR_AUTH_ENABLED=false no
  key is needed; on an authenticated central pass -ApiKey <admin key> for
  provisioning (token minting is an admin operation — downloads themselves
  ride agent-dist, the deliberately-unauthenticated first-install surface,
  so updates never require a key).

  Run from an ELEVATED PowerShell: service registration, Program Files, and
  ProgramData writes require admin. Re-running is always safe — provisioning
  is one-shot per machine, and every later run is a no-op unless there is an
  update or a config change to apply.

.PARAMETER ScanRoot
  Location the agent scans; repeat per root (-ScanRoot D:\media -ScanRoot
  E:\photos). Merged into the service's scan.json (presets/globs you added
  survive) and applied with a service restart. Works on both paths — set at
  provision time or change later.

.PARAMETER MtlsUrl
  The mTLS agent-plane URL (e.g. https://agents.example.com) for deployments
  on FILEARR_AGENT_AUTH_MODE=mtls-header/both. Enrollment always runs against
  -CentralUrl (the mTLS site refuses clients without a certificate); once
  enrolled, the sidecar's central_url is switched here and the agent presents
  its client certificate automatically. Works on both paths — fresh installs
  land directly on mTLS, existing installs migrate (the per-machine half of
  the docs/ops/tls.md mode-flip runbook).

.PARAMETER Force
  Update path only: reinstall central's binary even when versions match
  (e.g. to recover a corrupted binary).

.PARAMETER SkipTools
  Do not touch the extraction host tools (ffmpeg/poppler/exiftool/tesseract).
  By default BOTH paths check them: provisioning installs what is missing, and
  an update installs anything a newer agent release has started needing. That
  matters because a self-updating agent otherwise keeps whatever tool set it was
  provisioned with forever, and a capability added in a later release (poppler
  arrived this way) just silently reports as unavailable.

.PARAMETER ForceTools
  Reinstall the extraction tools through winget even when they are already
  present. The default is to leave an existing tool alone, which is right for a
  package-manager install but wrong for one someone dropped in by hand years
  ago: a hand-installed tesseract 4.x is invisible to winget's upgrade and reads
  scanned pages materially worse than 5.x. This is the escape hatch — it
  replaces whatever is there with the current packaged build.
#>
param(
  [string]$CentralUrl = "__CENTRAL_URL__",
  [string]$ApiKey,
  [string]$Name = $env:COMPUTERNAME,
  [string[]]$ScanRoot,
  [string]$MtlsUrl,
  [string]$RolloutGroup = "default",
  [ValidateRange(1, 1440)] [int]$TokenTtlMinutes = 60,
  [switch]$Force,
  [switch]$SkipTools,
  [switch]$ForceTools
)
$ErrorActionPreference = "Stop"

# Split so serve-time templating (which rewrites the literal token in the
# param default above) can never touch this comparison.
$placeholder = "__CENTRAL" + "_URL__"
if (-not $CentralUrl -or $CentralUrl -eq $placeholder) {
  throw "pass -CentralUrl https://your-central - or download this script FROM your central (…/api/v1/agent-dist/manage-windows-agent.ps1), which bakes the URL in"
}
$base = $CentralUrl.TrimEnd("/")
$headers = @{}
if ($ApiKey) { $headers["Authorization"] = "Bearer $ApiKey" }

$bin = Join-Path $env:ProgramFiles "Filearr Agent\filearr-agent.exe"
$dataDir = Join-Path $env:ProgramData "Filearr Agent"
$installed = Test-Path $bin

# JSON-merge helper: preserves every existing key, changes only what we set.
function Merge-JsonFile([string]$path, [hashtable]$updates) {
  $obj = [ordered]@{}
  if (Test-Path $path) {
    (Get-Content $path -Raw | ConvertFrom-Json).PSObject.Properties |
      ForEach-Object { $obj[$_.Name] = $_.Value }
  }
  foreach ($k in $updates.Keys) { $obj[$k] = $updates[$k] }
  $obj | ConvertTo-Json -Depth 8 | Set-Content -Encoding utf8 $path
}

# The windows/amd64 artifact from central's agent-dist manifest, downloaded to
# $dest and sha256-verified against the manifest.
function Save-VerifiedBinary([string]$dest) {
  $manifest = Invoke-RestMethod "$base/api/v1/agent-dist" -Headers $headers
  $art = $manifest.artifacts | Where-Object { $_.os -eq "windows" -and $_.arch -eq "amd64" }
  if (-not $art) { throw "central's agent-dist has no windows/amd64 artifact" }
  Write-Host "downloading $($art.url)"
  Invoke-WebRequest $art.url -Headers $headers -OutFile $dest -UseBasicParsing
  $got = (Get-FileHash $dest -Algorithm SHA256).Hash.ToLower()
  if ($got -ne $art.sha256.ToLower()) { throw "sha256 mismatch: expected $($art.sha256) got $got" }
  Write-Host "verified sha256 $got"
  return $manifest.version
}

$stoppedForSwap = $false
$updatedFrom = $null
$offered = $null

if (-not $installed) {
  # ==== PROVISION ============================================================
  Write-Host "no installed agent found - provisioning"
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

  $staged = Join-Path $env:TEMP "filearr-agent-provision.exe"
  $null = Save-VerifiedBinary $staged

  # Sidecar the installer consumes. The token is single-use: the agent blanks
  # it in the installed copy once enrollment succeeds (a synchronous install
  # step), so nothing secret lingers — and the -MtlsUrl switch below is safe
  # immediately after install returns.
  $sidecarTmp = Join-Path $env:TEMP "filearr-agent.json"
  [ordered]@{ central_url = $base; enrollment_token = $mint.token; agent_name = $Name } |
    ConvertTo-Json | Set-Content -Encoding utf8 $sidecarTmp

  & $staged install --config $sidecarTmp
  if ($LASTEXITCODE -ne 0) { throw "service install failed (exit $LASTEXITCODE)" }
  Remove-Item $sidecarTmp, $staged -ErrorAction SilentlyContinue
} else {
  # ==== UPDATE ===============================================================
  $running = ((& $bin --version) -join "") -replace "^filearr-agent\s+", ""
  $manifest = Invoke-RestMethod "$base/api/v1/agent-dist" -Headers $headers
  $offered = $manifest.version
  Write-Host "running: $running"
  Write-Host "central serves: $offered"
  # Build stamps are exact-build identifiers, so "different" is the signal —
  # the same string-inequality contract the self-update dist channel uses.
  if ($Force -or $running -ne $offered) {
    $staged = Join-Path $env:TEMP "filearr-agent-update.exe"
    $null = Save-VerifiedBinary $staged
    Write-Host "stopping service filearr-agent"
    Stop-Service filearr-agent
    $stoppedForSwap = $true
    $updatedFrom = $running
    Copy-Item $bin "$bin.old" -Force        # manual rollback: copy back + start
    Copy-Item $staged $bin -Force
    Remove-Item $staged -ErrorAction SilentlyContinue
  } else {
    Write-Host "binary already current (use -Force to reinstall anyway)"
  }
}

# ==== CONFIG (both paths; while stopped when a swap is in flight) ===========
$configChanged = $false
if ($ScanRoot) {
  foreach ($r in $ScanRoot) {
    if (-not (Test-Path $r)) { Write-Warning "scan root does not exist (yet): $r" }
  }
  Merge-JsonFile (Join-Path $dataDir "scan.json") @{ roots = @($ScanRoot) }
  $configChanged = $true
  Write-Host "scan roots configured: $($ScanRoot -join ', ')"
}
if ($MtlsUrl) {
  Merge-JsonFile (Join-Path $dataDir "filearr-agent.json") @{ central_url = $MtlsUrl.TrimEnd("/") }
  $configChanged = $true
  Write-Host "central_url switched to mTLS endpoint: $($MtlsUrl.TrimEnd('/'))"
}


# ==== EXTRACTION HOST TOOLS =================================================
#
# Runs on BOTH paths. Provisioning installs what is missing; an UPDATE installs
# anything a newer agent release has started needing — without this, a
# self-updating agent keeps its original tool set forever and a capability added
# later (poppler, in the 2026-08 parity work) silently reports unavailable with
# nothing to explain why.
#
# winget only, deliberately: the alternative is fetching third-party archives,
# which would make this script an installer of arbitrary executables and put
# hash/signature verification on us for four tools. winget validates manifests
# against publisher hashes already. No winget => print the links and continue.
function Sync-ExtractionTools {
  param([switch]$ForceAll)
  $wanted = @(
    @{ Cmd = "ffprobe";   Id = "Gyan.FFmpeg";              Name = "ffmpeg (ffprobe)" },
    @{ Cmd = "pdftotext"; Id = "oschwartz10612.Poppler";   Name = "poppler-utils" },
    @{ Cmd = "exiftool";  Id = "OliverBetz.ExifTool";      Name = "exiftool" },
    @{ Cmd = "tesseract"; Id = "UB-Mannheim.TesseractOCR"; Name = "tesseract" }
  )
  # Presence must be checked the same generous way the AGENT resolves tools —
  # PATH plus the conventional install directories — or every run would reinstall
  # Tesseract, whose installer never adds itself to PATH.
  $wellKnown = @(
    "$env:ProgramFiles\Tesseract-OCR", "$env:ProgramFiles\ExifTool",
    "$env:ProgramFiles\ffmpeg\bin", "$env:LOCALAPPDATA\Microsoft\WinGet\Links"
  ) + (Get-ChildItem "$env:ProgramFiles\poppler*\Library\bin" -Directory -EA SilentlyContinue |
        ForEach-Object { $_.FullName })

  $todo = @()
  foreach ($t in $wanted) {
    $found = [bool](Get-Command $t.Cmd -EA SilentlyContinue)
    if (-not $found) {
      foreach ($d in $wellKnown) {
        if ($d -and (Test-Path (Join-Path $d ($t.Cmd + ".exe")))) { $found = $true; break }
      }
    }
    # -ForceTools reinstalls a PRESENT tool: the case it exists for is a
    # hand-installed copy that no package manager knows about and therefore
    # never upgrades.
    if (-not $found -or $ForceAll) { $todo += $t } 
  }
  if ($todo.Count -eq 0) { Write-Host "extraction tools: present and current"; return }

  if (-not (Get-Command winget -EA SilentlyContinue)) {
    Write-Warning "winget not available - install these yourself, then restart the service:"
    Write-Host "  ffmpeg     https://www.gyan.dev/ffmpeg/builds/"
    Write-Host "  poppler    https://github.com/oschwartz10612/poppler-windows/releases"
    Write-Host "  exiftool   https://exiftool.org/  (rename exiftool(-k).exe to exiftool.exe)"
    Write-Host "  tesseract  https://github.com/UB-Mannheim/tesseract/wiki"
    return
  }
  foreach ($t in $todo) {
    $verb = if ($ForceAll) { "reinstalling" } else { "installing" }
    Write-Host "extraction tools: $verb $($t.Name) via winget"
    # --force replaces a present package (that is the whole point of -ForceTools);
    # without it winget no-ops on an already-installed id.
    $args = @("install", "--id", $t.Id, "--exact", "--silent",
              "--accept-package-agreements", "--accept-source-agreements",
              "--disable-interactivity")
    if ($ForceAll) { $args += "--force" }
    & winget @args 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "  $($t.Name) failed (winget exit $LASTEXITCODE) - reported absent"
    }
  }
  Write-Host "extraction tools: done (a new PATH entry needs a service restart to be seen)"
}

if ($SkipTools) {
  Write-Host "skipping extraction tools (-SkipTools)"
} else {
  # Never fatal: extraction is opt-in and an agent with no tools is a perfectly
  # good inventory agent, so a tool problem must not fail a binary update.
  try { Sync-ExtractionTools -ForceAll:$ForceTools }
  catch { Write-Warning "extraction tools: $_ - continuing" }
}

# ==== APPLY + VERIFY ========================================================
if ($stoppedForSwap) {
  Write-Host "starting service filearr-agent"
  Start-Service filearr-agent
  $now = ((& $bin --version) -join "") -replace "^filearr-agent\s+", ""
  if ($now -ne $offered) {
    Write-Warning "service restarted but binary reports '$now' (expected '$offered') - previous binary kept at $bin.old"
    exit 1
  }
  Write-Host "agent updated $updatedFrom -> $now (previous binary kept at $bin.old)"
} elseif ($configChanged) {
  Restart-Service filearr-agent
  Write-Host "configuration applied and service restarted"
}

if (-not $installed) {
  Write-Host "done: agent '$Name' enrolled against $base, running as service 'filearr-agent'"
  Write-Host "it appears under Admin -> Agents on central within a minute"
} elseif (-not $stoppedForSwap -and -not $configChanged) {
  Write-Host "nothing to do"
}
