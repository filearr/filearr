package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"

	"github.com/filearr/filearr/agent/internal/agentlog"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/query"
)

// envWebUIAddr overrides the local web UI loopback bind address (host:port). The
// -web-addr flag wins when both are set.
const envWebUIAddr = "FILEARR_AGENT_WEBUI_ADDR"

// envWebUIAllowRemote (containerized/NAS agents) opts the web UI into a
// NON-loopback bind: a Docker port mapping cannot reach a loopback listener,
// so a container exposing the UI needs this. With it set (and no explicit
// addr) the bind defaults to 0.0.0.0:8686. The central policy gate
// (web_ui_enabled) and the auth gate still apply — this only widens the
// LISTENER, deliberately and loudly (a startup WARN names the exposure).
const envWebUIAllowRemote = "FILEARR_AGENT_WEBUI_ALLOW_REMOTE"

// startWebUI launches the P7-T5 local web UI for the `run` daemon: a read-only
// browser search surface on a loopback TCP listener (127.0.0.1), SEPARATE from the
// P7-T2 unix-socket/named-pipe query transport (browsers can't dial a UDS). It
// returns a done-channel for a clean stop.
//
// The listener gates on the cached policy's EFFECTIVE web-UI capability
// (web_ui_enabled AND policy fresh within offline grace — config.LocalSurface /
// PolicyView.WebUIEnabled). A central disable, or the policy going stale past
// grace, takes the UI down within one gate interval with no central push (R4
// fail-closed asymmetry); the query socket transport is unaffected. A
// never-contacted agent starts with the web UI OFF.
func startWebUI(ctx context.Context, dataDir, webAddr string, idx *index.Store, hist *history.Store) <-chan struct{} {
	done := make(chan struct{})
	log := newLogger()

	searcher, err := query.NewSearcher(filepath.Join(dataDir, indexDBName))
	if err != nil {
		log.Error("local web UI disabled: cannot open read-only index", "err", err)
		close(done)
		return done
	}

	allowRemote, _ := strconv.ParseBool(os.Getenv(envWebUIAllowRemote))
	addr := webAddr
	if addr == "" {
		if allowRemote {
			addr = "0.0.0.0:8686" // container opt-in: reachable via port mapping
		} else {
			addr = localapi.DefaultWebAddr
		}
	}
	cache := agentcfg.NewETagCache(dataDir)

	policyFn := func() localapi.PolicyView { return loadPolicyView(cache) }
	wcfg := localapi.WebUIConfig{
		Addr:        addr,
		AllowRemote: allowRemote,
		Searcher:    searcher,
		Count:       func(ctx context.Context) (int, error) { return countActiveItems(ctx, idx) },
		Policy:      policyFn,
		// Status + Logs panels (user request 2026-07-27): a read-only
		// settings/state snapshot and the agentlog ring. No secrets — the
		// snapshot carries paths/config/policy, never tokens or key material.
		SettingsFn: webSettingsSnapshot(
			dataDir, addr, allowRemote, policyFn,
			func(ctx context.Context) (int, error) {
				return outbox.New(idx.DB()).CountUnsent(ctx)
			},
			func(ctx context.Context) (map[string][2]int64, error) {
				return indexRootAggregates(ctx, idx)
			},
		),
		// Full multi-process log when a log dir is active (the container
		// default): the daemon's ring only sees its OWN lines, but scans run as
		// separate processes whose output lands in their per-command file —
		// TailFiles merges every file (daemon + scan + entrypoint) by
		// timestamp. Stderr-only installs keep the ring fallback.
		// Local reports + full-path resolution run read-only SELECTs over the
		// index handle.
		ReportsFn: localapi.NewReportsFn(idx.DB(), nil),
		RootPaths: func(ctx context.Context) (map[string]string, error) {
			rows, err := idx.DB().QueryContext(ctx, `SELECT id, path FROM roots`)
			if err != nil {
				return nil, err
			}
			defer rows.Close()
			out := map[string]string{}
			for rows.Next() {
				var id, p string
				if rows.Scan(&id, &p) == nil {
					out[id] = p
				}
			}
			return out, rows.Err()
		},
		LogsFn: func(limit int) []string {
			if dir := activeLogDir(); dir != "" {
				if lines := agentlog.TailFiles(dir, limit); len(lines) > 0 {
					return lines
				}
			}
			return agentlog.Recent()
		},
		Logger: log,
	}
	// The web UI records history but is given only the write-side Recorder — it
	// cannot read history back (that surface is the socket API only).
	if hist != nil {
		wcfg.Recorder = hist
	}
	srv, err := localapi.NewWebUI(wcfg)
	if err != nil {
		log.Error("local web UI disabled: cannot initialize server", "err", err)
		searcher.Close()
		close(done)
		return done
	}

	go func() {
		defer close(done)
		defer searcher.Close()
		if err := srv.Run(ctx); err != nil && ctx.Err() == nil {
			log.Error("local web UI loop exited", "err", err)
		}
	}()
	return done
}

