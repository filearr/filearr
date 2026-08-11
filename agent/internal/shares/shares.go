// Package shares is the agent's best-effort network-share discovery (P10-T11,
// docs/tasks/phase-10-agent-transfer-tasks.md §P10-T11). It answers ONE
// question: "if a local absolute file path is exported over a network share, how
// does a remote client open it?" — returning a UNC (\\host\share\rel) and/or a
// URL (smb://host/share/rel, nfs://host/export/rel) *hint* that central attaches
// to the replicated item so the UI can render a network-open link.
//
// # Best-effort by construction (Architect ruling R1)
//
// Discovery is advisory only. Anonymous-share visibility, permission-scoped
// enumeration, multi-homed hosts, and locked-down shells mean a MISSING hint is
// the normal case, never an error: every enumeration failure yields no exports
// (and therefore no hint), never a propagated error, so a file simply falls
// through to the central mapping fallback (P10-T12). Ambiguity is resolved the
// same honest way — if two equally-specific exports cover a path we return NO
// hint rather than guess (see Resolver.Hint).
//
// # Per-OS enumeration (see enumerate.go)
//
//   - Windows: PowerShell `Get-SmbShare` → CSV (dependable quoting for paths with
//     spaces, unlike `net share`'s space-ambiguous columns), falling back to
//     `net share` when PowerShell is unavailable/locked down.
//   - Linux:   /etc/samba/smb.conf `[share]` `path =` sections (SMB) + /etc/exports
//     (NFS).
//   - macOS:   `sharing -l` share points (SMB).
//
// The pure PARSERS (parse.go) are platform-neutral and fixture-tested; only the
// exec/file-read dispatch (enumerate.go) is OS-specific, and it compiles on every
// target (runtime.GOOS switch, no build tags, no cgo).
//
// # Caching
//
// Enumeration shells out / reads files, so results are cached for a TTL (default
// 5 min): a scan touching thousands of files enumerates at most once per window.
package shares

import (
	"errors"
	"os"
	"runtime"
	"strings"
	"sync"
	"time"
)

// DefaultTTL bounds how often enumeration shells out. A share topology changes
// rarely; a few minutes of staleness is immaterial for a display hint.
const DefaultTTL = 5 * time.Minute

// Hint is the resolved network location for one local path — the exact additive
// share_hint object the agent attaches to a replicated event (P10-T11). ShareURL
// is always set on a non-nil hint; UNC/ShareName are empty for non-SMB (NFS)
// exports. Source is always "agent" (agent-discovered), distinguishing it on the
// wire from a central-mapping-derived location.
type Hint struct {
	ShareURL  string
	UNC       string
	ShareName string
	Host      string
	Source    string // always "agent"
}

// export is one discovered network export: a local absolute path made reachable
// under a share name (SMB) or as an NFS export. Statically-configured entries
// (SetStaticMap) may additionally carry their own host (a container's random
// hostname is never the NAS the shares live on) and sub-path segments between
// the share name and the file remainder (a root mapped INTO a share, e.g.
// /mnt/user/media/tv -> smb://tower/media/tv).
type export struct {
	name string   // SMB share name; "" for NFS
	path string   // local absolute path exported
	kind string   // "smb" | "nfs"
	host string   // static entries only: overrides the resolver host
	sub  []string // static entries only: URL segments between share name and rel
	// source labels the SURFACE this export came from ("FILEARR_AGENT_SHARE_MAP",
	// "local override", "discovered smb export", …). It never affects matching;
	// it exists so the local web UI can answer "which layer supplied the location
	// I am looking at?" — the question an operator has the moment two surfaces
	// can both configure a mapping (see Resolver.Resolve).
	source string
}

// Mapping is one operator-authored static share mapping in the exact form it was
// written, tagged with the surface that supplied it. Callers hand the resolver a
// PRECEDENCE-ORDERED slice of these (see SetStaticMappings); the resolver never
// decides which configuration surface outranks which.
type Mapping struct {
	Local    string // local absolute path the mapping covers
	Location string // smb://host/share[/sub], \\host\share[\sub], or nfs://host/export[/sub]
	Source   string // provenance label rendered in the local UI
}

// Reject is one malformed mapping entry kept VERBATIM, with the surface that
// supplied it. Rejects are returned rather than swallowed because a typo in a
// share map is otherwise invisible: hints stay best-effort (R1) and the entry is
// skipped, so the only symptom is a root that silently never reports a location.
// Surfacing the raw text lets the UI show exactly what was thrown away.
type Reject struct {
	Entry  string
	Source string
}

