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
  [string[]]$ConfigGroup = @(),
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
  # config_group_names must serialise as a JSON ARRAY even for one entry, and
  # ConvertTo-Json unwraps a single-element array unless it is forced.
  $mintBody = @{ config_group_names = @($ConfigGroup); ttl_minutes = $TokenTtlMinutes } |
    ConvertTo-Json -Depth 4
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
  $groupNote = if ($mint.config_group_names) { "config groups: $($mint.config_group_names -join ', ')" } else { "Global only" }
  Write-Host "minted enrollment token ($groupNote, expires $($mint.expires_at))"

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
#
# Everything below is written from ONE question: can the SERVICE run this tool?
# The agent runs as LocalSystem, so the operator's PATH answers a different
# (irrelevant) question — and answering the wrong one is what let a box sit with
# exiftool and poppler "present" while the agent reported them absent
# (2026-08-11): `winget install` defaults to USER scope, so this script's own
# installs landed in the operator's %LOCALAPPDATA% where LocalSystem, whose
# profile is under System32\config\systemprofile, can never reach them.
#
# Nothing here probes a user profile, and that is a security rule rather than
# tidiness: a tool resolved out of a user-writable directory would let any local
# user drop their own exiftool.exe there and have SYSTEM execute it. A tool
# installed into a profile is treated as NOT INSTALLED and reinstalled
# machine-wide. The agent applies the same rule (see
# agent/internal/inventory/wellknown_windows.go).

# Directories a LocalSystem service can actually read: the MACHINE PATH (not
# $env:PATH, which is the operator's machine+user merge) plus the same
# well-known locations agent/internal/inventory/wellknown_windows.go probes.
# Keep the two lists in step — if they disagree, this script and the agent
# disagree about what "installed" means, which is the bug this whole section
# exists to prevent.
#
# The well-known half is Program Files ONLY (tightened 2026-08-11, matching the
# agent). %ProgramData% and C:\ both let Authenticated Users create
# subdirectories, so a well-known path under them that does not exist yet can be
# created and filled by a non-admin and then executed by SYSTEM. Chocolatey and
# Scoop installs are still found: both put their shim directory on the MACHINE
# PATH, which is read above.
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
  # Versioned payloads: poppler's own layout, and winget PORTABLE packages, which
  # unpack to Packages\<Id>_<hash>\<inner-versioned-dir>\... (both depths, since
  # some packages have no inner directory). Get-Item, not Get-ChildItem: we want
  # the matching directories themselves, not their contents.
  $globs = @(
    "$env:ProgramFiles\poppler*\Library\bin",
    "$env:ProgramFiles\WinGet\Packages\*\*\Library\bin",
    "$env:ProgramFiles\WinGet\Packages\*\Library\bin",
    "$env:ProgramFiles\WinGet\Packages\*\*\bin",
    "$env:ProgramFiles\WinGet\Packages\*\bin"
  )
  foreach ($g in $globs) {
    $dirs += (Get-Item $g -EA SilentlyContinue |
              Where-Object { $_.PSIsContainer } | ForEach-Object { $_.FullName })
  }
  return ($dirs | Where-Object { $_ })
}

# True when <cmd>.exe exists in one of the service-visible directories.
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

