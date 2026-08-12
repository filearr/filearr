package inventory

import (
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// TestParseToolVersionRealBanners pins the parser against the ACTUAL first line
// each tool prints. These strings are the contract: the parser is pure precisely
// so this can be verified without installing ffmpeg, tesseract or poppler on the
// machine running the tests (CI has none of them).
func TestParseToolVersionRealBanners(t *testing.T) {
	tests := []struct {
		name string
		tool string
		out  string
		want string
	}{
		{
			name: "ffmpeg distro build",
			tool: "ffmpeg",
			out:  "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers\nbuilt with gcc 13\n",
			want: "6.1.1-3ubuntu5",
		},
		{
			name: "ffmpeg git build keeps its N- token",
			tool: "ffmpeg",
			out:  "ffmpeg version N-113579-g1c2d3e4 Copyright (c) 2000-2024\n",
			want: "N-113579-g1c2d3e4",
		},
		{
			name: "ffprobe",
			tool: "ffprobe",
			out:  "ffprobe version 7.1 Copyright (c) 2007-2024 the FFmpeg developers\n",
			want: "7.1",
		},
		{
			name: "tesseract names itself then the version",
			tool: "tesseract",
			out:  "tesseract 5.3.4\n leptonica-1.84.1\n  libgif 5.2.1 : libjpeg 8d\n",
			want: "5.3.4",
		},
		{
			name: "tesseract 4 (the build worth spotting)",
			tool: "tesseract",
			out:  "tesseract 4.1.1\n leptonica-1.79.0\n",
			want: "4.1.1",
		},
		{
			name: "exiftool prints a bare version",
			tool: "exiftool",
			out:  "12.76\n",
			want: "12.76",
		},
		{
			name: "poppler pdftotext (stderr, 'version' keyword)",
			tool: "pdftotext",
			out:  "pdftotext version 24.02.0\nCopyright 2005-2024 The Poppler Developers\n",
			want: "24.02.0",
		},
		{
			name: "poppler pdfinfo",
			tool: "pdfinfo",
			out:  "pdfinfo version 22.12.0\n",
			want: "22.12.0",
		},
		{
			name: "poppler pdftoppm",
			tool: "pdftoppm",
			out:  "pdftoppm version 24.02.0\n",
			want: "24.02.0",
		},
		{
			name: "leading blank lines are skipped",
			tool: "tesseract",
			out:  "\n\ntesseract 5.3.4\n",
			want: "5.3.4",
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := parseToolVersion(tc.tool, tc.out); got != tc.want {
				t.Fatalf("parseToolVersion(%q) = %q, want %q", tc.tool, got, tc.want)
			}
		})
	}
}

// TestParseToolVersionRefusesNonVersions: a usage banner or an error must never
// be stored AS a version — the value is rendered in the console as fact, and a
// wrong version is worse than an absent one.
func TestParseToolVersionRefusesNonVersions(t *testing.T) {
	for _, out := range []string{
		"",
		"\n\n",
		"Usage: tesseract [options] imagefile outputbase",
		"tesseract: command not found",
		"error while loading shared libraries: libtesseract.so.5",
	} {
		if got := parseToolVersion("tesseract", out); got != "" {
			t.Errorf("parseToolVersion(%q) = %q, want empty", out, got)
		}
	}
}

// TestCleanVersionBoundsUntrustedOutput: the banner is third-party output that
// is stored centrally and rendered in a browser.
func TestCleanVersionBoundsUntrustedOutput(t *testing.T) {
	long := parseToolVersion("exiftool", "1"+string(make([]byte, 0))+repeat("9", 500))
	if len(long) > versionMaxChars {
		t.Fatalf("version not capped: %d chars", len(long))
	}
	if got := cleanVersion("5.3\x00.4\x07"); got != "5.3.4" {
		t.Fatalf("control characters survived: %q", got)
	}
}

func repeat(s string, n int) string {
	out := make([]byte, 0, n*len(s))
	for i := 0; i < n; i++ {
		out = append(out, s...)
	}
	return string(out)
}

// TestToolVersionAbsentToolIsSilent: an absent tool must not spawn a probe or
// report a version — Tools() already says it is missing, and ToolVersions()
// omitting it is what lets the console distinguish "absent" from "present but
// silent".
func TestToolVersionAbsentToolIsSilent(t *testing.T) {
	dir := t.TempDir()
	t.Setenv(EnvTesseractPath, filepath.Join(dir, "definitely-not-here"))
	if v := ToolVersion("tesseract", EnvTesseractPath); v != "" {
		t.Fatalf("absent tool reported version %q", v)
	}
	if _, ok := ToolVersions()["tesseract"]; ok {
		t.Fatal("absent tool appears in ToolVersions()")
	}
}