// Resolver enumerates the host's network shares (cached) and maps a local
// absolute path to a Hint. The zero value is not usable — call New.
type Resolver struct {
	host     string
	ttl      time.Duration
	caseFold bool // fold path case (Windows) when matching exports
	now      func() time.Time
	enum     func() []export // injectable in tests; defaults to enumerateOS
	static   []export        // SetStaticMap entries; win over enumeration

	mu sync.Mutex
	// enumerated caches ENUMERATION only, not the merge with static entries, so
	// re-installing the static map (the local web UI does it on every render, as
	// an operator may have just edited one) does not force the host to be
	// re-enumerated — on Windows that means a PowerShell invocation per page load.
	enumerated []export
	loadedAt   time.Time
	loaded     bool
}

// New builds a Resolver for host (the name rendered into every hint — normally
// os.Hostname(), or a config override). An empty host falls back to os.Hostname()
// and finally "localhost", so a hint always carries *some* host.
func New(host string) *Resolver {
	if host == "" {
		if h, err := os.Hostname(); err == nil {
			host = h
		}
	}
	if host == "" {
		host = "localhost"
	}
	r := &Resolver{
		host:     host,
		ttl:      DefaultTTL,
		caseFold: runtime.GOOS == "windows",
		now:      time.Now,
	}
	r.enum = func() []export { return enumerateOS() }
	return r
}

// SetStaticMap installs operator-configured share locations from a spec of
// comma-separated ``localpath=location`` pairs, e.g.
//
//	/mnt/user/media=smb://tower/media,/mnt/user/docs=\\tower\documents
//
// This exists for environments where enumeration can see nothing — the Docker
// agent chief among them: inside the container there is no smb.conf, and the
// NAS's shares are exported by the HOST, under the host's name, not the
// container's. A location may be an ``smb://host/share[/sub]`` URL, a
// ``\\host\share[\sub]`` UNC, or an ``nfs://host/export[/sub]`` URL; the
// local path maps to it prefix-wise (longest match wins, exactly like
// discovered exports). Static entries carry their own host and take
// precedence over an enumerated export of the same local path. Returns how
// many entries were applied plus the malformed ones (skipped, never fatal —
// hints stay best-effort, R1).
func (r *Resolver) SetStaticMap(spec string) (applied int, bad []string) {
	mappings, rejects := ParseSpec(spec, StaticMapSource)
	for _, rj := range rejects {
		bad = append(bad, rj.Entry)
	}
	applied, _ = r.SetStaticMappings(mappings)
	return applied, bad
}

// StaticMapSource is the default provenance label for entries parsed out of a
// single `localpath=location,…` spec string — the shape the environment
// variable uses.
const StaticMapSource = "FILEARR_AGENT_SHARE_MAP"

// ParseSpec splits a comma-separated localpath=location spec into mappings
// tagged with source, plus the malformed entries verbatim. It is the ONLY spec
// parser in the agent: the scan process, the local web UI's per-root display and
// the local edit endpoint all validate through here (via ParseSpec or
// ValidateLocation), so a value one of them accepts cannot be a value another
// silently skips.
func ParseSpec(spec, source string) (mappings []Mapping, bad []Reject) {
	for _, pair := range strings.Split(spec, ",") {
		pair = strings.TrimSpace(pair)
		if pair == "" {
			continue
		}
		local, loc, cut := strings.Cut(pair, "=")
		local, loc = strings.TrimSpace(local), strings.TrimSpace(loc)
		if !cut || local == "" || ValidateLocation(loc) != nil {
			bad = append(bad, Reject{Entry: pair, Source: source})
			continue
		}
		mappings = append(mappings, Mapping{Local: local, Location: loc, Source: source})
	}
	return mappings, bad
}

// SetStaticMappings installs already-parsed mappings in CALLER PRECEDENCE ORDER:
// the first mapping covering a given local path wins, and a later duplicate of
// the same path is dropped. Which configuration SURFACE outranks which is
// deliberately not decided here — the resolver honours the order it is handed —
// so the agent's precedence chain stays stated in one place (see
// staticShareMappings in cmd/filearr-agent) instead of being re-derived by every
// caller. Entries that do not parse are returned (never fatal, R1).
func (r *Resolver) SetStaticMappings(ms []Mapping) (applied int, bad []Mapping) {
	var entries []export
	seen := map[string]bool{}
	for _, m := range ms {
		e, ok := parseStaticLocation(strings.TrimSpace(m.Local), strings.TrimSpace(m.Location))
		if !ok {
			bad = append(bad, m)
			continue
		}
		key := normPath(e.path, r.caseFold)
		if seen[key] {
			continue // a higher-precedence surface already mapped this exact path
		}
		seen[key] = true
		e.source = m.Source
		entries = append(entries, e)
	}
	r.mu.Lock()
	r.static = entries
	r.mu.Unlock()
	return len(entries), bad
}

