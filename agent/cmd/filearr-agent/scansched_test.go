package main

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

func discard() *slog.Logger {
	return slog.New(slog.NewTextHandler(nilWriter{}, &slog.HandlerOptions{Level: slog.LevelError + 4}))
}

type nilWriter struct{}

func (nilWriter) Write(p []byte) (int, error) { return len(p), nil }

func savePolicy(t *testing.T, dataDir string, policy map[string]any) {
	t.Helper()
	raw, err := json.Marshal(policy)
	if err != nil {
		t.Fatal(err)
	}
	err = agentcfg.NewETagCache(dataDir).Save(agentcfg.PolicyDoc{
		ETag: "t", Version: 1, FetchedAt: time.Now(), VerifiedAt: time.Now(), Policy: raw,
	})
	if err != nil {
		t.Fatal(err)
	}
}

func env(m map[string]string) func(string) string {
	return func(k string) string { return m[k] }
}

func TestResolveScanScheduleOffByDefault(t *testing.T) {
	spec := resolveScanSchedule(t.TempDir(), env(nil), discard())
	if spec.enabled() {
		t.Fatalf("no policy, no env: scheduler must be OFF, got %+v", spec)
	}
}

func TestResolveScanSchedulePolicyWinsOverEnv(t *testing.T) {
	dir := t.TempDir()
	savePolicy(t, dir, map[string]any{
		"scan_cron": "0 3 * * *", "scan_interval_seconds": 7200, "scan_on_start": true,
	})
	spec := resolveScanSchedule(dir, env(map[string]string{
		envScanCron: "30 5 * * *", envScanEvery: "15m", envScanOnBoot: "false",
	}), discard())
	if spec.cronStr != "0 3 * * *" || spec.cron == nil {
		t.Errorf("cron: policy must win, got %q", spec.cronStr)
	}
	if spec.every != 2*time.Hour {
		t.Errorf("every: policy must win, got %s", spec.every)
	}
	if !spec.onStart {
		t.Error("on_start: policy must win")
	}
}

func TestResolveScanScheduleGroupCron(t *testing.T) {
	// Config-group settings arrive under the policy "group" section; the
	// documented precedence is top-level policy key > group > env.
	dir := t.TempDir()
	savePolicy(t, dir, map[string]any{
		"group": map[string]any{"scan_schedule_cron": "0 4 * * *"},
	})
	spec := resolveScanSchedule(dir, env(map[string]string{envScanCron: "0 6 * * *"}), discard())
	if spec.cronStr != "0 4 * * *" {
		t.Fatalf("group cron must beat env, got %q", spec.cronStr)
	}

	savePolicy(t, dir, map[string]any{
		"scan_cron": "0 2 * * *",
		"group":     map[string]any{"scan_schedule_cron": "0 4 * * *"},
	})
	spec = resolveScanSchedule(dir, env(nil), discard())
	if spec.cronStr != "0 2 * * *" {
		t.Fatalf("top-level policy cron must beat group, got %q", spec.cronStr)
	}
}

func TestResolveScanScheduleEnvFallback(t *testing.T) {
	spec := resolveScanSchedule(t.TempDir(), env(map[string]string{
		envScanCron: "*/30 * * * *", envScanEvery: "6h", envScanOnBoot: "1",
	}), discard())
	if spec.cronStr != "*/30 * * * *" || spec.every != 6*time.Hour || !spec.onStart {
		t.Fatalf("env fallback not honored: %+v", spec)
	}
}

func TestResolveScanScheduleBadValuesIgnored(t *testing.T) {
	dir := t.TempDir()
	savePolicy(t, dir, map[string]any{"scan_cron": "not a cron"})
	spec := resolveScanSchedule(dir, env(map[string]string{envScanEvery: "banana"}), discard())
	if spec.cron != nil || spec.every != 0 {
		t.Fatalf("bad knobs must be ignored, got %+v", spec)
	}
	if spec.enabled() {
		t.Fatal("all knobs invalid: scheduler must resolve OFF")
	}
}

// --- local overrides (2026-08-10) --------------------------------------------

func saveLocal(t *testing.T, dataDir string, ls agentcfg.LocalSettings) {
	t.Helper()
	if err := agentcfg.SaveLocalSettings(dataDir, ls); err != nil {
		t.Fatal(err)
	}
}

func sp(s string) *string { return &s }
func ip(n int) *int       { return &n }
func bp(b bool) *bool     { return &b }

