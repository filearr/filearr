package localapi

// webcontrol.go is the LOCAL SCAN CONTROL surface of the agent web UI
// (2026-08-10): a small, tightly gated set of POST endpoints that let an
// operator sitting at the machine pause/resume this agent's scanning, trigger a
// scan, edit the scan schedule, and manage the scan roots — but only as far as
// central's policy permits.
//
// What this is NOT: a write path into the catalog. The `read_only` policy key
// keeps its exact meaning — the local surface never mutates items or metadata,
// and there is no endpoint here that touches the index. Agent SELF-
// ADMINISTRATION is a different axis from catalog writability, and the
// read-only invariant never covered it.
//
// Three security rules are enforced here, each for a reason worth stating:
//
//  1. **Auth is required for every mutation, unconditionally** — even when the
//     policy says auth_required:false. Anonymous reads are a posture an operator
//     can legitimately choose for a loopback status page; anonymous "stop
//     scanning this machine" is not the same decision and must never be
//     inherited from it. The bootstrap token is therefore always exchangeable
//     for a session (see authGate), even when reads are open.
//  2. **A key central explicitly set is LOCKED locally.** Local editing may only
//     fill in keys central left unset. Without this, central re-applies its
//     value on the next poll and the local edit silently reverts a minute later
//     — strictly worse than refusing the edit and naming the document that owns
//     the key.
//  3. **Local can never defeat central.** A local resume clears ONLY the local
//     pause flag; if central suspended this agent, the agent stays paused and
//     the UI says so and offers no resume. The whole point of the permission
//     gates is that the fleet control outranks the local one.
//
// The four layers the read-only web UI was built with are untouched: the
// loopback bind + Host allow-list, stdlib CrossOriginProtection, the auth gate,
// and the method backstop — which is opened for THESE EXACT PATHS ONLY (see
// controlPaths / webMethodBackstop in webui.go); every other path still 405s any
// non-GET/HEAD verb.

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"strconv"
	"strings"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/schedule"
	"github.com/filearr/filearr/agent/internal/shares"
)

// Control endpoint paths. They are listed once, here, and consumed by both the
// route registration and the method backstop's allow-list so the two can never
// drift (a route the backstop does not know about would be dead on arrival; a
// backstop entry with no route would open a verb on a 404).
const (
	pathControl         = "/api/control"
	pathControlPause    = "/api/control/pause"
	pathControlScanNow  = "/api/control/scan-now"
	pathControlSchedule = "/api/control/schedule"
	pathControlRoots    = "/api/control/roots"
)

// controlMutationPaths is the EXACT set of paths the method backstop lets a POST
// reach. Everything else keeps 405ing every non-GET/HEAD verb.
var controlMutationPaths = map[string]bool{
	pathControlPause:    true,
	pathControlScanNow:  true,
	pathControlSchedule: true,
	pathControlRoots:    true,
}

// ControlsState is the agent-supplied half of the local-controls snapshot: the
// live values the daemon knows and the web UI package does not (what the
// scheduler resolved, where each value came from, what the roots are). The
// permission/lock half is computed here from the policy view.
type ControlsState struct {
	// LocalPaused is the local scan-only pause flag (local-settings.json).
	LocalPaused bool `json:"local_paused"`
	// CentralSuspended is central's fleet suspend (suspend.json). Reported so
	// the page can explain a pause it must not offer to lift.
	CentralSuspended bool `json:"central_suspended"`
	// ScanRunning is true while a scheduled scan child process is running.
	ScanRunning bool `json:"scan_running"`

	ScanCron                 string `json:"scan_cron"`
	ScanCronSource           string `json:"scan_cron_source"`
	ScanIntervalSeconds      int    `json:"scan_interval_seconds"`
	ScanIntervalSource       string `json:"scan_interval_seconds_source"`
	ScanOnStart              bool   `json:"scan_on_start"`
	ScanOnStartSource        string `json:"scan_on_start_source"`
	LocalScanCron            string `json:"local_scan_cron"`
	LocalScanIntervalSeconds int    `json:"local_scan_interval_seconds"`
	LocalScanOnStartSet      bool   `json:"local_scan_on_start_set"`
	LocalScanOnStart         bool   `json:"local_scan_on_start"`

	Roots []string `json:"roots"`

	// ShareRoots pairs each configured root with the network location a file
	// under it would report (2026-08-10). One entry per root, same order as
	// Roots; an entry with an empty Location is the explicit "no share mapping"
	// answer, which is the one the UI most needs to show — an unmapped root
	// silently produces no share hint and nothing else says so.
	ShareRoots []RootShare `json:"share_roots"`
	// ShareMapRejects are the malformed share-map entries, verbatim, from either
	// surface. They are skipped, never fatal (hints are best-effort, R1), so
	// showing them is the only way a typo ever becomes visible.
	ShareMapRejects []ShareMapReject `json:"share_map_rejects"`
	// ShareMapEnvPaths are the local paths FILEARR_AGENT_SHARE_MAP maps. The
	// environment outranks a locally-authored mapping on the same path (see
	// staticShareMappings in cmd/filearr-agent), so these paths are LOCKED for
	// local editing and the edit endpoint refuses them.
	ShareMapEnvPaths []string `json:"share_map_env_paths"`
}