// webSettingsSnapshot assembles the Status panel's read-only snapshot from
// what the daemon can reach with just the data dir: identity/state
// (state.json), scan configuration (scan.json), the share-map/env knobs, and
// the live policy view. Deliberately NEVER includes secrets — no enrollment
// tokens (consumed anyway), no key material, no cert bytes; paths and config
// only. Files are re-read per request (cheap: two small JSON files) so the
// panel always shows current truth.
// indexRootAggregates returns {root path: [active item count, total bytes]}
// from the local index (sidecars included — they are cataloged items).
func indexRootAggregates(ctx context.Context, idx *index.Store) (map[string][2]int64, error) {
	rows, err := idx.DB().QueryContext(ctx, `
		SELECT r.path, COUNT(i.id), COALESCE(SUM(i.size), 0)
		FROM roots r
		LEFT JOIN items i ON i.root_id = r.id AND i.status = 'active'
		GROUP BY r.id, r.path`)
	if err != nil {
		return nil, err
	}
	defer rows.Close()
	out := map[string][2]int64{}
	for rows.Next() {
		var p string
		var n, b int64
		if rows.Scan(&p, &n, &b) == nil {
			out[p] = [2]int64{n, b}
		}
	}
	return out, rows.Err()
}

func webSettingsSnapshot(
	dataDir, addr string, allowRemote bool, policy func() localapi.PolicyView,
	outboxPending func(ctx context.Context) (int, error),
	rootAggregates func(ctx context.Context) (map[string][2]int64, error),
) func(ctx context.Context) (map[string]any, error) {
	readJSON := func(name string) map[string]any {
		b, err := os.ReadFile(filepath.Join(dataDir, name))
		if err != nil {
			return nil
		}
		var m map[string]any
		if json.Unmarshal(b, &m) != nil {
			return nil
		}
		return m
	}
	return func(ctx context.Context) (map[string]any, error) {
		snap := map[string]any{
			"agent_version":   Version,
			"data_dir":        dataDir,
			"web_addr":        addr,
			"web_remote":      allowRemote,
			"self_update":     !selfUpdateDisabled(),
			"log_level":       os.Getenv("FILEARR_AGENT_LOG_LEVEL"),
			"share_map":       os.Getenv(envShareMap),
			"share_host":      os.Getenv(envShareHost),
			"scan_roots_env":  os.Getenv("FILEARR_AGENT_SCAN_ROOTS"),
			"ffmpeg":          inventory.HasFFmpeg(),
		}
		if st := readJSON("state.json"); st != nil {
			// identity + endpoints only; state.json holds no secrets.
			for _, k := range []string{"agent_id", "central_url", "rollout_group", "ca_url"} {
				if v, ok := st[k]; ok {
					snap[k] = v
				}
			}
		}
		if sc := readJSON("scan.json"); sc != nil {
			snap["scan"] = sc
		}
		// Activity (user request 2026-07-27: "current jobs"): the live/last
		// scan crosses over from the scan PROCESS via scan-status.json; the
		// replication backlog is the outbox's unsent count.
		activity := map[string]any{}
		if st := readJSON("scan-status.json"); st != nil {
			activity["scan"] = st
		}
		if outboxPending != nil {
			if n, err := outboxPending(ctx); err == nil {
				activity["outbox_pending"] = n
			}
		}
		snap["activity"] = activity
		// Per-root view (user request 2026-07-27): merge the index aggregates
		// (item count + bytes per root) with each root's last scan outcome
		// (scan-roots.json, written by the scan process). Union of both key
		// sets so a configured-but-never-scanned root still shows a row.
		var lastScans map[string]map[string]any
		if b, err := os.ReadFile(filepath.Join(dataDir, "scan-roots.json")); err == nil {
			_ = json.Unmarshal(b, &lastScans)
		}
		agg := map[string][2]int64{}
		if rootAggregates != nil {
			if a, err := rootAggregates(ctx); err == nil {
				agg = a
			}
		}
		paths := map[string]bool{}
		for p := range agg {
			paths[p] = true
		}
		for p := range lastScans {
			paths[p] = true
		}
		roots := make([]map[string]any, 0, len(paths))
		for p := range paths {
			row := map[string]any{"path": p, "items": agg[p][0], "bytes": agg[p][1]}
			if ls, ok := lastScans[p]; ok {
				row["last_scan"] = ls
			}
			roots = append(roots, row)
		}
		sort.Slice(roots, func(i, j int) bool {
			return roots[i]["path"].(string) < roots[j]["path"].(string)
		})
		snap["roots"] = roots
		pv := policy()
		snap["policy"] = map[string]any{
			"web_ui_enabled":       pv.WebUIEnabled,
			"local_access_enabled": pv.LocalAccessEnabled,
			"auth_required":        pv.AuthRequired,
			"stale":                pv.Stale,
			"version":              pv.Version,
			"path_scope":           pv.Predicates,
		}
		return snap, nil
	}
}
