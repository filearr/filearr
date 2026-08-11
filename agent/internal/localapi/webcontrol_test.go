package localapi

// Tests for the local scan-control surface (2026-08-10). Everything here is a
// SECURITY property of a surface that was read-only by construction until this
// feature, so each test names the rule it pins:
//
//   - a control refuses unless central granted its permission (default off);
//   - a key central explicitly set is locked locally, whoever asks;
//   - a mutation needs a session even when the policy says auth_required:false;
//   - the method backstop still 405s every non-control path, every verb.

import (
	"context"
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"os"
	"strings"
	"testing"
)

// recordingSeams is a ControlSeams whose actions only record that they ran, so
// a test can assert "the handler refused BEFORE touching the daemon".
type recordingSeams struct {
	state       ControlsState
	pausedCalls []bool
	scanNow     int
	schedule    []ScheduleEdit
	addRoot     []string
	removeRoot  []string
	rootShare   []string // "path=location" per accepted share-mapping edit
}

func (rs *recordingSeams) seams() *ControlSeams {
	return &ControlSeams{
		State: func(context.Context) (ControlsState, error) { return rs.state, nil },
		SetLocalPaused: func(_ context.Context, p bool) error {
			rs.pausedCalls = append(rs.pausedCalls, p)
			rs.state.LocalPaused = p
			return nil
		},
		ScanNow:     func(context.Context) error { rs.scanNow++; return nil },
		SetSchedule: func(_ context.Context, e ScheduleEdit) error { rs.schedule = append(rs.schedule, e); return nil },
		AddRoot:     func(_ context.Context, p string) error { rs.addRoot = append(rs.addRoot, p); return nil },
		RemoveRoot:  func(_ context.Context, p string) error { rs.removeRoot = append(rs.removeRoot, p); return nil },
		SetRootShare: func(_ context.Context, p, loc string) error {
			rs.rootShare = append(rs.rootShare, p+"="+loc)
			return nil
		},
	}
}

// controlWebUI wires a web UI with control seams over the seeded index.
func controlWebUI(t *testing.T, policy func() PolicyView, seams *ControlSeams) *WebUIServer {
	t.Helper()
	ws := testWebUI(t, DefaultWebAddr, policy)
	ws.cfg.Controls = seams
	return ws
}

// post issues an authenticated control POST (session cookie attached).
func post(t *testing.T, h http.Handler, auth webAuth, path, body string) *httptest.ResponseRecorder {
	t.Helper()
	r := httptest.NewRequest(http.MethodPost, path, strings.NewReader(body))
	r.Host = "127.0.0.1"
	r.Header.Set("Content-Type", "application/json")
	r.AddCookie(&http.Cookie{Name: webSessionCookie, Value: auth.session})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	return rec
}

func decodeErr(t *testing.T, rec *httptest.ResponseRecorder) errorBody {
	t.Helper()
	var eb errorBody
	if err := json.Unmarshal(rec.Body.Bytes(), &eb); err != nil {
		t.Fatalf("response body is not an error envelope: %s", rec.Body.String())
	}
	return eb
}

// allControlsPolicy grants all three permissions with nothing centrally set.
func allControlsPolicy() PolicyView {
	return PolicyView{
		WebUIEnabled: true, ScanControl: true, ScheduleControl: true, RootsControl: true,
		CentralKeys: map[string]bool{}, PolicySource: "central policy global v3",
	}
}

// --- permission gating: every action is refused when its key is off ----------

