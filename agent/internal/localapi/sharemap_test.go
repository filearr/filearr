package localapi

// Tests for the per-root share mapping surface (2026-08-10). The display half
// must work on a CONTAINER agent, where the mapping comes from the environment
// and cannot be edited locally; the edit half is gated exactly like every other
// local control. Each test names the rule it pins.

import (
	"encoding/json"
	"net/http"
	"net/http/httptest"
	"strings"
	"testing"
)

// containerState is the shape a Docker/Unraid agent reports: a root mapped by
// FILEARR_AGENT_SHARE_MAP, another with no mapping at all, and one malformed
// entry that was skipped.
func containerState() ControlsState {
	return ControlsState{
		Roots: []string{"/data/media", "/data/backups"},
		ShareRoots: []RootShare{
			{
				Path: "/data/media", Location: "smb://tower/media",
				UNC: `\\tower\media`, Source: "FILEARR_AGENT_SHARE_MAP",
				EnvValue: "smb://tower/media",
			},
			{Path: "/data/backups"},
		},
		ShareMapRejects:  []ShareMapReject{{Entry: "/data/x=ftp://nope", Source: "FILEARR_AGENT_SHARE_MAP"}},
		ShareMapEnvPaths: []string{"/data/media"},
	}
}

// shareSnapshot is the share half of GET /api/control.
type shareSnapshot struct {
	Permissions map[string]bool `json:"permissions"`
	ShareRoots  []struct {
		Path       string `json:"path"`
		Location   string `json:"location"`
		Source     string `json:"source"`
		EnvValue   string `json:"env_value"`
		LocalValue string `json:"local_value"`
	} `json:"share_roots"`
	ShareMapRejects []struct {
		Entry  string `json:"entry"`
		Source string `json:"source"`
	} `json:"share_map_rejects"`
	ShareMapEnvPaths []string `json:"share_map_env_paths"`
}

func getShareSnapshot(t *testing.T, h http.Handler) shareSnapshot {
	t.Helper()
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, webReq("GET", pathControl, "127.0.0.1"))
	if rec.Code != http.StatusOK {
		t.Fatalf("GET %s = %d, want 200", pathControl, rec.Code)
	}
	var got shareSnapshot
	if err := json.Unmarshal(rec.Body.Bytes(), &got); err != nil {
		t.Fatalf("snapshot is not JSON: %v", err)
	}
	return got
}

// A container-shaped agent renders the mapping READ-ONLY and says which layer
// supplied it: the environment is the machine's own configuration and the web UI
// cannot rewrite it.
func TestContainerShapedShareMapRendersReadOnly(t *testing.T) {
	rs := &recordingSeams{state: containerState()}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	got := getShareSnapshot(t, h)
	if len(got.ShareRoots) != 2 {
		t.Fatalf("share_roots = %+v", got.ShareRoots)
	}
	mapped := got.ShareRoots[0]
	if mapped.Location != "smb://tower/media" || mapped.Source != "FILEARR_AGENT_SHARE_MAP" {
		t.Errorf("mapped root must carry its location AND its source: %+v", mapped)
	}
	if mapped.EnvValue == "" || mapped.LocalValue != "" {
		t.Errorf("an env-provided mapping must be reported as env-provided: %+v", mapped)
	}
	// The explicit no-mapping state is the useful one: nothing else on the agent
	// tells an operator that this root produces no share hint.
	if unmapped := got.ShareRoots[1]; unmapped.Location != "" || unmapped.Source != "" {
		t.Errorf("an unmapped root must resolve to nothing: %+v", unmapped)
	}
	if len(got.ShareMapEnvPaths) != 1 || got.ShareMapEnvPaths[0] != "/data/media" {
		t.Errorf("env-owned paths must be advertised so the field can lock: %+v", got.ShareMapEnvPaths)
	}

	// Even with the permission granted, the env-owned path refuses the edit and
	// names the variable rather than storing a value that would never be used.
	rec := post(t, h, auth, pathControlRoots,
		`{"action":"set-share","path":"/data/media","location":"smb://other/media"}`)
	if rec.Code != http.StatusConflict {
		t.Fatalf("editing an env-owned mapping = %d, want 409", rec.Code)
	}
	eb := decodeErr(t, rec)
	if eb.Code != "managed_by_environment" || !strings.Contains(eb.Error, "FILEARR_AGENT_SHARE_MAP") {
		t.Errorf("the refusal must name the owning surface: %+v", eb)
	}
	if len(rs.rootShare) != 0 {
		t.Fatalf("a refused edit reached the daemon: %+v", rs.rootShare)
	}
}

