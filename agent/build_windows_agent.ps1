$env:Path += ';C:\Program Files\Go\bin'
cd D:\repos\filearr\agent

$pub = Get-Content "$env:USERPROFILE\.filearr-signing\filearr-release.pub"
go build -ldflags "-X main.Version=v1.4.0 -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=$pub" -o filearr-agent.exe ./cmd/filearr-agent