package inventory

import (
	"os"
	"path/filepath"
	"runtime"
	"slices"
	"testing"
)

// fakeTool writes an executable stub into dir and returns its path. It is how
// these tests get a deterministic "tool present" answer without depending on
// what happens to be installed on the machine running them.
func fakeTool(t *testing.T, dir, name string) string {
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

func TestResolveToolEnvOverride(t *testing.T) {
	dir := t.TempDir()
	real := fakeTool(t, dir, "pretend-ffprobe")

	tests := []struct {
		name     string
		override string
		want     string
	}{
		{name: "absolute override resolves", override: real, want: real},
		{name: "absent override resolves to empty", override: filepath.Join(dir, "nope"), want: ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			t.Setenv(EnvFFprobePath, tc.override)
			if got := FFprobePath(); got != tc.want {
				t.Fatalf("FFprobePath() = %q, want %q", got, tc.want)
			}
			if HasFFprobe() != (tc.want != "") {
				t.Fatalf("HasFFprobe() = %v, want %v", HasFFprobe(), tc.want != "")
			}
		})
	}
}

// TestResolveToolCacheKeyedOnOverride pins the cache design: memoization must be
// keyed on the override VALUE, so changing the environment yields a fresh
// lookup. A cache keyed on the tool name alone would make the first test in a
// process poison every later one.
func TestResolveToolCacheKeyedOnOverride(t *testing.T) {
	dir := t.TempDir()
	a := fakeTool(t, dir, "tool-a")
	b := fakeTool(t, dir, "tool-b")

	t.Setenv(EnvExiftoolPath, a)
	if got := ExiftoolPath(); got != a {
		t.Fatalf("first resolve = %q, want %q", got, a)
	}
	// Second resolve of the SAME value must hit the cache and agree.
	if got := ExiftoolPath(); got != a {
		t.Fatalf("cached resolve = %q, want %q", got, a)
	}
	t.Setenv(EnvExiftoolPath, b)
	if got := ExiftoolPath(); got != b {
		t.Fatalf("resolve after override change = %q, want %q", got, b)
	}
}

func TestToolsMatrixShape(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(EnvTesseractPath, fakeTool(t, dir, "tess"))
	t.Setenv(EnvExiftoolPath, filepath.Join(dir, "absent-exiftool"))

	tools := Tools()
	for _, key := range []string{"ffmpeg", "ffprobe", "tesseract", "exiftool"} {
		if _, ok := tools[key]; !ok {
			t.Fatalf("tools matrix missing %q: %v", key, tools)
		}
	}
	if !tools["tesseract"] {
		t.Fatal("tesseract should be present via the env override")
	}
	if tools["exiftool"] {
		t.Fatal("exiftool override points at a nonexistent file; want absent")
	}
}

func TestExtractFormatsFollowsFFprobe(t *testing.T) {
	dir := t.TempDir()

	t.Setenv(EnvFFprobePath, filepath.Join(dir, "absent-ffprobe"))
	got := ExtractFormats()
	want := []string{"archive", "audio", "document", "image"}
	if !slices.Equal(got, want) {
		t.Fatalf("formats without ffprobe = %v, want %v", got, want)
	}

	t.Setenv(EnvFFprobePath, fakeTool(t, dir, "probe"))
	got = ExtractFormats()
	want = []string{"archive", "audio", "document", "image", "video"}
	if !slices.Equal(got, want) {
		t.Fatalf("formats with ffprobe = %v, want %v", got, want)
	}
}

func TestCapabilitiesAdvertisesExtraction(t *testing.T) {
	caps := Capabilities()

	if caps["extract"] != true {
		t.Fatalf("extract = %v, want true", caps["extract"])
	}
	if caps["extract_schema"] != 1 {
		t.Fatalf("extract_schema = %v, want 1", caps["extract_schema"])
	}
	tools, ok := caps["tools"].(map[string]bool)
	if !ok {
		t.Fatalf("tools = %T, want map[string]bool", caps["tools"])
	}
	if _, ok := tools["ffprobe"]; !ok {
		t.Fatalf("tools missing ffprobe: %v", tools)
	}
	formats, ok := caps["formats"].([]string)
	if !ok || len(formats) == 0 {
		t.Fatalf("formats = %v (%T), want a non-empty []string", caps["formats"], caps["formats"])
	}
	// The pre-existing keys must survive: older centrals read them.
	for _, key := range []string{"inventory_collectors", "inventory_version", "ffmpeg", "container"} {
		if _, ok := caps[key]; !ok {
			t.Fatalf("capability advertisement dropped %q", key)
		}
	}
}
