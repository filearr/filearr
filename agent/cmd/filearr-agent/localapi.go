package main

import (
	"context"
	"encoding/json"
	"fmt"
	"path/filepath"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/query"
)

// startLocalAPI launches the P7-T2 local query transport for the `run` daemon: an
// HTTP/1.1 server over a same-user-only Unix socket (linux/darwin) or Windows
// named pipe, serving the read-only query engine. It returns a done-channel for a
// clean stop.
//
// The server gates on the cached central policy's local_access_enabled key: it
// refuses to start when disabled and stops within one gate interval if a policy
// update disables it (server.Run re-reads the gate). Because the poller persists
// policy.json each cycle, re-reading the cache here honors a flip within one poll
// interval — the "check the cached policy per request" option (the Applier's typed
// view does not yet carry the local-access keys; P7-T4 promotes them).
func startLocalAPI(ctx context.Context, dataDir, socketOverride string, idx *index.Store, hist *history.Store) <-chan struct{} {
	done := make(chan struct{})
	log := newLogger()

	searcher, err := query.NewSearcher(filepath.Join(dataDir, indexDBName))
	if err != nil {
		log.Error("local query API disabled: cannot open read-only index", "err", err)
		close(done)
		return done
	}

	path := socketOverride
	if path == "" {
		path = localapi.DefaultPath(dataDir)
	}
	cache := agentcfg.NewETagCache(dataDir)

	cfg := localapi.Config{
		Path:     path,
		Searcher: searcher,
		Count:    func(ctx context.Context) (int, error) { return countActiveItems(ctx, idx) },
		Policy:   func() localapi.PolicyView { return loadPolicyView(cache) },
		Logger:   log,
	}
	// Only wire history when the store actually opened — a typed-nil interface would
	// panic on Record. The socket surface gets the full History view (record + read).
	if hist != nil {
		cfg.History = hist
	}
	srv := localapi.New(cfg)

	go func() {
		defer close(done)
		defer searcher.Close()
		if err := srv.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("local query API loop exited", "err", err)
		}
	}()
	return done
}

// countActiveItems reports the count of active (non-tombstoned) items for the
// health probe. Uses the writable store's handle for a read-only COUNT — the
// query surface itself only ever touches the separate read-only connection.
func countActiveItems(ctx context.Context, idx *index.Store) (int, error) {
	var n int
	err := idx.DB().QueryRowContext(ctx,
		`SELECT COUNT(*) FROM items WHERE status = ?`, index.StatusActive).Scan(&n)
	return n, err
}

// loadPolicyView reads the cached central policy and derives the localapi gate
// view, including the P7-T4 freshness (offline-grace) computation and the
// path-scope predicate list. A never-contacted agent (no cache) defaults to local
// access ENABLED, web UI DISABLED, no scope (CLI default-on, brief §5.2). The
// offline-grace default is DefaultOfflineGrace (== defaultReconcileInterval, R4).
func loadPolicyView(cache *agentcfg.ETagCache) localapi.PolicyView {
	doc, ok, err := cache.Load()
	if err != nil || !ok {
		return localapi.PolicyView{LocalAccessEnabled: true}
	}
	ls := doc.LocalSurface(time.Now(), agentcfg.DefaultOfflineGrace)
	pv := localapi.PolicyView{
		LocalAccessEnabled: ls.LocalAccessEnabled,
		WebUIEnabled:       ls.WebUIEnabled, // effective (policy intent AND fresh)
		AuthRequired:       ls.AuthRequired,
		HasVersion:         true,
		Version:            ls.Version,
		Predicates:         ls.Predicates,
		Stale:              ls.Stale,
		// Local self-administration (2026-08-10): the three permissions plus the
		// set of keys central EXPLICITLY set — the local UI renders any key in
		// that set read-only ("managed by central") and refuses to edit it,
		// because central re-applies its document every poll.
		ScanControl:     ls.ScanControl,
		ScheduleControl: ls.ScheduleControl,
		RootsControl:    ls.RootsControl,
		CentralKeys:     ls.CentralKeys,
		PolicySource:    policySourceLabel(ls.Scope, ls.Version),
		// Roots have no top-level policy key: they are centrally MANAGED only
		// when a config group's scan_selections derives them (the same document
		// consumeScanRootSeam expands), in which case a local edit would be
		// recomputed away.
		RootsManagedByCentral: hasGroupScanSelections(doc),
	}
	if !ls.GraceExpiresAt.IsZero() {
		g := ls.GraceExpiresAt
		pv.GraceExpiresAt = &g
	}
	return pv
}

// policySourceLabel names the document a centrally-managed value came from, in
// the same wording the Status panel's extraction "source" already uses ("central
// policy global v7"). A local operator who is refused an edit needs to know
// WHICH scope to go change, not just that "central" owns it.
func policySourceLabel(scope string, version int) string {
	if scope == "" {
		scope = "unknown scope"
	}
	return fmt.Sprintf("central policy %s v%d", scope, version)
}

// hasGroupScanSelections reports whether the cached policy carries an ENABLED
// config-group scan_selections entry — i.e. this agent's scan roots are DERIVED
// centrally, so a local root edit would be recomputed away.
//
// It reads the document rather than calling inventory.ExpandScanSelections
// deliberately: this runs on every local-API request (the policy view is
// rebuilt per request), and expansion walks the filesystem for globs and preset
// paths. The question here is only "does central author roots for this agent",
// which the document answers on its own. The `enabled` semantics mirror
// inventory's scanSelection.enabled(): absent means enabled.
func hasGroupScanSelections(doc agentcfg.PolicyDoc) bool {
	var body struct {
		Group struct {
			ScanSelections []struct {
				Enabled *bool `json:"enabled"`
			} `json:"scan_selections"`
		} `json:"group"`
	}
	if len(doc.Policy) == 0 || json.Unmarshal(doc.Policy, &body) != nil {
		return false
	}
	for _, sel := range body.Group.ScanSelections {
		if sel.Enabled == nil || *sel.Enabled {
			return true
		}
	}
	return false
}
