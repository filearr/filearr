package main

import (
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"log/slog"
	"os"
	"path/filepath"
	"strconv"
	"strings"
	"sync/atomic"
	"time"

	"github.com/filearr/filearr/agent/internal/agentlog"
	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/query"
	"github.com/filearr/filearr/agent/internal/scan"
	"github.com/filearr/filearr/agent/internal/shares"
	"github.com/filearr/filearr/agent/internal/taxonomy"
)

// envShareHost overrides the hostname rendered into a P10-T11 share hint
// (\\host\share, smb://host/...). Empty falls back to os.Hostname() — set it when
// clients reach this machine by a different name (a DNS alias, a NAS identity).
const envShareHost = "FILEARR_AGENT_SHARE_HOST"

// envShareMap (Docker/NAS agents) statically maps scan roots to the network
// locations they are shared at: comma-separated ``localpath=location`` pairs,
// where location is smb://host/share[/sub], \\host\share[\sub], or
// nfs://host/export. Inside a container, share DISCOVERY sees nothing (no
// smb.conf; the NAS exports the paths under ITS name, not the container's), so
// this is how a containerized agent still attaches share hints central can
// render as network-open links. Longest local prefix wins per file; entries
// override a discovered export of the same path.
const envShareMap = "FILEARR_AGENT_SHARE_MAP"

// newShareResolver builds the scan's share resolver: OS discovery plus any
// static FILEARR_AGENT_SHARE_MAP entries. Malformed map entries are skipped
// with a warning (hints are best-effort; a bad pair must not fail a scan).
func newShareResolver() *shares.Resolver {
	r := shares.New(os.Getenv(envShareHost))
	if spec := os.Getenv(envShareMap); spec != "" {
		applied, bad := r.SetStaticMap(spec)
		logger := newLogger()
		agentlog.Verbose(logger, "share map configured", "entries", applied)
		for _, b := range bad {
			logger.Warn("share map: skipping malformed entry", "entry", b,
				"want", "localpath=smb://host/share (or \\\\host\\share, nfs://host/export)")
		}
	}
	return r
}

// scanConfigName is the persistent scan configuration under the data dir. It
// records the roots, effective presets, and the content-hash ceiling so a
// scheduled/`--watch` run reproduces the same walk without re-passing flags.
const scanConfigName = "scan.json"

// indexDBName is the local SQLite catalog under the data dir.
const indexDBName = "index.db"

// historyDBName is the LOCAL-ONLY query frecency store (P7-T6), a SEPARATE SQLite
// file from index.db so the outbox/replication path (which only ever holds the
// index handle) is architecturally incapable of touching it. Wiping the agent's
// data dir wipes this file too — and with it, all local search history.
const historyDBName = "history.db"

// scanConfig is the on-disk form of a scan setup (persisted as scan.json).
//
// W8-E: the old media-type gate (enabled_types) is replaced by the File
// Extension Similarity Taxonomy gate — enabled_categories (file_category) +
// enabled_groups (file_group). A file is included iff BOTH are empty OR its
// category is enabled OR its group is enabled (central's library model). This is
// a BREAKING config change (an old scan.json's enabled_types key is ignored); a
// fresh redeploy is expected.
type scanConfig struct {
	Roots             []string `json:"roots"`
	Presets           []string `json:"presets,omitempty"`
	ExcludeGlobs      []string `json:"exclude_globs,omitempty"`
	IncludeGlobs      []string `json:"include_globs,omitempty"`
	EnabledCategories []string `json:"enabled_categories,omitempty"`
	EnabledGroups     []string `json:"enabled_groups,omitempty"`
	ContentCeiling    int64    `json:"content_ceiling_bytes,omitempty"`
}

// stringSlice is a repeatable string flag (e.g. -root a -root b).
type stringSlice []string

func (s *stringSlice) String() string { return strings.Join(*s, ",") }
func (s *stringSlice) Set(v string) error {
	*s = append(*s, v)
	return nil
}

