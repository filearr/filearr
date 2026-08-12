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
// THE RULE (tightened 2026-08-11): automatic discovery is limited to
// directories WHOSE DEFAULT ACL DENIES NON-ADMIN WRITES. In practice that means
// exactly one root — %ProgramFiles% and its (x86) twin, whose default ACL grants
// write only to Administrators, SYSTEM and TrustedInstaller. Nothing else is
// probed.
//
// Why, in one sentence: the service runs as LocalSystem, so any directory a
// non-admin can write is a directory a non-admin can use to make SYSTEM run
// their code.
//
// The first cut of this rule (earlier the same day) said "machine-wide" and kept
// %ProgramData%\chocolatey\bin, %ProgramData%\scoop\shims, C:\ffmpeg\bin,
// C:\exiftool and C:\poppler*. Machine-wide is not the same property as
// admin-only. Both C:\ and C:\ProgramData grant Authenticated Users the right to
// CREATE SUBDIRECTORIES by default (and the creator owns what it creates), so a
// well-known directory that DOES NOT EXIST YET on a given host — no Scoop
// installed, no C:\ffmpeg — can be created and populated by any local user and
// then executed by SYSTEM on the next extraction pass. That is the same shape as
// the user-profile hole closed earlier today, one step weaker: it needs the
// directory to be absent rather than the attacker to own it. So the whole class
// is gone rather than enumerated.
//
// It also stays true for the ANCESTORS of the entries kept here. A per-tool
// directory under Program Files inherits Program Files' ACL, so an attacker
// cannot pre-create C:\Program Files\ExifTool either.
//
// This is DEFENCE IN DEPTH layered on top of the PATH lookup, not the primary
// discovery mechanism — and that is why it costs nothing in practice.
// ResolveTool tries the MACHINE PATH FIRST, and every package manager that owns
// a shim directory puts that directory on the machine PATH as part of installing
// itself: Chocolatey (%ProgramData%\chocolatey\bin), global Scoop
// (%ProgramData%\scoop\shims), winget (its Links farm). Those installs still
// resolve, through PATH, exactly as before. The well-known list below only
// matters for installers that skip PATH entirely — and those land in Program
// Files, the UB-Mannheim Tesseract installer being the canonical example (it
// offers no "add to PATH" option at all, which is what produced the live
// 2026-08-10 report of a working-in-my-terminal tesseract the agent called
// absent). A tool in a genuinely non-standard location is still reachable two
// ways: put its directory on the MACHINE PATH, or set that tool's
// FILEARR_AGENT_*_PATH override (see hostToolEnvs in tools.go). Those are the
// two escape hatches, and they are deliberate operator choices rather than
// something the agent guesses at.
//
// So do not "helpfully" re-add a convenience path. Not
// %LOCALAPPDATA%\Microsoft\WinGet\Links, not a Scoop shim directory of either
// scope, not C:\ffmpeg\bin. The negatives are pinned by
// TestWellKnownNeverProbesUserProfile, TestWellKnownOnlyProbesProgramFiles and
// TestWellKnownDropsUserCreatableMachinePaths.
//
// A tool installed into a user profile, or into an attacker-creatable directory,
// is therefore deliberately treated as NOT INSTALLED — the console reports it
// absent and it stays absent until it is installed somewhere the service is
// willing to execute from. This bit us on the install side too (2026-08-11):
// `winget install` without a scope flag defaults to USER scope, so our own
// installers used to put these tools in the operator's
// %LOCALAPPDATA%\Microsoft\WinGet\{Links,Packages} — invisible to the service,
// which reported exiftool and poppler absent while ffmpeg and tesseract
// (installed machine-wide by hand) resolved fine. The installers now pass
// `--scope machine`, and the machine WinGet entries below are the detection half
// of that fix.
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

	// Package-manager shim directories, shared by every tool. Only winget's
	// `--scope machine` link farm survives, because it is the only one under
	// Program Files. Chocolatey's bin and Scoop's global shims both live under
	// %ProgramData%, which any Authenticated User may create subdirectories in —
	// excluded on purpose (see the header). Both put themselves on the MACHINE
	// PATH when installed, which is tried first, so those installs still resolve.
	shims := []string{
		filepath.Join(programFiles, "WinGet", "Links"),
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
		// The official distribution is a zip the user extracts; Program Files is
		// the conventional destination. (C:\exiftool, the other convention, is
		// gone: C:\ lets any user create it when it does not already exist.) Note
		// the executable inside the zip is named exiftool(-k).exe and must be
		// renamed to exiftool.exe to be useful — documented rather than guessed at
		// here, because probing for the (-k) form would find a binary that pauses
		// for a keypress and hangs the pass.
		specific = []string{
			filepath.Join(programFiles, "ExifTool"),
		}
	case "ffmpeg", "ffprobe":
		// C:\ffmpeg\bin, the other widespread convention, is deliberately not
		// probed — see the header. An unpack there is reachable via the machine
		// PATH or FILEARR_AGENT_FFMPEG_PATH.
		specific = []string{
			filepath.Join(programFiles, "ffmpeg", "bin"),
		}
		specific = append(specific, wingetPackageDirs(name)...)
	case "pdfinfo", "pdftotext", "pdftoppm":
		// Poppler's Windows releases unpack to a VERSIONED directory
		// (poppler-24.02.0\Library\bin), so a fixed path cannot find them —
		// glob instead and take the newest-sorting match. Only under Program
		// Files; the C:\poppler* forms are gone for the reason in the header.
		specific = globDirs(filepath.Join(programFiles, "poppler*", "Library", "bin"))
		specific = append(specific,
			filepath.Join(programFiles, "poppler", "bin"),
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
