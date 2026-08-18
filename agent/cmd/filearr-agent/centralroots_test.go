package main

import (
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"testing"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

// Live 2026-08-18: central scan_selections were resolved but NEVER written to
// scan.json ("seam only"), so a centrally configured agent scanned nothing while
// its local roots editor refused edits as managed_by_central. These pin the
// wiring: policy selections -> scan.json roots; no selections -> local roots
// untouched; unchanged roots -> no rewrite.
func seedPolicy(t *testing.T, dataDir string, selections []any) {
	t.Helper()
	body := map[string]any{"group": map[string]any{"scan_selections": selections}}
	raw, _ := json.Marshal(body)
	doc := agentcfg.PolicyDoc{Policy: raw, Version: 1, AppliedVersion: 1}
	if err := agentcfg.NewETagCache(dataDir).Save(doc); err != nil {
		t.Fatal(err)
	}
}

func readRoots(t *testing.T, dataDir string) []string {
	t.Helper()
	buf, err := os.ReadFile(filepath.Join(dataDir, scanConfigName))
	if os.IsNotExist(err) {
		return nil
	}
	if err != nil {
		t.Fatal(err)
	}
	var sc scanConfig
	if err := json.Unmarshal(buf, &sc); err != nil {
		t.Fatal(err)
	}
	return sc.Roots
}

func TestApplyCentralScanRootsWritesScanJSON(t *testing.T) {
	dataDir := t.TempDir()
	media := filepath.Join(t.TempDir(), "media")
	if err := os.MkdirAll(media, 0o755); err != nil {
		t.Fatal(err)
	}
	// A pre-existing LOCAL root: central selections replace it (the local
	// editor is locked as managed_by_central for exactly this reason).
	if err := writeScanConfig(filepath.Join(dataDir, scanConfigName), scanConfig{
		Roots: []string{filepath.Join(t.TempDir(), "old")}, Presets: []string{"video"},
	}); err != nil {
		t.Fatal(err)
	}
	seedPolicy(t, dataDir, []any{map[string]any{"preset": "custom", "paths": []any{media}}})

	roots, managed := applyCentralScanRoots(dataDir, slog.Default())
	if !managed || len(roots) != 1 || roots[0] != media {
		t.Fatalf("expected managed roots [%s], got managed=%v roots=%v", media, managed, roots)
	}
	got := readRoots(t, dataDir)
	if len(got) != 1 || got[0] != media {
		t.Fatalf("scan.json roots not derived from policy: %v", got)
	}
	// Other scan.json fields survive.
	buf, _ := os.ReadFile(filepath.Join(dataDir, scanConfigName))
	var sc scanConfig
	_ = json.Unmarshal(buf, &sc)
	if len(sc.Presets) != 1 || sc.Presets[0] != "video" {
		t.Fatalf("presets clobbered: %+v", sc)
	}
	// Idempotent: a second apply with the same policy does not rewrite.
	st1, _ := os.Stat(filepath.Join(dataDir, scanConfigName))
	if changed, err := setCentralScanRoots(dataDir, roots); err != nil || changed {
		t.Fatalf("second apply should be a no-op: changed=%v err=%v", changed, err)
	}
	st2, _ := os.Stat(filepath.Join(dataDir, scanConfigName))
	if !st1.ModTime().Equal(st2.ModTime()) {
		t.Fatalf("scan.json rewritten without a change")
	}
}

func TestApplyCentralScanRootsNoSelectionsLeavesLocalRoots(t *testing.T) {
	dataDir := t.TempDir()
	local := filepath.Join(t.TempDir(), "local")
	if err := writeScanConfig(filepath.Join(dataDir, scanConfigName), scanConfig{Roots: []string{local}}); err != nil {
		t.Fatal(err)
	}
	seedPolicy(t, dataDir, []any{}) // policy present, no selections
	if roots, managed := applyCentralScanRoots(dataDir, slog.Default()); managed || roots != nil {
		t.Fatalf("no selections must not manage roots: %v %v", managed, roots)
	}
	if got := readRoots(t, dataDir); len(got) != 1 || got[0] != local {
		t.Fatalf("local roots must be untouched: %v", got)
	}
	// No cached policy at all -> also untouched, no error.
	if roots, managed := applyCentralScanRoots(t.TempDir(), slog.Default()); managed || roots != nil {
		t.Fatalf("no policy must not manage roots: %v %v", managed, roots)
	}
}