// runScan implements `filearr-agent scan --root <path>... [--watch]`.
func runScan(args []string) error {
	fs := newFlagSet("scan")
	cfg := bindCommonFlags(fs)
	var roots stringSlice
	fs.Var(&roots, "root", "root directory to scan (repeatable)")
	watch := fs.Bool("watch", false, "keep watching the roots and rescan on change (settle-coalesced)")
	settle := fs.Duration("settle", scan.DefaultSettle, "watch settle window before a coalesced rescan")
	if err := fs.Parse(args); err != nil {
		return err
	}

	if err := adoptionGuard(cfg.DataDir); err != nil {
		return err
	}

	sc, err := loadOrInitScanConfig(cfg.DataDir, roots)
	if err != nil {
		return err
	}
	if len(sc.Roots) == 0 {
		return fmt.Errorf("no roots configured (pass -root or add them to %s)", filepath.Join(cfg.DataDir, scanConfigName))
	}

	// Central policy (P5-T6) overlays scan.json: for the keys it sets, policy WINS
	// (documented precedence), so one-shot `scan` invocations honor central config
	// the same way the daemon does. A false watch_mode gates --watch off.
	sc, watchDisabled := applyPolicyToScan(cfg.DataDir, sc, watch)
	if watchDisabled {
		fmt.Fprintln(os.Stderr, "central policy sets watch_mode=false; --watch disabled")
	}

	// W8-E: classify against the process-shared taxonomy cache — the compact
	// taxonomy the daemon's poller keeps fresh at <dataDir>/taxonomy.json, or the
	// baked-in seed when it has never been fetched (offline-first, symmetric with
	// the cached-policy read above). A one-shot scan reads the snapshot once.
	taxCache := taxonomy.NewCache(cfg.DataDir, newLogger())

	store, err := index.Open(filepath.Join(cfg.DataDir, indexDBName))
	if err != nil {
		return fmt.Errorf("open local index: %w", err)
	}
	defer store.Close()
	if store.Rebuilt {
		fmt.Fprintln(os.Stderr, "index.db failed integrity_check — deleted and rebuilt from scratch; a full rescan repopulates it")
	}

	ctx, cancel := signalContext()
	defer cancel()

	opts := scan.Options{
		EnabledPresets:    sc.Presets,
		ExcludeGlobs:      sc.ExcludeGlobs,
		IncludeGlobs:      sc.IncludeGlobs,
		EnabledCategories: sc.EnabledCategories,
		EnabledGroups:     sc.EnabledGroups,
		Taxonomy:          taxCache.Current(),
		Hash:              hashPolicy(sc),
		// Per-batch progress honors the configured log level (container logs
		// at warn/error stay quiet instead of ticking every 250 files) and
		// rides slog so every line carries a timestamp (user report: the raw
		// Printf lines had none). Progress also persists to scan-status.json
		// so the daemon's web UI Activity panel — a SEPARATE process — can
		// show the running scan.
		Progress: func(p scan.Progress) {
			writeScanStatus(cfg.DataDir, scanStatus{
				Root: currentRoot(), Running: true,
				Seen: p.Seen, New: p.New, Changed: p.Changed,
				UpdatedAt: time.Now().UTC().Format(time.RFC3339),
			})
			if !newLogger().Enabled(context.Background(), slog.LevelInfo) {
				return
			}
			newLogger().Info("scan progress",
				"root", currentRoot(),
				"seen", p.Seen, "new", p.New, "changed", p.Changed)
		},
		// P10-T11 best-effort share discovery: attach a network-open hint to each
		// created/modified item when a local share covers its path. A single
		// resolver (5-min TTL cache) is shared across all roots.
		Shares: newShareResolver(),
		// Agent-side extraction (2026-08-09 parity contract), gated on the cached
		// policy's extract_enabled. Nil when disabled — the default — so a fleet
		// that has not opted in never reads file CONTENT for metadata.
		Extract: scanExtractFn(cfg.DataDir),
	}

	scanAll := func() {
		for _, root := range sc.Roots {
			activeScanRoot.Store(root)
			started := time.Now().UTC()
			newLogger().Info("scan starting", "root", root)
			updateScanRoots(cfg.DataDir, root, rootScanStat{
				Status: "running", StartedAt: started.Format(time.RFC3339),
			})
			o := opts
			o.Root = root
			res, err := scan.Scan(ctx, store, o)
			finished := time.Now().UTC()
			if err != nil {
				newLogger().Error("scan failed", "root", root, "err", err)
				writeScanStatus(cfg.DataDir, scanStatus{
					Root: root, Running: false, Status: "failed",
					UpdatedAt: finished.Format(time.RFC3339),
				})
				updateScanRoots(cfg.DataDir, root, rootScanStat{
					Status: "failed", StartedAt: started.Format(time.RFC3339),
					FinishedAt:      finished.Format(time.RFC3339),
					DurationSeconds: int64(finished.Sub(started).Seconds()),
				})
				continue
			}
			reportScan(root, res)
			status := "finished"
			if res.Stopped {
				status = "stopped"
			}
			writeScanStatus(cfg.DataDir, scanStatus{
				Root: root, Running: false, Status: status,
				Seen: res.Seen, New: res.New, Changed: res.Changed,
				Missing:   res.Missing,
				UpdatedAt: finished.Format(time.RFC3339),
			})
			updateScanRoots(cfg.DataDir, root, rootScanStat{
				Status: status, Seen: res.Seen, New: res.New,
				Changed: res.Changed, Missing: res.Missing,
				StartedAt:       started.Format(time.RFC3339),
				FinishedAt:      finished.Format(time.RFC3339),
				DurationSeconds: int64(finished.Sub(started).Seconds()),
			})
		}
	}

	scanAll()
	if !*watch {
		return nil
	}

	fmt.Printf("watching %d root(s) (settle %s); Ctrl-C to stop\n", len(sc.Roots), *settle)
	return watchRoots(ctx, sc.Roots, *settle, scanAll)
}

