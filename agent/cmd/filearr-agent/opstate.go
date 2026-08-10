package main

// Operational state shared across daemon subsystems (2026-08-09):
//
//   - `suspended` — the operator paused THIS agent's processing (console
//     `suspend` command). Gates the scan scheduler AND the replication push;
//     persisted to <dataDir>/suspend.json so it survives restarts. Command
//     polling, policy polling, health reporting and cert renewal keep running
//     (or the operator could never resume the agent remotely).
//   - `centralMaint` — CENTRAL advertised global maintenance mode on the last
//     command poll (X-Filearr-Maintenance header). Gates ONLY the replication
//     push: the agent keeps scanning and collecting inventory locally; the
//     outbox accumulates and drains when the mode lifts. Process-lifetime
//     (level-triggered per poll), never persisted.
//   - `localPaused` — a LOCAL operator paused scanning from the agent's own web
//     UI (2026-08-10), gated by the central `local_scan_control` permission.
//     Deliberately a SEPARATE, scan-only flag from `suspended`: central's
//     suspend is a fleet control that also stops replication, and a local
//     resume must never be able to lift it (otherwise the machine's operator
//     could defeat the fleet control, which is exactly what the permission
//     gates exist to prevent). Both flags gate the scheduler — scanning runs
//     only when NEITHER is set. Persisted in <dataDir>/local-settings.json
//     beside the local schedule overrides.

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"sync/atomic"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
)

const suspendStateName = "suspend.json"

type opState struct {
	dataDir      string
	log          *slog.Logger
	suspended    atomic.Bool
	centralMaint atomic.Bool
	localPaused  atomic.Bool
}

type suspendState struct {
	Suspended bool `json:"suspended"`
}

// newOpState loads the persisted suspend + local-pause flags (absent/broken
// file => running).
func newOpState(dataDir string, log *slog.Logger) *opState {
	s := &opState{dataDir: dataDir, log: log}
	b, err := os.ReadFile(filepath.Join(dataDir, suspendStateName))
	if err == nil {
		var st suspendState
		if json.Unmarshal(b, &st) == nil && st.Suspended {
			s.suspended.Store(true)
			log.Warn("agent processing is SUSPENDED (persisted operator state) — scans and replication are paused; resume from the central console")
		}
	}
	if ls, lerr := agentcfg.LoadLocalSettings(dataDir); lerr == nil && ls.ScanPaused {
		s.localPaused.Store(true)
		log.Warn("scanning is PAUSED LOCALLY (persisted local operator state) — replication is unaffected; resume from this agent's web UI")
	}
	return s
}

// SetSuspended applies + persists the operator's desired processing state.
// The persist failure is returned (the command reports it) but the in-memory
// state still flips — an operator action must take effect now, not after a
// disk hiccup clears.
func (s *opState) SetSuspended(_ context.Context, suspended bool) error {
	s.suspended.Store(suspended)
	blob, err := json.Marshal(suspendState{Suspended: suspended})
	if err != nil {
		return err
	}
	tmp := filepath.Join(s.dataDir, suspendStateName+".tmp")
	if err := os.WriteFile(tmp, blob, 0o644); err != nil {
		return err
	}
	return os.Rename(tmp, filepath.Join(s.dataDir, suspendStateName))
}

// SetCentralMaintenance records central's advertisement, logging edges only.
func (s *opState) SetCentralMaintenance(active bool) {
	if s.centralMaint.Swap(active) != active {
		if active {
			s.log.Info("central is in maintenance mode — pausing replication push (local scanning continues; outbox accumulates)")
		} else {
			s.log.Info("central maintenance mode lifted — resuming replication push")
		}
	}
}

// SetLocalScanPaused applies + persists the LOCAL scan pause (agent web UI).
//
// It touches ONLY the local flag: clearing it never clears a central suspend, so
// a local operator cannot resume an agent the fleet paused. The persisted state
// lives in local-settings.json (a read-modify-write that preserves the local
// schedule overrides stored alongside it).
func (s *opState) SetLocalScanPaused(paused bool) error {
	s.localPaused.Store(paused)
	_, err := agentcfg.UpdateLocalSettings(s.dataDir, func(ls *agentcfg.LocalSettings) {
		ls.ScanPaused = paused
	})
	return err
}

// Suspended reports the operator-suspend flag (gates scans AND replication).
func (s *opState) Suspended() bool { return s.suspended.Load() }

// LocalScanPaused reports the LOCAL scan-only pause flag.
func (s *opState) LocalScanPaused() bool { return s.localPaused.Load() }

// ScanHold reports whether scanning is currently held, and by whom. Central's
// suspend is named FIRST because it is the one a local operator cannot lift —
// the scheduler logs this reason, and reporting "local pause" while a central
// suspend is also in force would send an operator to the wrong console.
func (s *opState) ScanHold() (bool, string) {
	if s.suspended.Load() {
		return true, "suspended by central"
	}
	if s.localPaused.Load() {
		return true, "paused locally"
	}
	return false, ""
}

// ReplicationPaused gates the outbox drain: operator suspend OR central
// maintenance.
func (s *opState) ReplicationPaused() bool {
	return s.suspended.Load() || s.centralMaint.Load()
}

// CentralMaintenance reports the last-advertised central maintenance state.
func (s *opState) CentralMaintenance() bool { return s.centralMaint.Load() }
