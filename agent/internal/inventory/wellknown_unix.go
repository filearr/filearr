//go:build !windows && !darwin

package inventory

// Well-known install locations for the extraction host tools on Linux and the
// other Unixes.
//
// This is the platform where PATH is most likely to already be right — distro
// packages land in /usr/bin and a systemd unit inherits a usable PATH. It is
// still not guaranteed: a unit with an explicit `Environment=PATH=...`, a
// container with a trimmed PATH, or a Snap/Flatpak/Nix install all produce the
// same symptom as the Windows service case, so the same fallback applies.
//
// Deliberately NOT probed: /usr/games, and anything under $HOME (~/.local/bin,
// ~/bin, a per-user Nix or Flatpak profile). Every location here is
// MACHINE-WIDE, and that is a security rule rather than a preference — see
// wellknown_windows.go for the full statement. The agent commonly runs as a
// system service; resolving a host tool out of a user-writable directory would
// let a local user drop their own ffprobe there and have the service execute it
// as root. A tool installed only into a home directory is deliberately reported
// as absent until it is installed machine-wide, and the FILEARR_AGENT_*_PATH
// override remains the explicit, operator-chosen escape hatch for anything
// unusual.

// exeSuffix is empty on Unix — tool names are used verbatim.
const exeSuffix = ""

var unixToolDirs = []string{
	"/usr/bin",
	"/usr/local/bin",
	"/bin",
	"/usr/sbin",
	"/snap/bin",                         // Snap
	"/var/lib/flatpak/exports/bin",      // Flatpak, system-wide
	"/run/current-system/sw/bin",        // NixOS
	"/nix/var/nix/profiles/default/bin", // Nix on a non-NixOS host
}

func wellKnownDirs(_ string) []string { return unixToolDirs }