func TestControlActionsRefusedWhenPermissionOff(t *testing.T) {
	// The never-contacted / never-delegated default: web UI on, no permissions.
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView { return PolicyView{WebUIEnabled: true} }, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	cases := []struct {
		path, body, key string
	}{
		{pathControlPause, `{"paused":true}`, "local_scan_control"},
		{pathControlScanNow, `{}`, "local_scan_control"},
		{pathControlSchedule, `{"scan_cron":"0 3 * * *"}`, "local_schedule_control"},
		{pathControlRoots, `{"action":"add","path":"."}`, "local_roots_control"},
	}
	for _, c := range cases {
		rec := post(t, h, auth, c.path, c.body)
		if rec.Code != http.StatusForbidden {
			t.Errorf("POST %s with no permission = %d, want 403", c.path, rec.Code)
			continue
		}
		eb := decodeErr(t, rec)
		if eb.Code != "local_control_denied" {
			t.Errorf("POST %s error code = %q, want local_control_denied", c.path, eb.Code)
		}
		if len(eb.Keys) != 1 || eb.Keys[0] != c.key {
			t.Errorf("POST %s must name the policy key it needs, got %v", c.path, eb.Keys)
		}
	}
	// Nothing reached the daemon.
	if len(rs.pausedCalls) != 0 || rs.scanNow != 0 || len(rs.schedule) != 0 || len(rs.addRoot) != 0 {
		t.Fatalf("a refused control still invoked its seam: %+v", rs)
	}
}

// One permission granted must not grant the others.
func TestControlPermissionsAreIndependent(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		return PolicyView{WebUIEnabled: true, ScanControl: true}
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	if rec := post(t, h, auth, pathControlPause, `{"paused":true}`); rec.Code != http.StatusOK {
		t.Fatalf("granted scan control must allow pause: %d %s", rec.Code, rec.Body.String())
	}
	if rec := post(t, h, auth, pathControlSchedule, `{"scan_on_start":true}`); rec.Code != http.StatusForbidden {
		t.Fatalf("scan control must NOT imply schedule control: got %d", rec.Code)
	}
	if rec := post(t, h, auth, pathControlRoots, `{"action":"remove","path":"/x"}`); rec.Code != http.StatusForbidden {
		t.Fatalf("scan control must NOT imply roots control: got %d", rec.Code)
	}
}

// --- the centrally-managed lock ----------------------------------------------

func TestScheduleKeyCentrallySetIsLockedLocally(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.CentralKeys = map[string]bool{"scan_cron": true}
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	// Setting the locked key is refused, and the refusal names the document.
	rec := post(t, h, auth, pathControlSchedule, `{"scan_cron":"0 4 * * *"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("editing a centrally-set key = %d, want 409", rec.Code)
	}
	eb := decodeErr(t, rec)
	if eb.Code != "managed_by_central" {
		t.Fatalf("error code = %q, want managed_by_central", eb.Code)
	}
	if !strings.Contains(eb.Error, "central policy global v3") {
		t.Errorf("refusal must name the owning document, got %q", eb.Error)
	}
	if len(eb.Keys) != 1 || eb.Keys[0] != "scan_cron" {
		t.Errorf("refusal must name the locked key, got %v", eb.Keys)
	}

	// CLEARING a locked key is refused too: a clear against a key central owns
	// is just as pointless as a set, and accepting it would imply the local
	// value mattered.
	if rec := post(t, h, auth, pathControlSchedule, `{"clear":["scan_cron"]}`); rec.Code != http.StatusConflict {
		t.Errorf("clearing a centrally-set key = %d, want 409", rec.Code)
	}

	// A key central left UNSET is still editable — that is the whole point of
	// "local may only fill in what central did not set".
	if rec := post(t, h, auth, pathControlSchedule, `{"scan_on_start":true}`); rec.Code != http.StatusOK {
		t.Fatalf("an unset key must stay locally editable: %d %s", rec.Code, rec.Body.String())
	}
	if len(rs.schedule) != 1 || rs.schedule[0].ScanOnStart == nil || !*rs.schedule[0].ScanOnStart {
		t.Fatalf("the allowed edit did not reach the daemon: %+v", rs.schedule)
	}
}

func TestRootsLockedWhenDerivedCentrally(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.RootsManagedByCentral = true
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	rec := post(t, h, auth, pathControlRoots, `{"action":"add","path":"."}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("root edit with centrally-derived roots = %d, want 409", rec.Code)
	}
	if eb := decodeErr(t, rec); eb.Code != "managed_by_central" {
		t.Fatalf("error code = %q, want managed_by_central", eb.Code)
	}
	if len(rs.addRoot) != 0 {
		t.Fatalf("a locked root edit still reached the daemon: %v", rs.addRoot)
	}
}

