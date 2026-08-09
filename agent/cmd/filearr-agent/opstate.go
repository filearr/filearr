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

import (
	"context"
	"encoding/json"
	"log/slog"
	"os"
	"path/filepath"
	"sync/atomic"
)

const suspendStateName = "suspend.json"

type opState struct {
	dataDir      string
	log          *slog.Logger
	suspended    atomic.Bool
	centralMaint atomic.Bool
}

type suspendState struct {
	Suspended bool `json:"suspended"`
}

// newOpState loads the persisted suspend flag (absent/broken file => running).
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

// Suspended reports the operator-suspend flag (gates scans AND replication).
func (s *opState) Suspended() bool { return s.suspended.Load() }

// ReplicationPaused gates the outbox drain: operator suspend OR central
// maintenance.
func (s *opState) ReplicationPaused() bool {
	return s.suspended.Load() || s.centralMaint.Load()
}

// CentralMaintenance reports the last-advertised central maintenance state.
func (s *opState) CentralMaintenance() bool { return s.centralMaint.Load() }
