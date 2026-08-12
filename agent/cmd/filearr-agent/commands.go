package main

import (
	"context"
	"encoding/json"
	"net/http"
	"os"
	"path/filepath"
	"strconv"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	"github.com/filearr/filearr/agent/internal/commands"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/enroll"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/pathspec"
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
func startCommandPoller(ctx context.Context, idx *index.Store, certStore *enroll.CertStore, centralURL, agentID string, httpClient *http.Client, onAuthError func(), updTrigger updateTriggerFn, ops *opState) <-chan struct{} {
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
	})
	done := make(chan struct{})
	go func() {
		defer close(done)
		// W6-D3 consumption seam: resolve the group scan_selections policy into the
		// effective scan-root set once at startup (logged + persisted), WITHOUT
		// starting a scan — proves the policy vocabulary resolves end-to-end.
		consumeScanRootSeam(envOr(envDataDir, defaultDataDir()))
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
		if st := readJSON("scan-status.json"); st != nil {
			h["scan"] = st
		}
		if roots := readJSON("scan-roots.json"); roots != nil {
			h["scan_roots"] = roots
		}
		return h
	}
}

// consumeScanRootSeam reads the cached group policy, expands its scan_selections
// into the effective scan-root set via the SHARED pathspec engine, and LOGS +
// PERSISTS the result to <dataDir>/inventory/scan-roots.json. It deliberately does
// NOT start a scan (W6-D3): auto-start from a group policy is a follow-up that must
// coordinate with the scan scheduler/cancellation path. Best-effort throughout — a
// missing cache or unwritable dir is logged at debug and never fatal.
func consumeScanRootSeam(dataDir string) {
	log := newLogger()
	doc, ok, err := agentcfg.NewETagCache(dataDir).Load()
	if err != nil || !ok {
		return // no cached policy yet; nothing to consume
	}
	res := inventory.ExpandScanSelections(pathspec.OSHost(), doc.Policy)
	if res.SelectionsCount == 0 {
		return // no group scan_selections configured
	}
	log.Info("group scan_selections resolved (seam only — no scan started)",
		"selections", res.SelectionsCount, "roots", len(res.Roots), "truncated", res.Truncated)
	dir := filepath.Join(dataDir, "inventory")
	if mkErr := os.MkdirAll(dir, 0o755); mkErr != nil {
		agentlog.Verbose(log, "scan-root seam: cannot create dir", "err", mkErr)
		return
	}
	blob, mErr := json.MarshalIndent(res, "", "  ")
	if mErr != nil {
		return
	}
	if wErr := os.WriteFile(filepath.Join(dir, "scan-roots.json"), blob, 0o644); wErr != nil {
		agentlog.Verbose(log, "scan-root seam: cannot persist", "err", wErr)
	}
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
