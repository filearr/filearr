package inventory

// Host-tool VERSION reporting (2026-08-10, user request: "include the versions
// of ffmpeg and tesseract found").
//
// Knowing a tool is on PATH answers "will this capability run here". It does not
// answer the question an operator actually hits when output looks wrong — WHICH
// build is running. A tesseract 4 reads a scanned page materially worse than a
// 5.x, an ancient ffmpeg misreports HDR side data, and "installed" on Windows
// routinely means a five-year-old zip someone dropped in Program Files. The
// version is the difference between "the tool is there" and "the tool is there
// and it is the one you think".
//
// Cost discipline: each version costs ONE subprocess, so it is resolved at most
// once per (tool, resolved path) per toolCacheTTL — the capability
// advertisement is rebuilt on every command poll (~1/min) and must never spawn
// seven processes to do it. The probe is bounded by its own short timeout: a
// wedged binary must degrade to "version unknown", never hang the poll that
// reports it.
//
// The cache is TTL'd rather than permanent (2026-08-11) for the same reason the
// resolution cache is — see toolCacheTTL in tools.go. A tool UPGRADED in place
// keeps its path, so a path-keyed permanent cache would report the old version
// forever; a quarter of an hour later this one re-probes and tells the truth.

import (
	"bytes"
	"context"
	"os/exec"
	"strings"
	"sync"
	"time"
	"unicode"
)

// versionProbeTimeout bounds one `<tool> -version` call. These are local
// processes that print a line and exit, so a second is already generous; the
// ceiling exists for the pathological case (a binary on a dead network mount,
// a Windows exe waiting on a message box), where the honest answer is "unknown"
// rather than a stalled command poll.
const versionProbeTimeout = 3 * time.Second

// versionMaxChars caps a stored version string. It is untrusted output from a
// third-party binary that ends up in the console and in central's agent row; a
// build banner can run to kilobytes, and no real version needs more than this.
const versionMaxChars = 64

var versionCache sync.Map // toolKey -> cachedString ("" value == unknown/absent)

// versionArgs is the argument that makes each tool print its version, and it is
// deliberately per-tool rather than a guessed common flag: `-version` is what
// the ffmpeg family answers, tesseract wants `--version`, exiftool wants `-ver`
// (its `-version` is something else entirely), and the poppler tools answer
// `-v`. Getting this wrong does not fail loudly — most of these print usage to
// stderr and exit non-zero — so the mapping is explicit and tested.
var versionArgs = map[string]string{
	"ffmpeg":    "-version",
	"ffprobe":   "-version",
	"tesseract": "--version",
	"exiftool":  "-ver",
	"pdfinfo":   "-v",
	"pdftotext": "-v",
	"pdftoppm":  "-v",
}

// ToolVersion returns the version string of a resolvable host tool, or "" when
// the tool is absent or would not say. Never fatal, never blocking beyond
// versionProbeTimeout.
func ToolVersion(name, envVar string) string {
	path := ResolveTool(name, envVar)
	if path == "" {
		return "" // absent: Tools() already reports that, no probe to run
	}
	key := toolKey{name: name, override: path}
	if v, ok := cacheLoad(&versionCache, key); ok {
		return v
	}
	version := probeToolVersion(name, path)
	cacheStore(&versionCache, key, version)
	return version
}

// probeToolVersion runs the tool's version argument and parses the answer.
//
// stdout AND stderr are both captured because these tools disagree about where
// a version belongs: ffmpeg and tesseract print to stdout, the poppler trio
// prints to stderr, and a non-zero exit is NOT a failure signal here (several
// of them exit 1 after printing their version quite happily). So the exit
// status is ignored and the OUTPUT is what decides.
func probeToolVersion(name, path string) string {
	arg, ok := versionArgs[name]
	if !ok {
		return ""
	}
	ctx, cancel := context.WithTimeout(context.Background(), versionProbeTimeout)
	defer cancel()

	cmd := exec.CommandContext(ctx, path, arg)
	var stdout, stderr bytes.Buffer
	cmd.Stdout = &stdout
	cmd.Stderr = &stderr
	_ = cmd.Run() // exit status is not evidence; see the doc comment

	out := stdout.String()
	if strings.TrimSpace(out) == "" {
		out = stderr.String()
	}
	return parseToolVersion(name, out)
}

// parseToolVersion extracts the version token from a tool's version banner.
// Pure, so the shape of every tool's real output can be pinned in tests without
// installing any of them.
//
// The three shapes actually seen:
//
//	ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 …   -> token after "version"
//	tesseract 5.3.4                                            -> second token
//	12.76                                                      -> the whole line (exiftool)
func parseToolVersion(name, out string) string {
	line := firstNonEmptyLine(out)
	if line == "" {
		return ""
	}
	fields := strings.Fields(line)
	if len(fields) == 0 {
		return ""
	}

	// "<something> version <token> …" — the ffmpeg/poppler shape. Taking the
	// token AFTER the word is what keeps the vendor prefix out of the value.
	for i, f := range fields {
		if strings.EqualFold(f, "version") && i+1 < len(fields) {
			return cleanVersion(fields[i+1])
		}
	}
	// "tesseract 5.3.4" — the tool names itself, then the version.
	if len(fields) >= 2 && strings.EqualFold(fields[0], name) {
		return cleanVersion(fields[1])
	}
	// A bare version on its own line (exiftool's `-ver`). Guarded on the first
	// character being a digit so a usage banner never gets stored as a version.
	if len(fields) == 1 && startsWithDigit(fields[0]) {
		return cleanVersion(fields[0])
	}
	return ""
}

func firstNonEmptyLine(s string) string {
	for _, line := range strings.Split(s, "\n") {
		if t := strings.TrimSpace(line); t != "" {
			return t
		}
	}
	return ""
}

func startsWithDigit(s string) bool {
	return s != "" && s[0] >= '0' && s[0] <= '9'
}

// cleanVersion strips control characters and caps the length — the value is
// untrusted third-party output that is stored centrally and rendered in a
// browser.
func cleanVersion(s string) string {
	var b strings.Builder
	for _, r := range s {
		if unicode.IsControl(r) {
			continue
		}
		b.WriteRune(r)
		if b.Len() >= versionMaxChars {
			break
		}
	}
	return strings.TrimSpace(b.String())
}

// ToolVersions is the version half of the host-tool matrix: the same keys
// Tools() reports, but only for tools that are actually present AND willing to
// state a version. A present tool with an unknown version is OMITTED rather than
// reported as empty, so the console can say "installed, version unknown" from
// the pair of maps instead of rendering a blank.
func ToolVersions() map[string]string {
	out := map[string]string{}
	for name, env := range hostToolEnvs {
		if v := ToolVersion(name, env); v != "" {
			out[name] = v
		}
	}
	return out
}
