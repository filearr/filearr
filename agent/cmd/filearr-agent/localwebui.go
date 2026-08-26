package main

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sort"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/extract"
	"github.com/filearr/filearr/agent/internal/history"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/localapi"
	"github.com/filearr/filearr/agent/internal/outbox"
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
func startWebUI(ctx context.Context, cfg *config, webAddr string, idx *index.Store, hist *history.Store, ops *opState) <-chan struct{} {
	dataDir := cfg.DataDir
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
			idx.ExtractState,
			webExtras(cache, ops),
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
		// Local scan controls (2026-08-10). These administer THIS AGENT — pause
		// its scanning, edit its schedule, manage its roots — under central's
		// three permission gates. They never write to the catalog: the local
		// surface stays read-only over items and metadata by invariant.
		Controls: webControlSeams(ctx, cfg, ops, log),
		Logger:   log,
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

// hostToolRows builds the local status page's host-tool table: every tool
// Filearr knows about, in a stable order, each with its presence, version and
// resolved path.
//
// Absent tools are KEPT as rows (present=false, no version, no path) rather
// than filtered out — the wire contract's whole point is the full matrix, and
// an operator scanning for "why is OCR not running here" needs to see a
// tesseract row saying absent, not to notice that a row is missing.
//
// Assembled here rather than in package inventory so that inventory keeps
// depending on nothing of the transport, and the wire STRUCT stays the single
// authoritative statement of this shape (localapi/wire.go, mirrored in
// backend/filearr/localapi_contracts.py).
func hostToolRows() []localapi.HostToolInfo {
	names := inventory.HostToolNames()
	versions := inventory.ToolVersions()
	paths := inventory.ToolPaths()
	rows := make([]localapi.HostToolInfo, 0, len(names))
	for _, name := range names {
		path := paths[name]
		rows = append(rows, localapi.HostToolInfo{
			Name:    name,
			Present: path != "",
			Version: versions[name],
			Path:    path,
		})
	}
	return rows
}

func webSettingsSnapshot(
	dataDir, addr string, allowRemote bool, policy func() localapi.PolicyView,
	outboxPending func(ctx context.Context) (int, error),
	rootAggregates func(ctx context.Context) (map[string][2]int64, error),
	reextractState func(ctx context.Context) (index.ExtractState, error),
	extras func() map[string]any,
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
			"agent_version":  Version,
			"data_dir":       dataDir,
			"web_addr":       addr,
			"web_remote":     allowRemote,
			"self_update":    !selfUpdateDisabled(),
			"log_level":      os.Getenv("FILEARR_AGENT_LOG_LEVEL"),
			"share_map":      os.Getenv(envShareMap),
			"share_host":     os.Getenv(envShareHost),
			"scan_roots_env": os.Getenv("FILEARR_AGENT_SCAN_ROOTS"),
			// ffmpeg stays a top-level key (the panel has always shown it);
			// the full matrix lives under "tools" below.
			"ffmpeg": inventory.HasFFmpeg(),
			// Agent-parity capability advertisement — the SAME object the
			// command poll sends central, so the local page and the console
			// can never disagree about what this host can do.
			"capabilities": map[string]any{
				"extract":        true,
				"extract_schema": extract.Schema,
				"formats":        inventory.ExtractFormats(),
			},
			"tools":         inventory.Tools(),
			"tool_versions": inventory.ToolVersions(),
			// The same three facts as one ordered table (2026-08-11): name,
			// version, and the resolved LOCATION. The bool maps stay for the
			// older panel keys and for anything already reading them; this is
			// what the page renders. Sorted, because Go randomizes map order
			// and a table that reshuffles on every refresh is unreadable.
			"host_tools": hostToolRows(),
		}
		// Effective extraction settings + the settings this host will ignore.
		// This is the "never shell into a machine to answer what is this agent
		// actually doing" surface from the parity design.
		snap["extract"] = extractSnapshot(dataDir)
		// Parity phase 3: the last re-extraction sweep. Reported next to the
		// effective settings because the two answer one question together — what
		// this agent extracts, and whether the items it already holds have been
		// brought up to that configuration yet. Absent until a sweep has run.
		if reextractState != nil {
			if st, err := reextractState(ctx); err == nil && st.StartedAt != "" {
				rx := map[string]any{
					"fp":        st.FP,
					"started":   st.StartedAt,
					"seen":      st.Seen,
					"extracted": st.Extracted,
					"skipped":   st.Skipped,
					"cursor":    st.CursorRowID,
					"complete":  st.FinishedAt != "",
				}
				if st.FinishedAt != "" {
					rx["finished"] = st.FinishedAt
				}
				snap["reextract"] = rx
			}
		}
		if st := readJSON("state.json"); st != nil {
			// identity + endpoints only; state.json holds no secrets.
			for _, k := range []string{"agent_id", "central_url", "ca_url"} {
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
		// Each root's resolved network location (2026-08-10). The Status panel is
		// the read-only view a CONTAINER operator lands on — where the mapping
		// comes from the environment and cannot be edited here — so the answer to
		// "what does this root look like from another machine?" belongs next to
		// the root, not buried in a raw FILEARR_AGENT_SHARE_MAP string.
		pathList := make([]string, 0, len(paths))
		for p := range paths {
			pathList = append(pathList, p)
		}
		shareViews, shareRejects := rootShareViews(dataDir, pathList, osGetenv)
		shareByPath := make(map[string]localapi.RootShare, len(shareViews))
		for _, v := range shareViews {
			shareByPath[v.Path] = v
		}
		roots := make([]map[string]any, 0, len(paths))
		for p := range paths {
			row := map[string]any{"path": p, "items": agg[p][0], "bytes": agg[p][1]}
			if ls, ok := lastScans[p]; ok {
				row["last_scan"] = ls
			}
			if sv, ok := shareByPath[p]; ok {
				row["share_location"] = sv.Location
				row["share_source"] = sv.Source
				row["share_inherited_from"] = sv.InheritedFrom
				row["share_ambiguous"] = sv.Ambiguous
			}
			roots = append(roots, row)
		}
		// Malformed entries are skipped by the resolver, never fatal (R1) — so
		// this list is the only place a typo ever becomes visible.
		rejects := make([]map[string]any, 0, len(shareRejects))
		for _, rj := range shareRejects {
			rejects = append(rejects, map[string]any{"entry": rj.Entry, "source": rj.Source})
		}
		snap["share_map_rejects"] = rejects
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
		if pv.GraceExpiresAt != nil {
			snap["policy"].(map[string]any)["grace_expires_at"] = pv.GraceExpiresAt.UTC().Format(time.RFC3339)
		}
		// 2026-08-25: sync status, logging, sidecar identity, the policy keys
		// the page never showed, and the inventory configuration + last run.
		if extras != nil {
			for k, v := range extras() {
				snap[k] = v
			}
		}
		if st := readJSON(inventoryStatusName); st != nil {
			snap["inventory_last"] = st
		}
		return snap, nil
	}
}

// webExtras builds the 2026-08-25 additions to the Status snapshot (user
// request: the local page never showed the inventory configuration, the sync
// state, the log file location, or a dozen policy keys the agent honours).
func webExtras(cache *agentcfg.ETagCache, ops *opState) func() map[string]any {
	return func() map[string]any {
		out := map[string]any{}

		// --- sync with central ---
		sync := map[string]any{"channels": syncStatus.snapshot()}
		if ops != nil {
			sync["central_maintenance"] = ops.CentralMaintenance()
			sync["suspended"] = ops.Suspended()
		}
		out["sync"] = sync

		// --- logging ---
		sc := activeSidecar()
		level, levelSource := "info", "default"
		if sc.LogLevel != "" {
			level, levelSource = sc.LogLevel, "sidecar"
		}
		if v := os.Getenv("FILEARR_AGENT_LOG_LEVEL"); v != "" {
			level, levelSource = v, "env"
		}
		logging := map[string]any{
			"dir":          activeLogDir(),
			"rotation":     agentlog.RotationPolicy(),
			"file_sink":    activeLogDir() != "",
			"level":        level,
			"level_source": levelSource,
		}
		out["logging"] = logging

		// --- sidecar (never the enrollment token) ---
		out["sidecar"] = map[string]any{
			"agent_name":   sc.AgentName,
			"config_group": sc.ConfigGroup,
			"log_dir":      sc.LogDir,
			"ffmpeg_path":  sc.FFmpegPath,
			"central_url":  sc.CentralURL,
		}

		// --- policy keys honoured but never displayed ---
		if cache != nil {
			if doc, ok, err := cache.Load(); err == nil && ok {
				if pol, perr := doc.Parsed(); perr == nil {
					px := map[string]any{
						"read_only":                    pol.ReadOnly == nil || *pol.ReadOnly,
						"read_only_set":                pol.ReadOnly != nil,
						"include_globs":                pol.IncludeGlobs,
						"exclude_globs":                pol.ExcludeGlobs,
						"presets":                      pol.Presets,
						"watch_mode":                   derefBool(pol.WatchMode),
						"watch_mode_set":               pol.WatchMode != nil,
						"extract_exif":                 derefBool(pol.ExtractEXIF),
						"extract_exif_set":             pol.ExtractEXIF != nil,
						"content_hash_max_bytes":       derefInt64(pol.ContentHashMaxBytes),
						"reconcile_interval_seconds":   derefInt(pol.ReconcileIntervalSeconds),
						"poll_interval_seconds":        derefInt(pol.PollIntervalSeconds),
						"offline_grace_seconds":        derefInt(pol.OfflineGraceSeconds),
						"upload_rate_bytes_per_sec":    derefInt64(pol.UploadRatePerSec),
						"update_poll_interval_seconds": derefInt(pol.UpdatePollIntervalSeconds),
						"taxonomy_version":             derefInt(pol.TaxonomyVersion),
					}
					if pol.Group != nil {
						if pol.Group.LogLevel != nil {
							px["group_log_level"] = *pol.Group.LogLevel
							logging["level"], logging["level_source"] = *pol.Group.LogLevel, "central (group log_level)"
						}
						if pol.Group.ScanScheduleCron != nil {
							px["group_scan_schedule_cron"] = *pol.Group.ScanScheduleCron
						}
						if pol.Group.Inventory != nil {
							out["inventory_config"] = pol.Group.Inventory
						}
					}
					out["policy_extra"] = px
				}
			}
		}
		return out
	}
}

func derefBool(p *bool) bool {
	return p != nil && *p
}

func derefInt(p *int) any {
	if p == nil {
		return nil
	}
	return *p
}

func derefInt64(p *int64) any {
	if p == nil {
		return nil
	}
	return *p
}
