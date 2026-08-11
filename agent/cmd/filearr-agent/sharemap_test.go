package main

// Tests for the two-surface share map (2026-08-10): the precedence between the
// machine's FILEARR_AGENT_SHARE_MAP and the mappings an operator saves in the
// local web UI, the per-root view the UI renders, and the persistence round trip.

import (
	"os"
	"path/filepath"
	"testing"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

// The documented rule: on an EXACT local-path conflict the environment wins,
// because it is what the deployment manifest declares and the web UI cannot
// rewrite it. The local mapping is still stored (the operator's intent is not
// destroyed) but it is reported as superseded rather than shown as effective.
func TestShareMapEnvWinsExactPathConflict(t *testing.T) {
	dir := t.TempDir()
	if err := setLocalShareMapping(dir, "/mnt/user/media", "smb://localnas/media"); err != nil {
		t.Fatal(err)
	}
	getenv := env(map[string]string{envShareMap: "/mnt/user/media=smb://tower/media"})

	resolver, rejects := shareResolverFor(dir, getenv)
	if len(rejects) != 0 {
		t.Fatalf("unexpected rejects: %+v", rejects)
	}
	got := resolver.Resolve("/mnt/user/media/a.mkv")
	if got.Hint == nil || got.Hint.Host != "tower" {
		t.Fatalf("the environment must win the exact conflict: %+v", got.Hint)
	}
	if got.Source != "FILEARR_AGENT_SHARE_MAP" {
		t.Errorf("source = %q, want the environment", got.Source)
	}

	views, _ := rootShareViews(dir, []string{"/mnt/user/media"}, getenv)
	if len(views) != 1 {
		t.Fatalf("views: %+v", views)
	}
	v := views[0]
	if v.Location != "smb://tower/media" || v.Source != "FILEARR_AGENT_SHARE_MAP" {
		t.Errorf("the rendered row must show the winning layer: %+v", v)
	}
	if v.EnvValue == "" || v.LocalValue == "" || !v.Superseded {
		t.Errorf("a shadowed local mapping must be reported as superseded, not hidden: %+v", v)
	}
}

// A path the environment does NOT mention is the local operator's to fill in —
// the same "local fills the gaps the higher layer left" rule the schedule
// controls follow against central.
func TestShareMapLocalFillsPathsTheEnvironmentOmits(t *testing.T) {
	dir := t.TempDir()
	if err := setLocalShareMapping(dir, "/mnt/user/docs", `\\tower\documents`); err != nil {
		t.Fatal(err)
	}
	getenv := env(map[string]string{envShareMap: "/mnt/user/media=smb://tower/media"})

	views, _ := rootShareViews(dir, []string{"/mnt/user/media", "/mnt/user/docs", "/mnt/user/backups"}, getenv)
	if len(views) != 3 {
		t.Fatalf("views: %+v", views)
	}
	if views[1].Location != "smb://tower/documents" || views[1].Source != localShareSource {
		t.Errorf("locally-authored mapping did not take effect: %+v", views[1])
	}
	if views[1].Superseded {
		t.Errorf("nothing supersedes this one: %+v", views[1])
	}
	// The explicit no-mapping state — the signal an operator has no other way to
	// get, since an unmapped root simply produces no share hint.
	if views[2].Location != "" || views[2].Source != "" {
		t.Errorf("an unmapped root must render as unmapped: %+v", views[2])
	}
}

// A root nested under a mapped parent inherits the location and says where the
// mapping actually lives, so nobody edits the wrong row.
func TestShareMapNestedRootReportsInheritedMapping(t *testing.T) {
	dir := t.TempDir()
	getenv := env(map[string]string{envShareMap: "/mnt/user=smb://tower/user"})
	views, _ := rootShareViews(dir, []string{"/mnt/user/media"}, getenv)
	if views[0].Location != "smb://tower/user/media" {
		t.Fatalf("nested root location: %+v", views[0])
	}
	if views[0].InheritedFrom != "/mnt/user" {
		t.Errorf("the covering mapping's own path must be reported: %+v", views[0])
	}
	if views[0].EnvValue != "" {
		t.Errorf("the row's own env value is empty — the mapping is on the parent: %+v", views[0])
	}
}

// Malformed entries from EITHER surface are reported (verbatim, with their
// source) rather than only logged: hints are best-effort (R1), so a skipped
// entry produces no other symptom than a root that never reports a location.
func TestShareMapRejectsFromBothSurfaces(t *testing.T) {
	dir := t.TempDir()
	// A hand-edited local-settings.json can hold a malformed value even though
	// the API validates every write.
	if _, err := agentcfg.UpdateLocalSettings(dir, func(ls *agentcfg.LocalSettings) {
		ls.ShareMappings = map[string]string{"/mnt/user/docs": "ftp://nope/docs"}
	}); err != nil {
		t.Fatal(err)
	}
	getenv := env(map[string]string{envShareMap: "/mnt/user/media=smb://tower/media,garbage"})

	_, rejects := rootShareViews(dir, []string{"/mnt/user/media", "/mnt/user/docs"}, getenv)
	if len(rejects) != 2 {
		t.Fatalf("rejects = %+v, want one per surface", rejects)
	}
	bySource := map[string]string{}
	for _, rj := range rejects {
		bySource[rj.Source] = rj.Entry
	}
	if bySource["FILEARR_AGENT_SHARE_MAP"] != "garbage" {
		t.Errorf("env reject: %+v", rejects)
	}
	if bySource[localShareSource] != "/mnt/user/docs=ftp://nope/docs" {
		t.Errorf("local reject: %+v", rejects)
	}
}

// Persistence round trip: a saved mapping survives a reload, an empty location
// clears it, and editing mappings never disturbs the schedule/pause state stored
// in the same document.
func TestLocalShareMappingRoundTrip(t *testing.T) {
	dir := t.TempDir()
	if _, err := agentcfg.UpdateLocalSettings(dir, func(ls *agentcfg.LocalSettings) {
		ls.ScanPaused = true
		ls.ScanCron = strPtr("0 3 * * *")
	}); err != nil {
		t.Fatal(err)
	}
	if err := setLocalShareMapping(dir, "/mnt/user/media", "smb://tower/media"); err != nil {
		t.Fatal(err)
	}
	got, err := agentcfg.LoadLocalSettings(dir)
	if err != nil {
		t.Fatal(err)
	}
	if got.ShareMappings["/mnt/user/media"] != "smb://tower/media" {
		t.Fatalf("mapping did not round-trip: %+v", got.ShareMappings)
	}
	if !got.ScanPaused || got.ScanCron == nil {
		t.Fatalf("a share edit disturbed its siblings in the same file: %+v", got)
	}

	if err := setLocalShareMapping(dir, "/mnt/user/media", ""); err != nil {
		t.Fatal(err)
	}
	got, _ = agentcfg.LoadLocalSettings(dir)
	if len(got.ShareMappings) != 0 {
		t.Fatalf("an empty location must clear the mapping: %+v", got.ShareMappings)
	}
	if !got.ScanPaused {
		t.Fatal("clearing a mapping wiped the local pause flag")
	}
}

// A corrupt local-settings.json degrades to "no local mappings" and heals on the
// next write — the same posture the rest of that file already has: this document
// gates nothing security relevant, and refusing to scan because an editor
// truncated it would be the worse outcome.
func TestShareMapSurvivesCorruptLocalSettings(t *testing.T) {
	dir := t.TempDir()
	path := filepath.Join(dir, agentcfg.LocalSettingsName)
	if err := os.WriteFile(path, []byte(`{"share_mappings":`), 0o644); err != nil {
		t.Fatal(err)
	}
	getenv := env(map[string]string{envShareMap: "/mnt/user/media=smb://tower/media"})

	views, rejects := rootShareViews(dir, []string{"/mnt/user/media"}, getenv)
	if len(rejects) != 0 {
		t.Fatalf("a corrupt file is not a malformed ENTRY: %+v", rejects)
	}
	if views[0].Location != "smb://tower/media" {
		t.Fatalf("the environment map must still apply: %+v", views[0])
	}
	if views[0].LocalValue != "" {
		t.Errorf("a corrupt file must yield no local mappings: %+v", views[0])
	}
	// And a later good write heals it.
	if err := setLocalShareMapping(dir, "/mnt/user/docs", "smb://tower/documents"); err != nil {
		t.Fatal(err)
	}
	views, _ = rootShareViews(dir, []string{"/mnt/user/docs"}, getenv)
	if views[0].Location != "smb://tower/documents" {
		t.Fatalf("write after corruption did not take: %+v", views[0])
	}
}

// The container shape: the environment supplies everything, nothing is local, so
// every mapped path is locked for local editing and reports as env-provided.
func TestContainerShapeHasNoLocalMappings(t *testing.T) {
	dir := t.TempDir()
	getenv := env(map[string]string{
		envShareMap: "/data/media=smb://tower/media,/data/music=smb://tower/music",
	})
	views, rejects := rootShareViews(dir, []string{"/data/media", "/data/music"}, getenv)
	if len(rejects) != 0 {
		t.Fatalf("rejects: %+v", rejects)
	}
	for _, v := range views {
		if v.LocalValue != "" || v.Superseded {
			t.Errorf("no local mapping exists on a container agent: %+v", v)
		}
		if v.EnvValue == "" || v.Source != "FILEARR_AGENT_SHARE_MAP" {
			t.Errorf("every mapping must be attributed to the environment: %+v", v)
		}
	}
	locked := envShareMapPaths(getenv)
	if len(locked) != 2 {
		t.Fatalf("both env paths must be advertised as locked: %+v", locked)
	}
}

// Windows spells the same directory several ways; a second spelling must replace
// the mapping rather than add a duplicate the resolver would have to arbitrate.
func TestSetLocalShareMappingReplacesEquivalentPath(t *testing.T) {
	dir := t.TempDir()
	if err := setLocalShareMapping(dir, "/mnt/user/media", "smb://tower/media"); err != nil {
		t.Fatal(err)
	}
	if err := setLocalShareMapping(dir, "/mnt/user/media/", "smb://tower/media2"); err != nil {
		t.Fatal(err)
	}
	got, _ := agentcfg.LoadLocalSettings(dir)
	if len(got.ShareMappings) != 1 {
		t.Fatalf("a re-spelled path must replace, not duplicate: %+v", got.ShareMappings)
	}
	for _, loc := range got.ShareMappings {
		if loc != "smb://tower/media2" {
			t.Errorf("the newer value must win: %q", loc)
		}
	}
}

func strPtr(s string) *string { return &s }