// The GET snapshot must advertise the lock so the page can render the field
// read-only with a reason instead of offering an edit that will 409.
func TestControlSnapshotAdvertisesPermissionsAndLocks(t *testing.T) {
	rs := &recordingSeams{state: ControlsState{
		LocalPaused: true, ScanCron: "0 3 * * *", ScanCronSource: "central policy global v3",
		Roots: []string{"/data/media"},
	}}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.ScheduleControl = false
		pv.CentralKeys = map[string]bool{"scan_cron": true}
		pv.RootsManagedByCentral = true
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", pathControl, "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET %s = %d, want 200", pathControl, rec.Code)
	}
	var body struct {
		Available      bool              `json:"available"`
		Permissions    map[string]bool   `json:"permissions"`
		Managed        map[string]string `json:"managed"`
		RootsManagedBy string            `json:"roots_managed_by"`
		ReadOnly       bool              `json:"read_only"`
		LocalPaused    bool              `json:"local_paused"`
		Roots          []string          `json:"roots"`
	}
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if !body.Available || !body.Permissions["scan"] || body.Permissions["schedule"] {
		t.Errorf("permissions not advertised faithfully: %+v", body.Permissions)
	}
	if body.Managed["scan_cron"] != "central policy global v3" {
		t.Errorf("locked key must carry its owning document, got %q", body.Managed["scan_cron"])
	}
	if !strings.Contains(body.RootsManagedBy, "scan_selections") {
		t.Errorf("roots lock not advertised: %q", body.RootsManagedBy)
	}
	if !body.ReadOnly {
		t.Error("the snapshot must keep restating that the CATALOG is read-only here")
	}
	if !body.LocalPaused || len(body.Roots) != 1 {
		t.Errorf("live state not passed through: %+v", body)
	}
}

// --- auth: mutations always need a session ------------------------------------

func TestMutationsRequireAuthEvenWhenAuthNotRequired(t *testing.T) {
	rs := &recordingSeams{}
	// auth_required is FALSE: reads are deliberately open on this agent.
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.AuthRequired = false
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	// A read still serves anonymously.
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/api/status", "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("anonymous read with auth_required=false = %d, want 200", rec.Code)
	}

	// Every mutation without a session is 401 — anonymous "stop scanning this
	// machine" must never be inherited from anonymous reads.
	for _, p := range []string{pathControlPause, pathControlScanNow, pathControlSchedule, pathControlRoots} {
		r := httptest.NewRequest(http.MethodPost, p, strings.NewReader(`{}`))
		r.Host = "127.0.0.1"
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, r)
		if rec.Code != http.StatusUnauthorized {
			t.Errorf("anonymous POST %s = %d, want 401", p, rec.Code)
		}
	}
	if len(rs.pausedCalls) != 0 || rs.scanNow != 0 {
		t.Fatalf("an unauthenticated control reached the daemon: %+v", rs)
	}

	// The bootstrap token is still exchangeable in this mode, which is how an
	// operator signs in for the controls when reads are open.
	rec = httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", "/?token="+auth.token, "127.0.0.1"))
	if rec.Code != http.StatusSeeOther {
		t.Fatalf("token exchange with auth_required=false = %d, want 303", rec.Code)
	}
	var session string
	for _, c := range rec.Result().Cookies() {
		if c.Name == webSessionCookie {
			session = c.Value
		}
	}
	if session == "" {
		t.Fatal("no session cookie issued: the controls would be unreachable")
	}
	if rec := post(t, h, auth, pathControlPause, `{"paused":true}`); rec.Code != http.StatusOK {
		t.Fatalf("authenticated pause after exchange = %d, want 200", rec.Code)
	}
}

// --- validation ---------------------------------------------------------------

