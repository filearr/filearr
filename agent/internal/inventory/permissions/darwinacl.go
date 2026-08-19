package permissions

import (
	"strings"
)

// W7-T4 (2026-08-20): macOS extended-ACL text parsing. The fleet builds
// CGO_ENABLED=0, which blocks the native acl_get_file libSystem call, so the
// darwin reader execs `/bin/ls -led` (LC_ALL=C) and parses its ACE lines:
//
//	drwxr-xr-x+ 5 eric  staff  160 Aug 20 10:00 /path
//	 0: user:alice allow read,write,append,readattr,file_inherit
//	 1: group:staff deny delete
//	 2: FFFFEEEE-DDDD-CCCC-BBBB-AAAA00000059 allow read
//
// This file is deliberately UNTAGGED so the parser unit-tests run on every
// platform; only the exec lives behind the darwin build tag.

// darwinPermVerbs maps macOS's NAMED ACL permissions to normalized verbs
// (brief §3.2; the names are what `ls -le` / `chmod +a` print).
var darwinPermVerbs = map[string]string{
	"read":             VerbRead,
	"write":            VerbWrite,
	"execute":          VerbExecute,
	"delete":           VerbDelete,
	"append":           VerbAppend,
	"readattr":         VerbReadAttr,
	"writeattr":        VerbWriteAttr,
	"readextattr":      VerbReadAttr,
	"writeextattr":     VerbWriteAttr,
	"readsecurity":     VerbReadPerms,
	"writesecurity":    VerbChangePerms,
	"chown":            VerbTakeOwn,
	"list":             VerbList,
	"search":           VerbExecute,
	"add_file":         VerbWrite,
	"add_subdirectory": VerbAppend,
	"delete_child":     VerbDeleteChild,
}

// darwinInheritPerms are inheritance flags, not grants — they ride
// InheritFlags and shape Scope, never the verb list.
var darwinInheritPerms = map[string]string{
	"file_inherit":      "object_inherit",
	"directory_inherit": "container_inherit",
	"only_inherit":      "inherit_only",
	"limit_inherit":     "no_propagate",
}

// ParseDarwinACL parses the ACE lines of `ls -led` output (everything after the
// first line). Unparseable lines are SKIPPED, never fatal — a truncated read
// yields fewer ACEs, not a crash. Raw order is preserved via OrderIndex
// (macOS ACLs are order-dependent like NTFS DACLs).
func ParseDarwinACL(out string, startIndex int) []ACE {
	var aces []ACE
	for _, line := range strings.Split(out, "\n")[1:] {
		line = strings.TrimSpace(line)
		// " N: principal allow|deny perm,perm,..."
		colon := strings.IndexByte(line, ':')
		if colon < 0 || !allDigits(line[:colon]) {
			continue
		}
		rest := strings.TrimSpace(line[colon+1:])
		fields := strings.Fields(rest)
		if len(fields) < 3 {
			continue
		}
		// principal may contain spaces only in resolved names quoted? ls does
		// not quote; a name with spaces is ambiguous — take everything before
		// the allow/deny token as the principal.
		typeIdx := -1
		for i, f := range fields {
			if f == "allow" || f == "deny" {
				typeIdx = i
				break
			}
		}
		if typeIdx < 1 || typeIdx == len(fields)-1 {
			continue
		}
		principalRaw := strings.Join(fields[:typeIdx], " ")
		aceType := TypeAllow
		if fields[typeIdx] == "deny" {
			aceType = TypeDeny
		}
		perms := strings.Split(strings.Join(fields[typeIdx+1:], ""), ",")

		verbs := map[string]bool{}
		var inherit []string
		inheritOnly := false
		for _, perm := range perms {
			perm = strings.TrimSpace(perm)
			if v, ok := darwinPermVerbs[perm]; ok {
				verbs[v] = true
			} else if f, ok := darwinInheritPerms[perm]; ok {
				inherit = append(inherit, f)
				if perm == "only_inherit" {
					inheritOnly = true
				}
			}
			// unknown tokens (future macOS perms) are ignored, never invented
		}
		scope := ScopeThis
		if len(inherit) > 0 {
			scope = ScopeSubtree
		}
		if inheritOnly {
			scope = ScopeDirDefault
		}
		ace := ACE{
			Principal:  darwinPrincipal(principalRaw),
			Type:       aceType,
			Verbs:      orderVerbs(verbs),
			RawMask:    "macos:" + strings.Join(perms, ","),
			Scope:      scope,
			Source:     SourceLocal,
			OrderIndex: startIndex + len(aces),
		}
		for _, f := range inherit {
			switch f {
			case "object_inherit":
				ace.ObjectInherit = true
			case "container_inherit":
				ace.ContainerInherit = true
			case "inherit_only":
				ace.InheritOnly = true
			case "no_propagate":
				ace.NoPropagate = true
			}
		}
		aces = append(aces, ace)
	}
	return aces
}

// darwinPrincipal classifies an ls -le principal token: "user:name",
// "group:name", or a bare GUID for an unresolvable trustee.
func darwinPrincipal(raw string) Principal {
	switch {
	case strings.HasPrefix(raw, "user:"):
		name := raw[len("user:"):]
		p := Principal{Kind: KindUser, ID: name, Name: name}
		if wk := ClassifyPOSIXName(name); wk != "" {
			p.WellKnown = wk
		}
		return p
	case strings.HasPrefix(raw, "group:"):
		name := raw[len("group:"):]
		p := Principal{Kind: KindGroup, ID: name, Name: name}
		if name == "everyone" {
			p.Kind, p.WellKnown = KindWellKnown, "EVERYONE"
		} else if wk := ClassifyPOSIXName(name); wk != "" {
			p.WellKnown = wk
		}
		return p
	default:
		// unresolvable trustee: ls prints the raw GUID — keep it, never drop (§2.4.5)
		return Principal{Kind: KindUnmapped, ID: raw}
	}
}

func allDigits(s string) bool {
	s = strings.TrimSpace(s)
	if s == "" {
		return false
	}
	for _, ch := range s {
		if ch < '0' || ch > '9' {
			return false
		}
	}
	return true
}
