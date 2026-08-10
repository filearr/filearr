package main

// Local scan controls, daemon side (2026-08-10). The agent's own web UI can
// pause/resume scanning, trigger a scan, edit the scan schedule, and manage the
// scan roots — each gated by a central policy permission (local_scan_control /
// local_schedule_control / local_roots_control, all default OFF).
//
// The PERMISSION and LOCK decisions live in internal/localapi (webcontrol.go)
// so they are unit-testable without a daemon; this file supplies the ACTIONS
// and the live state snapshot the page renders. Nothing here writes to the
// catalog: the local surface stays read-only over items and metadata by
// invariant, and these controls only change what the agent DOES.

import (
	"context"
	"log/slog"
	"os"
	"time"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/localapi"
)

// webControlSeams builds the localapi control seams for the running daemon.
// daemonCtx (not a request context) owns any scan a control starts — an HTTP
// handler's context dies with its response and would kill the child instantly.
func webControlSeams(daemonCtx context.Context, cfg *config, ops *opState, log *slog.Logger) *localapi.ControlSeams {
	dataDir := cfg.DataDir
	return &localapi.ControlSeams{
		State: func(_ context.Context) (localapi.ControlsState, error) {
			return controlsState(dataDir, ops), nil
		},
		// Local pause only — see opState.SetLocalScanPaused: it cannot clear a
		// central suspend, by construction.
		SetLocalPaused: func(_ context.Context, paused bool) error {
			return ops.SetLocalScanPaused(paused)
		},
		ScanNow: func(_ context.Context) error {
			return triggerScanNow(daemonCtx, cfg, log)
		},
		SetSchedule: func(_ context.Context, edit localapi.ScheduleEdit) error {
			return applyLocalScheduleEdit(dataDir, edit)
		},
		AddRoot: func(_ context.Context, path string) error {
			if err := addScanRoot(dataDir, path); err != nil {
				return err
			}
			return stampRootsEdited(dataDir)
		},
		RemoveRoot: func(_ context.Context, path string) error {
			if err := removeScanRoot(dataDir, path); err != nil {
				return err
			}
			return stampRootsEdited(dataDir)
		},
	}
}

// applyLocalScheduleEdit persists one schedule edit into local-settings.json.
// A nil field is left alone; a key named in Clear removes its local override so
// the knob falls back through env → sidecar → default. Central-owned keys never
// reach here — localapi refuses those before calling the seam.
func applyLocalScheduleEdit(dataDir string, edit localapi.ScheduleEdit) error {
	_, err := agentcfg.UpdateLocalSettings(dataDir, func(ls *agentcfg.LocalSettings) {
		if edit.ScanCron != nil {
			v := *edit.ScanCron
			ls.ScanCron = &v
		}
		if edit.ScanIntervalSeconds != nil {
			v := *edit.ScanIntervalSeconds
			ls.ScanIntervalSeconds = &v
		}
		if edit.ScanOnStart != nil {
			v := *edit.ScanOnStart
			ls.ScanOnStart = &v
		}
		for _, k := range edit.Clear {
			switch k {
			case "scan_cron":
				ls.ScanCron = nil
			case "scan_interval_seconds":
				ls.ScanIntervalSeconds = nil
			case "scan_on_start":
				ls.ScanOnStart = nil
			}
		}
	})
	return err
}

// stampRootsEdited records that an operator edited the scan roots on this
// machine. Roots themselves live in scan.json; the stamp lives with the other
// local overrides so the health snapshot can tell central that this agent's
// setup was changed locally (a fleet-wide answer to "why does this box scan
// something different from its group").
func stampRootsEdited(dataDir string) error {
	_, err := agentcfg.UpdateLocalSettings(dataDir, func(ls *agentcfg.LocalSettings) {
		ls.RootsEditedAt = time.Now().UTC().Format(time.RFC3339)
	})
	return err
}

// controlsState assembles the live snapshot: the pause flags, the resolved
// schedule with the surface each value came from, the local overrides as
// stored, and the configured roots.
func controlsState(dataDir string, ops *opState) localapi.ControlsState {
	st := localapi.ControlsState{
		ScanRunning: scanInFlight.Load(),
		Roots:       scanRoots(dataDir),
	}
	if ops != nil {
		st.LocalPaused = ops.LocalScanPaused()
		st.CentralSuspended = ops.Suspended()
	}
	local, _ := agentcfg.LoadLocalSettings(dataDir)
	if local.ScanCron != nil {
		st.LocalScanCron = *local.ScanCron
	}
	if local.ScanIntervalSeconds != nil {
		st.LocalScanIntervalSeconds = *local.ScanIntervalSeconds
	}
	if local.ScanOnStart != nil {
		st.LocalScanOnStartSet = true
		st.LocalScanOnStart = *local.ScanOnStart
	}
	// The effective values come from the SAME resolver the scheduler runs, so
	// the page can never show a schedule the daemon is not actually using.
	spec := resolveScanSchedule(dataDir, os.Getenv, newLogger())
	st.ScanCron = spec.cronStr
	st.ScanIntervalSeconds = int(spec.every / time.Second)
	st.ScanOnStart = spec.onStart
	st.ScanCronSource = scheduleSource(dataDir, "scan_cron", local, os.Getenv)
	st.ScanIntervalSource = scheduleSource(dataDir, "scan_interval_seconds", local, os.Getenv)
	st.ScanOnStartSource = scheduleSource(dataDir, "scan_on_start", local, os.Getenv)
	return st
}

// scheduleSource names the surface that supplies one schedule knob today,
// walking the documented precedence in order:
//
//	central policy > local override > FILEARR_AGENT_* env > sidecar > default
//
// It answers the question an operator actually has when a control is disabled
// or a value looks wrong — "who set this?" — in the same wording the Status
// panel's extraction "source" already uses.
func scheduleSource(dataDir, key string, local agentcfg.LocalSettings, getenv func(string) string) string {
	doc, ok, err := agentcfg.NewETagCache(dataDir).Load()
	if err == nil && ok {
		central := doc.LocalSurface(time.Now(), agentcfg.DefaultOfflineGrace)
		if central.CentrallySet(key) {
			return policySourceLabel(central.Scope, central.Version)
		}
		if key == "scan_cron" {
			if pol, perr := doc.Parsed(); perr == nil && pol.Group != nil && pol.Group.ScanScheduleCron != nil {
				return "config group (scan_schedule_cron)"
			}
		}
	}
	switch key {
	case "scan_cron":
		if local.ScanCron != nil {
			return "local override"
		}
		if getenv(envScanCron) != "" {
			return envScanCron
		}
	case "scan_interval_seconds":
		if local.ScanIntervalSeconds != nil {
			return "local override"
		}
		if getenv(envScanEvery) != "" {
			return envScanEvery
		}
	case "scan_on_start":
		if local.ScanOnStart != nil {
			return "local override"
		}
		if getenv(envScanOnBoot) != "" {
			return envScanOnBoot
		}
	}
	return "default"
}
