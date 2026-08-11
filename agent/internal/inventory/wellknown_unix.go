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
// Deliberately NOT probed: /usr/games, ~/.local/bin (per-user, and a service
// running as root would resolve the wrong user's copy). The env override
// remains the escape hatch for anything unusual.

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
