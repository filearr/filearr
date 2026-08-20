package scan

import (
	"context"
	"errors"
	"os"
	"path/filepath"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
)

// Roadmap §19 parity: a FULL walk that observes zero entries over a library that
// still holds active rows is refused (dead/stale-mount signature) rather than
// tombstoning everything. ForceEmpty is the operator's escape hatch.
func TestScanEmptyMountGuardRefusesMassTombstone(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4", "b.mkv"})
	st := newStore(t)

	r1 := mustScan(t, st, Options{Root: root})
	if r1.New != 2 {
		t.Fatalf("seed scan: %+v", r1)
	}

	// Simulate a dead mount: the tree is now empty but the library holds items.
	for _, f := range []string{"a.mp4", "b.mkv"} {
		if err := os.Remove(filepath.Join(root, f)); err != nil {
			t.Fatal(err)
		}
	}

	_, err := Scan(context.Background(), st, Options{Root: root})
	var sre *ScanRootError
	if !errors.As(err, &sre) {
		t.Fatalf("expected ScanRootError from empty-mount guard, got %v", err)
	}
	// Nothing was tombstoned — both rows are still active.
	items := loadByRel(t, st, r1.RootID)
	for _, rel := range []string{"a.mp4", "b.mkv"} {
		if items[rel].Status != index.StatusActive {
			t.Errorf("%s must stay active after a refused empty scan, got %s", rel, items[rel].Status)
		}
	}
}

func TestScanEmptyMountGuardForceEmptyTombstones(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4"})
	st := newStore(t)
	r1 := mustScan(t, st, Options{Root: root})
	if err := os.Remove(filepath.Join(root, "a.mp4")); err != nil {
		t.Fatal(err)
	}
	// ForceEmpty consents to the everything-was-deleted rescan.
	r2 := mustScan(t, st, Options{Root: root, ForceEmpty: true})
	if r2.Missing != 1 {
		t.Fatalf("force-empty should tombstone: %+v", r2)
	}
	if loadByRel(t, st, r1.RootID)["a.mp4"].Status != index.StatusMissing {
		t.Error("force-empty should have tombstoned the deleted file")
	}
}

func TestScanEmptyGuardAllowsGenuinelyEmptyNewLibrary(t *testing.T) {
	// An empty tree with NO prior active rows is fine (a fresh, empty library).
	root := t.TempDir()
	st := newStore(t)
	r := mustScan(t, st, Options{Root: root})
	if r.Missing != 0 || r.New != 0 {
		t.Fatalf("empty new library: %+v", r)
	}
}

// Finding #15: a transient read failure / hash timeout on a CHANGED file must
// NOT clobber the previously-good digests with empty strings.
func TestChangedFileHashFailureKeepsPriorHash(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp4"})
	st := newStore(t)
	r1 := mustScan(t, st, Options{Root: root})
	priorHash := loadByRel(t, st, r1.RootID)["a.mp4"].QuickHash
	if priorHash == "" {
		t.Fatal("seed should have hashed the file")
	}

	// Force the hasher to fail (as an open/read error or timeout would), then
	// change the file so the diff takes the "changed" branch.
	orig := hashSyncFn
	hashSyncFn = func(string, int64, HashPolicy) (string, string) { return "", "" }
	defer func() { hashSyncFn = orig }()
	if err := os.WriteFile(filepath.Join(root, "a.mp4"), []byte("different bytes now"), 0o644); err != nil {
		t.Fatal(err)
	}

	r2 := mustScan(t, st, Options{Root: root})
	if r2.Changed != 1 {
		t.Fatalf("expected the changed branch: %+v", r2)
	}
	got := loadByRel(t, st, r1.RootID)["a.mp4"].QuickHash
	if got != priorHash {
		t.Errorf("a failed re-hash must keep the prior digest, not clobber it: was %q, now %q", priorHash, got)
	}
}