// ValidateLocation reports why a share location cannot be used, or nil when it
// parses. Callers validating operator input (the local web UI's share-location
// field) MUST use this rather than a private regex: a location accepted by a
// second, similar-looking check would be stored and then silently skipped by the
// resolver — exactly the failure mode the reject list exists to expose.
func ValidateLocation(location string) error {
	if strings.TrimSpace(location) == "" {
		return errors.New("share location is empty")
	}
	if _, _, ok := parseLocation(strings.TrimSpace(location)); !ok {
		return errors.New(`share location must be smb://host/share[/sub], \\host\share[\sub], or nfs://host/export[/sub]`)
	}
	return nil
}

// SamePath reports whether two local paths denote the same location under this
// platform's rules (slash direction, surrounding slashes, and case folding on
// Windows). Exported because callers deciding "does a higher-precedence surface
// already own this path?" must compare paths the way the resolver does, not with
// a string ==.
func SamePath(a, b string) bool {
	fold := runtime.GOOS == "windows"
	return normPath(a, fold) == normPath(b, fold)
}

// parseLocation splits a share location into its kind and URL segments. It is
// the single point of truth for the accepted forms.
func parseLocation(loc string) (kind string, segs []string, ok bool) {
	switch {
	case strings.HasPrefix(loc, "smb://"):
		kind, segs = "smb", splitSegs(normPath(strings.TrimPrefix(loc, "smb://"), false))
	case strings.HasPrefix(loc, `\\`):
		kind, segs = "smb", splitSegs(normPath(strings.TrimPrefix(loc, `\\`), false))
	case strings.HasPrefix(loc, "nfs://"):
		kind, segs = "nfs", splitSegs(normPath(strings.TrimPrefix(loc, "nfs://"), false))
	default:
		return "", nil, false
	}
	if len(segs) < 2 { // need at least host + share/export root
		return "", nil, false
	}
	return kind, segs, true
}

// parseStaticLocation builds the export for one static mapping. Returns
// ok=false for anything it cannot understand (empty parts, unknown scheme,
// missing host/share).
func parseStaticLocation(local, loc string) (export, bool) {
	if local == "" || loc == "" {
		return export{}, false
	}
	kind, segs, ok := parseLocation(loc)
	if !ok {
		return export{}, false
	}
	host := segs[0]
	e := export{path: local, kind: kind, host: host}
	if kind == "smb" {
		e.name = segs[1]
		e.sub = segs[2:]
	} else {
		e.sub = segs[1:] // NFS: the remote export path
	}
	return e, true
}

// exports returns the cached export set, (re)enumerating when the TTL has lapsed.
// Enumeration never errors (R1): a failure yields an empty set that is cached for
// the TTL like any other, so a locked-down host does not re-shell every call.
// Static entries come first and suppress an enumerated export of the same local
// path (an operator's explicit mapping outranks a discovered guess — and must
// not TIE with it into ambiguity).
func (r *Resolver) exports() []export {
	r.mu.Lock()
	defer r.mu.Unlock()
	if !r.loaded || r.now().Sub(r.loadedAt) >= r.ttl {
		r.enumerated = r.enum()
		r.loadedAt = r.now()
		r.loaded = true
	}
	// The MERGE is recomputed per call (a handful of entries, no I/O) so that
	// installing a new static map does not invalidate the enumeration cache.
	merged := append([]export{}, r.static...)
	for _, e := range r.enumerated {
		conflict := false
		for _, s := range r.static {
			if normPath(s.path, r.caseFold) == normPath(e.path, r.caseFold) {
				conflict = true
				break
			}
		}
		if !conflict {
			if e.source == "" {
				// Label enumeration here rather than in every per-OS enumerator:
				// the display layer needs to distinguish "an operator told us
				// this" from "we found this on the host" (see Resolve).
				e.source = "discovered " + e.kind + " export"
			}
			merged = append(merged, e)
		}
	}
	return merged
}

// Hint resolves absPath to a network-open Hint, or nil when no share covers it
// (the normal, non-error case — R1). Selection is longest-export-path-wins on
// segment boundaries (the same discipline as the central resolve_share_url); a
// TIE between two distinct equally-specific exports is treated as multi-homed
// AMBIGUITY and returns nil rather than fabricate a guess.
func (r *Resolver) Hint(absPath string) *Hint {
	return r.Resolve(absPath).Hint
}