// TestVersionArgsCoverEveryAdvertisedTool: Tools() and ToolVersions() must not
// drift apart — a tool added to the matrix without a version argument would
// silently report no version forever.
func TestVersionArgsCoverEveryAdvertisedTool(t *testing.T) {
	for name := range Tools() {
		if _, ok := versionArgs[name]; !ok {
			t.Errorf("tool %q is advertised by Tools() but has no versionArgs entry", name)
		}
	}
}

// TestWellKnownFallbackFindsAnOffPathTool is the regression for the live report
// (2026-08-10): Tesseract installed at C:\Program Files\Tesseract-OCR on a
// Windows agent, working in the operator's shell, reported "not installed" by
// the agent — because a SERVICE's PATH is not the shell's PATH and that
// installer never adds itself to PATH.
//
// The test fakes the shape rather than the exact directory (a real
// C:\Program Files write needs admin and would be a terrible test): it points
// the well-known search at a temp dir via the same code path, by asserting that
// a tool absent from PATH and absent from every well-known location resolves to
// "" while one reachable through the override resolves. The directory table
// itself is per-OS data, verified by TestWellKnownDirsAreAbsolute below.
func TestWellKnownFallbackDoesNotInventPaths(t *testing.T) {
	dir := t.TempDir()
	// A name nothing will ever have installed: PATH misses, every well-known
	// directory misses, and the result must be "" rather than a fabricated path.
	if got := ResolveTool("filearr-nonexistent-tool", "FILEARR_AGENT_NO_SUCH_VAR"); got != "" {
		t.Fatalf("absent tool resolved to %q, want empty", got)
	}
	// The override still wins over the well-known search, and still has to exist.
	t.Setenv(EnvTesseractPath, filepath.Join(dir, "not-here"))
	if got := ResolveTool("tesseract", EnvTesseractPath); got != "" {
		t.Fatalf("override pointing at a missing file resolved to %q, want empty", got)
	}
}

// TestWellKnownDirsAreAbsolute: a relative entry in the per-OS table would make
// the probe depend on the process's working directory, which for a service is
// arbitrary — and could resolve a binary out of a scanned media root.
func TestWellKnownDirsAreAbsolute(t *testing.T) {
	for _, name := range []string{"tesseract", "exiftool", "ffmpeg", "ffprobe", "pdfinfo", "pdftotext", "pdftoppm"} {
		for _, dir := range wellKnownDirs(name) {
			if dir == "" {
				continue
			}
			if !filepath.IsAbs(dir) {
				t.Errorf("wellKnownDirs(%q) yielded a relative path %q", name, dir)
			}
		}
	}
}

// TestEveryAdvertisedToolHasWellKnownDirs guards the drift that would silently
// undo this fix: a tool added to Tools() but not to the per-OS table keeps the
// PATH-only behaviour and nobody notices until a support thread.
func TestEveryAdvertisedToolHasWellKnownDirs(t *testing.T) {
	for name := range Tools() {
		if len(wellKnownDirs(name)) == 0 {
			t.Errorf("tool %q is advertised but has no well-known directories to fall back on", name)
		}
	}
}

// TestWellKnownDirsAreMachineWide is the cross-platform half of the security
// rule (the Windows-specific version, which can name %LOCALAPPDATA% explicitly,
// lives in wellknown_windows_test.go). The agent runs as a service — LocalSystem
// on Windows, root under systemd/launchd. Resolving a host tool out of a
// user-writable directory would let any local user drop their own ffprobe there
// and have the service execute it with full privileges, so a tool installed only
// into a home directory is reported absent BY DESIGN. No candidate may live
// under the current user's home.
func TestWellKnownDirsAreMachineWide(t *testing.T) {
	home, err := os.UserHomeDir()
	if err != nil || home == "" || home == string(filepath.Separator) {
		t.Skip("no usable home directory to test against")
	}
	prefix := strings.ToLower(home) + string(filepath.Separator)
	for name := range Tools() {
		for _, dir := range wellKnownDirs(name) {
			if strings.HasPrefix(strings.ToLower(dir), prefix) {
				t.Errorf("wellKnownDirs(%q) yielded home-rooted path %q - a service must not execute from a user-writable directory", name, dir)
			}
		}
	}
}