function Sync-ExtractionTools {
  param([switch]$ForceAll)
  $wanted = @(
    @{ Cmd = "ffprobe";   Id = "Gyan.FFmpeg";              Name = "ffmpeg (ffprobe)"; Link = "https://www.gyan.dev/ffmpeg/builds/" },
    @{ Cmd = "pdftotext"; Id = "oschwartz10612.Poppler";   Name = "poppler-utils";    Link = "https://github.com/oschwartz10612/poppler-windows/releases" },
    @{ Cmd = "exiftool";  Id = "OliverBetz.ExifTool";      Name = "exiftool";         Link = "https://exiftool.org/  (rename exiftool(-k).exe to exiftool.exe)" },
    @{ Cmd = "tesseract"; Id = "UB-Mannheim.TesseractOCR"; Name = "tesseract";        Link = "https://github.com/UB-Mannheim/tesseract/wiki" }
  )
  $visibleDirs = Get-ServiceVisibleDirs

  $todo = @()
  foreach ($t in $wanted) {
    # Machine-visible or missing — there is no third answer. A copy in someone's
    # profile is not consulted (see the header) and is reinstalled machine-wide.
    $found = Test-ServiceVisible $t.Cmd $visibleDirs
    # -ForceTools reinstalls a PRESENT tool: the case it exists for is a
    # hand-installed copy that no package manager knows about and therefore
    # never upgrades.
    if (-not $found -or $ForceAll) { $todo += $t }
  }
  if ($todo.Count -eq 0) { Write-Host "extraction tools: present machine-wide and current"; return }

  if (-not (Get-Command winget -EA SilentlyContinue)) {
    Write-Warning "winget not available - install these yourself, MACHINE-WIDE (under $env:ProgramFiles, never a per-user directory: the service runs as LocalSystem and IGNORES user-profile installs by design), then restart the service:"
    foreach ($t in $wanted) { Write-Host ("  {0,-10} {1}" -f $t.Cmd, $t.Link) }
    return
  }
  if (-not (Test-Elevated)) {
    Write-Warning "not elevated - 'winget --scope machine' requires admin. Re-run this script from an elevated PowerShell, or the tools land in YOUR profile, which the agent ignores - they will read as absent."
  }
  foreach ($t in $todo) {
    $verb = if ($ForceAll) { "reinstalling" } else { "installing" }
    Write-Host "extraction tools: $verb $($t.Name) via winget (machine scope)"
    # --force replaces a present package (that is the whole point of -ForceTools);
    # without it winget no-ops on an already-installed id.
    $wa = @("install", "--id", $t.Id, "--exact", "--silent",
            "--accept-package-agreements", "--accept-source-agreements",
            "--disable-interactivity")
    if ($ForceAll) { $wa += "--force" }
    & winget @wa --scope machine 2>&1 | Out-Null
    if ($LASTEXITCODE -eq 0) { $script:toolsInstalled = $true; continue }
    # Not every package honours machine scope (some installer types and some
    # portables only support per-user). Retry once WITHOUT the flag so the tool
    # at least exists, and be explicit that the service still will not see it.
    & winget @wa 2>&1 | Out-Null
    if ($LASTEXITCODE -ne 0) {
      Write-Warning "  $($t.Name) failed (winget exit $LASTEXITCODE) - reported absent"
    } else {
      Write-Warning "  $($t.Name) refused machine scope and installed for THIS USER only - the agent IGNORES user-profile installs (LocalSystem must not execute from a user-writable directory) and will still report it absent. Install it machine-wide by hand: $($t.Link)"
    }
  }
  if ($script:toolsInstalled) {
    # No searching needed to say this, and it heads off the obvious confusion:
    # a pre-existing user-scope copy is still on the OPERATOR's PATH and may take
    # precedence there. It is left in place deliberately — uninstalling someone
    # else's software is not this script's job.
    Write-Host "extraction tools: note - if you had a copy installed for your user, it is still there and may win in YOUR terminal, so 'Get-Command <tool>' can show a different path than the service uses. The service uses the machine-wide install; remove the user copy yourself if you want them to agree."
  }
  Write-Host "extraction tools: done (this script restarts the service below; a service reads the MACHINE environment at start, which is when new tools become visible to it)"
}

$toolsInstalled = $false
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
} elseif ($toolsInstalled) {
  # A service snapshots the MACHINE environment when it STARTS, so a tool
  # installed underneath a running agent stays invisible until this restart —
  # which is why the script does it rather than telling the operator to.
  Restart-Service filearr-agent
  Write-Host "extraction tools installed - service restarted so it picks them up"
}

if (-not $installed) {
  Write-Host "done: agent '$Name' enrolled against $base, running as service 'filearr-agent'"
  Write-Host "it appears under Admin -> Agents on central within a minute"
} elseif (-not $stoppedForSwap -and -not $configChanged -and -not $toolsInstalled) {
  Write-Host "nothing to do"
}