// Malformed entries are surfaced, not swallowed: a typo in the share map is
// otherwise a root that silently reports no location forever.
func TestMalformedShareMapEntriesAreSurfaced(t *testing.T) {
	rs := &recordingSeams{state: containerState()}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()

	got := getShareSnapshot(t, ws.buildHandler(auth))
	if len(got.ShareMapRejects) != 1 {
		t.Fatalf("rejects = %+v", got.ShareMapRejects)
	}
	if got.ShareMapRejects[0].Entry != "/data/x=ftp://nope" {
		t.Errorf("the malformed entry must appear VERBATIM: %+v", got.ShareMapRejects[0])
	}
	if got.ShareMapRejects[0].Source != "FILEARR_AGENT_SHARE_MAP" {
		t.Errorf("a reject must name the surface it came from: %+v", got.ShareMapRejects[0])
	}
}

// The share mapping rides local_roots_control: with the permission off, the edit
// is refused before the daemon is touched.
func TestShareMappingEditNeedsRootsPermission(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.RootsControl = false
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()

	rec := post(t, ws.buildHandler(auth), auth, pathControlRoots,
		`{"action":"set-share","path":"/data/media","location":"smb://tower/media"}`)
	if rec.Code != http.StatusForbidden {
		t.Fatalf("set-share without local_roots_control = %d, want 403", rec.Code)
	}
	if eb := decodeErr(t, rec); eb.Code != "local_control_denied" ||
		!strings.Contains(strings.Join(eb.Keys, ","), "local_roots_control") {
		t.Errorf("refusal must name the policy key: %+v", eb)
	}
	if len(rs.rootShare) != 0 {
		t.Fatalf("a denied edit reached the daemon: %+v", rs.rootShare)
	}
}

// A mutation needs a session even where the policy leaves reads open — the same
// rule every other control obeys, restated for the new action because "it is
// only a display hint" is exactly the argument that would erode it.
func TestShareMappingEditRequiresAuth(t *testing.T) {
	rs := &recordingSeams{state: containerState()}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.AuthRequired = false
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	r := httptest.NewRequest(http.MethodPost, pathControlRoots,
		strings.NewReader(`{"action":"set-share","path":"/data/new","location":"smb://tower/new"}`))
	r.Host = "127.0.0.1"
	rec := httptest.NewRecorder()
	h.ServeHTTP(rec, r)
	if rec.Code != http.StatusUnauthorized {
		t.Fatalf("anonymous share edit = %d, want 401", rec.Code)
	}
	if len(rs.rootShare) != 0 {
		t.Fatalf("an unauthenticated edit reached the daemon: %+v", rs.rootShare)
	}
	// The same request WITH a session is accepted, so the 401 above is the auth
	// gate and not a broken request shape.
	if rec := post(t, h, auth, pathControlRoots,
		`{"action":"set-share","path":"/data/new","location":"smb://tower/new"}`); rec.Code != http.StatusOK {
		t.Fatalf("authenticated share edit = %d (%s), want 200", rec.Code, rec.Body.String())
	}
}

// A submitted location is validated with the resolver's own parser, so a value
// this endpoint accepts cannot be one the resolver silently skips later.
func TestShareLocationValidation(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	bad := []string{
		`{"action":"set-share","path":"/data/media","location":"tower/media"}`,
		`{"action":"set-share","path":"/data/media","location":"ftp://tower/media"}`,
		`{"action":"set-share","path":"/data/media","location":"smb://tower"}`,
	}
	for _, body := range bad {
		rec := post(t, h, auth, pathControlRoots, body)
		if rec.Code != http.StatusBadRequest {
			t.Errorf("%s = %d, want 400", body, rec.Code)
			continue
		}
		if got := decodeErr(t, rec).Code; got != "invalid_share_location" {
			t.Errorf("%s error code = %q", body, got)
		}
	}
	if len(rs.rootShare) != 0 {
		t.Fatalf("an invalid location was stored: %+v", rs.rootShare)
	}

	// The UNC form travels as JSON, so its backslashes are escaped twice here:
	// the wire value is \\tower\media\tv.
	good := []string{"smb://tower/media", `\\\\tower\\media\\tv`, "nfs://tower/mnt/user/iso"}
	for _, loc := range good {
		body := `{"action":"set-share","path":"/data/media","location":"` + loc + `"}`
		if rec := post(t, h, auth, pathControlRoots, body); rec.Code != http.StatusOK {
			t.Errorf("%s = %d (%s), want 200", body, rec.Code, rec.Body.String())
		}
	}
	if len(rs.rootShare) != len(good) {
		t.Fatalf("valid edits did not all reach the daemon: %+v", rs.rootShare)
	}

	// An empty location CLEARS the mapping (a distinct, legitimate action), while
	// omitting the key entirely is not a share edit at all.
	if rec := post(t, h, auth, pathControlRoots,
		`{"action":"set-share","path":"/data/media","location":""}`); rec.Code != http.StatusOK {
		t.Fatalf("clearing a mapping = %d, want 200", rec.Code)
	}
	if last := rs.rootShare[len(rs.rootShare)-1]; last != "/data/media=" {
		t.Errorf("clear did not reach the daemon as an empty location: %q", last)
	}
	rec := post(t, h, auth, pathControlRoots, `{"action":"set-share","path":"/data/media"}`)
	if rec.Code != http.StatusBadRequest || decodeErr(t, rec).Code != "bad_request" {
		t.Errorf("set-share without a location must be a 400, got %d", rec.Code)
	}
}

