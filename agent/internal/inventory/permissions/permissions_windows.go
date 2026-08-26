//go:build windows

package permissions

import (
	"fmt"
	"io/fs"
	"time"
	"unsafe"

	"golang.org/x/sys/windows"
)

// readSecurityDescriptor is the Windows read (W7-T3, 2026-08-19): owner, group
// and the FULL DACL with inheritance flags via GetNamedSecurityInfo (works on
// local and UNC paths alike). No SACL in v1 (needs SeSecurityPrivilege; the
// posture would need a distinct health state -- deferred). Pure
// golang.org/x/sys/windows, no CGO.
func readSecurityDescriptor(path string, isDir bool) (*Record, error) {
	sd, err := windows.GetNamedSecurityInfo(
		path, windows.SE_FILE_OBJECT,
		windows.OWNER_SECURITY_INFORMATION|windows.GROUP_SECURITY_INFORMATION|windows.DACL_SECURITY_INFORMATION,
	)
	if err != nil {
		return nil, err
	}
	rec := &Record{CollectedAt: time.Now().UTC(), Fidelity: FidelityFullNative}
	if owner, _, oerr := sd.Owner(); oerr == nil && owner != nil {
		rec.Owner = sidPrincipal(owner)
	} else {
		rec.Owner = Principal{Kind: KindUnmapped, ID: "?"}
	}
	if grp, _, gerr := sd.Group(); gerr == nil && grp != nil {
		g := sidPrincipal(grp)
		rec.Group = &g
	}
	dacl, defaulted, derr := sd.DACL()
	if derr != nil {
		return nil, derr
	}
	_ = defaulted
	if dacl == nil {
		// NULL DACL = no protection at all (everyone full control). Record it
		// as such rather than as "no entries".
		rec.Posture = Posture{DaclPresent: false}
		rec.Entries = append(rec.Entries, ACE{
			Principal:  Principal{Kind: KindWellKnown, ID: "S-1-1-0", Name: "Everyone", WellKnown: "EVERYONE"},
			Type:       TypeAllow,
			Verbs:      NTFSMaskToVerbs(0x1F01FF, isDir),
			RawMask:    "null_dacl",
			Scope:      ScopeThis,
			Source:     SourceLocal,
			OrderIndex: 0,
		})
		return rec, nil
	}
	rec.Posture = Posture{DaclPresent: true, DaclCanonical: true}
	seenAllow := false
	for i := uint16(0); i < dacl.AceCount; i++ {
		var ace *windows.ACCESS_ALLOWED_ACE
		if err := windows.GetAce(dacl, uint32(i), &ace); err != nil {
			continue
		}
		var typ string
		// AceType values per winnt.h: 0 allow, 1 deny, 5 allow-object, 6 deny-object,
		// 9 allow-callback, 10 deny-callback (x/sys names only the first two).
		switch ace.Header.AceType {
		case windows.ACCESS_ALLOWED_ACE_TYPE, 5, 9:
			typ = TypeAllow
			seenAllow = true
		case windows.ACCESS_DENIED_ACE_TYPE, 6, 10:
			typ = TypeDeny
			// A deny AFTER an allow (among non-inherited entries) is a
			// non-canonical DACL -- worth flagging in the posture.
			if seenAllow && ace.Header.AceFlags&windows.INHERITED_ACE == 0 {
				rec.Posture.DaclCanonical = false
			}
		default:
			continue // audit/alarm/system ACEs do not belong in a DACL walk
		}
		sid := (*windows.SID)(unsafe.Pointer(&ace.SidStart))
		flags := ace.Header.AceFlags
		e := ACE{
			Principal:        sidPrincipal(sid),
			Type:             typ,
			Verbs:            NTFSMaskToVerbs(uint32(ace.Mask), isDir),
			RawMask:          fmt.Sprintf("0x%08X", uint32(ace.Mask)),
			Inherited:        flags&windows.INHERITED_ACE != 0,
			ContainerInherit: flags&windows.CONTAINER_INHERIT_ACE != 0,
			ObjectInherit:    flags&windows.OBJECT_INHERIT_ACE != 0,
			NoPropagate:      flags&windows.NO_PROPAGATE_INHERIT_ACE != 0,
			InheritOnly:      flags&windows.INHERIT_ONLY_ACE != 0,
			Scope:            ScopeThis,
			Source:           SourceLocal,
			OrderIndex:       int(i),
		}
		if e.ContainerInherit || e.ObjectInherit {
			e.Scope = ScopeSubtree
		}
		rec.Entries = append(rec.Entries, e)
	}
	return rec, nil
}

func sidPrincipal(sid *windows.SID) Principal {
	p := Principal{Kind: KindUnmapped, ID: "?"}
	if sid == nil || !sid.IsValid() {
		return p
	}
	p.ID = sid.String()
	if account, domain, use, err := sid.LookupAccount(""); err == nil {
		if domain != "" {
			p.Name = domain + `\` + account
		} else {
			p.Name = account
		}
		switch use {
		case windows.SidTypeGroup, windows.SidTypeAlias, windows.SidTypeWellKnownGroup:
			p.Kind = KindGroup
		default:
			p.Kind = KindUser
		}
	}
	if wk := ClassifySID(p.ID); wk != "" {
		p.Kind, p.WellKnown = KindWellKnown, wk
	} else if wk := ClassifyPOSIXName(p.Name); wk != "" {
		p.WellKnown = wk
	}
	return p
}

// collectRecord is the uniform per-OS entry point Collect routes through.
// 2026-08-26: the file's own DACL plus the ACL of every local SMB share that
// covers the path (Source=share), so central can reconcile the two layers.
func collectRecord(path string, info fs.FileInfo) (*Record, error) {
	rec, err := readSecurityDescriptor(path, info != nil && info.IsDir())
	if err != nil {
		return nil, err
	}
	rec.Entries = append(rec.Entries, shareLayer(path, len(rec.Entries))...)
	return rec, nil
}

const supported = true
