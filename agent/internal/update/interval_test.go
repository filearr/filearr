package update

import (
	"context"
	"net/http"
	"net/http/httptest"
	"sync/atomic"
	"testing"
	"time"
)

// SetInterval (update_poll_interval_seconds policy key, 2026-08-19) must
// retune the cadence AND wake Run out of its current sleep: an operator who
// tightens the interval to fit an update window must not wait out the old 6h
// nap first.
func TestSetIntervalWakesRunLoop(t *testing.T) {
	var polls atomic.Int32
	srv := httptest.NewServer(http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		polls.Add(1)
		w.WriteHeader(http.StatusNoContent)
	}))
	defer srv.Close()

	u := New(Config{
		BaseURL:  srv.URL,
		AgentID:  "agent-1",
		DataDir:  t.TempDir(),
		Interval: time.Hour, // the first sleep would be ~1h
	})
	if got := u.Interval(); got != time.Hour {
		t.Fatalf("initial interval = %s, want 1h", got)
	}

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()
	done := make(chan struct{})
	go func() { defer close(done); _ = u.Run(ctx) }()

	// First poll happens immediately.
	waitFor(t, func() bool { return polls.Load() >= 1 })

	// Shortening the cadence interrupts the 1h sleep: a second poll arrives
	// promptly, and the live interval reflects the change.
	u.SetInterval(50 * time.Millisecond)
	waitFor(t, func() bool { return polls.Load() >= 3 })
	if got := u.Interval(); got != 50*time.Millisecond {
		t.Fatalf("interval after SetInterval = %s", got)
	}

	// Same value again is a no-op (no wake, no change).
	before := polls.Load()
	u.SetInterval(50 * time.Millisecond)
	u.SetInterval(0) // ignored
	if got := u.Interval(); got != 50*time.Millisecond {
		t.Fatalf("SetInterval(0) must be ignored, got %s", got)
	}
	_ = before

	cancel()
	select {
	case <-done:
	case <-time.After(5 * time.Second):
		t.Fatal("Run did not stop on cancel")
	}
}

func waitFor(t *testing.T, cond func() bool) {
	t.Helper()
	deadline := time.Now().Add(5 * time.Second)
	for time.Now().Before(deadline) {
		if cond() {
			return
		}
		time.Sleep(5 * time.Millisecond)
	}
	t.Fatal("condition not met in time")
}
