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
// Ordering is deliberate: machine-wide installs first (what a service can
// actually see), then per-user package-manager shims, which are only reachable
// when the service runs as that user. A hit is still verified with LookPath by
// the caller, so a leftover directory never yields an unusable path.

import (
	"os"
	"path/filepath"
)

// exeSuffix is appended to a bare tool name when probing a directory.
const exeSuffix = ".exe"

func wellKnownDirs(name string) []string {
	programFiles := envOrDefault("ProgramFiles", `C:\Program Files`)
	programFilesX86 := envOrDefault("ProgramFiles(x86)", `C:\Program Files (x86)`)
	localAppData := os.Getenv("LOCALAPPDATA")
	programData := envOrDefault("ProgramData", `C:\ProgramData`)
	userProfile := os.Getenv("USERPROFILE")

	// Package-manager shim directories, shared by every tool: winget's link farm,
	// Chocolatey's bin, and Scoop's shims. These are how most people actually end
	// up with ffmpeg on Windows.
	shims := []string{
		filepath.Join(localAppData, "Microsoft", "WinGet", "Links"),
		filepath.Join(programData, "chocolatey", "bin"),
		filepath.Join(userProfile, "scoop", "shims"),
	}

	var specific []string
	switch name {
	case "tesseract":
		// The UB-Mannheim build is the de-facto Windows distribution and installs
		// here. It does NOT modify PATH.
		specific = []string{
			filepath.Join(programFiles, "Tesseract-OCR"),
			filepath.Join(programFilesX86, "Tesseract-OCR"),
			filepath.Join(localAppData, "Programs", "Tesseract-OCR"),
		}
	case "exiftool":
		// The official distribution is a zip the user extracts; these are the
		// conventional destinations. Note the executable inside the zip is named
		// exiftool(-k).exe and must be renamed to exiftool.exe to be useful —
		// documented rather than guessed at here, because probing for the (-k)
		// form would find a binary that pauses for a keypress and hangs the pass.
		specific = []string{
			filepath.Join(programFiles, "ExifTool"),
			filepath.Join(localAppData, "Programs", "ExifTool"),
			`C:\exiftool`,
		}
	case "ffmpeg", "ffprobe":
		specific = []string{
			filepath.Join(programFiles, "ffmpeg", "bin"),
			`C:\ffmpeg\bin`,
		}
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
	}
	return append(specific, shims...)
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
