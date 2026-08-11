package shares

// Tests for the EXPLAINED resolution surface (2026-08-10): the local web UI
// shows, per scan root, the network location a file under it would report and
// which configuration surface supplied it. Each test pins a property the UI
// depends on — most importantly that "no mapping" and "malformed entry" are
// reported states, not silence (hints are best-effort by construction, ruling
// R1, so a skipped entry is otherwise invisible).

import (
	"testing"
	"time"
)

func TestResolvePerRootReportsSourceAndNoMapping(t *testing.T) {
	r := New("containerhost")
	r.enum = func() []export { return nil }
	applied, bad := r.SetStaticMappings([]Mapping{
		{Local: "/mnt/user/media", Location: "smb://tower/media", Source: StaticMapSource},
		{Local: "/mnt/user/docs", Location: `\\tower\documents`, Source: "local override"},
	})
	if applied != 2 || len(bad) != 0 {
		t.Fatalf("applied=%d bad=%+v", applied, bad)
	}

	// A root that IS the mapped path resolves to the share root itself.
	got := r.Resolve("/mnt/user/media")
	if got.Hint == nil || got.Hint.ShareURL != "smb://tower/media" {
		t.Fatalf("root location: %+v", got.Hint)
	}
	if got.Source != StaticMapSource {
		t.Errorf("source = %q, want the env map label", got.Source)
	}
	if got.ExportPath != "/mnt/user/media" {
		t.Errorf("export path = %q", got.ExportPath)
	}

	// The locally-authored entry is distinguishable from the env one.
	if got := r.Resolve("/mnt/user/docs"); got.Source != "local override" {
		t.Errorf("local mapping source = %q", got.Source)
	}

	// A root nothing covers: the explicit no-mapping answer the UI renders.
	none := r.Resolve("/mnt/user/backups")
	if none.Hint != nil || none.Source != "" || none.Ambiguous {
		t.Fatalf("an unmapped root must resolve to nothing at all: %+v", none)
	}
}

// A root nested UNDER a mapped parent still resolves, and reports the parent as
// the mapping's home so the UI can say the value is not edited on this row.
func TestResolveInheritsFromParentMapping(t *testing.T) {
	r := New("containerhost")
	r.enum = func() []export { return nil }
	r.SetStaticMappings([]Mapping{
		{Local: "/mnt/user", Location: "smb://tower/user", Source: StaticMapSource},
	})
	got := r.Resolve("/mnt/user/media/tv")
	if got.Hint == nil || got.Hint.ShareURL != "smb://tower/user/media/tv" {
		t.Fatalf("nested root: %+v", got.Hint)
	}
	if got.ExportPath != "/mnt/user" {
		t.Errorf("export path = %q, want the parent mapping", got.ExportPath)
	}
}

// Discovered exports are labelled distinctly from operator-configured ones: the
// UI must be able to say "this came from the host, nobody configured it".
func TestResolveLabelsDiscoveredExports(t *testing.T) {
	r := newTestResolver("NAS", false, []export{{name: "media", path: "/srv/media", kind: "smb"}})
	got := r.Resolve("/srv/media/a.mkv")
	if got.Hint == nil {
		t.Fatal("expected a hint")
	}
	if got.Source != "discovered smb export" {
		t.Errorf("source = %q, want the discovered label", got.Source)
	}
}

// Ambiguity stays a non-answer (R1: never guess between two equally specific
// exports) but is now REPORTABLE, so the page can explain the empty location.
func TestResolveReportsAmbiguity(t *testing.T) {
	r := newTestResolver("NAS", false, []export{
		{name: "one", path: "/srv/media", kind: "smb"},
		{name: "two", path: "/srv/media", kind: "smb"},
	})
	got := r.Resolve("/srv/media/a.mkv")
	if got.Hint != nil {
		t.Fatalf("ambiguous coverage must not produce a hint: %+v", got.Hint)
	}
	if !got.Ambiguous {
		t.Error("ambiguity must be reported, not silently indistinguishable from unmapped")
	}
}