// RootShare is one scan root's resolved network location plus the provenance an
// operator needs in order to know which layer they are looking at.
type RootShare struct {
	Path string `json:"path"`
	// Location is the resolved share URL for the root itself ("" = unmapped).
	Location string `json:"location,omitempty"`
	UNC      string `json:"unc,omitempty"`
	// Source names the supplying surface: "FILEARR_AGENT_SHARE_MAP", "local
	// override", or "discovered smb export"/"discovered nfs export".
	Source string `json:"source,omitempty"`
	// InheritedFrom is set when the covering mapping is on a PARENT path rather
	// than on this root.
	InheritedFrom string `json:"inherited_from,omitempty"`
	// EnvValue / LocalValue are the raw configured locations for EXACTLY this
	// path on each surface, so the UI can show the field's stored value and lock
	// it when the environment owns the path.
	EnvValue   string `json:"env_value,omitempty"`
	LocalValue string `json:"local_value,omitempty"`
	// Superseded marks a stored local mapping that the environment outranks.
	Superseded bool `json:"superseded,omitempty"`
	// Ambiguous marks a multi-homed tie: two equally specific exports cover the
	// root, so no hint is fabricated (R1).
	Ambiguous bool `json:"ambiguous,omitempty"`
}

// ShareMapReject is one malformed share-map entry kept verbatim with the surface
// that supplied it.
type ShareMapReject struct {
	Entry  string `json:"entry"`
	Source string `json:"source"`
}

// ScheduleEdit is one local schedule change. A non-nil field SETS the local
// override for that knob; a key named in Clear REMOVES the local override (so
// the knob falls back through env → sidecar → default). The two are separate
// because a JSON null could not distinguish "clear this" from "not mentioned".
type ScheduleEdit struct {
	ScanCron            *string  `json:"scan_cron"`
	ScanIntervalSeconds *int     `json:"scan_interval_seconds"`
	ScanOnStart         *bool    `json:"scan_on_start"`
	Clear               []string `json:"clear"`
}

// ControlSeams are the daemon-side actions the control endpoints invoke. Every
// one is optional: a nil seam makes its endpoint report "unavailable" rather
// than panicking, which is what a build/wiring gap should look like.
type ControlSeams struct {
	// State returns the live snapshot (see ControlsState).
	State func(ctx context.Context) (ControlsState, error)
	// SetLocalPaused sets/clears the LOCAL scan pause only. It must never touch
	// central's suspend flag.
	SetLocalPaused func(ctx context.Context, paused bool) error
	// ScanNow triggers one immediate scan (no-op if one is already running).
	ScanNow func(ctx context.Context) error
	// SetSchedule persists the local schedule overrides.
	SetSchedule func(ctx context.Context, edit ScheduleEdit) error
	// AddRoot / RemoveRoot edit the agent's scan roots (scan.json).
	AddRoot    func(ctx context.Context, path string) error
	RemoveRoot func(ctx context.Context, path string) error
	// SetRootShare stores one locally-authored share location for path, or
	// clears it when location is empty. The location has already been validated
	// with the resolver's own parser by the time this runs.
	SetRootShare func(ctx context.Context, path, location string) error
}