// watchRoots runs a settle-coalesced watcher per root, each triggering a full
// rescan of all roots on a settled burst.
func watchRoots(ctx context.Context, roots []string, settle time.Duration, rescan func()) error {
	errc := make(chan error, len(roots))
	for _, root := range roots {
		go func(r string) {
			errc <- scan.Watch(ctx, r, settle, rescan)
		}(root)
	}
	// Block until ctx is cancelled; report the first non-cancel error.
	<-ctx.Done()
	return nil
}

// applyPolicyToScan overlays the cached central policy onto sc (policy wins for
// the keys it sets) and gates --watch: when the policy sets watch_mode=false it
// flips *watch off and reports watchDisabled=true. With no cached policy (or a
// parse error) sc and *watch are returned unchanged — the agent falls back to
// its local scan.json, never failing a scan on a missing/broken policy.
func applyPolicyToScan(dataDir string, sc scanConfig, watch *bool) (out scanConfig, watchDisabled bool) {
	pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
	if err != nil || !ok {
		return sc, false
	}
	overlaid := pol.OverlayScan(agentcfg.ScanSettings{
		Presets:             sc.Presets,
		IncludeGlobs:        sc.IncludeGlobs,
		ExcludeGlobs:        sc.ExcludeGlobs,
		ContentCeilingBytes: sc.ContentCeiling,
	})
	sc.Presets = overlaid.Presets
	sc.IncludeGlobs = overlaid.IncludeGlobs
	sc.ExcludeGlobs = overlaid.ExcludeGlobs
	sc.ContentCeiling = overlaid.ContentCeilingBytes

	if watch != nil && *watch {
		if allowed, set := pol.WatchAllowed(); set && !allowed {
			*watch = false
			watchDisabled = true
		}
	}
	return sc, watchDisabled
}

