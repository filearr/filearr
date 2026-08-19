package permissions

import (
	"fmt"
	"os/user"
	"strconv"
)

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
