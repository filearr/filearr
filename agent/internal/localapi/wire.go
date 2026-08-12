// Package localapi is the agent's local query transport (P7-T2): an HTTP/1.1
// server over a same-user-only local channel — a Unix domain socket on
// linux/darwin, a current-user-restricted Windows named pipe — serving the
// P7-T1 read-only query engine to a same-machine CLI (P7-T3) and, later, the
// local web UI (P7-T5, localhost TCP, NOT part of this package).
//
// Security posture (brief §3, CLAUDE.md priority order security > …):
//   - Transport is a filesystem/namespace object the OS access-controls, never a
//     network socket — DNS rebinding is impossible against it (brief §3.1).
//   - Every accepted connection is peer-credential checked: the connecting
//     process must run as the SAME OS user as the agent, else it is dropped
//     before any request is read (the Postgres `peer` model, brief §3.3).
//   - The query path holds only the P7-T1 SQLITE_OPEN_READONLY handle, so no
//     request can mutate the catalog (defense in depth, brief §3.4).
//
// The wire types below mirror backend/filearr/localapi_contracts.py byte-for-byte
// (snake_case JSON keys; the Python mirror is extra="forbid", so an unknown key
// here is a review-caught drift). Keep the two in lockstep.
package localapi

// QueryRequest is the CLI/UI → agent request body for POST /v1/query. The client
// sends a raw DSL string (parsed server-side) and NEVER a scope predicate — scope
// is server-cached and applied by the agent, never trusted from the client
// (brief §4.4). Mirrors localapi_contracts.QueryRequest.
type QueryRequest struct {
	Query  string `json:"query"`
	Limit  int    `json:"limit"`
	Offset int    `json:"offset"`
}

// ResultRow is one matched item — the R1 narrow field set only (no central-only
// extracted metadata crosses this boundary). Mirrors localapi_contracts.ResultRow.
type ResultRow struct {
	ID           string   `json:"id"`
	RelPath      string   `json:"rel_path"`
	Filename     string   `json:"filename"`
	Extension    *string  `json:"extension"`
	Size         int64    `json:"size"`
	Mtime        string   `json:"mtime"` // ISO-8601 UTC
	Kind         *string  `json:"kind"`
	Group        *string  `json:"group,omitempty"`
	QuickHash    *string  `json:"quick_hash"`
	ContentHash  *string  `json:"content_hash"`
	FuzzyMatched bool     `json:"fuzzy_matched"`
	Score        *float64 `json:"score"`
	// RootID never crosses the wire; the web UI handler resolves it to Root
	// (the scan-root's absolute path) so the browser can render/copy full paths.
	RootID string  `json:"-"`
	Root   *string `json:"root,omitempty"`
}

// ScopeInfo is the R3 restricted-view affordance. In P7-T2 no path scope exists
// yet (that lands in P7-T4), so Active is always false and Predicates is an empty
// (non-nil) list. Mirrors localapi_contracts.ScopeInfo.
type ScopeInfo struct {
	Active     bool     `json:"active"`
	Predicates []string `json:"predicates"`
	Stale      bool     `json:"stale"`
}

// QueryResponse is the agent → CLI/UI response. Mirrors
// localapi_contracts.QueryResponse.
type QueryResponse struct {
	Rows      []ResultRow `json:"rows"`
	Total     int         `json:"total"`
	Truncated bool        `json:"truncated"`
	Fuzzy     bool        `json:"fuzzy"`
	Scope     ScopeInfo   `json:"scope"`
	ElapsedMs int64       `json:"elapsed_ms"`
	Notice    *string     `json:"notice"`
}

// HistoryEntry is one local-frecency query suggestion (P7-T6). It is NOT part of
// the localapi_contracts.py mirror — search history is a strictly-local surface
// that never crosses to central, so it has no central-side contract.
type HistoryEntry struct {
	Query    string  `json:"query"`
	Hits     float64 `json:"hits"`
	LastUsed string  `json:"last_used"` // ISO-8601 UTC
	Score    float64 `json:"score"`
}

// HistoryResponse is the agent → CLI response for GET /v1/history: the top
// frecency-ranked past queries on THIS machine. Local-only (see HistoryEntry).
type HistoryResponse struct {
	Entries []HistoryEntry `json:"entries"`
}

// HealthResponse is the agent → CLI/UI status probe (GET /v1/health). ReadOnly is
// always true (invariant — never a write surface, brief §3.4). Mirrors
// localapi_contracts.HealthResponse.
type HealthResponse struct {
	Status                string  `json:"status"` // "ok" | "degraded" | "starting"
	IndexReady            bool    `json:"index_ready"`
	ItemCount             int     `json:"item_count"`
	ReadOnly              bool    `json:"read_only"`
	WebUIEnabled          bool    `json:"web_ui_enabled"`
	AuthRequired          bool    `json:"auth_required"`
	PolicyVersion         *int    `json:"policy_version"`
	PolicyStale           bool    `json:"policy_stale"`
	OfflineGraceExpiresAt *string `json:"offline_grace_expires_at"`
}

// HostToolInfo is one row of the local status page's host-tool table
// (2026-08-11). Mirrors localapi_contracts.HostToolInfo.
//
// It replaces a boolean. The panel used to print "Host tools  exiftool: no,
// ffmpeg: yes, …", which answers only whether a capability runs on this host —
// and an operator standing at the machine, having just installed the thing, is
// asking two further questions the booleans cannot answer: which version, and
// WHICH COPY. On Windows in particular there are routinely several exiftools on
// one box and only one of them is the one the service will execute.
//
// Absent tools are still listed, with Present false and no version or path: the
// point of the table is the full matrix, and dropping the absent rows would
// make "not installed" indistinguishable from "not a tool Filearr knows about".
//
// NO CENTRAL DATA crosses this surface and no judgement is made here: there is
// no verdict field, because the minimum-version opinion lives centrally
// (filearr/toolversions.py) and this page is reachable when central is not.
type HostToolInfo struct {
	Name    string `json:"name"`
	Present bool   `json:"present"`
	// Version is "" when the tool is absent OR present but unwilling to state a
	// version (an old build, a wrapper script). The UI distinguishes those two
	// using Present, and never renders an empty cell for either.
	Version string `json:"version,omitempty"`
	// Path is the resolved absolute location, "" when absent. On Windows this
	// is always a machine-wide, admin-writable directory by design — a path
	// under a user profile would mean the resolver's security rule regressed.
	Path string `json:"path,omitempty"`
}

// errorBody is the JSON envelope for a failed request. It is NOT part of the
// localapi_contracts.py mirror (that file models only success shapes); P7-T2
// defines it here and the P7-T3 CLI decodes it. Kept intentionally small.
type errorBody struct {
	Error    string   `json:"error"`
	Code     string   `json:"code,omitempty"`
	Position *int     `json:"position,omitempty"`
	Reason   string   `json:"reason,omitempty"`
	Keys     []string `json:"keys,omitempty"`
}