// runSearch implements `filearr-agent search <query>` over the full local query
// DSL (agent/internal/query), executed on a dedicated read-only connection. The
// typo-tolerance is a LOCAL, bounded edit-distance re-rank (fires only on zero
// exact hits or an explicit ~ term) — it is NOT central's Meilisearch ranking;
// results may differ from the central UI.
//
// LEGACY PATH: `search` opens the index file directly, bypassing the P7-T2 policy
// gate + peer-credential boundary. The SUPPORTED local query surface is
// `filearr-agent query`, which dials the agent's socket/pipe. Prefer it.
func runSearch(args []string) error {
	fs := newFlagSet("search")
	cfg := bindCommonFlags(fs)
	includeSidecars := fs.Bool("sidecars", false, "include sidecar rows (hidden by default)")
	limit := fs.Int("limit", 50, "max results")
	asJSON := fs.Bool("json", false, "emit one JSON object per result line (NDJSON)")
	if err := fs.Parse(args); err != nil {
		return err
	}
	raw := strings.TrimSpace(strings.Join(fs.Args(), " "))
	if raw == "" {
		return fmt.Errorf("usage: filearr-agent search [--json] [--limit N] [--sidecars] <query>\n  (legacy direct-index path; the supported surface is `filearr-agent query`)")
	}

	searcher, err := query.NewSearcher(filepath.Join(cfg.DataDir, indexDBName))
	if err != nil {
		return fmt.Errorf("open local index (read-only): %w", err)
	}
	defer searcher.Close()

	results, err := searcher.Search(context.Background(), raw, *includeSidecars, *limit)
	if err != nil {
		var pe *query.ParseError
		var ee *query.ExecError
		switch {
		case errors.As(err, &pe):
			return fmt.Errorf("query syntax error [%s] at position %d: %s", pe.Code, pe.Position, pe.Reason)
		case errors.As(err, &ee):
			return fmt.Errorf("query not runnable locally [%s]: %s", ee.Code, ee.Message)
		default:
			return err
		}
	}

	fuzzy := false
	for _, r := range results {
		if r.FuzzyMatched {
			fuzzy = true
		}
		if *asJSON {
			row := map[string]any{
				"id":            r.Item.ID,
				"rel_path":      r.Item.RelPath,
				"filename":      r.Item.Filename,
				"extension":     r.Item.Extension,
				"size":          r.Item.Size,
				"mtime_ns":      r.Item.MtimeNs,
				"file_category": r.Item.FileCategory,
				"file_group":    r.Item.FileGroup,
				"status":        r.Item.Status,
				"fuzzy_matched": r.FuzzyMatched,
				"score":         r.Score,
			}
			buf, _ := json.Marshal(row)
			fmt.Println(string(buf))
			continue
		}
		flag := ""
		if r.FuzzyMatched {
			flag = fmt.Sprintf("\t~fuzzy(%d)", r.Score)
		}
		fmt.Printf("%s\t%d\t%s%s\n", r.Item.RelPath, r.Item.Size, r.Item.Status, flag)
	}
	if len(results) == 0 {
		fmt.Fprintln(os.Stderr, "no matches")
	} else if fuzzy {
		fmt.Fprintln(os.Stderr, "note: results include local typo-tolerant (fuzzy) matches; central search may rank differently")
	}
	return nil
}

// loadOrInitScanConfig reads scan.json, merges any -root flags (which win and are
// persisted), and writes it back. A first run with -root creates the file.
func loadOrInitScanConfig(dataDir string, roots []string) (scanConfig, error) {
	path := filepath.Join(dataDir, scanConfigName)
	var sc scanConfig
	if buf, err := os.ReadFile(path); err == nil {
		if err := json.Unmarshal(buf, &sc); err != nil {
			return sc, fmt.Errorf("parse %s: %w", path, err)
		}
	} else if !os.IsNotExist(err) {
		return sc, fmt.Errorf("read %s: %w", path, err)
	}

	if len(roots) > 0 {
		sc.Roots = mergeAbs(sc.Roots, roots)
		if err := writeScanConfig(path, sc); err != nil {
			return sc, err
		}
	}
	return sc, nil
}