// Malformed entries are surfaced verbatim, with the surface that supplied them —
// the whole point: a typo'd pair is skipped forever and produces no other signal.
func TestParseSpecSurfacesMalformedEntriesVerbatim(t *testing.T) {
	mappings, bad := ParseSpec(
		"/mnt/user/media=smb://tower/media, garbage ,/x=ftp://nope/share,/y=smb://hostonly,=smb://tower/z",
		StaticMapSource)
	if len(mappings) != 1 || mappings[0].Local != "/mnt/user/media" {
		t.Fatalf("valid entries: %+v", mappings)
	}
	want := []string{"garbage", "/x=ftp://nope/share", "/y=smb://hostonly", "=smb://tower/z"}
	if len(bad) != len(want) {
		t.Fatalf("rejects = %+v, want %d entries", bad, len(want))
	}
	for i, w := range want {
		if bad[i].Entry != w {
			t.Errorf("reject %d = %q, want the entry verbatim %q", i, bad[i].Entry, w)
		}
		if bad[i].Source != StaticMapSource {
			t.Errorf("reject %d source = %q", i, bad[i].Source)
		}
	}
	// And the valid entry still applies — one typo must not disarm the rest.
	r := New("h")
	r.enum = func() []export { return nil }
	if applied, _ := r.SetStaticMappings(mappings); applied != 1 {
		t.Fatalf("applied = %d", applied)
	}
}

// SetStaticMappings honours the CALLER's order on an exact path conflict. The
// precedence policy itself lives in cmd/filearr-agent; this pins the mechanism
// it relies on.
func TestSetStaticMappingsFirstMappingWinsPerPath(t *testing.T) {
	r := New("h")
	r.enum = func() []export { return nil }
	applied, bad := r.SetStaticMappings([]Mapping{
		{Local: "/mnt/user/media", Location: "smb://env-host/media", Source: StaticMapSource},
		{Local: "/mnt/user/media", Location: "smb://local-host/other", Source: "local override"},
	})
	if applied != 1 || len(bad) != 0 {
		t.Fatalf("applied=%d bad=%+v", applied, bad)
	}
	got := r.Resolve("/mnt/user/media/a.mkv")
	if got.Hint == nil || got.Hint.Host != "env-host" {
		t.Fatalf("the first mapping for a path must win: %+v", got.Hint)
	}
	if got.Source != StaticMapSource {
		t.Errorf("source = %q", got.Source)
	}
}

// Re-installing the static map must NOT re-enumerate: the local web UI installs
// it on every render (an operator may have just edited a mapping), and on
// Windows enumeration is a PowerShell invocation.
func TestSetStaticMappingsKeepsTheEnumerationCache(t *testing.T) {
	calls := 0
	r := &Resolver{host: "h", ttl: time.Minute, now: time.Now, enum: func() []export {
		calls++
		return []export{{name: "media", path: "/srv/media", kind: "smb"}}
	}}
	r.Resolve("/srv/media/a.mkv")
	for i := 0; i < 3; i++ {
		r.SetStaticMappings([]Mapping{{Local: "/mnt/user", Location: "smb://tower/user"}})
		r.Resolve("/srv/media/a.mkv")
	}
	if calls != 1 {
		t.Fatalf("enumeration ran %d times; re-installing the static map must not invalidate its cache", calls)
	}
	// …and the newly installed mapping is nonetheless in effect immediately.
	if got := r.Resolve("/mnt/user/x"); got.Hint == nil || got.Hint.Host != "tower" {
		t.Fatalf("a freshly installed mapping must apply at once: %+v", got.Hint)
	}
}

func TestValidateLocationMatchesTheResolversParser(t *testing.T) {
	good := []string{
		"smb://tower/media", "smb://tower/media/tv", `\\tower\media`, `\\tower\media\tv`,
		"nfs://tower/mnt/user/iso",
	}
	for _, loc := range good {
		if err := ValidateLocation(loc); err != nil {
			t.Errorf("ValidateLocation(%q) = %v, want nil", loc, err)
		}
		// Whatever validation accepts, the resolver must actually install.
		r := New("h")
		r.enum = func() []export { return nil }
		if applied, _ := r.SetStaticMappings([]Mapping{{Local: "/p", Location: loc}}); applied != 1 {
			t.Errorf("resolver skipped a location validation accepted: %q", loc)
		}
	}
	bad := []string{"", "   ", "tower/media", "ftp://tower/media", "smb://tower", `\\tower`, "nfs://tower"}
	for _, loc := range bad {
		if err := ValidateLocation(loc); err == nil {
			t.Errorf("ValidateLocation(%q) = nil, want an error", loc)
		}
	}
}

func TestSamePathFollowsPlatformRules(t *testing.T) {
	if !SamePath("/mnt/user/media", "/mnt/user/media/") {
		t.Error("a trailing slash must not make two paths different")
	}
	if !SamePath(`C:\Media`, "C:/Media") {
		t.Error("slash direction must not make two paths different")
	}
	if SamePath("/mnt/user/media", "/mnt/user/mediax") {
		t.Error("distinct paths must not compare equal")
	}
}
