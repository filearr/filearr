package inventory

// Tests for the per-agent About payload (2026-08-11): resolved tool paths, the
// build block, the module list, the self-imposed capabilities budget, and the
// cache TTL that is the whole reason a newly installed tool now appears without
// a service restart.

import (
	"encoding/json"
	"fmt"
	"os"
	"path/filepath"
	"runtime/debug"
	"strings"
	"testing"
	"time"
)

// TestToolPathsOmitsAbsentTools pins the omit-don't-blank discipline: a present
// tool contributes its resolved path, an absent one contributes NO KEY. An
// empty-string entry would render as a blank cell in the console, which reads
// as "installed at nowhere" rather than "not installed".
func TestToolPathsOmitsAbsentTools(t *testing.T) {
	dir := t.TempDir()
	present := fakeTool(t, dir, "path-probe")
	t.Setenv(EnvFFprobePath, present)
	t.Setenv(EnvExiftoolPath, filepath.Join(dir, "definitely-absent"))

	paths := ToolPaths()
	if got := paths["ffprobe"]; got != present {
		t.Fatalf("ToolPaths()[ffprobe] = %q, want %q", got, present)
	}
	if _, ok := paths["exiftool"]; ok {
		t.Fatalf("absent tool must be omitted, got %q", paths["exiftool"])
	}
	// Every reported path must be a path the resolver would actually execute —
	// ToolPaths reuses ResolveTool precisely so the two cannot diverge.
	for name, p := range paths {
		if p != ResolveTool(name, hostToolEnvs[name]) {
			t.Fatalf("%s: ToolPaths reported %q but ResolveTool says %q",
				name, p, ResolveTool(name, hostToolEnvs[name]))
		}
	}
}

// TestHostToolNamesCoversMatrix guards the single-source rule: the three views
// built from hostToolEnvs (presence, versions, paths) must describe the same
// set of tools, or the console renders a matrix with an unexplainable hole.
func TestHostToolNamesCoversMatrix(t *testing.T) {
	names := HostToolNames()
	tools := Tools()
	if len(names) != len(tools) {
		t.Fatalf("HostToolNames() has %d entries, Tools() has %d", len(names), len(tools))
	}
	for _, n := range names {
		if _, ok := tools[n]; !ok {
			t.Fatalf("Tools() missing %q", n)
		}
	}
	// Sorted, so the local web UI's table does not reshuffle between refreshes.
	for i := 1; i < len(names); i++ {
		if names[i-1] >= names[i] {
			t.Fatalf("HostToolNames() not sorted: %v", names)
		}
	}
}

// TestToolCacheTTLReprobes is the regression test for the incident this feature
// came out of: a tool installed AFTER the agent started stayed invisible
// forever, because the resolution cache was process-lifetime. With a TTL the
// stale entry re-probes and the new binary is found.
//
// The TTL is collapsed to zero rather than slept through — every entry is then
// immediately stale, which exercises exactly the re-probe branch.
func TestToolCacheTTLReprobes(t *testing.T) {
	dir := t.TempDir()
	// A PATH lookup (no override) is the real-world shape: the operator installs
	// into a directory that is already on the machine PATH.
	t.Setenv("PATH", dir+string(os.PathListSeparator)+os.Getenv("PATH"))
	const tool = "ttl-probe-tool"

	if got := ResolveTool(tool, "FILEARR_AGENT_TTL_PROBE_PATH"); got != "" {
		t.Fatalf("expected %q absent before install, got %q", tool, got)
	}

	// The operator installs it. Nothing invalidates the cache — that is the
	// point; only time does.
	installed := fakeTool(t, dir, tool)

	// With the production TTL still in force the stale-but-fresh entry stands,
	// which is the cache doing its job (and what makes the extraction hot path
	// cheap).
	if got := ResolveTool(tool, "FILEARR_AGENT_TTL_PROBE_PATH"); got != "" {
		t.Fatalf("within the TTL the cached miss must stand, got %q", got)
	}

	restore := setToolCacheTTL(t, 0)
	defer restore()
	got := ResolveTool(tool, "FILEARR_AGENT_TTL_PROBE_PATH")
	if got == "" {
		t.Fatal("after the TTL expired the tool must be re-probed and found")
	}
	if !strings.EqualFold(got, installed) {
		t.Fatalf("re-probe resolved %q, want %q", got, installed)
	}
}

