package permissions

import "testing"

const lsSample = `drwxr-xr-x+ 5 eric  staff  160 Aug 20 10:00 /Shared/media
 0: user:alice allow read,write,append,readattr,readextattr,file_inherit,directory_inherit
 1: group:staff deny delete,writesecurity
 2: group:everyone allow read,list,search
 3: FFFFEEEE-DDDD-CCCC-BBBB-AAAA00000059 allow read
 4: user:bob allow read,only_inherit
garbage line that is not an ace
`

func TestParseDarwinACL(t *testing.T) {
	aces := ParseDarwinACL(lsSample, 3)
	if len(aces) != 5 {
		t.Fatalf("got %d aces: %+v", len(aces), aces)
	}
	a := aces[0]
	if a.Principal.Kind != KindUser || a.Principal.Name != "alice" || a.Type != TypeAllow {
		t.Fatalf("ace0 = %+v", a)
	}
	if !a.ObjectInherit || !a.ContainerInherit || a.Scope != ScopeSubtree {
		t.Fatalf("ace0 inherit = %+v", a)
	}
	if a.OrderIndex != 3 || aces[4].OrderIndex != 7 {
		t.Fatalf("order preserved from startIndex: %d %d", a.OrderIndex, aces[4].OrderIndex)
	}
	hasVerb := func(ace ACE, v string) bool {
		for _, x := range ace.Verbs {
			if x == v {
				return true
			}
		}
		return false
	}
	if !hasVerb(a, VerbRead) || !hasVerb(a, VerbWrite) || !hasVerb(a, VerbAppend) || !hasVerb(a, VerbReadAttr) {
		t.Fatalf("ace0 verbs = %v", a.Verbs)
	}
	d := aces[1]
	if d.Type != TypeDeny || !hasVerb(d, VerbDelete) || !hasVerb(d, VerbChangePerms) {
		t.Fatalf("deny ace = %+v", d)
	}
	ev := aces[2]
	if ev.Principal.WellKnown != "EVERYONE" || !hasVerb(ev, VerbList) || !hasVerb(ev, VerbExecute) {
		t.Fatalf("everyone ace = %+v", ev)
	}
	guid := aces[3]
	if guid.Principal.Kind != KindUnmapped || guid.Principal.ID != "FFFFEEEE-DDDD-CCCC-BBBB-AAAA00000059" {
		t.Fatalf("guid trustee = %+v", guid.Principal)
	}
	oi := aces[4]
	if !oi.InheritOnly || oi.Scope != ScopeDirDefault {
		t.Fatalf("only_inherit ace = %+v", oi)
	}
	if a.RawMask == "" || a.RawMask[:6] != "macos:" {
		t.Fatalf("raw mask = %q", a.RawMask)
	}
}

func TestParseDarwinACLNoAces(t *testing.T) {
	if got := ParseDarwinACL("-rw-r--r-- 1 e s 10 Aug 20 10:00 /f\n", 3); len(got) != 0 {
		t.Fatalf("plain file parsed aces: %+v", got)
	}
}
