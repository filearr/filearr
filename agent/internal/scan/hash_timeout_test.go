package scan

import (
	"log/slog"
	"os"
	"path/filepath"
	"testing"
	"time"
)

// Regression (2026-07-27, live): a corrupt/locked file on an Unraid shfs mount
// blocked read(2) forever inside hashFile, freezing the single-threaded walk at
// the same file (seen=65000) on every scan. HashPolicy.Timeout must abandon the
// hash at the deadline and leave the file unhashed instead of wedging.
func TestHashFileTimeoutAbandonsHungFile(t *testing.T) {
	orig := hashSyncFn
	t.Cleanup(func() { hashSyncFn = orig })
	release := make(chan struct{})
	t.Cleanup(func() { close(release) })
	hashSyncFn = func(string, int64, HashPolicy) (string, string) {
		<-release // simulate a read(2) that never returns
		return "deadbeef", "cafe"
	}

	done := make(chan [2]string, 1)
	go func() {
		q, c := hashFile("/poison/file", 1<<20, HashPolicy{
			Timeout: 50 * time.Millisecond,
			Log:     slog.New(slog.DiscardHandler),
		})
		done <- [2]string{q, c}
	}()
	select {
	case r := <-done:
		if r[0] != "" || r[1] != "" {
			t.Fatalf("timed-out hash must return empty hashes, got %q/%q", r[0], r[1])
		}
	case <-time.After(5 * time.Second):
		t.Fatal("hashFile did not honor Timeout — walk would wedge forever")
	}
}

// A healthy file under a generous Timeout must produce identical digests to the
// unbounded path (the bound may not perturb hashing itself).
func TestHashFileTimeoutPassthrough(t *testing.T) {
	p := filepath.Join(t.TempDir(), "f.bin")
	if err := os.WriteFile(p, []byte("hello filearr"), 0o644); err != nil {
		t.Fatal(err)
	}
	wantQ, wantC := hashFileSync(p, 13, HashPolicy{ComputeContent: true, FullMaxBytes: 1 << 30})
	gotQ, gotC := hashFile(p, 13, HashPolicy{ComputeContent: true, FullMaxBytes: 1 << 30, Timeout: 10 * time.Second})
	if gotQ != wantQ || gotC != wantC {
		t.Fatalf("bounded hash diverged: got %q/%q want %q/%q", gotQ, gotC, wantQ, wantC)
	}
	if wantQ == "" || wantC == "" {
		t.Fatal("sanity: sync path produced empty hashes")
	}
	// Timeout 0 = unbounded passthrough (pre-existing behavior).
	zq, zc := hashFile(p, 13, HashPolicy{ComputeContent: true, FullMaxBytes: 1 << 30})
	if zq != wantQ || zc != wantC {
		t.Fatalf("zero-timeout hash diverged: got %q/%q", zq, zc)
	}
}
