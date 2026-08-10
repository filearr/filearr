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

// The LOCAL scan pause (2026-08-10) is a separate, scan-only flag: it persists
// on its own, does not touch replication, and — the rule the permission gates
// exist for — a local resume can never lift a CENTRAL suspend.
func TestLocalScanPauseIsSeparateFromCentralSuspend(t *testing.T) {
	dir := t.TempDir()
	s := newOpState(dir, discard())
	if held, _ := s.ScanHold(); held {
		t.Fatal("a fresh data dir must not hold scanning")
	}

	if err := s.SetLocalScanPaused(true); err != nil {
		t.Fatalf("SetLocalScanPaused: %v", err)
	}
	held, by := s.ScanHold()
	if !held || by != "paused locally" {
		t.Fatalf("local pause must hold scanning, got held=%v by=%q", held, by)
	}
	if s.ReplicationPaused() {
		t.Fatal("a LOCAL pause must not stop the replication push — that is central's suspend, not this")
	}
	if s.Suspended() {
		t.Fatal("a local pause must not masquerade as a central suspend")
	}

	// It survives a restart, like suspend.json does.
	if held, _ := newOpState(dir, discard()).ScanHold(); !held {
		t.Fatal("the local pause must survive a restart")
	}

	// Now central suspends the agent too. A LOCAL resume clears only the local
	// flag: the agent stays held, and it is held BY CENTRAL — otherwise the
	// machine's operator could defeat the fleet control.
	if err := s.SetSuspended(context.Background(), true); err != nil {
		t.Fatal(err)
	}
	if err := s.SetLocalScanPaused(false); err != nil {
		t.Fatal(err)
	}
	held, by = s.ScanHold()
	if !held {
		t.Fatal("a local resume lifted a CENTRAL suspend")
	}
	if by != "suspended by central" {
		t.Fatalf("the surviving hold must be attributed to central, got %q", by)
	}
	if !s.Suspended() || !s.ReplicationPaused() {
		t.Fatal("the central suspend must be entirely untouched by a local resume")
	}

	// And only central can lift its own.
	if err := s.SetSuspended(context.Background(), false); err != nil {
		t.Fatal(err)
	}
	if held, _ := s.ScanHold(); held {
		t.Fatal("with both holds cleared, scanning must resume")
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
