package main

// In-daemon scan scheduler: makes a lone `filearr-agent run` service
// self-sufficient. Live gap 2026-08-02: a Windows agent's external scheduled
// scan task did not survive a re-enroll — the daemon heartbeated for nine
// days while its catalog froze. The daemon now runs scans itself, driven by
// central policy (scan_cron / scan_interval_seconds / scan_on_start) with
// local env fallbacks for installs that don't author policy.
//
// Scans run as a CHILD PROCESS of the same binary (`filearr-agent scan`),
// exactly like the container entrypoint and a hand-run scan: identical config
// resolution, per-command log file, and crash isolation — a scan felled by a
// poison file never takes the daemon down. The scheduler re-resolves its
// schedule every tick, so a policy change applies on the next minute without
// a restart. All knobs absent = scheduler off: the container entrypoint keeps
// its own loop (FILEARR_AGENT_SCAN_INTERVAL) and must not be double-scanned.

import (
	"context"
	"errors"
	"log/slog"
	"os"
	"os/exec"
	"strconv"
	"sync/atomic"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/schedule"
)

// Env fallbacks — DISTINCT names from the container entrypoint's shell-loop
// vars (FILEARR_AGENT_SCAN_INTERVAL / _SCAN_ON_START) so an existing
// container env never accidentally arms the in-daemon scheduler too.
const (
	envScanCron   = "FILEARR_AGENT_SCAN_CRON"     // 5-field cron, local time
	envScanEvery  = "FILEARR_AGENT_SCAN_EVERY"    // Go duration, e.g. "6h"
	envScanOnBoot = "FILEARR_AGENT_SCAN_ON_BOOT"  // bool: one scan ~30s after start
)

// Loop cadences — package vars so the scheduler tests run in milliseconds.
var (
	schedTick         = 20 * time.Second
	schedOnStartDelay = 30 * time.Second
)

// scanInFlight is the process-wide "a scheduled/triggered scan child is
// running" guard. It is package-level rather than a scheduler local because the
// local web UI's "scan now" button (2026-08-10) must share the SAME guard: two
// concurrent scans of the same roots would fight over the local index and
// double-emit replication events.
var scanInFlight atomic.Bool

// errScanAlreadyRunning is returned by triggerScanNow when the guard is held.
var errScanAlreadyRunning = errors.New("a scan is already running")

// triggerScanNow starts one scan child immediately, honouring the shared
// overlap guard. daemonCtx (NOT a request context) owns the child: an HTTP
// handler's context is cancelled the moment the response is written, which
// would kill the scan it just started.
func triggerScanNow(daemonCtx context.Context, cfg *config, log *slog.Logger) error {
	if !scanInFlight.CompareAndSwap(false, true) {
		return errScanAlreadyRunning
	}
	log.Info("scan scheduler: starting scan", "reason", "local web UI trigger")
	go func() {
		defer scanInFlight.Store(false)
		t0 := time.Now()
		err := runScanChild(daemonCtx, cfg)
		if err != nil && daemonCtx.Err() == nil {
			log.Error("scan scheduler: scan failed", "reason", "local web UI trigger",
				"duration", time.Since(t0).Round(time.Second).String(), "err", err)
			return
		}
		log.Info("scan scheduler: scan finished", "reason", "local web UI trigger",
			"duration", time.Since(t0).Round(time.Second).String())
	}()
	return nil
}

type schedSpec struct {
	cron    *schedule.Cron
	cronStr string
	every   time.Duration
	onStart bool
}

func (s schedSpec) enabled() bool { return s.cron != nil || s.every > 0 || s.onStart }

// resolveScanSchedule computes the effective schedule: per-knob, the documented
// precedence
//
//	central policy > local override > FILEARR_AGENT_* env > sidecar > default
//
// with unset knobs falling through. Invalid values are logged and ignored (the
// daemon must keep running on a bad knob, not crash-loop).
//
// The LOCAL override (local-settings.json, written by the agent's own web UI
// under the central `local_schedule_control` permission) sits UNDER central by
// construction: central re-applies its document on every poll, so a local value
// for a key central set would silently revert within a poll interval. The local
// UI therefore refuses to edit a centrally-set key at all — this ordering is the
// second half of that rule and the reason it is safe.
func resolveScanSchedule(dataDir string, getenv func(string) string, log *slog.Logger) schedSpec {
	pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
	if err != nil {
		log.Warn("scan scheduler: cached policy unreadable; using env only", "err", err)
		ok = false
	}
	local, lerr := agentcfg.LoadLocalSettings(dataDir)
	if lerr != nil {
		log.Warn("scan scheduler: local settings unreadable; ignoring local overrides", "err", lerr)
		local = agentcfg.LocalSettings{}
	}
	var spec schedSpec

	// Cron precedence: top-level policy key > config-group settings
	// (group.scan_schedule_cron) > local override > env. Both policy sources are
	// CENTRAL, so both outrank the local file.
	cronStr := getenv(envScanCron)
	if local.ScanCron != nil {
		cronStr = *local.ScanCron
	}
	if ok && pol.Group != nil && pol.Group.ScanScheduleCron != nil {
		cronStr = *pol.Group.ScanScheduleCron
	}
	if ok && pol.ScanCron != nil {
		cronStr = *pol.ScanCron
	}
	if cronStr != "" {
		if c, err := schedule.Parse(cronStr); err != nil {
			log.Warn("scan scheduler: invalid scan cron ignored", "cron", cronStr, "err", err)
		} else {
			spec.cron, spec.cronStr = c, cronStr
		}
	}

	switch {
	case ok && pol.ScanIntervalSeconds != nil:
		if *pol.ScanIntervalSeconds > 0 {
			spec.every = time.Duration(*pol.ScanIntervalSeconds) * time.Second
		}
	case local.ScanIntervalSeconds != nil:
		if *local.ScanIntervalSeconds > 0 {
			spec.every = time.Duration(*local.ScanIntervalSeconds) * time.Second
		}
	default:
		if v := getenv(envScanEvery); v != "" {
			if d, err := time.ParseDuration(v); err != nil || d <= 0 {
				log.Warn("scan scheduler: invalid "+envScanEvery+" ignored", "value", v)
			} else {
				spec.every = d
			}
		}
	}

	switch {
	case ok && pol.ScanOnStart != nil:
		spec.onStart = *pol.ScanOnStart
	case local.ScanOnStart != nil:
		spec.onStart = *local.ScanOnStart
	default:
		if v := getenv(envScanOnBoot); v != "" {
			b, err := strconv.ParseBool(v)
			spec.onStart = err == nil && b
		}
	}
	return spec
}

