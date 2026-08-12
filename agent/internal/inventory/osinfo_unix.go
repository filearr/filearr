//go:build !windows && !darwin

package inventory

import (
	"os"
	"strconv"
	"strings"
)

// osReleasePath is the freedesktop-standard file every mainstream Linux
// distribution ships (systemd's os-release(5)); /usr/lib/os-release is the
// vendor copy for images whose /etc is a writable overlay. Reading a file is
// the whole implementation — no subprocess, and no dependency on lsb_release,
// which is a Python script that half the container images do not install.
var osReleasePaths = []string{"/etc/os-release", "/usr/lib/os-release"}

// osVersion returns the distribution's PRETTY_NAME ("Ubuntu 22.04.4 LTS",
// "Alpine Linux v3.20"), or "" when nothing readable says.
//
// PRETTY_NAME rather than NAME+VERSION_ID because it is the string the
// distribution itself chose to identify the release, and it is the one an
// operator will recognise from `cat /etc/os-release` when comparing hosts.
//
// Note what this does NOT report: the kernel version. That would need a uname
// syscall wrapper per Unix and it is not the fact that explains an agent's
// behaviour — the DISTRIBUTION is, because the distribution is what pins the
// host tools (Ubuntu 22.04 shipping tesseract 4.1.1 is the worked example in
// the minimum-versions docs).
func osVersion() string {
	for _, path := range osReleasePaths {
		b, err := os.ReadFile(path)
		if err != nil {
			continue
		}
		if v := parseOSRelease(string(b), "PRETTY_NAME"); v != "" {
			return v
		}
	}
	return ""
}

// parseOSRelease pulls one key out of os-release content. Pure so the real
// shapes (quoted, unquoted, comments, CRLF from an oddly-built image) can be
// pinned in tests on any platform.
func parseOSRelease(content, key string) string {
	for _, line := range strings.Split(content, "\n") {
		line = strings.TrimSpace(line)
		if line == "" || strings.HasPrefix(line, "#") {
			continue
		}
		name, value, ok := strings.Cut(line, "=")
		if !ok || strings.TrimSpace(name) != key {
			continue
		}
		value = strings.TrimSpace(value)
		// Values are shell-quoted per the spec. Unquote when it parses; fall
		// back to the raw text rather than dropping a value we could not
		// unquote, since a rough answer beats no answer here.
		if unquoted, err := strconv.Unquote(value); err == nil {
			return strings.TrimSpace(unquoted)
		}
		return strings.Trim(value, `"'`)
	}
	return ""
}