// setToolCacheTTL overrides the package TTL for one test and returns the
// restore func. A package var (rather than an injected clock everywhere) is the
// cheapest thing that makes the expiry branch testable without a 15-minute
// sleep.
func setToolCacheTTL(t *testing.T, d time.Duration) func() {
	t.Helper()
	prev := toolCacheTTL
	toolCacheTTL = d
	return func() { toolCacheTTL = prev }
}

func TestBuildInfoReportsToolchainAndPlatform(t *testing.T) {
	b := BuildInfo()
	for _, key := range []string{"go_version", "goos", "goarch", "num_cpu"} {
		if _, ok := b[key]; !ok {
			t.Fatalf("BuildInfo missing %q: %v", key, b)
		}
	}
	gv, _ := b["go_version"].(string)
	if !strings.HasPrefix(gv, "go") {
		t.Fatalf("go_version = %q, want a go1.x toolchain string", gv)
	}
	// Never an empty string on the wire: an unavailable field is omitted, so
	// the console can say "not reported" instead of rendering a blank cell.
	for key, v := range b {
		if s, ok := v.(string); ok && s == "" {
			t.Fatalf("BuildInfo emitted an empty %q; it should have been omitted", key)
		}
	}
}

// TestBuildInfoWithoutBuildTable pins the degraded path. A binary whose build
// table cannot be read must still report the runtime facts rather than panic on
// the nil *BuildInfo.
func TestBuildInfoWithoutBuildTable(t *testing.T) {
	restore := stubBuildInfo(t, nil, false)
	defer restore()

	b := BuildInfo()
	if b["goos"] == nil || b["go_version"] == nil {
		t.Fatalf("runtime facts must survive a missing build table: %v", b)
	}
	if _, ok := b["vcs_revision"]; ok {
		t.Fatalf("no build table means no VCS stamp: %v", b)
	}
	if mods := Modules(); mods != nil {
		t.Fatalf("Modules() with no build table = %v, want nil", mods)
	}
}

// stubBuildInfo replaces the debug.ReadBuildInfo indirection for one test.
func stubBuildInfo(t *testing.T, bi *debug.BuildInfo, ok bool) func() {
	t.Helper()
	prev := readBuildInfo
	readBuildInfo = func() (*debug.BuildInfo, bool) { return bi, ok }
	return func() { readBuildInfo = prev }
}

func TestModulesSortedDedupedAndCapped(t *testing.T) {
	deps := []*debug.Module{
		{Path: "example.com/zeta", Version: "v1.2.3"},
		{Path: "example.com/alpha", Version: "v0.1.0"},
		// A replace directive: the REPLACEMENT is what is linked in, so that is
		// what must be reported.
		{Path: "example.com/orig", Version: "v1.0.0", Replace: &debug.Module{
			Path: "example.com/fork", Version: "v9.9.9",
		}},
		// A duplicate of the main module must not appear twice.
		{Path: "example.com/main", Version: "(devel)"},
	}
	restore := stubBuildInfo(t, &debug.BuildInfo{
		Main: debug.Module{Path: "example.com/main", Version: "(devel)"},
		Deps: deps,
	}, true)
	defer restore()

	mods := Modules()
	want := []string{"example.com/alpha", "example.com/fork", "example.com/main", "example.com/zeta"}
	if len(mods) != len(want) {
		t.Fatalf("Modules() = %v, want %d entries", mods, len(want))
	}
	for i, w := range want {
		if mods[i]["path"] != w {
			t.Fatalf("Modules()[%d].path = %q, want %q (list: %v)", i, mods[i]["path"], w, mods)
		}
	}
	if v := mods[1]["version"]; v != "v9.9.9" {
		t.Fatalf("replaced module version = %q, want the replacement's v9.9.9", v)
	}
	// "(devel)" is not a version anyone can act on; it is omitted rather than
	// displayed next to the real agent_version.
	if _, ok := mods[2]["version"]; ok {
		t.Fatalf("main module (devel) should carry no version: %v", mods[2])
	}
}