// The full documented chain, per knob:
//
//	central policy > local override > FILEARR_AGENT_* env > default
func TestResolveScanSchedulePrecedenceCentralLocalEnvDefault(t *testing.T) {
	envAll := env(map[string]string{
		envScanCron: "30 5 * * *", envScanEvery: "15m", envScanOnBoot: "false",
	})

	// (1) env only.
	dir := t.TempDir()
	spec := resolveScanSchedule(dir, envAll, discard())
	if spec.cronStr != "30 5 * * *" || spec.every != 15*time.Minute || spec.onStart {
		t.Fatalf("env-only tier wrong: %+v", spec)
	}

	// (2) a local override beats env.
	saveLocal(t, dir, agentcfg.LocalSettings{
		ScanCron: sp("0 2 * * *"), ScanIntervalSeconds: ip(3600), ScanOnStart: bp(true),
	})
	spec = resolveScanSchedule(dir, envAll, discard())
	if spec.cronStr != "0 2 * * *" || spec.every != time.Hour || !spec.onStart {
		t.Fatalf("local override must beat env: %+v", spec)
	}

	// (3) central policy beats the local override.
	savePolicy(t, dir, map[string]any{
		"scan_cron": "0 4 * * *", "scan_interval_seconds": 7200, "scan_on_start": false,
	})
	spec = resolveScanSchedule(dir, envAll, discard())
	if spec.cronStr != "0 4 * * *" || spec.every != 2*time.Hour || spec.onStart {
		t.Fatalf("central policy must beat the local override: %+v", spec)
	}

	// (4) a config group's cron is CENTRAL too, so it also beats local.
	savePolicy(t, dir, map[string]any{"group": map[string]any{"scan_schedule_cron": "0 6 * * *"}})
	spec = resolveScanSchedule(dir, envAll, discard())
	if spec.cronStr != "0 6 * * *" {
		t.Fatalf("group cron must beat the local override: %q", spec.cronStr)
	}

	// (5) with no policy at all, the local override is back in charge, and a
	// knob it does NOT set still falls through to env.
	dir2 := t.TempDir()
	saveLocal(t, dir2, agentcfg.LocalSettings{ScanCron: sp("0 1 * * *")})
	spec = resolveScanSchedule(dir2, envAll, discard())
	if spec.cronStr != "0 1 * * *" {
		t.Fatalf("local cron: %q", spec.cronStr)
	}
	if spec.every != 15*time.Minute {
		t.Fatalf("an unset local knob must fall through to env, got %s", spec.every)
	}

	// (6) nothing anywhere: scheduler off.
	if resolveScanSchedule(t.TempDir(), env(nil), discard()).enabled() {
		t.Fatal("no policy, no local, no env: the scheduler must be OFF")
	}
}

// A corrupt local-settings.json must not disturb resolution — the chain simply
// carries on without local overrides.
func TestResolveScanScheduleIgnoresCorruptLocalSettings(t *testing.T) {
	dir := t.TempDir()
	if err := os.WriteFile(filepath.Join(dir, agentcfg.LocalSettingsName), []byte("{oops"), 0o644); err != nil {
		t.Fatal(err)
	}
	spec := resolveScanSchedule(dir, env(map[string]string{envScanCron: "0 7 * * *"}), discard())
	if spec.cronStr != "0 7 * * *" {
		t.Fatalf("a corrupt local file must fall through to env, got %q", spec.cronStr)
	}
}

// The scheduler must not fire while scanning is held — by either holder.
func TestSchedulerSkipsWhileHeld(t *testing.T) {
	oldTick, oldDelay := schedTick, schedOnStartDelay
	schedTick, schedOnStartDelay = 10*time.Millisecond, 5*time.Millisecond
	t.Cleanup(func() { schedTick, schedOnStartDelay = oldTick, oldDelay })
	t.Cleanup(func() { scanInFlight.Store(false) })

	// An INTERVAL schedule (not scan-on-start): on-start fires at most once for
	// the process lifetime, so it cannot show that a release lets scanning
	// resume — an interval retries every tick, which is exactly the property
	// under test.
	dir := t.TempDir()
	t.Setenv(envScanEvery, "20ms")

	ops := newOpState(dir, discard())
	if err := ops.SetLocalScanPaused(true); err != nil {
		t.Fatal(err)
	}

	var fired atomic.Int32
	run := func(context.Context, *config) error { fired.Add(1); return nil }

	ctx, cancel := context.WithCancel(context.Background())
	done := make(chan struct{})
	go func() {
		defer close(done)
		runScanScheduler(ctx, &config{DataDir: dir}, discard(), run, ops.ScanHold)
	}()
	time.Sleep(150 * time.Millisecond) // many ticks past the on-start delay
	if got := fired.Load(); got != 0 {
		t.Fatalf("a locally paused agent must not scan, got %d run(s)", got)
	}

	// Resuming locally releases it (nothing else is holding).
	if err := ops.SetLocalScanPaused(false); err != nil {
		t.Fatal(err)
	}
	deadline := time.After(3 * time.Second)
	for fired.Load() == 0 {
		select {
		case <-deadline:
			cancel()
			<-done
			t.Fatal("scanning did not resume after the local pause was cleared")
		case <-time.After(5 * time.Millisecond):
		}
	}
	cancel()
	<-done
}

// The loop fires the injected runner from a policy interval and never
// overlaps a still-running scan.
func TestSchedulerLoopFiresAndSkipsOverlap(t *testing.T) {
	oldTick, oldDelay := schedTick, schedOnStartDelay
	schedTick, schedOnStartDelay = 20*time.Millisecond, 10*time.Millisecond
	t.Cleanup(func() { schedTick, schedOnStartDelay = oldTick, oldDelay })

	dir := t.TempDir()
	savePolicy(t, dir, map[string]any{"scan_on_start": true})

	var fired atomic.Int32
	block := make(chan struct{})
	run := func(ctx context.Context, _ *config) error {
		fired.Add(1)
		select {
		case <-block:
		case <-ctx.Done():
		}
		return nil
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() {
		defer close(done)
		runScanScheduler(ctx, &config{DataDir: dir}, discard(), run, nil)
	}()

	// on-start fires after the (scaled) grace; then several more ticks pass
	// while the runner blocks — the overlap guard must hold it to one run.
	deadline := time.After(5 * time.Second)
	for fired.Load() == 0 {
		select {
		case <-deadline:
			t.Fatal("on-start scan never fired")
		case <-time.After(10 * time.Millisecond):
		}
	}
	time.Sleep(200 * time.Millisecond) // ~10 further ticks with the run blocked
	if got := fired.Load(); got != 1 {
		t.Fatalf("overlap guard: want exactly 1 concurrent run, got %d", got)
	}
	close(block)
	cancel()
	<-done
}