// Resolution is Resolve's explained answer for one local path: the hint plus
// WHERE it came from. A zero Resolution (nil Hint, empty Source) is the honest
// "no share mapping covers this path" answer — the state the local UI must be
// able to render explicitly, because an unmapped root produces no share hint at
// all and nothing else on the agent says so.
type Resolution struct {
	// Hint is nil when nothing covers the path, or when coverage is ambiguous.
	Hint *Hint
	// Source labels the surface that supplied the covering mapping — a static
	// map entry's Mapping.Source, or "discovered smb export" for enumeration.
	// Empty when there is no mapping.
	Source string
	// ExportPath is the local path of the covering mapping (which may be a
	// PARENT of the queried path, since matching is longest-prefix).
	ExportPath string
	// Ambiguous marks the multi-homed tie: two equally-specific exports cover
	// the path, so no hint is fabricated (R1) — but the UI can say why.
	Ambiguous bool
}

// Resolve is Hint plus provenance. Selection is identical (longest export path
// wins on segment boundaries; a tie is ambiguity, not a guess); the extra
// return fields exist so the local web UI can show, per scan root, which layer
// supplied the location an operator is looking at.
func (r *Resolver) Resolve(absPath string) Resolution {
	if absPath == "" {
		return Resolution{}
	}
	target := normPath(absPath, r.caseFold)
	exports := r.exports()

	var best *export
	bestLen := -1
	tie := false
	for i := range exports {
		e := &exports[i]
		base := normPath(e.path, r.caseFold)
		if !covers(base, target) {
			continue
		}
		switch {
		case len(base) > bestLen:
			best, bestLen, tie = e, len(base), false
		case len(base) == bestLen && best != nil && !sameExport(*best, *e):
			tie = true
		}
	}
	if best == nil {
		return Resolution{} // nothing covers this path — the normal case (R1)
	}
	if tie {
		return Resolution{Ambiguous: true} // ambiguous multi-homed coverage
	}
	baseSegs := splitSegs(normPath(best.path, r.caseFold))
	// Recompute the remainder from the ORIGINAL (case-preserving) path so the hint
	// keeps the real filename case, not the case-folded compare key.
	origSegs := splitSegs(normPath(absPath, false))
	rel := origSegs[len(baseSegs):]
	return Resolution{Hint: r.build(*best, rel), Source: best.source, ExportPath: best.path}
}

// build renders the UNC + URL forms for a covering export and its remainder
// segments. Segments are joined with the native separator (backslash for UNC,
// forward slash for URL); nothing is percent-encoded (a display/open path, not an
// href), mirroring the central _join_share discipline.
func (r *Resolver) build(e export, rel []string) *Hint {
	host := r.host
	if e.host != "" {
		host = e.host // static mapping: the NAS's name, not this process's
	}
	switch e.kind {
	case "nfs":
		// NFS has no UNC form. A discovered export's remote path IS its local
		// path (/etc/exports semantics); a static mapping carries the remote
		// path explicitly in e.sub.
		remote := e.sub
		if e.host == "" {
			remote = splitSegs(normPath(e.path, false))
		}
		segs := append([]string{host}, remote...)
		segs = append(segs, rel...)
		return &Hint{
			ShareURL: "nfs://" + strings.Join(segs, "/"),
			Host:     host,
			Source:   "agent",
		}
	default: // "smb"
		mid := append([]string{e.name}, e.sub...)
		urlSegs := append(append([]string{host}, mid...), rel...)
		uncSegs := append(mid, rel...)
		return &Hint{
			ShareURL:  "smb://" + strings.Join(urlSegs, "/"),
			UNC:       `\\` + host + `\` + strings.Join(uncSegs, `\`),
			ShareName: e.name,
			Host:      host,
			Source:    "agent",
		}
	}
}

// --- pure path helpers (mirror central _norm_local / segment-boundary cover) ---

// normPath converts backslashes to forward slashes and strips surrounding
// slashes; when fold is set it also lowercases (Windows path case-insensitivity).
func normPath(p string, fold bool) string {
	p = strings.ReplaceAll(p, `\`, "/")
	p = strings.Trim(p, "/")
	if fold {
		p = strings.ToLower(p)
	}
	return p
}

func splitSegs(norm string) []string {
	if norm == "" {
		return nil
	}
	return strings.Split(norm, "/")
}

// covers reports whether base (an export path) covers target (a file path) on
// segment boundaries: equal, base is a root (empty), or target is under base.
func covers(base, target string) bool {
	return base == "" || target == base || strings.HasPrefix(target, base+"/")
}

func sameExport(a, b export) bool {
	return a.name == b.name && a.path == b.path && a.kind == b.kind && a.host == b.host
}
