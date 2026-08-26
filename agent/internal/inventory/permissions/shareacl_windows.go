//go:build windows

package permissions

import (
	"context"
	"os/exec"
	"strings"
	"sync"
	"time"

	"golang.org/x/sys/windows"

	"github.com/filearr/filearr/agent/internal/shares"
)

// Windows share-ACL read (2026-08-26): Get-SmbShareAccess per local share,
// cached for the life of a run (shareACLTTL) so a 100k-file walk costs one
// PowerShell call per share, not per file.
const shareACLTTL = 15 * time.Minute

var shareCache struct {
	mu       sync.Mutex
	loadedAt time.Time
	shares   []struct{ Name, Path string }
	aces     map[string][]ShareAccessEntry // by share name
}

func loadShareCache() {
	shareCache.mu.Lock()
	defer shareCache.mu.Unlock()
	if !shareCache.loadedAt.IsZero() && time.Since(shareCache.loadedAt) < shareACLTTL {
		return
	}
	shareCache.loadedAt = time.Now()
	shareCache.shares = nil
	shareCache.aces = map[string][]ShareAccessEntry{}
	for _, sh := range shares.LocalSMBShares() {
		shareCache.shares = append(shareCache.shares, struct{ Name, Path string }{sh.Name, sh.Path})
	}
	if len(shareCache.shares) == 0 {
		return
	}
	ctx, cancel := context.WithTimeout(context.Background(), 20*time.Second)
	defer cancel()
	out, err := exec.CommandContext(ctx, "powershell", "-NoProfile", "-NonInteractive", "-Command",
		"Get-SmbShareAccess -Name * | Select-Object Name,AccountName,AccessControlType,AccessRight | ConvertTo-Csv -NoTypeInformation").Output()
	if err != nil {
		return // R1: no share layer rather than a failed collection
	}
	for _, e := range ParseSmbShareAccessCSV(string(out)) {
		shareCache.aces[strings.ToLower(e.Name)] = append(shareCache.aces[strings.ToLower(e.Name)], e)
	}
}

// resolveAccount turns an account name into a SID-bearing principal when
// Windows can look it up, else a name-only principal.
func resolveAccount(account string) Principal {
	if sid, _, _, err := windows.LookupSID("", account); err == nil && sid != nil {
		p := sidPrincipal(sid)
		if p.Name == "" {
			p.Name = account
		}
		return p
	}
	return NamePrincipal(account)
}

// shareLayer returns the share-source ACEs for every local share covering
// path (empty when the path is not shared or the read failed).
func shareLayer(path string, orderBase int) []ACE {
	loadShareCache()
	shareCache.mu.Lock()
	covering := sharesCovering(path, shareCache.shares)
	byName := shareCache.aces
	shareCache.mu.Unlock()
	var out []ACE
	for _, sh := range covering {
		entries := byName[strings.ToLower(sh.Name)]
		if len(entries) == 0 {
			continue
		}
		out = append(out, ShareACEs(entries, resolveAccount, orderBase+len(out))...)
	}
	return out
}