// controlsResponse is what GET /api/control returns: the live state plus the
// permission/lock computation the page needs to render every control with an
// explicit reason when it is disabled.
type controlsResponse struct {
	ControlsState
	// Available is false when the daemon wired no control seams at all.
	Available bool `json:"available"`
	// Permissions are the three central-authored gates.
	Permissions map[string]bool `json:"permissions"`
	// Managed maps a locked key to the document that owns it. A key present
	// here must render read-only with a "managed by central" note.
	Managed map[string]string `json:"managed"`
	// RootsManagedBy is non-empty when roots are derived centrally.
	RootsManagedBy string `json:"roots_managed_by"`
	// ReadOnly restates the catalog invariant for the page footer: these
	// controls administer the agent, never the catalog.
	ReadOnly bool `json:"read_only"`
}

// scheduleKeys are the policy keys the schedule control may write, and the exact
// keys whose central presence locks the corresponding local field.
var scheduleKeys = []string{"scan_cron", "scan_interval_seconds", "scan_on_start"}

// handleControls serves GET /api/control — the snapshot the page renders. It is
// a READ, so it rides the ordinary auth posture (anonymous when the policy says
// auth_required:false), unlike every mutation below.
func (ws *WebUIServer) handleControls(w http.ResponseWriter, r *http.Request) {
	pv := ws.policy()
	if !pv.WebUIEnabled {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "web UI disabled by policy", Code: "web_ui_disabled"})
		return
	}
	resp := controlsResponse{
		Available: ws.cfg.Controls != nil,
		Permissions: map[string]bool{
			"scan":     pv.ScanControl,
			"schedule": pv.ScheduleControl,
			"roots":    pv.RootsControl,
		},
		Managed:  map[string]string{},
		ReadOnly: true,
	}
	for _, k := range scheduleKeys {
		if managed, src := pv.Managed(k); managed {
			resp.Managed[k] = src
		}
	}
	if pv.RootsManagedByCentral {
		src := pv.PolicySource
		if src == "" {
			src = "central policy"
		}
		resp.RootsManagedBy = src + " (config group scan_selections)"
	}
	if ws.cfg.Controls != nil && ws.cfg.Controls.State != nil {
		st, err := ws.cfg.Controls.State(r.Context())
		if err != nil {
			ws.log.Error("local controls: state snapshot failed", "err", err)
			writeError(w, http.StatusInternalServerError, errorBody{Error: "control state unavailable", Code: "control_state_error"})
			return
		}
		resp.ControlsState = st
	}
	if resp.Roots == nil {
		resp.Roots = []string{}
	}
	// Absent share data must render as "no mapping", not as a broken page: send
	// empty arrays rather than nulls so the client never has to special-case it.
	if resp.ShareRoots == nil {
		resp.ShareRoots = []RootShare{}
	}
	if resp.ShareMapRejects == nil {
		resp.ShareMapRejects = []ShareMapReject{}
	}
	if resp.ShareMapEnvPaths == nil {
		resp.ShareMapEnvPaths = []string{}
	}
	writeJSON(w, http.StatusOK, resp)
}

// controlGate is the shared precondition for every mutation: the web UI must be
// enabled, the seams wired, and the named permission granted. It returns the
// live policy view when the request may proceed, or writes the refusal and
// returns false.
//
// Note what is NOT here: authentication. That is enforced one layer out by
// mutationGate (webui.go), BEFORE the request reaches a handler, so an
// unauthenticated caller never learns which permissions this agent has.
func (ws *WebUIServer) controlGate(w http.ResponseWriter, r *http.Request, permission string) (PolicyView, bool) {
	pv := ws.policy()
	if !pv.WebUIEnabled {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "web UI disabled by policy", Code: "web_ui_disabled"})
		return pv, false
	}
	if ws.cfg.Controls == nil {
		writeError(w, http.StatusServiceUnavailable, errorBody{
			Error: "local controls are not available on this agent", Code: "control_unavailable",
		})
		return pv, false
	}
	allowed := false
	var key string
	switch permission {
	case "scan":
		allowed, key = pv.ScanControl, "local_scan_control"
	case "schedule":
		allowed, key = pv.ScheduleControl, "local_schedule_control"
	case "roots":
		allowed, key = pv.RootsControl, "local_roots_control"
	}
	if !allowed {
		// 403, not 401: the caller IS authenticated (mutationGate ran first);
		// central simply has not delegated this power to the local surface.
		writeError(w, http.StatusForbidden, errorBody{
			Error: "central policy does not allow this change from the local UI (" + key + " is off)",
			Code:  "local_control_denied",
			Keys:  []string{key},
		})
		return pv, false
	}
	return pv, true
}