// mergeAbs unions existing roots with new (absolutised) ones, order-preserving.
func mergeAbs(existing, added []string) []string {
	seen := map[string]bool{}
	var out []string
	add := func(p string) {
		if abs, err := filepath.Abs(p); err == nil {
			p = abs
		}
		if !seen[p] {
			seen[p] = true
			out = append(out, p)
		}
	}
	for _, p := range existing {
		add(p)
	}
	for _, p := range added {
		add(p)
	}
	return out
}

func writeScanConfig(path string, sc scanConfig) error {
	if err := os.MkdirAll(filepath.Dir(path), 0o700); err != nil {
		return fmt.Errorf("create data dir: %w", err)
	}
	buf, err := json.MarshalIndent(sc, "", "  ")
	if err != nil {
		return err
	}
	buf = append(buf, '\n')
	// Temp-then-rename: the daemon (web UI Status panel, local root controls)
	// reads this file from a DIFFERENT process than the scan that writes it, so
	// a reader must never observe a half-written document.
	tmp := path + ".tmp"
	if err := os.WriteFile(tmp, buf, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, path)
}

// --- local scan-root controls (2026-08-10) -----------------------------------
// The agent's own web UI can add/remove scan roots when central's
// `local_roots_control` permission is on. Roots live in scan.json — the scan
// PROCESS's configuration — so these helpers are read-modify-write over that
// file, not over the policy cache.

// scanRoots returns the configured scan roots (empty when none/unreadable).
func scanRoots(dataDir string) []string {
	sc, err := readScanConfig(dataDir)
	if err != nil {
		return nil
	}
	return sc.Roots
}

// readScanConfig loads scan.json, treating a missing file as an empty config.
func readScanConfig(dataDir string) (scanConfig, error) {
	var sc scanConfig
	buf, err := os.ReadFile(filepath.Join(dataDir, scanConfigName))
	if err != nil {
		if os.IsNotExist(err) {
			return sc, nil
		}
		return sc, err
	}
	if err := json.Unmarshal(buf, &sc); err != nil {
		return scanConfig{}, fmt.Errorf("parse scan config: %w", err)
	}
	return sc, nil
}

// addScanRoot unions one absolute root into scan.json, preserving every other
// key in the file (presets, globs, categories) — this is an edit, not a rewrite.
func addScanRoot(dataDir, root string) error {
	sc, err := readScanConfig(dataDir)
	if err != nil {
		return err
	}
	sc.Roots = mergeAbs(sc.Roots, []string{root})
	return writeScanConfig(filepath.Join(dataDir, scanConfigName), sc)
}

// removeScanRoot drops one root from scan.json. It deliberately does NOT touch
// the local index: the rows for that root are the agent's record of what it
// saw, and deleting them here would replicate to central as a mass deletion.
// Removing a root only stops FUTURE walks; tombstoning stays the scan's job.
func removeScanRoot(dataDir, root string) error {
	sc, err := readScanConfig(dataDir)
	if err != nil {
		return err
	}
	target := root
	if abs, aerr := filepath.Abs(root); aerr == nil {
		target = abs
	}
	kept := make([]string, 0, len(sc.Roots))
	for _, p := range sc.Roots {
		if p == root || p == target {
			continue
		}
		kept = append(kept, p)
	}
	sc.Roots = kept
	return writeScanConfig(filepath.Join(dataDir, scanConfigName), sc)
}

// envHashTimeout bounds the wall clock spent hashing one file, in seconds
// (default 300; 0 disables). A corrupt/locked file on a FUSE/network mount can
// block read(2) forever and freeze the whole walk — the bound skips the file's
// hashes and WARNs its path instead.
const envHashTimeout = "FILEARR_AGENT_HASH_TIMEOUT_SECONDS"

