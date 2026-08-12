package inventory

import (
	"path/filepath"
	"slices"
	"strings"
	"testing"
)

// allToolNames is every name ResolveTool is called with, plus an unrecognised
// one. The security assertions below run over the whole set, because the
// natural regression is a new case in the switch that quietly reintroduces a
// user-profile directory for one tool.
var allToolNames = []string{
	"ffmpeg", "ffprobe", "tesseract", "exiftool",
	"pdfinfo", "pdftotext", "pdftoppm",
	"definitely-not-a-tool",
}

// TestWellKnownNeverProbesUserProfile is a SECURITY test, not a tidiness one.
// The agent runs as LocalSystem. If it resolved a host tool out of a
// user-writable directory, any local user could drop their own exiftool.exe
// there and have SYSTEM execute it. A tool installed into a profile is
// therefore treated as absent by design — so no candidate directory may live
// under %LOCALAPPDATA%, %USERPROFILE%, or C:\Users.
func TestWellKnownNeverProbesUserProfile(t *testing.T) {
	t.Setenv("ProgramFiles", `C:\PF`)
	t.Setenv("ProgramFiles(x86)", `C:\PFx86`)
	t.Setenv("ProgramData", `C:\PD`)
	t.Setenv("LOCALAPPDATA", `C:\Users\op\AppData\Local`)
	t.Setenv("USERPROFILE", `C:\Users\op`)

	// %USERPROFILE% and %LOCALAPPDATA% are set to profile paths above, so any
	// entry derived from either is caught by these two substrings — including a
	// re-added per-user Scoop shim directory, which lives under %USERPROFILE%.
	forbidden := []string{`c:\users\`, `\appdata\`}
	for _, name := range allToolNames {
		for _, dir := range wellKnownDirs(name) {
			lower := strings.ToLower(dir)
			for _, bad := range forbidden {
				if strings.Contains(lower, bad) {
					t.Fatalf("%s: user-writable candidate %q (matched %q) - LocalSystem must never resolve tools from a profile", name, dir, bad)
				}
			}
		}
	}
}

// TestWellKnownUsesMachineWinget pins the positive half of the same fix: the
// MACHINE link farm is probed (that is where `winget --scope machine` puts
// shims) and the per-user one is not.
func TestWellKnownUsesMachineWinget(t *testing.T) {
	t.Setenv("ProgramFiles", `C:\PF`)
	t.Setenv("LOCALAPPDATA", `C:\Users\op\AppData\Local`)

	machine := filepath.Join(`C:\PF`, "WinGet", "Links")
	user := filepath.Join(`C:\Users\op\AppData\Local`, "Microsoft", "WinGet", "Links")

	for _, name := range allToolNames {
		dirs := wellKnownDirs(name)
		if !slices.Contains(dirs, machine) {
			t.Fatalf("%s: machine winget links %q missing from %v", name, machine, dirs)
		}
		if slices.Contains(dirs, user) {
			t.Fatalf("%s: per-user winget links %q must not be probed: %v", name, user, dirs)
		}
	}
}

// TestWingetPackagePatternsShape pins the explicit-depth globs. filepath.Glob
// has no `**`, so the two-level (portable with an inner versioned directory) and
// one-level forms both have to be spelled out — dropping the two-level poppler
// pattern is exactly the regression that hid oschwartz10612.Poppler.
func TestWingetPackagePatternsShape(t *testing.T) {
	root := filepath.Join(`C:\PF`, "WinGet", "Packages")

	poppler := wingetPackagePatterns(root, "pdftotext")
	wantPoppler := []string{
		filepath.Join(root, "*", "*", "Library", "bin"),
		filepath.Join(root, "*", "Library", "bin"),
	}
	if !slices.Equal(poppler, wantPoppler) {
		t.Fatalf("poppler patterns = %v, want %v", poppler, wantPoppler)
	}
	// Every poppler binary shares the package, so they must share the patterns.
	for _, name := range []string{"pdfinfo", "pdftoppm"} {
		if got := wingetPackagePatterns(root, name); !slices.Equal(got, wantPoppler) {
			t.Fatalf("%s patterns = %v, want %v", name, got, wantPoppler)
		}
	}

	ffmpeg := wingetPackagePatterns(root, "ffprobe")
	wantFFmpeg := []string{
		filepath.Join(root, "*", "*", "bin"),
		filepath.Join(root, "*", "bin"),
	}
	if !slices.Equal(ffmpeg, wantFFmpeg) {
		t.Fatalf("ffmpeg patterns = %v, want %v", ffmpeg, wantFFmpeg)
	}

	if got := wingetPackagePatterns(root, "tesseract"); got != nil {
		// Tesseract's winget package is a real installer, not a portable — it
		// lands in Program Files\Tesseract-OCR, already covered by the switch.
		t.Fatalf("tesseract patterns = %v, want nil", got)
	}
}

// TestWingetPackageRootIsMachineScope: the payload root is under Program Files,
// never %LOCALAPPDATA%.
func TestWingetPackageRootIsMachineScope(t *testing.T) {
	t.Setenv("ProgramFiles", `C:\PF`)
	t.Setenv("LOCALAPPDATA", `C:\Users\op\AppData\Local`)

	if got, want := wingetPackageRoot(), filepath.Join(`C:\PF`, "WinGet", "Packages"); got != want {
		t.Fatalf("winget package root = %q, want %q", got, want)
	}
}

// TestWellKnownUnknownToolIsStable: an unrecognised name contributes no
// tool-specific directories, so the answer is exactly the shared shims — and it
// does not vary between calls (the globbing must not leak state).
func TestWellKnownUnknownToolIsStable(t *testing.T) {
	t.Setenv("ProgramFiles", `C:\PF`)
	t.Setenv("ProgramData", `C:\PD`)

	want := []string{
		filepath.Join(`C:\PF`, "WinGet", "Links"),
		filepath.Join(`C:\PD`, "chocolatey", "bin"),
		filepath.Join(`C:\PD`, "scoop", "shims"),
	}
	first := wellKnownDirs("definitely-not-a-tool")
	if !slices.Equal(first, want) {
		t.Fatalf("unknown tool dirs = %v, want %v", first, want)
	}
	if second := wellKnownDirs("definitely-not-a-tool"); !slices.Equal(first, second) {
		t.Fatalf("unknown tool dirs not stable: %v then %v", first, second)
	}
}
