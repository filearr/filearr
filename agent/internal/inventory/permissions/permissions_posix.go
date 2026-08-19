//go:build linux

package permissions

import (
	"fmt"
	"io/fs"
	"os"
	"os/user"
	"strconv"
	"syscall"
	"time"

	"golang.org/x/sys/unix"
)

// readACL is the Linux read (W7-T2, 2026-08-19). Pure Go, no CGO, no shell-out:
// the POSIX.1e ACL lives in the "system.posix_acl_access" (+ "_default" for
// directories) xattrs whose binary layout DecodePosixACL already understands,
// and the mode bits + owner/group come from the stat the walk already did.
//
// Emitted entries, in order:
//   - the mode bits as three synthetic ACEs (user_obj / group_obj / other) --
//     always present, so a file with no ACL xattr still has a complete record;
//   - the access ACL (ScopeThis) when the xattr exists (a real ACL supersedes
//     the mode's group_obj: the ACL's mask entry is carried verbatim as an ACE
//     of principal "mask" for the forensic reader);
//   - the default ACL (ScopeDirDefault) on directories.
//
// Fidelity is stamped from /proc/mounts: cifs without cifsacl -> the mode is
// synthesized by the mount options and MUST NOT be read as an ACL
// (FidelitySynthesizedFromMode); nfs4 -> nfs4 (real NFSv4 ACLs exist but are
// not decoded here -- the record honestly carries mode + fidelity=nfs4);
// otherwise full_native when an ACL xattr was read, posix_mode_only when not.
func readACL(path string, info fs.FileInfo) (*Record, error) {
	st, ok := info.Sys().(*syscall.Stat_t)
	if !ok {
		return nil, fmt.Errorf("permissions: no POSIX stat for entry")
	}
	rec := &Record{CollectedAt: time.Now().UTC()}
	rec.Owner = posixPrincipal(st.Uid, false)
	g := posixPrincipal(st.Gid, true)
	rec.Group = &g
	isDir := info.IsDir()

	mode := uint16(info.Mode().Perm())
	rec.Entries = append(rec.Entries, modeACEs(mode, rec.Owner, g, isDir)...)

	fid := DetectMountFidelity(readProcMounts(), path)
	switch fid {
	case MountSynthesizedCIFS, MountSynthesizedSamba:
		rec.Fidelity = FidelitySynthesizedFromMode
		return rec, nil
	}

	haveACL := false
	if raw, err := lgetxattr(path, "system.posix_acl_access"); err == nil && len(raw) > 0 {
		if acl, derr := DecodePosixACL(raw); derr == nil {
			haveACL = true
			for _, ace := range acl.ToACEs(ScopeThis) {
				ace.OrderIndex = len(rec.Entries)
				resolvePosixACEPrincipal(&ace)
				rec.Entries = append(rec.Entries, ace)
			}
		}
	}
	if isDir {
		if raw, err := lgetxattr(path, "system.posix_acl_default"); err == nil && len(raw) > 0 {
			if acl, derr := DecodePosixACL(raw); derr == nil {
				haveACL = true
				for _, ace := range acl.ToACEs(ScopeDirDefault) {
					ace.OrderIndex = len(rec.Entries)
					resolvePosixACEPrincipal(&ace)
					rec.Entries = append(rec.Entries, ace)
				}
			}
		}
	}
	switch {
	case fid == MountNFS4:
		rec.Fidelity = "nfs4"
	case haveACL:
		rec.Fidelity = FidelityFullNative
	default:
		rec.Fidelity = FidelityPosixModeOnly
	}
	return rec, nil
}

// modeACEs renders the classic rwx triplet as three allow ACEs (owner, group,
// other) so every record has the baseline grant set even without an ACL.
func modeACEs(mode uint16, owner, group Principal, isDir bool) []ACE {
	other := Principal{Kind: KindWellKnown, ID: "other", Name: "other", WellKnown: "EVERYONE"}
	mk := func(i int, p Principal, rwx uint16, tag string) ACE {
		return ACE{
			Principal:  p,
			Type:       TypeAllow,
			Verbs:      PosixRWXToVerbs(rwx, isDir),
			RawMask:    fmt.Sprintf("mode:%s=0%o", tag, rwx),
			Scope:      ScopeThis,
			Source:     SourceLocal,
			OrderIndex: i,
		}
	}
	return []ACE{
		mk(0, owner, (mode>>6)&0x7, "user_obj"),
		mk(1, group, (mode>>3)&0x7, "group_obj"),
		mk(2, other, mode&0x7, "other"),
	}
}

func posixPrincipal(id uint32, isGroup bool) Principal {
	p := Principal{Kind: KindUser, ID: strconv.FormatUint(uint64(id), 10)}
	if isGroup {
		p.Kind = KindGroup
		if g, err := user.LookupGroupId(p.ID); err == nil {
			p.Name = g.Name
		}
	} else if u, err := user.LookupId(p.ID); err == nil {
		p.Name = u.Username
	}
	if wk := ClassifyPOSIXID(id, isGroup); wk != "" {
		p.Kind, p.WellKnown = KindWellKnown, wk
	} else if wk := ClassifyPOSIXName(p.Name); wk != "" {
		p.WellKnown = wk
	}
	return p
}

// resolvePosixACEPrincipal fills Name/WellKnown for a numeric ACL_USER/ACL_GROUP
// qualifier; tag-only entries (user_obj/group_obj/mask/other) keep their token.
func resolvePosixACEPrincipal(ace *ACE) {
	id, err := strconv.ParseUint(ace.Principal.ID, 10, 32)
	if err != nil {
		return
	}
	isGroup := ace.Principal.Kind == KindGroup
	p := posixPrincipal(uint32(id), isGroup)
	ace.Principal = p
}

func lgetxattr(path, name string) ([]byte, error) {
	buf := make([]byte, 1024)
	for {
		n, err := unix.Lgetxattr(path, name, buf)
		if err == unix.ERANGE {
			buf = make([]byte, len(buf)*2)
			continue
		}
		if err != nil {
			return nil, err
		}
		return buf[:n], nil
	}
}

func readProcMounts() string {
	b, err := os.ReadFile("/proc/mounts")
	if err != nil {
		return ""
	}
	return string(b)
}

// collectRecord is the uniform per-OS entry point Collect routes through.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	return readACL(path, info)
}

const supported = true