func TestScheduleValidationMatchesCentral(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	bad := []struct{ body, code string }{
		{`{"scan_cron":"not a cron"}`, "invalid_cron"},
		{`{"scan_cron":"0 3 * *"}`, "invalid_cron"},
		{`{"scan_cron":"  "}`, "invalid_cron"},
		{`{"scan_interval_seconds":299}`, "invalid_interval"},
		{`{}`, "bad_request"},
		{`{"clear":["nope"]}`, "bad_request"},
		{`{"totally_unknown":1}`, "bad_request"},
	}
	for _, c := range bad {
		rec := post(t, h, auth, pathControlSchedule, c.body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("schedule %s = %d, want 400", c.body, rec.Code)
			continue
		}
		if got := decodeErr(t, rec).Code; got != c.code {
			t.Errorf("schedule %s error code = %q, want %q", c.body, got, c.code)
		}
	}
	// 300s is central's floor and must be accepted here too.
	if rec := post(t, h, auth, pathControlSchedule, `{"scan_interval_seconds":300}`); rec.Code != http.StatusOK {
		t.Fatalf("the exact central floor must be accepted: %d %s", rec.Code, rec.Body.String())
	}
	if len(rs.schedule) != 1 {
		t.Fatalf("only the valid edit should have reached the daemon: %+v", rs.schedule)
	}
}

func TestRootValidation(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	dir := t.TempDir()
	file := dir + "/afile"
	if err := writeTempFile(file); err != nil {
		t.Fatal(err)
	}

	bad := []struct{ body, code string }{
		{`{"action":"add","path":""}`, "bad_request"},
		{`{"action":"sideways","path":"` + jsonPath(dir) + `"}`, "bad_request"},
		{`{"action":"add","path":"` + jsonPath(dir+"/nope") + `"}`, "invalid_root"},
		{`{"action":"add","path":"` + jsonPath(file) + `"}`, "invalid_root"},
	}
	for _, c := range bad {
		rec := post(t, h, auth, pathControlRoots, c.body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("roots %s = %d, want 400", c.body, rec.Code)
			continue
		}
		if got := decodeErr(t, rec).Code; got != c.code {
			t.Errorf("roots %s error code = %q, want %q", c.body, got, c.code)
		}
	}
	if rec := post(t, h, auth, pathControlRoots, `{"action":"add","path":"`+jsonPath(dir)+`"}`); rec.Code != http.StatusOK {
		t.Fatalf("a real directory must be accepted: %d %s", rec.Code, rec.Body.String())
	}
	// A remove is NOT existence-checked: an operator must be able to drop a root
	// whose disk is already gone (the usual reason for removing one).
	if rec := post(t, h, auth, pathControlRoots, `{"action":"remove","path":"`+jsonPath(dir+"/nope")+`"}`); rec.Code != http.StatusOK {
		t.Fatalf("removing a vanished root must be allowed: %d %s", rec.Code, rec.Body.String())
	}
}

// --- pause / central suspend composition ---------------------------------------

func TestLocalResumeDoesNotClearACentralSuspend(t *testing.T) {
	rs := &recordingSeams{state: ControlsState{LocalPaused: true, CentralSuspended: true}}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	rec := post(t, h, auth, pathControlPause, `{"paused":false}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("local resume = %d, want 200", rec.Code)
	}
	var body map[string]any
	if err := json.Unmarshal(rec.Body.Bytes(), &body); err != nil {
		t.Fatal(err)
	}
	if body["still_paused_by_central"] != true {
		t.Fatalf("a local resume under a central suspend must say so: %v", body)
	}
	// Only the LOCAL flag was touched; the central suspend is untouched, and a
	// scan trigger is still refused.
	if len(rs.pausedCalls) != 1 || rs.pausedCalls[0] {
		t.Fatalf("resume must clear only the local flag: %v", rs.pausedCalls)
	}
	if !rs.state.CentralSuspended {
		t.Fatal("a local resume cleared the CENTRAL suspend — the local operator just defeated the fleet control")
	}
	rec = post(t, h, auth, pathControlScanNow, `{}`)
	if rec.Code != http.StatusConflict || decodeErr(t, rec).Code != "central_suspended" {
		t.Fatalf("scan now under a central suspend = %d %s, want 409 central_suspended", rec.Code, rec.Body.String())
	}
}

func TestScanNowRefusedWhilePausedLocally(t *testing.T) {
	rs := &recordingSeams{state: ControlsState{LocalPaused: true}}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	rec := post(t, h, auth, pathControlScanNow, `{}`)
	if rec.Code != http.StatusConflict || decodeErr(t, rec).Code != "scan_paused" {
		t.Fatalf("scan now while paused = %d %s, want 409 scan_paused", rec.Code, rec.Body.String())
	}
	if rs.scanNow != 0 {
		t.Fatal(`"scan now" worked around a local pause`)
	}
}

// --- unwired seams / disabled UI ----------------------------------------------

func TestControlsUnavailableWithoutSeams(t *testing.T) {
	ws := testWebUI(t, DefaultWebAddr, allControlsPolicy) // no Controls wired
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	rec := post(t, h, auth, pathControlPause, `{"paused":true}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("unwired controls = %d, want 503", rec.Code)
	}
	if got := decodeErr(t, rec).Code; got != "control_unavailable" {
		t.Fatalf("error code = %q, want control_unavailable", got)
	}
}