// startScanScheduler launches the scheduler loop; the returned channel closes
// when it unwinds. A no-op-cheap loop: one policy-cache read per tick.
// hold (nil => never) gates firing and NAMES the holder: an agent suspended by
// central (2026-08-09) or paused locally (2026-08-10) skips every trigger until
// released, logged with the reason so the operator knows which console to use.
func startScanScheduler(ctx context.Context, cfg *config, log *slog.Logger, hold func() (bool, string)) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		runScanScheduler(ctx, cfg, log, runScanChild, hold)
	}()
	return done
}

// runScanChild executes one `filearr-agent scan` as a child of this daemon,
// forwarding the daemon's resolved config so the child resolves identically.
func runScanChild(ctx context.Context, cfg *config) error {
	exe, err := os.Executable()
	if err != nil {
		return err
	}
	args := []string{"scan", "-data", cfg.DataDir}
	if cfg.ConfigPath != "" {
		args = append(args, "-config", cfg.ConfigPath)
	}
	if cfg.LogDir != "" {
		args = append(args, "-log-dir", cfg.LogDir)
	}
	if cfg.LogLevel != "" {
		args = append(args, "-log-level", cfg.LogLevel)
	}
	cmd := exec.CommandContext(ctx, exe, args...)
	return cmd.Run()
}

// runScanScheduler is the loop body (child runner injected for tests).
func runScanScheduler(
	ctx context.Context,
	cfg *config,
	log *slog.Logger,
	run func(context.Context, *config) error,
	hold func() (bool, string),
) {
	tick, onStartDelay := schedTick, schedOnStartDelay
	// running is the SHARED process-wide guard (scanInFlight), so a scan the
	// local web UI triggered also blocks a scheduled one, and vice versa.
	running := &scanInFlight
	var (
		lastMinute string       // cron dedup: fire once per matching minute
		lastEndNs  atomic.Int64 // interval baseline (unix ns); 0 until first run ends
		started    = time.Now()
		bootFired  bool
		wasEnabled bool
	)

	fire := func(reason string) {
		if hold != nil {
			if held, by := hold(); held {
				log.Info("scan scheduler: scanning is held; skipping", "held_by", by, "reason", reason)
				return
			}
		}
		if !running.CompareAndSwap(false, true) {
			log.Info("scan scheduler: previous scan still running; skipping", "reason", reason)
			return
		}
		log.Info("scan scheduler: starting scan", "reason", reason)
		go func() {
			defer running.Store(false)
			t0 := time.Now()
			err := run(ctx, cfg)
			lastEndNs.Store(time.Now().UnixNano())
			if err != nil && ctx.Err() == nil {
				log.Error("scan scheduler: scan failed", "reason", reason,
					"duration", time.Since(t0).Round(time.Second).String(), "err", err)
				return
			}
			log.Info("scan scheduler: scan finished", "reason", reason,
				"duration", time.Since(t0).Round(time.Second).String())
		}()
	}

	tk := time.NewTicker(tick)
	defer tk.Stop()
	for {
		select {
		case <-ctx.Done():
			return
		case now := <-tk.C:
			spec := resolveScanSchedule(cfg.DataDir, os.Getenv, log)
			if spec.enabled() != wasEnabled {
				wasEnabled = spec.enabled()
				if wasEnabled {
					log.Info("scan scheduler: enabled",
						"cron", spec.cronStr, "every", spec.every.String(), "on_start", spec.onStart)
				} else {
					log.Info("scan scheduler: disabled (no schedule configured)")
				}
			}

			if spec.onStart && !bootFired && now.Sub(started) >= onStartDelay {
				bootFired = true
				fire("on-start")
				continue
			}
			if spec.cron != nil {
				minute := now.Format("2006-01-02T15:04")
				if spec.cron.Matches(now) && minute != lastMinute {
					lastMinute = minute
					fire("cron " + spec.cronStr)
					continue
				}
			}
			if spec.every > 0 && !running.Load() {
				base := started
				if ns := lastEndNs.Load(); ns != 0 {
					base = time.Unix(0, ns)
				}
				if now.Sub(base) >= spec.every {
					fire("interval " + spec.every.String())
				}
			}
		}
	}
}