// decodeControlBody decodes a small JSON body, rejecting unknown fields (the
// same strictness the socket transport applies) so a typo'd key is an error
// rather than a silently ignored setting.
func decodeControlBody(w http.ResponseWriter, r *http.Request, dst any) bool {
	dec := json.NewDecoder(http.MaxBytesReader(w, r.Body, maxRequestBytes))
	dec.DisallowUnknownFields()
	if err := dec.Decode(dst); err != nil {
		writeError(w, http.StatusBadRequest, errorBody{
			Error: "malformed request body", Code: "bad_request", Reason: err.Error(),
		})
		return false
	}
	return true
}

// pauseRequest is the POST /api/control/pause body.
type pauseRequest struct {
	Paused bool `json:"paused"`
}

// handleControlPause sets or clears the LOCAL scan pause.
//
// Composition with central's suspend: both gate the scheduler and scanning runs
// only when NEITHER is set. Clearing the local pause therefore does NOT resume
// an agent central suspended — and the response says so explicitly
// (still_paused_by_central), because a resume that appears to succeed while the
// agent stays paused is the most confusing possible outcome.
func (ws *WebUIServer) handleControlPause(w http.ResponseWriter, r *http.Request) {
	if _, ok := ws.controlGate(w, r, "scan"); !ok {
		return
	}
	var req pauseRequest
	if !decodeControlBody(w, r, &req) {
		return
	}
	if ws.cfg.Controls.SetLocalPaused == nil {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "pause is not available on this agent", Code: "control_unavailable"})
		return
	}
	if err := ws.cfg.Controls.SetLocalPaused(r.Context(), req.Paused); err != nil {
		ws.log.Error("local controls: pause failed", "paused", req.Paused, "err", err)
		writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the pause state", Code: "control_write_error"})
		return
	}
	ws.log.Info("local controls: scan pause changed from the local web UI", "paused", req.Paused)
	out := map[string]any{"ok": true, "local_paused": req.Paused}
	if !req.Paused {
		// Report the surviving central hold rather than implying a full resume.
		if st, err := ws.controlState(r.Context()); err == nil && st.CentralSuspended {
			out["still_paused_by_central"] = true
			out["notice"] = "this agent is suspended by central; scanning stays paused until it is resumed from the console"
		}
	}
	writeJSON(w, http.StatusOK, out)
}

// handleControlScanNow triggers one immediate scan.
func (ws *WebUIServer) handleControlScanNow(w http.ResponseWriter, r *http.Request) {
	if _, ok := ws.controlGate(w, r, "scan"); !ok {
		return
	}
	if ws.cfg.Controls.ScanNow == nil {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "scan trigger is not available on this agent", Code: "control_unavailable"})
		return
	}
	// A pause is a standing instruction, not a mood: honour it here too, so
	// "pause" cannot be worked around by pressing "scan now".
	st, err := ws.controlState(r.Context())
	if err == nil {
		if st.CentralSuspended {
			writeError(w, http.StatusConflict, errorBody{
				Error: "this agent is suspended by central; resume it from the console first",
				Code:  "central_suspended",
			})
			return
		}
		if st.LocalPaused {
			writeError(w, http.StatusConflict, errorBody{
				Error: "scanning is paused locally; resume before triggering a scan",
				Code:  "scan_paused",
			})
			return
		}
		if st.ScanRunning {
			writeError(w, http.StatusConflict, errorBody{Error: "a scan is already running", Code: "scan_running"})
			return
		}
	}
	if err := ws.cfg.Controls.ScanNow(r.Context()); err != nil {
		ws.log.Error("local controls: scan trigger failed", "err", err)
		writeError(w, http.StatusInternalServerError, errorBody{Error: "could not start a scan", Code: "control_action_error"})
		return
	}
	ws.log.Info("local controls: scan triggered from the local web UI")
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "started": true})
}

