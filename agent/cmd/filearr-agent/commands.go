package main

import (
	"context"
	"encoding/json"
	"fmt"
	"log/slog"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"sync/atomic"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/commands"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/pathspec"
	"github.com/filearr/filearr/agent/internal/reconcile"
)

// Command-poller env fallbacks (flags are not plumbed for this loop — it is a
// background daemon concern, not an operator-facing one).
const (
	envCommandPollInterval = "FILEARR_AGENT_COMMAND_POLL_INTERVAL" // Go duration (default 60s)
	envCommandPollMax      = "FILEARR_AGENT_COMMAND_POLL_MAX"      // per-poll drain cap (default 10)
	envCommandLeaseSeconds = "FILEARR_AGENT_COMMAND_LEASE_SECONDS" // picked_up lease; heartbeat = lease/3 (default 300)
)

// startCommandPoller launches the P10-T3 on-demand command poller for the `run`
// daemon: it plain-polls central's per-agent command queue and executes each
// stat_check / rehash_check against local disk, reusing the shared bearer-auth +
// mTLS HTTP client. It returns a done-channel so the daemon waits for a clean
// stop, mirroring startReplication / startPoller.
func startCommandPoller(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client, onAuthError func(), updTrigger updateTriggerFn, ops *opState, sup *reconcile.Supervisor) <-chan struct{} {
	dataDir := envOr(envDataDir, defaultDataDir())
	log := newLogger()
	poller := commands.NewPoller(commands.Config{
		BaseURL:      centralURL,
		AgentID:      agentID,
		AuthFn:       authProvider(certStore),
		HTTP:         httpClient,
		Executor:     commands.NewExecutor(idx, 0), // 0 => central default size ceiling
		RateProvider: uploadRateProvider(),
		// W6-D3: the inventory runner (real OS host) + the additive capability
		// advertisement it attaches to every poll so central can store what this
		// agent supports.
		Inventory: inventory.NewRunner(nil, nil),
		// Re-evaluated per poll (2026-08-11), not snapshotted here: the tool
		// caches inside it are TTL'd, so this is what lets a host tool
		// installed after the agent started show up in the console within
		// ~15 minutes instead of never.
		Capabilities: inventory.Capabilities,
		// 2026-08-08 fleet health: the compact self-reported snapshot central
		// stores on the agent row and the console renders per agent — plus the
		// running version, so central stays current even when the self-update
		// subsystem (the historical version channel) is disabled.
		Health:       agentHealthProvider(idx, dataDir, time.Now(), ops),
		Version:      Version,
		MaxCommands:  envInt(envCommandPollMax, 10),
		Interval:     envDuration(envCommandPollInterval, 60*time.Second),
		LeaseSeconds: envInt(envCommandLeaseSeconds, 300),
		Logger:       log,
		OnAuthError:  onAuthError,
		// Local "Sync with central" panel (2026-08-25).
		OnPollResult: syncStatus.reporter("commands"),
		// The local Status page's "last inventory run" (2026-08-25): persisted
		// per report so it survives the run and is readable cross-process.
		InventoryStatus: func(st map[string]any) { writeJSONFile(dataDir, inventoryStatusName, st) },
		// Console "update now" button → self_update command → one immediate
		// check-and-apply. Nil when self-update is disabled (the handler then
		// completes ok=false with an explanatory result).
		TriggerUpdate: updTrigger,
		// 2026-08-09 maintenance: central's mode advertisement pauses the
		// replication push; the suspend/agent_maintenance commands bridge onto
		// the daemon's opState + local maintenance pass.
		OnMaintenance: ops.SetCentralMaintenance,
		SetSuspended:  ops.SetSuspended,
		RunMaintenance: func(mctx context.Context) (map[string]any, error) {
			return runLocalMaintenance(mctx, idx, dataDir, log)
		},
		// 2026-08-10 parity phase 3: the operator-triggered sweep that re-emits
		// already-indexed items with a fresh extraction, for the files no scan
		// will ever report as changed again.
		RunReextract: reextractRunner(idx, dataDir, ops, log),
		// 2026-08-12 QH-T6: the operator-triggered migration that re-reads the
		// 64-128 KiB band and corrects the quick hashes the pre-2026-07-18
		// hasher got wrong. Distinct from the `rehash_check` command kind (one
		// item, verify only) — see internal/commands/rehash_sweep.go.
		RunRehashSweep: rehashSweepRunner(idx, dataDir, ops, log),
		// 2026-08-22: the console-triggered full-manifest sweep, routed
		// through the reconcile supervisor's single-flight gate so it can
		// never interleave with a periodic/reconnect sweep in flight. Nil
		// supervisor (defensive) => the kind completes ok=false.
		RunReconcile: func(rctx context.Context, payload map[string]any) (map[string]any, error) {
			if sup == nil {
				return nil, fmt.Errorf("reconcile supervisor not running")
			}
			force, _ := payload["force_reset"].(bool)
			res, err := sup.RunNow(rctx, force)
			return reconcileResultMap(res), err
		},
	})
	// The local web UI's "inventory now" seam asks this poller to request a
	// run from central (see localcontrols.go).
	activeCommandPoller.Store(poller)
	done := make(chan struct{})
	go func() {
		defer close(done)
		// Resolve the group scan_selections policy into the effective scan roots
		// once at startup (and again after every policy fetch — see policy.go).
		applyCentralScanRoots(envOr(envDataDir, defaultDataDir()), newLogger())
		// Run only returns on ctx cancellation (a poll failure backs off, never
		// exits); a non-cancel error is logged but must not crash the daemon.
		if err := poller.Run(ctx); err != nil && ctx.Err() == nil {
			newLogger().Error("command poll loop exited", "err", err)
		}
	}()
	return done
}

