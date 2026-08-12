package inventory

// Host OS VERSION reporting for the per-agent About view (2026-08-11).
//
// "platform: windows" is already on the agent row and is not the fact anyone
// needs. "Windows 10.0 (build 26100)" versus "Windows 10.0 (build 19045)", or
// "Ubuntu 22.04.4 LTS" versus "Ubuntu 24.04.1 LTS", is what explains why two
// agents built from the same commit behave differently — and in the host-tool
// case it is often the WHOLE explanation, because a distro's package repository
// is what pins tesseract to 4.1.1 (see docs-site/agents.md on minimums).
//
// Cached for the life of the process, permanently and without a TTL, unlike the
// host-tool caches next door: a machine's OS version cannot change while this
// process is running — the upgrade that changes it reboots the box and starts a
// new process. That is what makes it safe for the darwin implementation to
// shell out once.
//
// Best effort throughout: a platform that cannot answer cheaply returns "" and
// the field is omitted from the advertisement rather than sent empty.

import "sync"

var (
	osVersionOnce   sync.Once
	osVersionCached string
)

// OSVersion returns a human-readable host OS version, or "" when this platform
// cannot answer. Computed at most once per process; see the file comment.
func OSVersion() string {
	osVersionOnce.Do(func() { osVersionCached = osVersion() })
	return osVersionCached
}
