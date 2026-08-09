package main

// opState persistence + the temp-file sweep (2026-08-09 maintenance).

import (
	"context"
	"os"
	"path/filepath"
	"testing"
	"time"
)

func TestOpStateSuspendPersistsAcrossRestart(t *testing.T) {
	dir := t.TempDir()
	s := newOpState(dir, discard())
	if s.Suspended() {
		t.Fatal("fresh data dir must start unsuspended")
	}
	if err := s.SetSuspended(context.Background(), true); err != nil {
		t.Fatalf("SetSuspended: %v", err)
	}
	if !s.Suspended() || !s.ReplicationPaused() {
		t.Fatal("suspend must gate both scan and replication")
	}

	// "Restart": a fresh opState over the same dir loads the persisted flag.
	s2 := newOpState(dir, discard())
	if !s2.Suspended() {
		t.Fatal("suspend flag must survive a restart")
	}
	if err := s2.SetSuspended(context.Background(), false); err != nil {
		t.Fatalf("resume: %v", err)
	}
	if newOpState(dir, discard()).Suspended() {
		t.Fatal("resume must persist too")
	}
}

func TestOpStateCentralMaintenanceGatesReplicationOnly(t *testing.T) {
	s := newOpState(t.TempDir(), discard())
	s.SetCentralMaintenance(true)
	if !s.ReplicationPaused() {
		t.Fatal("central maintenance must pause replication")
	}
	if s.Suspended() {
		t.Fatal("central maintenance must NOT suspend local scanning")
	}
	s.SetCentralMaintenance(false)
	if s.ReplicationPaused() {
		t.Fatal("lifting maintenance must resume replication")
	}
}

func TestSweepTempFiles(t *testing.T) {
	dir := t.TempDir()
	old := time.Now().Add(-48 * time.Hour)
	mk := func(name string, stale bool) {
		p := filepath.Join(dir, name)
		if err := os.WriteFile(p, []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		if stale {
			if err := os.Chtimes(p, old, old); err != nil {
				t.Fatal(err)
			}
		}
	}
	mk(".tmp-123", true)          // stale atomic-write leftover -> removed
	mk("download-abc", true)      // stale update download -> removed
	mk("scan-status.json.tmp", true) // stale .tmp suffix -> removed
	mk(".tmp-fresh", false)       // young -> kept
	mk("state.json", true)        // real state file -> kept (name mismatch)
	if err := os.Mkdir(filepath.Join(dir, "inventory"), 0o755); err != nil {
		t.Fatal(err)
	}

	files, bytes, err := sweepTempFiles(dir, time.Now().Add(-24*time.Hour))
	if err != nil {
		t.Fatal(err)
	}
	if files != 3 || bytes != 3 {
		t.Fatalf("want 3 files / 3 bytes removed, got %d / %d", files, bytes)
	}
	for _, keep := range []string{".tmp-fresh", "state.json"} {
		if _, err := os.Stat(filepath.Join(dir, keep)); err != nil {
			t.Fatalf("%s must survive the sweep: %v", keep, err)
		}
	}
}