// agentHealthProvider assembles the compact self-reported health snapshot the
// poller attaches to every command poll: uptime, replication backlog (outbox
// unsent count), local index size, and the live/last scan state the scan
// PROCESS crosses over via scan-status.json / scan-roots.json (the same files
// the local web UI Status panel reads). Every piece is best-effort — a failed
// read simply omits its key; the poll must never depend on health assembly.
// Central stores the map VERBATIM (size-capped) with an arrival stamp; the
// fleet console renders it per agent.
func agentHealthProvider(idx *index.Store, dataDir string, startedAt time.Time, ops *opState) func(ctx context.Context) map[string]any {
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
	return func(ctx context.Context) map[string]any {
		h := map[string]any{
			"uptime_s": int(time.Since(startedAt).Seconds()),
		}
		// 2026-08-09: the console badges suspend/back-off from these (the agent
		// is the only source of truth for its own applied processing state).
		if ops != nil {
			if ops.Suspended() {
				h["suspended"] = true
			}
			if ops.CentralMaintenance() {
				h["central_maintenance"] = true
			}
			// 2026-08-10: the LOCAL scan pause is a separate flag from central's
			// suspend, and central has no other way to learn it — an agent that
			// simply stops scanning while still heartbeating is exactly the
			// silent-freeze failure the in-daemon scheduler exists to prevent.
			if ops.LocalScanPaused() {
				h["local_scan_paused"] = true
			}
		}
		// Local schedule overrides / local root edits: an operator changed this
		// agent's setup ON THE MACHINE (under the local_* permissions), so the
		// console must be able to see that its group's schedule is not the whole
		// story here. Omitted entirely when nothing was set locally.
		if ls, lerr := agentcfg.LoadLocalSettings(dataDir); lerr == nil {
			lo := map[string]any{}
			if ls.ScanCron != nil {
				lo["scan_cron"] = *ls.ScanCron
			}
			if ls.ScanIntervalSeconds != nil {
				lo["scan_interval_seconds"] = *ls.ScanIntervalSeconds
			}
			if ls.ScanOnStart != nil {
				lo["scan_on_start"] = *ls.ScanOnStart
			}
			if ls.RootsEditedAt != "" {
				lo["roots_edited_at"] = ls.RootsEditedAt
			}
			if len(lo) > 0 {
				if ls.UpdatedAt != "" {
					lo["updated_at"] = ls.UpdatedAt
				}
				h["local_overrides"] = lo
			}
		}
		// Per-root share mapping summary (2026-08-10). Central renders network-open
		// links from the hints agents attach, so "this agent's roots produce no
		// hints" is a fleet-visible condition — and a malformed share-map entry is
		// skipped rather than fatal (R1), which makes it invisible from the
		// console otherwise. Counts only: the mappings themselves are host
		// configuration the console has no key for, and this rides a 60 s poll.
		if roots := scanRoots(dataDir); len(roots) > 0 {
			views, rejects := rootShareViews(dataDir, roots, osGetenv)
			mapped := 0
			for _, v := range views {
				if v.Location != "" {
					mapped++
				}
			}
			sm := map[string]any{"roots": len(views), "mapped": mapped}
			if len(rejects) > 0 {
				sm["rejected"] = len(rejects)
			}
			h["share_map"] = sm
		}
		if n, err := outbox.New(idx.DB()).CountUnsent(ctx); err == nil {
			h["outbox_pending"] = n
		}
		if n, err := countActiveItems(ctx, idx); err == nil {
			h["index_items"] = n
		}
		// Parity phase 3: the last re-extraction sweep's outcome, so an operator
		// can answer "did the backfill finish on this box, and under which
		// extraction configuration" from the console without opening the command
		// history or shelling in. Omitted entirely until a sweep has run.
		if st, err := idx.ExtractState(ctx); err == nil && st.StartedAt != "" {
			rx := map[string]any{
				"fp":        st.FP,
				"started":   st.StartedAt,
				"seen":      st.Seen,
				"extracted": st.Extracted,
				"skipped":   st.Skipped,
				"cursor":    st.CursorRowID,
				// An empty FinishedAt is the durable "this sweep is partway
				// through" state the next command resumes from — the single most
				// useful bit here, so it is reported as a flag rather than left
				// to be inferred from a missing timestamp.
				"complete": st.FinishedAt != "",
			}
			if st.FinishedAt != "" {
				rx["finished"] = st.FinishedAt
			}
			h["reextract"] = rx
		}
		// QH-T6: the quick_hash migration's state. This rides HEALTH rather than
		// CAPABILITIES on purpose — health is re-evaluated on every poll while a
		// capability advertisement is a slower-moving statement about what the
		// agent CAN do, and this value changes minute by minute during a sweep.
		//
		// It is also the ONLY channel through which the migration's state can
		// reach an operator. Central cannot derive it from the rows: it holds no
		// hash provenance for agent-owned items (agentsync never writes
		// policy_version for them), so "has this agent been migrated" is
		// unanswerable from the catalogue side. Omitted entirely until a sweep has
		// run, so an un-swept agent reads as "not run" rather than as zeros.
		if st, err := idx.RehashState(ctx); err == nil && st.StartedAt != "" {
			rh := map[string]any{
				"fp":       st.FP,
				"started":  st.StartedAt,
				"seen":     st.Seen,
				"changed":  st.Changed,
				"verified": st.Verified,
				"skipped":  st.Skipped,
				"failed":   st.Failed,
				"cursor":   st.CursorRowID,
				"min_size": st.MinSize,
				"max_size": st.MaxSize,
				// An empty FinishedAt is the durable "this sweep is partway through"
				// state the next command resumes from — reported as a flag rather
				// than left to be inferred from a missing timestamp.
				"complete": st.FinishedAt != "",
			}
			if st.FinishedAt != "" {
				rh["finished"] = st.FinishedAt
			}
			h["rehash"] = rh
		}
		if st := readJSON("scan-status.json"); st != nil {
			h["scan"] = st
		}
		if roots := readJSON("scan-roots.json"); roots != nil {
			h["scan_roots"] = roots
		}
		return h
	}
}

