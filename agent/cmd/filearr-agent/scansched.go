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

type schedSpec struct {
	cron    *schedule.Cron
	cronStr string
	every   time.Duration
	onStart bool
}

func (s schedSpec) enabled() bool { return s.cron != nil || s.every > 0 || s.onStart }

// resolveScanSchedule computes the effective schedule: per-knob, policy wins
// over env; unset knobs fall through. Invalid values are logged and ignored
// (the daemon must keep running on a bad knob, not crash-loop).
func resolveScanSchedule(dataDir string, getenv func(string) string, log *slog.Logger) schedSpec {
	pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
	if err != nil {
		log.Warn("scan scheduler: cached policy unreadable; using env only", "err", err)
		ok = false
	}
	var spec schedSpec

	// Cron precedence (documented): top-level policy key > config-group
	// settings (group.scan_schedule_cron) > env.
	cronStr := getenv(envScanCron)
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

	if ok && pol.ScanIntervalSeconds != nil {
		if *pol.ScanIntervalSeconds > 0 {
			spec.every = time.Duration(*pol.ScanIntervalSeconds) * time.Second
		}
	} else if v := getenv(envScanEvery); v != "" {
		if d, err := time.ParseDuration(v); err != nil || d <= 0 {
			log.Warn("scan scheduler: invalid "+envScanEvery+" ignored", "value", v)
		} else {
			spec.every = d
		}
	}

	if ok && pol.ScanOnStart != nil {
		spec.onStart = *pol.ScanOnStart
	} else if v := getenv(envScanOnBoot); v != "" {
		b, err := strconv.ParseBool(v)
		spec.onStart = err == nil && b
	}
	return spec
}

// startScanScheduler launches the scheduler loop; the returned channel closes
// when it unwinds. A no-op-cheap loop: one policy-cache read per tick.
func startScanScheduler(ctx context.Context, cfg *config, log *slog.Logger) <-chan struct{} {
	done := make(chan struct{})
	go func() {
		defer close(done)
		runScanScheduler(ctx, cfg, log, runScanChild)
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
) {
	tick, onStartDelay := schedTick, schedOnStartDelay
	var (
		running    atomic.Bool
		lastMinute string       // cron dedup: fire once per matching minute
		lastEndNs  atomic.Int64 // interval baseline (unix ns); 0 until first run ends
		started    = time.Now()
		bootFired  bool
		wasEnabled bool
	)

	fire := func(reason string) {
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
