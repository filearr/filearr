package inventory

// Well-known install locations for the extraction host tools on macOS.
//
// The service case is the reason this exists. A launchd job does not inherit a
// login shell's PATH — it gets a minimal one that historically did not include
// /usr/local/bin and certainly does not include Apple Silicon Homebrew's
// /opt/homebrew/bin. So the overwhelmingly common macOS setup ("brew install
// ffmpeg tesseract") produces a tool the operator can run in Terminal and the
// agent cannot see.
//
// Apple Silicon Homebrew first because it is the current default; Intel
// Homebrew's /usr/local/bin next; then MacPorts. Every candidate is verified by
// the caller with LookPath.
//
// Every location here is MACHINE-WIDE, and no $HOME-rooted directory is probed
// on any platform (see wellknown_windows.go for the full statement of the rule).
// The agent commonly runs as a system service; resolving a host tool out of a
// user-writable directory would let a local user drop their own ffprobe there
// and have the service execute it as root/SYSTEM. A tool installed only into a
// user's home is deliberately reported as absent until it is installed
// machine-wide — the FILEARR_AGENT_*_PATH overrides remain the explicit,
// operator-chosen escape hatch.
//
// Windows narrowed this further on 2026-08-11 (candidates must be under
// %ProgramFiles%, the only conventional location whose default ACL denies
// non-admin writes). That was a WINDOWS-ACL-specific decision and is not
// mirrored here: /usr/local/bin is writable by the admin group on macOS, which
// is a known and accepted difference — essentially every Homebrew install on an
// Intel Mac lives there, so dropping it would break almost all macOS agents.
// POSIX agents also commonly run as root with a curated PATH rather than
// inheriting an arbitrary one.

import "path/filepath"

// exeSuffix is empty on Unix — tool names are used verbatim.
const exeSuffix = ""

// homebrewAndFriends are the package-manager bin directories every one of these
// tools lands in on macOS; none of the seven installs anywhere exotic, so there
// is no per-tool special-casing to do here (unlike Windows).
var homebrewAndFriends = []string{
	"/opt/homebrew/bin", // Homebrew, Apple Silicon
	"/usr/local/bin",    // Homebrew, Intel — and the conventional manual install
	"/opt/local/bin",    // MacPorts
	"/usr/bin",          // system (only pdf* ship here, and only sometimes)
	"/sw/bin",           // Fink, rare but harmless to probe
}

func wellKnownDirs(name string) []string {
	dirs := make([]string, 0, len(homebrewAndFriends)+1)
	dirs = append(dirs, homebrewAndFriends...)
	// Homebrew keeps tesseract's language data next to the binary but some
	// installs expose the binary only through the Cellar; the opt symlink is
	// stable across versions where the versioned Cellar path is not.
	if name == "tesseract" {
		dirs = append(dirs, filepath.Join("/opt/homebrew/opt/tesseract/bin"))
	}
	return dirs
}
