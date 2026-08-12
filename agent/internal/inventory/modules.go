package inventory

// The agent's Go MODULE DEPENDENCY LIST — the agent-side answer to central's
// "Backend (Python)" table on the About page (user request, 2026-08-11:
// "perform version checking on dependencies on each agent like the central
// console in an about page").
//
// Central reads its dependency versions from installed distribution metadata,
// live, because a pin states intent and the installed version states fact. The
// Go equivalent is stronger: a Go binary is statically linked, so the module
// table baked into it by the linker IS the fact — there is no possibility of
// the running code differing from what this reports. That makes this list
// exactly what an operator wants when a CVE lands in a transitive dependency:
// the question "does the fleet ship a vulnerable golang.org/x/crypto" becomes a
// per-agent lookup instead of an archaeology exercise against build logs.
//
// It is DERIVED, never judged. Like the host tools, the agent reports facts and
// central decides what they mean — there is one comparator in this codebase and
// it is in Python (filearr/versioncmp.py). Nothing here compares versions.

import (
	"sort"
)

// maxModules caps the reported list. The agent's own dependency graph is
// currently ~150 modules including indirects, and this is untrusted-adjacent
// data that lands in a JSON column and a browser table: an unbounded list from
// a future build with a much larger graph would be a payload problem rather
// than a useful report. Truncation is silent HERE because the capabilities
// budget check above it (see collector.go) is the mechanism that tells the
// console anything was left out.
const maxModules = 200

// Modules lists the Go modules linked into this binary, sorted by import path,
// main module first-class among them.
//
// Each entry is {path, version}. A module with no recorded version (the main
// module in a normal checkout build) is included WITHOUT the key rather than
// with an empty one — the console renders "version unknown" from the absence,
// and an empty string there would render as a blank cell.
//
// Replaced modules report the REPLACEMENT's path and version. That is
// deliberate: a `replace` directive means the code actually linked in is the
// replacement, and reporting the original would describe a dependency this
// binary does not contain.
func Modules() []map[string]string {
	bi, ok := readBuildInfo()
	if !ok || bi == nil {
		// No build table: the honest answer is "nothing to report", which the
		// caller turns into a null section rather than an empty list claiming
		// this binary has no dependencies.
		return nil
	}

	seen := map[string]bool{}
	out := make([]map[string]string, 0, len(bi.Deps)+1)
	add := func(path, version string) {
		if path == "" || seen[path] {
			return
		}
		seen[path] = true
		entry := map[string]string{"path": path}
		if version != "" && version != "(devel)" {
			entry["version"] = version
		}
		out = append(out, entry)
	}

	add(bi.Main.Path, bi.Main.Version)
	for _, d := range bi.Deps {
		if d == nil {
			continue
		}
		if d.Replace != nil {
			add(d.Replace.Path, d.Replace.Version)
			continue
		}
		add(d.Path, d.Version)
	}

	sort.Slice(out, func(i, j int) bool { return out[i]["path"] < out[j]["path"] })
	if len(out) > maxModules {
		out = out[:maxModules]
	}
	return out
}
