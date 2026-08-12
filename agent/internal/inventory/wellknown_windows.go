package inventory

// Well-known install locations for the extraction host tools on Windows.
//
// Why this file exists: none of these tools reliably put themselves on PATH,
// and the agent normally runs as a SERVICE, whose PATH is the machine
// environment rather than the operator's shell. The combination means an
// operator can install Tesseract, see it work in their terminal, and still have
// the console report "not installed" — which is exactly what happened on a live
// Windows agent (2026-08-10). The UB-Mannheim Tesseract installer in particular
// offers no "add to PATH" option at all.
//
// EVERY location here is MACHINE-WIDE and admin-writable. That is a security
// rule, not a preference. The service runs as LocalSystem; resolving an
// executable out of a user-writable directory would let any local user drop
// their own exiftool.exe or ffprobe.exe into their profile and have SYSTEM
// execute it — a plain local privilege-escalation path. So a tool installed
// into a user profile is deliberately treated as NOT INSTALLED: the console
// reports it absent, and it stays absent until someone installs it machine-wide.
// Do not "helpfully" re-add %LOCALAPPDATA%\Microsoft\WinGet\Links, a Scoop
// per-user shim directory, or %LOCALAPPDATA%\Programs — the negative is pinned
// by TestWellKnownNeverProbesUserProfile.
//
// This bites in one specific way (2026-08-11): `winget install` without a scope
// flag defaults to USER scope, so our own installers used to put these tools in
// the operator's %LOCALAPPDATA%\Microsoft\WinGet\{Links,Packages} — invisible to
// the service, which was reporting exiftool and poppler absent while ffmpeg and
// tesseract (installed machine-wide by hand) resolved fine. The fix is on the
// install side: the installers now pass `--scope machine`, and the machine
// WinGet entries below are the detection half of it.
//
// A hit is still verified with LookPath by the caller, so a leftover directory
// never yields an unusable path.

import (
	"os"
	"path/filepath"
)

// exeSuffix is appended to a bare tool name when probing a directory.
const exeSuffix = ".exe"

func wellKnownDirs(name string) []string {
	programFiles := envOrDefault("ProgramFiles", `C:\Program Files`)
	programFilesX86 := envOrDefault("ProgramFiles(x86)", `C:\Program Files (x86)`)
	programData := envOrDefault("ProgramData", `C:\ProgramData`)

	// Package-manager shim directories, shared by every tool. Only the
	// machine-wide farms: winget's `--scope machine` link directory,
	// Chocolatey's bin (always machine-wide), and Scoop's GLOBAL shims
	// ($SCOOP_GLOBAL, admin-installed). Their per-user counterparts under
	// %LOCALAPPDATA% / %USERPROFILE% are excluded on purpose — see the header.
	shims := []string{
		filepath.Join(programFiles, "WinGet", "Links"),
		filepath.Join(programData, "chocolatey", "bin"),
		filepath.Join(programData, "scoop", "shims"),
	}

	var specific []string
	switch name {
	case "tesseract":
		// The UB-Mannheim build is the de-facto Windows distribution and installs
		// here. It does NOT modify PATH.
		specific = []string{
			filepath.Join(programFiles, "Tesseract-OCR"),
			filepath.Join(programFilesX86, "Tesseract-OCR"),
		}
	case "exiftool":
		// The official distribution is a zip the user extracts; these are the
		// conventional destinations. Note the executable inside the zip is named
		// exiftool(-k).exe and must be renamed to exiftool.exe to be useful —
		// documented rather than guessed at here, because probing for the (-k)
		// form would find a binary that pauses for a keypress and hangs the pass.
		specific = []string{
			filepath.Join(programFiles, "ExifTool"),
			`C:\exiftool`,
		}
	case "ffmpeg", "ffprobe":
		specific = []string{
			filepath.Join(programFiles, "ffmpeg", "bin"),
			`C:\ffmpeg\bin`,
		}
		specific = append(specific, wingetPackageDirs(name)...)
	case "pdfinfo", "pdftotext", "pdftoppm":
		// Poppler's Windows releases unpack to a VERSIONED directory
		// (poppler-24.02.0\Library\bin), so a fixed path cannot find them —
		// glob instead and take the newest-sorting match.
		specific = append(
			globDirs(filepath.Join(programFiles, "poppler*", "Library", "bin")),
			globDirs(`C:\poppler*\Library\bin`)...,
		)
		specific = append(specific,
			filepath.Join(programFiles, "poppler", "bin"),
			`C:\poppler\bin`,
		)
		specific = append(specific, wingetPackageDirs(name)...)
	}
	return append(specific, shims...)
}

// wingetPackageRoot is where a `--scope machine` winget install unpacks package
// payloads. The user-scope root (%LOCALAPPDATA%\Microsoft\WinGet\Packages) is
// NOT probed — see the header: LocalSystem must not execute out of a profile.
func wingetPackageRoot() string {
	return filepath.Join(envOrDefault("ProgramFiles", `C:\Program Files`), "WinGet", "Packages")
}

// wingetPackagePatterns returns the glob patterns that locate `name` inside a
// winget PORTABLE package payload under `root`.
//
// Portables unpack to <root>\<PackageId>_<SourceHash>\<inner-versioned-dir>\...,
// where the inner directory is whatever the upstream archive contained
// (poppler-24.02.0\Library\bin, ffmpeg-7.1-essentials_build\bin). Some packages
// have no inner directory at all, so BOTH depths are probed. Go's filepath.Glob
// has no `**` operator — the depths must be spelled out, which is why this is a
// list of explicit two-level and one-level patterns rather than one recursive
// walk.
func wingetPackagePatterns(root, name string) []string {
	switch name {
	case "pdfinfo", "pdftotext", "pdftoppm":
		return []string{
			filepath.Join(root, "*", "*", "Library", "bin"),
			filepath.Join(root, "*", "Library", "bin"),
		}
	case "ffmpeg", "ffprobe":
		return []string{
			filepath.Join(root, "*", "*", "bin"),
			filepath.Join(root, "*", "bin"),
		}
	}
	return nil
}

// wingetPackageDirs expands wingetPackagePatterns over the machine package root,
// deeper pattern before shallower.
func wingetPackageDirs(name string) []string {
	var out []string
	for _, pattern := range wingetPackagePatterns(wingetPackageRoot(), name) {
		out = append(out, globDirs(pattern)...)
	}
	return out
}

func envOrDefault(key, def string) string {
	if v := os.Getenv(key); v != "" {
		return v
	}
	return def
}

// globDirs expands a glob to the directories that matched, newest-sorting last
// entry first so a machine with several poppler versions installed prefers the
// highest-numbered one. Errors are swallowed: a malformed pattern or an
// unreadable directory simply contributes no candidates.
func globDirs(pattern string) []string {
	matches, err := filepath.Glob(pattern)
	if err != nil || len(matches) == 0 {
		return nil
	}
	// Glob returns lexical order; reverse it so poppler-24.* beats poppler-9.*
	// for the common same-width version numbering these releases use.
	out := make([]string, 0, len(matches))
	for i := len(matches) - 1; i >= 0; i-- {
		out = append(out, matches[i])
	}
	return out
}
