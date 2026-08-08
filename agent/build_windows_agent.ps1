# Local Windows agent build. The version is NEVER hardcoded here: it comes
# from agent/VERSION (the single source of truth every build path reads —
# this script, both Dockerfiles via CI, and the Proxmox deploy) plus the git
# short-sha so two local builds are distinguishable. A rebuilt binary
# therefore always reports the current stamp — the old hardcoded "v1.4.0"
# made every rebuild claim the same version forever (live report 2026-08-07).
$env:Path += ';C:\Program Files\Go\bin'
cd D:\repos\filearr\agent

$ver = (Get-Content .\VERSION -TotalCount 1).Trim()
$sha = (git rev-parse --short=7 HEAD 2>$null); if (-not $sha) { $sha = 'local' }
$stamp = "$ver-$sha"

$pub = Get-Content "$env:USERPROFILE\.filearr-signing\filearr-release.pub"
go build -ldflags "-X main.Version=$stamp -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=$pub" -o filearr-agent.exe ./cmd/filearr-agent
Write-Host "built filearr-agent.exe $stamp"
