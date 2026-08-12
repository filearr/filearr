package inventory

// The agent's own BUILD PROVENANCE, for the per-agent About view (user request,
// 2026-08-11: "perform version checking on dependencies on each agent like the
// central console in an about page").
//
// Central's About page can answer "which Python, which Postgres, which
// Meilisearch is this box running" by asking the live process. Until this file
// existed, the equivalent question about an agent had exactly one answer —
// `agent_version` — and that string is whatever the release pipeline stamped
// into main.Version. It says nothing about the toolchain that compiled it, the
// platform it was cross-compiled for, whether the tree was dirty, or which
// commit it came from. Those are precisely the facts wanted when one agent in a
// fleet of identical-looking agents misbehaves.
//
// Everything here is BEST EFFORT and every field is omitted rather than
// emitted empty. A missing field means "this build cannot say"; an empty string
// on the wire would render as a blank cell in the console, which reads as "the
// answer is nothing" instead of "we do not know" — the exact misreading
// about.py's rule exists to prevent.
//
// Nothing here costs a syscall or a subprocess: debug.ReadBuildInfo() reads a
// table already linked into the binary, and runtime answers from memory. That
// matters because this rides the 60 s command poll.

import (
	"runtime"
	"runtime/debug"
)

// readBuildInfo is debug.ReadBuildInfo, indirected so tests can exercise the
// "no build info available" branch. That branch is real: a binary built by a
// toolchain with the build table stripped, or produced by a linker we do not
// control, returns ok=false, and the capability advertisement must degrade to
// the runtime-only facts rather than panicking on a nil pointer.
var readBuildInfo = debug.ReadBuildInfo

// BuildInfo reports what this agent binary IS.
//
// Keys (all optional except the runtime trio, which cannot fail):
//
//	go_version    the Go toolchain that BUILT this binary
//	goos/goarch   the platform it was built FOR
//	num_cpu       logical CPUs visible to this process
//	main_version  the main module's version, when it has a meaningful one
//	vcs_revision  the commit it was built from (-buildvcs stamping)
//	vcs_time      that commit's timestamp
//	vcs_modified  true when the working tree was dirty at build time
//
// ON go_version, because it is the field most likely to be misread: this is the
// COMPILER, not the `go` directive in go.mod. go.mod states the language
// version the source requires; bi.GoVersion states what actually compiled it,
// and the two disagree routinely (a go.mod saying 1.26 built by a 1.26.5
// toolchain). The console labels it "built with" for that reason — the same
// separation about.py draws between a pin in pyproject.toml (intent) and
// importlib.metadata (fact).
//
// ON vcs_modified: a dirty build is not an error, but it does mean the commit
// id is not sufficient to reproduce the binary, and an operator comparing two
// agents "on the same commit" needs to know that.
func BuildInfo() map[string]any {
	out := map[string]any{
		"go_version": runtime.Version(),
		"goos":       runtime.GOOS,
		"goarch":     runtime.GOARCH,
		"num_cpu":    runtime.NumCPU(),
	}
	bi, ok := readBuildInfo()
	if !ok || bi == nil {
		return out // runtime facts only; see the doc comment
	}
	if bi.GoVersion != "" {
		// Prefer the recorded toolchain over runtime.Version(): they are the
		// same for a normally-built binary, and where they differ the recorded
		// one is the honest answer to "what built this".
		out["go_version"] = bi.GoVersion
	}
	// "(devel)" is what the toolchain writes for a main module built from a
	// checkout rather than resolved as a dependency — i.e. for EVERY agent we
	// ship. Emitting it would put a meaningless string next to the real
	// agent_version and invite someone to compare them.
	if v := bi.Main.Version; v != "" && v != "(devel)" {
		out["main_version"] = v
	}
	for _, s := range bi.Settings {
		switch s.Key {
		case "vcs.revision":
			if s.Value != "" {
				out["vcs_revision"] = s.Value
			}
		case "vcs.time":
			if s.Value != "" {
				out["vcs_time"] = s.Value
			}
		case "vcs.modified":
			// Recorded as the strings "true"/"false"; sent as a real boolean so
			// the console does not have to parse a string to colour a chip.
			out["vcs_modified"] = s.Value == "true"
		}
	}
	return out
}
