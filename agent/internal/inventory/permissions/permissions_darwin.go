//go:build darwin

package permissions

import (
	"context"
	"fmt"
	"io/fs"
	"os/exec"
	"strings"
	"syscall"
	"time"
)

// readACL is the macOS read (W7-T4, 2026-08-20). The fleet builds
// CGO_ENABLED=0, which blocks the native acl_get_file libSystem call, so the
// extended ACL is read by exec'ing `/bin/ls -led` (LC_ALL=C forced) and parsing
// its ordered NFSv4-style ACE text (ParseDarwinACL; order preserved — macOS
// evaluation is order-dependent). The mode bits + owner/group come from the
// stat the walk already did, exactly like Linux; BSD file flags (uchg/schg/…)
// are surfaced as a whole-object list, never folded into ACE verbs (§1.3).
//
// TCC / Full-Disk-Access: a protected path fails the ls exec with EPERM — the
// error propagates as this entry's collector error (the same "FDA suspected"
// signal content listing produces), never a silent empty ACE list.
func readACL(path string, info fs.FileInfo) (*Record, error) {
	rec := &Record{CollectedAt: time.Now().UTC()}
	isDir := info.IsDir()
	var mode uint16 = uint16(info.Mode().Perm())
	if st, ok := info.Sys().(*syscall.Stat_t); ok {
		rec.Owner = posixPrincipal(st.Uid, false)
		g := posixPrincipal(st.Gid, true)
		rec.Group = &g
		rec.Flags = bsdFlags(st.Flags)
	} else {
		return nil, fmt.Errorf("permissions: no POSIX stat for entry")
	}
	rec.Entries = append(rec.Entries, modeACEs(mode, rec.Owner, *rec.Group, isDir)...)

	ctx, cancel := context.WithTimeout(context.Background(), 10*time.Second)
	defer cancel()
	cmd := exec.CommandContext(ctx, "/bin/ls", "-led", "--", path)
	cmd.Env = append(cmd.Environ(), "LC_ALL=C")
	out, err := cmd.Output()
	if err != nil {
		// EPERM here is the TCC/FDA gate — report it, don't fake an empty ACL.
		return nil, fmt.Errorf("permissions: ls -led: %w", err)
	}
	aces := ParseDarwinACL(string(out), len(rec.Entries))
	rec.Entries = append(rec.Entries, aces...)
	if len(aces) > 0 {
		rec.Fidelity = FidelityFullNative
	} else {
		rec.Fidelity = FidelityPosixModeOnly
	}
	return rec, nil
}

// bsdFlags renders the interesting st_flags bits by their chflags names.
func bsdFlags(flags uint32) []string {
	names := []struct {
		bit  uint32
		name string
	}{
		{0x00000002, "uchg"},   // UF_IMMUTABLE
		{0x00000004, "uappnd"}, // UF_APPEND
		{0x00008000, "hidden"}, // UF_HIDDEN
		{0x00020000, "schg"},   // SF_IMMUTABLE
		{0x00040000, "sappnd"}, // SF_APPEND
	}
	var out []string
	for _, n := range names {
		if flags&n.bit != 0 {
			out = append(out, n.name)
		}
	}
	return out
}

// collectRecord is the uniform per-OS entry point Collect routes through.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	return readACL(path, info)
}

const supported = true

var _ = strings.TrimSpace // keep strings imported if flags list shrinks