// handleControlSchedule edits the LOCAL schedule overrides.
func (ws *WebUIServer) handleControlSchedule(w http.ResponseWriter, r *http.Request) {
	pv, ok := ws.controlGate(w, r, "schedule")
	if !ok {
		return
	}
	var edit ScheduleEdit
	if !decodeControlBody(w, r, &edit) {
		return
	}
	if ws.cfg.Controls.SetSchedule == nil {
		writeError(w, http.StatusServiceUnavailable, errorBody{Error: "schedule editing is not available on this agent", Code: "control_unavailable"})
		return
	}

	// Which knobs does this request touch? Both a set and a clear count: both
	// are pointless (and misleading) against a key central owns.
	touched := map[string]bool{}
	if edit.ScanCron != nil {
		touched["scan_cron"] = true
	}
	if edit.ScanIntervalSeconds != nil {
		touched["scan_interval_seconds"] = true
	}
	if edit.ScanOnStart != nil {
		touched["scan_on_start"] = true
	}
	for _, k := range edit.Clear {
		touched[k] = true
	}
	for _, k := range edit.Clear {
		if !slicesContains(scheduleKeys, k) {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: "unknown schedule key: " + k, Code: "bad_request", Keys: scheduleKeys,
			})
			return
		}
	}
	if len(touched) == 0 {
		writeError(w, http.StatusBadRequest, errorBody{
			Error: "no schedule change requested", Code: "bad_request", Keys: scheduleKeys,
		})
		return
	}
	var locked []string
	var lockedBy string
	for _, k := range scheduleKeys {
		if !touched[k] {
			continue
		}
		if managed, src := pv.Managed(k); managed {
			locked = append(locked, k)
			lockedBy = src
		}
	}
	if len(locked) > 0 {
		writeError(w, http.StatusConflict, errorBody{
			Error: "managed by central (" + lockedBy + ") — edit it there; a local value would be overwritten on the next policy poll",
			Code:  "managed_by_central",
			Keys:  locked,
		})
		return
	}

	// Validate with the SAME validators the scheduler and central use, so a
	// value accepted here behaves identically to one pushed from the console.
	if edit.ScanCron != nil {
		expr := strings.TrimSpace(*edit.ScanCron)
		if expr == "" {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: "scan_cron cannot be empty — clear it instead to fall back to the local/default schedule",
				Code:  "invalid_cron", Keys: []string{"scan_cron"},
			})
			return
		}
		if _, err := schedule.Parse(expr); err != nil {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: err.Error(), Code: "invalid_cron", Keys: []string{"scan_cron"},
			})
			return
		}
		edit.ScanCron = &expr
	}
	if edit.ScanIntervalSeconds != nil && *edit.ScanIntervalSeconds < agentcfg.MinScanIntervalSeconds {
		writeError(w, http.StatusBadRequest, errorBody{
			Error: "scan_interval_seconds must be at least " +
				strconv.Itoa(agentcfg.MinScanIntervalSeconds) + " seconds (central enforces the same floor)",
			Code: "invalid_interval", Keys: []string{"scan_interval_seconds"},
		})
		return
	}
	if err := ws.cfg.Controls.SetSchedule(r.Context(), edit); err != nil {
		ws.log.Error("local controls: schedule edit failed", "err", err)
		writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the schedule", Code: "control_write_error"})
		return
	}
	ws.log.Info("local controls: scan schedule edited from the local web UI",
		"keys", strings.Join(sortedKeys(touched), ","))
	writeJSON(w, http.StatusOK, map[string]any{"ok": true, "keys": sortedKeys(touched)})
}

// rootsRequest is the POST /api/control/roots body. Location is a POINTER so
// "add a root, leave its share mapping alone" is distinguishable from "add a
// root and clear its mapping" — the same set/absent discipline ScheduleEdit uses.
type rootsRequest struct {
	Action   string  `json:"action"` // "add" | "remove" | "set-share"
	Path     string  `json:"path"`
	Location *string `json:"location"`
}

