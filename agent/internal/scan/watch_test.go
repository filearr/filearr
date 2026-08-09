package scan

import (
	"context"
	"os"
	"path/filepath"
	"sync/atomic"
	"testing"
	"time"
)

// TestWatchCoalescesBurst asserts a burst of writes settles into exactly one
// rescan. The injected settle window is generous relative to the burst spacing
// so a stalled shared CI runner cannot let the window elapse MID-burst and
// legitimately produce a second rescan (flaked live 2026-08-09: "got 2" —
// 60ms settle vs 8 writes at 5ms plus scheduler jitter left ~20ms of margin).
func TestWatchCoalescesBurst(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"seed.mp4"})

	var calls int32
	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	const settle = 250 * time.Millisecond
	done := make(chan struct{})
	go func() {
		_ = Watch(ctx, root, settle, func() { atomic.AddInt32(&calls, 1) })
		close(done)
	}()

	// Let the watcher register, then fire a burst well inside the settle window.
	time.Sleep(50 * time.Millisecond)
	for i := 0; i < 8; i++ {
		p := filepath.Join(root, "burst", "f")
		_ = os.MkdirAll(filepath.Dir(p), 0o755)
		if err := os.WriteFile(p+string(rune('0'+i)), []byte("x"), 0o644); err != nil {
			t.Fatal(err)
		}
		time.Sleep(10 * time.Millisecond)
	}

	// Wait well past the settle window for the coalesced callback.
	time.Sleep(800 * time.Millisecond)
	if got := atomic.LoadInt32(&calls); got != 1 {
		t.Fatalf("burst should coalesce to exactly one rescan, got %d", got)
	}

	// A second, separate burst after the window fires again.
	if err := os.WriteFile(filepath.Join(root, "later.mp4"), []byte("y"), 0o644); err != nil {
		t.Fatal(err)
	}
	time.Sleep(800 * time.Millisecond)
	if got := atomic.LoadInt32(&calls); got != 2 {
		t.Fatalf("a later change should trigger a second rescan, got %d", got)
	}

	cancel()
	<-done
}
