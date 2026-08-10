package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"runtime"
	"testing"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/inventory"
)

// fakeExecutable writes an executable stub and returns its path. The .exe
// suffix matters: exec.LookPath on Windows will not resolve an extensionless
// file, so a fixture without it would make every tool look absent.
func fakeExecutable(t *testing.T, dir, name string) string {
	t.Helper()
	if runtime.GOOS == "windows" {
		name += ".exe"
	}
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, []byte("#!/bin/sh\nexit 0\n"), 0o755); err != nil {
		t.Fatalf("write fake tool: %v", err)
	}
	return p
}

// writePolicyCache seeds <dataDir>/policy.json with a policy body, the same file
// the poller persists and the scan path reads.
func writePolicyCache(t *testing.T, dataDir, body string) {
	t.Helper()
	doc := agentcfg.PolicyDoc{
		ETag: "agent:test/3", Scope: "agent:test", Version: 3, AppliedVersion: 3,
		Policy: json.RawMessage(body),
	}
	buf, err := json.Marshal(doc)
	if err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(filepath.Join(dataDir, "policy.json"), buf, 0o600); err != nil {
		t.Fatal(err)
	}
}

func TestScanExtractFnGatedOnPolicy(t *testing.T) {
	tests := []struct {
		name string
		body string
		want bool // want a non-nil seam
	}{
		{name: "no policy key: extraction off", body: `{}`},
		{name: "explicitly disabled", body: `{"extract_enabled":false}`},
		{name: "enabled", body: `{"extract_enabled":true}`, want: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			dir := t.TempDir()
			writePolicyCache(t, dir, tc.body)
			got := scanExtractFn(dir)
			if (got != nil) != tc.want {
				t.Fatalf("seam non-nil = %v, want %v", got != nil, tc.want)
			}
		})
	}
}

// TestScanExtractFnWithoutPolicyCache: a never-contacted agent must scan
// normally, not fail, and never extract.
func TestScanExtractFnWithoutPolicyCache(t *testing.T) {
	if got := scanExtractFn(t.TempDir()); got != nil {
		t.Fatal("no cached policy should mean no extraction")
	}
}

func TestExtractOptionsResolveToolsOnlyWhenAsked(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(inventory.EnvTesseractPath, fakeExecutable(t, dir, "tess"))

	off, err := agentcfg.ParsePolicy(json.RawMessage(`{"extract_enabled":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if p := extractOptions(off).TesseractPath; p != "" {
		t.Errorf("tesseract resolved with OCR off: %q", p)
	}

	on, err := agentcfg.ParsePolicy(json.RawMessage(`{"extract_enabled":true,"extract_ocr":true}`))
	if err != nil {
		t.Fatal(err)
	}
	if p := extractOptions(on).TesseractPath; p == "" {
		t.Error("tesseract not resolved with OCR on")
	}
}

func TestExtractSnapshotReportsEffectiveValuesAndSource(t *testing.T) {
	dir := t.TempDir()
	writePolicyCache(t, dir, `{"extract_enabled":true,"extract_max_bytes":4096}`)

	snap := extractSnapshot(dir)
	eff := snap["effective"].(map[string]any)
	src := snap["source"].(map[string]any)

	if eff["extract_enabled"] != true {
		t.Errorf("extract_enabled = %v, want true", eff["extract_enabled"])
	}
	if eff["extract_max_bytes"] != int64(4096) {
		t.Errorf("extract_max_bytes = %v, want 4096", eff["extract_max_bytes"])
	}
	// An absent key falls back to the documented default, and says so.
	if eff["extract_body_text"] != false {
		t.Errorf("extract_body_text = %v, want false", eff["extract_body_text"])
	}
	if src["extract_body_text"] != "default" {
		t.Errorf("body-text source = %v, want default", src["extract_body_text"])
	}
	if src["extract_enabled"] != "central policy agent:test v3" {
		t.Errorf("enabled source = %v", src["extract_enabled"])
	}
	if _, ok := snap["ignored_settings"].([]map[string]string); !ok {
		t.Fatalf("ignored_settings = %T, want a (possibly empty) slice", snap["ignored_settings"])
	}
}

func TestExtractSnapshotWithoutPolicyCacheUsesDefaults(t *testing.T) {
	snap := extractSnapshot(t.TempDir())
	eff := snap["effective"].(map[string]any)
	if eff["extract_enabled"] != false {
		t.Errorf("never-contacted agent reports extraction on: %v", eff)
	}
	if eff["extract_max_bytes"] != agentcfg.DefaultExtractMaxBytes {
		t.Errorf("extract_max_bytes = %v, want the 32 MiB default", eff["extract_max_bytes"])
	}
	if snap["source"].(map[string]any)["extract_enabled"] != "default" {
		t.Errorf("source should be default: %v", snap["source"])
	}
}

func TestExtractSnapshotSurfacesIgnoredSettings(t *testing.T) {
	dir := t.TempDir()
	// OCR requested, but this test host is guaranteed to have no tesseract at
	// the (nonexistent) overridden path.
	t.Setenv(inventory.EnvTesseractPath, filepath.Join(dir, "absent-tesseract"))
	writePolicyCache(t, dir, `{"extract_enabled":true,"extract_ocr":true}`)

	list := extractSnapshot(dir)["ignored_settings"].([]map[string]string)
	found := false
	for _, it := range list {
		if it["key"] == "extract_ocr" {
			found = true
			if it["reason"] == "" {
				t.Error("ignored setting has no reason")
			}
		}
	}
	if !found {
		t.Fatalf("extract_ocr not reported as ignored: %v", list)
	}
}

func TestNewExtractFnRecordsPerExtractorErrors(t *testing.T) {
	dir := t.TempDir()
	broken := filepath.Join(dir, "broken.png")
	if err := os.WriteFile(broken, []byte("not a png"), 0o644); err != nil {
		t.Fatal(err)
	}
	pol, err := agentcfg.ParsePolicy(json.RawMessage(`{"extract_enabled":true}`))
	if err != nil {
		t.Fatal(err)
	}

	fn := newExtractFn(pol, newLogger())
	if fn == nil {
		t.Fatal("enabled policy produced no seam")
	}
	ex := fn(context.Background(), broken, "image")
	if ex == nil {
		t.Fatal("a failed extraction should still report the failure")
	}
	if ex.Meta["_extract_error"] == nil {
		t.Fatalf("no _extract_error recorded: %v", ex.Meta)
	}
	if ex.Meta["_extract_error_kind"] != "agent" {
		t.Errorf("_extract_error_kind = %v, want agent", ex.Meta["_extract_error_kind"])
	}
	if ex.Schema != 1 {
		t.Errorf("schema = %d, want 1", ex.Schema)
	}
}

// TestNewExtractFnSkipsUnreadableFile: a framing failure yields nil (no event
// field), never an error that could fail the scan.
func TestNewExtractFnSkipsUnreadableFile(t *testing.T) {
	pol, err := agentcfg.ParsePolicy(json.RawMessage(`{"extract_enabled":true}`))
	if err != nil {
		t.Fatal(err)
	}
	fn := newExtractFn(pol, newLogger())
	if got := fn(context.Background(), filepath.Join(t.TempDir(), "absent.png"), "image"); got != nil {
		t.Fatalf("missing file produced %+v, want nil", got)
	}
}