// handleControlRoots adds or removes one scan root, and edits the share location
// recorded for a root.
//
// The share location rides the ROOTS endpoint and the roots permission on
// purpose: it is per-root configuration edited in the same table, and adding a
// path to allow-list a second endpoint (controlMutationPaths / the method
// backstop) buys nothing but another way for the two lists to drift.
func (ws *WebUIServer) handleControlRoots(w http.ResponseWriter, r *http.Request) {
	pv, ok := ws.controlGate(w, r, "roots")
	if !ok {
		return
	}
	var req rootsRequest
	if !decodeControlBody(w, r, &req) {
		return
	}
	// The central lock covers the ROOT LIST only. A share location is host
	// knowledge with no policy key at all (central cannot know how this machine's
	// paths are exported), so a centrally-derived root may still have its
	// mapping filled in locally — the same split the settings reference draws
	// between fleet-shaped and host-shaped settings.
	if pv.RootsManagedByCentral && req.Action != "set-share" {
		src := pv.PolicySource
		if src == "" {
			src = "central policy"
		}
		writeError(w, http.StatusConflict, errorBody{
			Error: "scan roots are derived centrally (" + src + ", config group scan_selections) — " +
				"a local edit would be recomputed away on the next policy poll",
			Code: "managed_by_central", Keys: []string{"scan_roots"},
		})
		return
	}
	path := strings.TrimSpace(req.Path)
	if path == "" {
		writeError(w, http.StatusBadRequest, errorBody{Error: "path is required", Code: "bad_request"})
		return
	}
	if req.Action == "remove" && req.Location != nil {
		// Removing a root drops its mapping anyway; accepting a location here
		// would make the request read as though it set one.
		writeError(w, http.StatusBadRequest, errorBody{
			Error: `"location" is not valid with "remove" — removing a root also drops its share mapping`,
			Code:  "bad_request",
		})
		return
	}
	// A submitted location is validated with the resolver's OWN parser and
	// refused when the environment already owns the path — before anything is
	// stored. Storing a value the resolver would skip, or one a
	// higher-precedence layer overrides, is the silent-no-op this whole feature
	// exists to eliminate.
	location, ok := ws.shareLocationEdit(w, r, &req)
	if !ok {
		return
	}
	switch req.Action {
	case "set-share":
		if req.Location == nil {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: `set-share requires "location" (send "" to clear the mapping)`, Code: "bad_request",
			})
			return
		}
		if ws.cfg.Controls.SetRootShare == nil {
			writeError(w, http.StatusServiceUnavailable, errorBody{Error: "share mapping is not available on this agent", Code: "control_unavailable"})
			return
		}
		if err := ws.cfg.Controls.SetRootShare(r.Context(), path, location); err != nil {
			ws.log.Error("local controls: share mapping edit failed", "path", path, "err", err)
			writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the share mapping", Code: "control_write_error"})
			return
		}
		ws.log.Info("local controls: share mapping edited from the local web UI",
			"path", path, "location", location)
		writeJSON(w, http.StatusOK, map[string]any{
			"ok": true, "action": req.Action, "path": path, "location": location,
		})
		return
	case "add":
		if ws.cfg.Controls.AddRoot == nil {
			writeError(w, http.StatusServiceUnavailable, errorBody{Error: "root editing is not available on this agent", Code: "control_unavailable"})
			return
		}
		// Validate against the real filesystem: a typo'd root is otherwise a
		// silently empty scan an operator only notices days later.
		fi, err := os.Stat(path)
		if err != nil {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: "path does not exist or is not readable by the agent: " + path,
				Code:  "invalid_root", Reason: err.Error(),
			})
			return
		}
		if !fi.IsDir() {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: "a scan root must be a directory: " + path, Code: "invalid_root",
			})
			return
		}
		if err := ws.cfg.Controls.AddRoot(r.Context(), path); err != nil {
			ws.log.Error("local controls: add root failed", "path", path, "err", err)
			writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the scan roots", Code: "control_write_error"})
			return
		}
		ws.log.Info("local controls: scan root added from the local web UI", "path", path)
		// An add may carry the root's share location in the same request, so
		// "where is this folder on the network?" is answered while the operator
		// is looking at it — the alternative is a root that reports no location
		// until someone remembers to come back and fill it in.
		if req.Location != nil {
			if ws.cfg.Controls.SetRootShare == nil {
				writeError(w, http.StatusServiceUnavailable, errorBody{Error: "share mapping is not available on this agent", Code: "control_unavailable"})
				return
			}
			if err := ws.cfg.Controls.SetRootShare(r.Context(), path, location); err != nil {
				ws.log.Error("local controls: share mapping edit failed", "path", path, "err", err)
				writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the share mapping", Code: "control_write_error"})
				return
			}
			ws.log.Info("local controls: share mapping set from the local web UI",
				"path", path, "location", location)
		}
	case "remove":
		if ws.cfg.Controls.RemoveRoot == nil {
			writeError(w, http.StatusServiceUnavailable, errorBody{Error: "root editing is not available on this agent", Code: "control_unavailable"})
			return
		}
		// A removed root does NOT delete its already-indexed items: the local
		// index is the agent's record of what it saw, and dropping rows here
		// would replicate as a mass deletion to central. Removing a root only
		// stops future walks; the tombstoning path stays the scan's job.
		if err := ws.cfg.Controls.RemoveRoot(r.Context(), path); err != nil {
			ws.log.Error("local controls: remove root failed", "path", path, "err", err)
			writeError(w, http.StatusInternalServerError, errorBody{Error: "could not persist the scan roots", Code: "control_write_error"})
			return
		}
		ws.log.Info("local controls: scan root removed from the local web UI", "path", path)
	default:
		writeError(w, http.StatusBadRequest, errorBody{
			Error: `action must be "add", "remove" or "set-share"`, Code: "bad_request",
		})
		return
	}
	out := map[string]any{"ok": true, "action": req.Action, "path": path}
	if req.Location != nil {
		out["location"] = location
	}
	writeJSON(w, http.StatusOK, out)
}