// applyCentralScanRoots is the policy->roots consumption path. It reads the
// cached group policy, expands its scan_selections into the effective scan-root
// set via the SHARED pathspec engine, persists the resolved set to
// <dataDir>/inventory/scan-roots.json (diagnostics), and -- when at least one
// enabled selection exists -- WRITES those roots into scan.json so the scan
// command, the scheduler and the local web UI all see them. Until 2026-08-18
// this was a "seam only": the roots were logged and persisted but scan.json was
// never touched, so a fleet whose roots were configured centrally scanned
// nothing (live: XENON, `d:` selection, "no roots configured" every 2 h) while
// the local roots editor refused edits as managed_by_central. Returns the roots
// and whether central manages them. Best-effort throughout: a missing cache or
// unwritable dir is logged and never fatal.
func applyCentralScanRoots(dataDir string, log *slog.Logger) (roots []string, managed bool) {
	doc, ok, err := agentcfg.NewETagCache(dataDir).Load()
	if err != nil || !ok {
		return nil, false // no cached policy yet; nothing to consume
	}
	res := inventory.ExpandScanSelections(pathspec.OSHost(), doc.Policy)
	if res.SelectionsCount == 0 {
		return nil, false // no group scan_selections configured -> local roots rule
	}
	dir := filepath.Join(dataDir, "inventory")
	if mkErr := os.MkdirAll(dir, 0o755); mkErr == nil {
		if blob, mErr := json.MarshalIndent(res, "", "  "); mErr == nil {
			if wErr := os.WriteFile(filepath.Join(dir, "scan-roots.json"), blob, 0o644); wErr != nil {
				agentlog.Verbose(log, "scan-roots: cannot persist resolution", "err", wErr)
			}
		}
	}
	for spec, msg := range res.Errors {
		log.Warn("scan_selections: spec could not be expanded", "spec", spec, "err", msg)
	}
	changed, werr := setCentralScanRoots(dataDir, res.Roots)
	if werr != nil {
		log.Error("scan_selections: could not write scan roots", "err", werr)
		return res.Roots, true
	}
	if changed {
		log.Info("scan roots derived from central scan_selections",
			"selections", res.SelectionsCount, "roots", res.Roots, "truncated", res.Truncated)
	} else {
		agentlog.Verbose(log, "scan roots from central scan_selections unchanged",
			"selections", res.SelectionsCount, "roots", len(res.Roots))
	}
	if len(res.Roots) == 0 {
		log.Warn("scan_selections resolved to ZERO roots -- nothing will be scanned until a selection expands to an existing path",
			"selections", res.SelectionsCount, "errors", len(res.Errors))
	}
	return res.Roots, true
}