func hashPolicy(sc scanConfig) scan.HashPolicy {
	p := scan.DefaultHashPolicy()
	if sc.ContentCeiling > 0 {
		p.FullMaxBytes = sc.ContentCeiling
	}
	p.Timeout = 300 * time.Second
	if v := os.Getenv(envHashTimeout); v != "" {
		if n, err := strconv.Atoi(v); err == nil && n >= 0 {
			p.Timeout = time.Duration(n) * time.Second
		}
	}
	p.Log = newLogger()
	return p
}

func reportScan(root string, res scan.Result) {
	log := newLogger()
	if res.ScopeMissing {
		log.Info("scan finished: scope missing, nothing written", "root", root)
		return
	}
	status := "finished"
	if res.Stopped {
		status = "stopped (graceful)"
	}
	// slog (not Printf) so the line carries a timestamp + level like the rest
	// of the container/service log (user report: these lines had neither).
	log.Info("scan "+status, "root", root,
		"seen", res.Seen, "new", res.New, "changed", res.Changed,
		"missing", res.Missing, "moved", res.Moved,
		"ambiguous", res.MoveAmbiguous,
		"sidecars", res.Sidecars.Sidecars, "linked", res.Sidecars.Linked)
}

// --- scan-status seam (web UI Activity panel) -------------------------------
// The scan runs as its own PROCESS beside the `run` daemon (the container's
// documented model), so live progress crosses to the daemon's web UI via a
// tiny JSON file in the data dir. Best-effort by construction: a write
// failure never affects the scan itself.

type scanStatus struct {
	Root      string `json:"root"`
	Running   bool   `json:"running"`
	Seen      int    `json:"seen"`
	New       int    `json:"new"`
	Changed   int    `json:"changed"`
	Missing   int    `json:"missing,omitempty"`
	Status    string `json:"status,omitempty"`
	UpdatedAt string `json:"updated_at"`
}

// activeScanRoot carries the root the walk is currently in for the progress
// callback (which scan.Progress does not include).
var activeScanRoot atomic.Value

func currentRoot() string {
	if v, ok := activeScanRoot.Load().(string); ok {
		return v
	}
	return ""
}

func writeScanStatus(dataDir string, st scanStatus) {
	b, err := json.Marshal(st)
	if err != nil {
		return
	}
	tmp := filepath.Join(dataDir, "scan-status.json.tmp")
	if os.WriteFile(tmp, b, 0o644) == nil {
		_ = os.Rename(tmp, filepath.Join(dataDir, "scan-status.json"))
	}
}

// rootScanStat is one root's LAST scan outcome, persisted per root in
// scan-roots.json so the daemon's web UI Status panel (a separate process) can
// show when each root was last scanned and what that scan saw — scan-status.json
// only ever holds the single active/most-recent scan.
type rootScanStat struct {
	Status          string `json:"status"` // running | finished | stopped | failed
	Seen            int    `json:"seen"`
	New             int    `json:"new"`
	Changed         int    `json:"changed"`
	Missing         int    `json:"missing,omitempty"`
	StartedAt       string `json:"started_at,omitempty"`
	FinishedAt      string `json:"finished_at,omitempty"`
	DurationSeconds int64  `json:"duration_seconds,omitempty"`
}

const scanRootsFile = "scan-roots.json"

// updateScanRoots read-modify-writes one root's entry in scan-roots.json
// (atomic tmp+rename; only the scan process writes it, the daemon only reads).
func updateScanRoots(dataDir, root string, st rootScanStat) {
	path := filepath.Join(dataDir, scanRootsFile)
	m := map[string]rootScanStat{}
	if b, err := os.ReadFile(path); err == nil {
		_ = json.Unmarshal(b, &m)
	}
	m[root] = st
	b, err := json.Marshal(m)
	if err != nil {
		return
	}
	tmp := path + ".tmp"
	if os.WriteFile(tmp, b, 0o644) == nil {
		_ = os.Rename(tmp, path)
	}
}