// shareLocationEdit validates the share location on a roots request and refuses
// the edit when the environment already maps that path. It returns the trimmed
// location (empty = clear) and false when it has already written a refusal.
//
// Two rules, both "refuse rather than store something inert":
//
//  1. The location is parsed by shares.ValidateLocation — the SAME parser the
//     resolver uses. A location this endpoint accepted but the resolver could
//     not parse would be skipped at scan time (hints are best-effort, R1) and
//     the root would silently report nothing, which is precisely the failure the
//     reject list was added to make visible.
//  2. FILEARR_AGENT_SHARE_MAP outranks a locally-authored mapping for the same
//     local path (see staticShareMappings in cmd/filearr-agent for the full
//     argument). Rather than storing a value the resolver will never reach, the
//     edit is refused with the variable named — the same shape as the
//     managed-by-central refusal, with the machine's environment in the owning
//     role because the web UI cannot rewrite it.
func (ws *WebUIServer) shareLocationEdit(w http.ResponseWriter, r *http.Request, req *rootsRequest) (string, bool) {
	if req.Location == nil {
		return "", true
	}
	location := strings.TrimSpace(*req.Location)
	if location != "" {
		if err := shares.ValidateLocation(location); err != nil {
			writeError(w, http.StatusBadRequest, errorBody{
				Error: err.Error(), Code: "invalid_share_location", Keys: []string{"location"},
				Reason: `example: \\tower\media or smb://tower/media`,
			})
			return "", false
		}
	}
	st, err := ws.controlState(r.Context())
	if err != nil {
		ws.log.Error("local controls: state snapshot failed", "err", err)
		writeError(w, http.StatusInternalServerError, errorBody{Error: "control state unavailable", Code: "control_state_error"})
		return "", false
	}
	for _, envPath := range st.ShareMapEnvPaths {
		if !shares.SamePath(envPath, strings.TrimSpace(req.Path)) {
			continue
		}
		writeError(w, http.StatusConflict, errorBody{
			Error: "the share location for this path is set by this machine's environment " +
				"(" + shares.StaticMapSource + ") — edit it there; a local value would never be used",
			Code: "managed_by_environment", Keys: []string{"share_map"},
		})
		return "", false
	}
	return location, true
}

// controlState fetches the live snapshot, tolerating an unwired seam.
func (ws *WebUIServer) controlState(ctx context.Context) (ControlsState, error) {
	if ws.cfg.Controls == nil || ws.cfg.Controls.State == nil {
		return ControlsState{}, nil
	}
	return ws.cfg.Controls.State(ctx)
}

// --- tiny local helpers (no new dependencies) --------------------------------

func slicesContains(list []string, want string) bool {
	for _, s := range list {
		if s == want {
			return true
		}
	}
	return false
}

// sortedKeys returns the set's keys in the fixed scheduleKeys order, so a log
// line and an API response list them the same way every time.
func sortedKeys(set map[string]bool) []string {
	out := make([]string, 0, len(set))
	for _, k := range scheduleKeys {
		if set[k] {
			out = append(out, k)
		}
	}
	return out
}
