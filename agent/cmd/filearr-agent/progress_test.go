package main

import (
	"testing"
	"time"
)

// The progress log line is time-throttled: at most one per progressLogEvery
// per root, with the first batch of every scan always logging (2026-08-25
// user report: a fast no-change refresh logged every 250 files).
func TestProgressLogGateThrottles(t *testing.T) {
	g := &progressLogGate{}
	t0 := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	if due, _ := g.due(`d:\`, 250, t0); !due {
		t.Fatal("first batch must log")
	}
	for i := 1; i <= 30; i++ { // 30 batches in 9 s: silent
		if due, _ := g.due(`d:\`, 250+250*i, t0.Add(time.Duration(i)*300*time.Millisecond)); due {
			t.Fatalf("batch %d logged inside the throttle window", i)
		}
	}
	due, rate := g.due(`d:\`, 10250, t0.Add(10*time.Second))
	if !due {
		t.Fatal("a batch 10 s after the last line must log")
	}
	if rate != 1000 { // (10250-250) files / 10 s
		t.Fatalf("rate = %v, want 1000 files/s", rate)
	}
	// A different root, or a seen count that went backwards (new scan of the
	// same root), resets the clock and logs immediately.
	if due, _ := g.due(`e:\`, 250, t0.Add(10*time.Second+time.Millisecond)); !due {
		t.Fatal("new root must log at once")
	}
	if due, _ := g.due(`e:\`, 100, t0.Add(11*time.Second)); !due {
		t.Fatal("seen going backwards (fresh scan) must log at once")
	}
}