func TestModulesCap(t *testing.T) {
	deps := make([]*debug.Module, 0, maxModules+50)
	for i := range maxModules + 50 {
		deps = append(deps, &debug.Module{
			Path:    fmt.Sprintf("example.com/mod%04d", i),
			Version: "v1.0.0",
		})
	}
	restore := stubBuildInfo(t, &debug.BuildInfo{
		Main: debug.Module{Path: "example.com/main"},
		Deps: deps,
	}, true)
	defer restore()

	if got := len(Modules()); got != maxModules {
		t.Fatalf("Modules() returned %d entries, want the %d cap", got, maxModules)
	}
}

// TestCapabilitiesCarriesAboutPayload checks the additive keys reach the wire
// and — critically — that inventory_version was NOT bumped for them. Central
// gates collector composition on that number; moving it for an additive field
// would announce a contract change that never happened.
func TestCapabilitiesCarriesAboutPayload(t *testing.T) {
	caps := Capabilities()
	for _, key := range []string{"tool_paths", "build"} {
		if _, ok := caps[key]; !ok {
			t.Fatalf("capability advertisement missing %q", key)
		}
	}
	if caps["inventory_version"] != CapabilityVersion {
		t.Fatalf("inventory_version = %v, want %d (additive keys must not bump it)",
			caps["inventory_version"], CapabilityVersion)
	}
	build, ok := caps["build"].(map[string]any)
	if !ok {
		t.Fatalf("build = %T, want map[string]any", caps["build"])
	}
	if build["goos"] == nil {
		t.Fatalf("build block missing goos: %v", build)
	}
	if _, ok := caps["tool_paths"].(map[string]string); !ok {
		t.Fatalf("tool_paths = %T, want map[string]string", caps["tool_paths"])
	}
	// The real advertisement, on a real host, must fit the budget it sets
	// itself — otherwise every agent silently drops its module list.
	body, err := json.Marshal(caps)
	if err != nil {
		t.Fatalf("marshal capabilities: %v", err)
	}
	if len(body) > capabilitiesBudget {
		t.Fatalf("capabilities are %d bytes, over the %d budget", len(body), capabilitiesBudget)
	}
}

// TestCapabilitiesTrimsOversizeModules is the reason `modules_omitted` exists.
// Central drops an oversize capabilities body SILENTLY and keeps the poll
// successful, so an advertisement that grew past the cap would freeze the whole
// capability report with no error anywhere. The agent measures itself, drops
// the one big block, and says so.
func TestCapabilitiesTrimsOversizeModules(t *testing.T) {
	prev := moduleLister
	moduleLister = func() []map[string]string {
		out := make([]map[string]string, 0, maxModules)
		for i := range maxModules {
			out = append(out, map[string]string{
				// Long enough that 200 of them comfortably clear 12 KiB.
				"path":    fmt.Sprintf("example.com/very/long/module/path/segment/%04d/%s", i, strings.Repeat("x", 40)),
				"version": "v1.2.3-0.20260811120000-abcdef123456",
			})
		}
		return out
	}
	defer func() { moduleLister = prev }()

	caps := Capabilities()
	if _, ok := caps["modules"]; ok {
		t.Fatal("an oversize module list must be dropped, not sent")
	}
	if caps["modules_omitted"] != true {
		t.Fatalf("modules_omitted = %v, want true", caps["modules_omitted"])
	}
	body, err := json.Marshal(caps)
	if err != nil {
		t.Fatalf("marshal capabilities: %v", err)
	}
	if len(body) > capabilitiesBudget {
		t.Fatalf("trimmed capabilities are still %d bytes, over the %d budget",
			len(body), capabilitiesBudget)
	}
}

// TestCapabilitiesIncludesModulesWhenSmall is the other half: a module list that
// fits IS sent, and no omission flag is set (which would make the console claim
// data was suppressed when it was not).
func TestCapabilitiesIncludesModulesWhenSmall(t *testing.T) {
	prev := moduleLister
	moduleLister = func() []map[string]string {
		return []map[string]string{{"path": "example.com/tiny", "version": "v1.0.0"}}
	}
	defer func() { moduleLister = prev }()

	caps := Capabilities()
	mods, ok := caps["modules"].([]map[string]string)
	if !ok || len(mods) != 1 {
		t.Fatalf("modules = %v (%T), want the single small entry", caps["modules"], caps["modules"])
	}
	if _, ok := caps["modules_omitted"]; ok {
		t.Fatalf("modules_omitted must be absent when nothing was omitted: %v", caps["modules_omitted"])
	}
}