// uploadRateProvider returns the per-agent staging-upload rate cap (bytes/sec, 0
// = unlimited) read from the cached central policy at each upload start (P10-T4).
// It resolves the data dir the same way the daemon's own default does
// (FILEARR_AGENT_DATA_DIR or the per-user default) — the common deployment; an
// explicit `run -data <dir>` override is not reflected in this lookup, which then
// finds no cache and returns 0 (unlimited). That is a deliberate fail-open: the
// rate cap is a soft throttle, not an integrity or security control, so a missing
// cache must never wedge an upload. A mid-upload policy change is picked up on the
// NEXT upload (the value is re-read per stage_upload, not per chunk).
func uploadRateProvider() func() int64 {
	dataDir := envOr(envDataDir, defaultDataDir())
	return func() int64 {
		pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
		if err != nil || !ok {
			return 0
		}
		return pol.UploadRateBytesPerSec()
	}
}

// envDuration parses a Go duration from key, falling back to def when unset or
// unparseable/non-positive.
func envDuration(key string, def time.Duration) time.Duration {
	if v := os.Getenv(key); v != "" {
		if d, err := time.ParseDuration(v); err == nil && d > 0 {
			return d
		}
	}
	return def
}

// envInt parses a positive int from key, falling back to def otherwise.
func envInt(key string, def int) int {
	if v := os.Getenv(key); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n > 0 {
			return n
		}
	}
	return def
}

// inventoryStatusName is the data-dir file the local Status page reads for the
// running/last inventory command (written through Config.InventoryStatus).
const inventoryStatusName = "inventory-status.json"

// activeCommandPoller is the daemon's command poller once started, for the
// local web UI's inventory-now seam. Nil until startCommandPoller ran.
var activeCommandPoller atomic.Pointer[commands.Poller]

// writeJSONFile atomically replaces <dataDir>/<name> with v as JSON. Best
// effort: a status file that fails to write is logged, never fatal.
func writeJSONFile(dataDir, name string, v any) {
	b, err := json.Marshal(v)
	if err != nil {
		return
	}
	path := filepath.Join(dataDir, name)
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, b, 0o644); err != nil {
		newLogger().Warn("write status file failed", "file", name, "err", err)
		return
	}
	if err := os.Rename(tmp, path); err != nil {
		newLogger().Warn("replace status file failed", "file", name, "err", err)
	}
}