func TestControlsRefusedWhenWebUIDisabledByPolicy(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.WebUIEnabled = false // central disabled it, or the policy went stale
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	if rec := post(t, h, auth, pathControlPause, `{"paused":true}`); rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("controls with the web UI disabled = %d, want 503", rec.Code)
	}
	if len(rs.pausedCalls) != 0 {
		t.Fatal("a control ran while the web UI was policy-disabled")
	}
}

// --- the narrow backstop opening ------------------------------------------------

// Only POST, and only on the four control paths, may pass the method backstop.
func TestBackstopOpensOnlyPostOnControlPaths(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	for _, p := range []string{pathControlPause, pathControlScanNow, pathControlSchedule, pathControlRoots} {
		for _, m := range []string{http.MethodPut, http.MethodDelete, http.MethodPatch} {
			rec := httptest.NewRecorder()
			h.ServeHTTP(rec, webReq(m, p, "127.0.0.1"))
			if rec.Code != http.StatusMethodNotAllowed {
				t.Errorf("%s %s = %d, want 405 (only POST is opened)", m, p, rec.Code)
			}
		}
	}
	// A path that merely LOOKS like a control path is not one.
	for _, p := range []string{"/api/control", "/api/control/", "/api/control/pause/", "/api/control/roots/x", "/api/controlx"} {
		rec := httptest.NewRecorder()
		h.ServeHTTP(rec, webReq(http.MethodPost, p, "127.0.0.1"))
		if rec.Code != http.StatusMethodNotAllowed {
			t.Errorf("POST %s = %d, want 405 (not in the backstop allow-list)", p, rec.Code)
		}
	}
}

// The backstop runs BEFORE auth, so an unauthenticated POST to a non-control
// path is 405 rather than 401 — a mutating verb never reaches a handler at all.
func TestBackstopBeatsAuthOnNonControlPaths(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.AuthRequired = true
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	r := httptest.NewRequest(http.MethodPost, "/api/query", strings.NewReader(`{}`))
	r.Host = "127.0.0.1"
	r.AddCookie(&http.Cookie{Name: webSessionCookie, Value: auth.session})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Code != http.StatusMethodNotAllowed {
		t.Fatalf("POST /api/query with a valid session = %d, want 405", rec.Code)
	}
}

// A forged Host is still 403'd before a control ever runs.
func TestControlsRejectForgedHost(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	r := httptest.NewRequest(http.MethodPost, pathControlPause, strings.NewReader(`{"paused":true}`))
	r.Host = "evil.example"
	r.AddCookie(&http.Cookie{Name: webSessionCookie, Value: auth.session})
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("control POST with a forged Host = %d, want 403", rec.Code)
	}
	if len(rs.pausedCalls) != 0 {
		t.Fatal("a DNS-rebinding-style request reached a control")
	}
}

// --- small helpers -------------------------------------------------------------

func writeTempFile(path string) error {
	return os.WriteFile(path, []byte("x"), 0o644)
}

// jsonPath escapes a filesystem path for embedding in a JSON string literal
// (Windows paths are full of backslashes).
func jsonPath(p string) string {
	b, _ := json.Marshal(p)
	return string(b[1 : len(b)-1])
}