// Adding a root may carry its share location in the same request: both seams run
// so the new root reports a location immediately.
func TestAddRootWithShareLocation(t *testing.T) {
	rs := &recordingSeams{}
	ws := controlWebUI(t, allControlsPolicy, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)
	dir := t.TempDir()

	rec := post(t, h, auth, pathControlRoots,
		`{"action":"add","path":"`+jsonPath(dir)+`","location":"smb://tower/media"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("add with location = %d (%s)", rec.Code, rec.Body.String())
	}
	if len(rs.addRoot) != 1 || len(rs.rootShare) != 1 {
		t.Fatalf("both the root and its mapping must be persisted: roots=%+v shares=%+v", rs.addRoot, rs.rootShare)
	}
	if !strings.HasSuffix(rs.rootShare[0], "=smb://tower/media") {
		t.Errorf("mapping not persisted for the added root: %q", rs.rootShare[0])
	}

	// A malformed location must fail BEFORE the root is added: a half-applied
	// request that adds a root and drops its mapping is worse than a refusal.
	rs2 := &recordingSeams{}
	ws2 := controlWebUI(t, allControlsPolicy, rs2.seams())
	auth2, _ := newWebAuth()
	rec = post(t, ws2.buildHandler(auth2), auth2, pathControlRoots,
		`{"action":"add","path":"`+jsonPath(dir)+`","location":"nope"}`)
	if rec.Code != http.StatusBadRequest || decodeErr(t, rec).Code != "invalid_share_location" {
		t.Fatalf("add with a malformed location = %d (%s)", rec.Code, rec.Body.String())
	}
	if len(rs2.addRoot) != 0 {
		t.Fatalf("the root was added despite the invalid mapping: %+v", rs2.addRoot)
	}
}

// Centrally-derived ROOTS stay locked, but their share mapping does not: a share
// location is host knowledge with no policy key, so central cannot supply it and
// must not block it.
func TestCentrallyDerivedRootsStillAllowShareMapping(t *testing.T) {
	rs := &recordingSeams{state: ControlsState{Roots: []string{"/data/media"}}}
	ws := controlWebUI(t, func() PolicyView {
		pv := allControlsPolicy()
		pv.RootsManagedByCentral = true
		return pv
	}, rs.seams())
	auth, _ := newWebAuth()
	h := ws.buildHandler(auth)

	if rec := post(t, h, auth, pathControlRoots, `{"action":"add","path":"/data/other"}`); rec.Code != http.StatusConflict {
		t.Fatalf("adding a root under central derivation = %d, want 409", rec.Code)
	}
	rec := post(t, h, auth, pathControlRoots,
		`{"action":"set-share","path":"/data/media","location":"smb://tower/media"}`)
	if rec.Code != http.StatusOK {
		t.Fatalf("share mapping under central root derivation = %d (%s), want 200", rec.Code, rec.Body.String())
	}
	if len(rs.rootShare) != 1 {
		t.Fatalf("the mapping did not reach the daemon: %+v", rs.rootShare)
	}
}

// A build without the share seam reports unavailable rather than panicking —
// the same shape every other unwired seam has.
func TestShareMappingUnavailableWithoutSeam(t *testing.T) {
	rs := &recordingSeams{}
	seams := rs.seams()
	seams.SetRootShare = nil
	ws := controlWebUI(t, allControlsPolicy, seams)
	auth, _ := newWebAuth()

	rec := post(t, ws.buildHandler(auth), auth, pathControlRoots,
		`{"action":"set-share","path":"/data/media","location":"smb://tower/media"}`)
	if rec.Code != http.StatusServiceUnavailable {
		t.Fatalf("unwired share seam = %d, want 503", rec.Code)
	}
	if got := decodeErr(t, rec).Code; got != "control_unavailable" {
		t.Errorf("error code = %q", got)
	}
}
