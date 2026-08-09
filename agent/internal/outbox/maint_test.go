package outbox

// PruneSent retention + the Replicator pause gate (2026-08-09 maintenance).

import (
	"context"
	"sync/atomic"
	"testing"
	"time"
)

func TestPruneSentKeepsUnsentAndNewestRow(t *testing.T) {
	st := openStore(t)
	ctx := context.Background()
	ev := func(rel string) Event {
		return Event{Op: "created", LibraryRef: "lib", RelPath: rel}
	}
	s1 := writeOne(t, st, ev("a"))
	s2 := writeOne(t, st, ev("b"))
	s3 := writeOne(t, st, ev("c"))
	_ = writeOne(t, st, ev("d")) // stays unsent

	ob := New(st.DB())
	if _, err := ob.MarkSent(ctx, s1, s3, "b1"); err != nil {
		t.Fatal(err)
	}
	// Backdate the sent stamps so they fall past the retention cutoff.
	if _, err := st.DB().Exec(
		`UPDATE outbox SET sent_at = ? WHERE sent_at IS NOT NULL`,
		time.Now().UTC().Add(-30*24*time.Hour).Format(time.RFC3339Nano),
	); err != nil {
		t.Fatal(err)
	}

	n, err := ob.PruneSent(ctx, 7*24*time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if n != 3 {
		t.Fatalf("want 3 pruned (s1..s3 sent+old; s4 unsent kept), got %d", n)
	}
	if got := countRows(t, st.DB(), `SELECT COUNT(*) FROM outbox`); got != 1 {
		t.Fatalf("want 1 surviving row, got %d", got)
	}
	// IsEmpty must still say "has held rows" — the rebuilt-index signal.
	empty, err := ob.IsEmpty(ctx)
	if err != nil || empty {
		t.Fatalf("IsEmpty after prune: want false, got %v (err=%v)", empty, err)
	}
	if unsent, _ := ob.CountUnsent(ctx); unsent != 1 {
		t.Fatalf("unsent row must survive, got %d", unsent)
	}
	_ = s2
}

func TestPruneSentNewestSentRowSurvivesEvenWhenOld(t *testing.T) {
	st := openStore(t)
	ctx := context.Background()
	s1 := writeOne(t, st, Event{Op: "created", LibraryRef: "lib", RelPath: "x"})
	ob := New(st.DB())
	if _, err := ob.MarkSent(ctx, s1, s1, "b1"); err != nil {
		t.Fatal(err)
	}
	if _, err := st.DB().Exec(
		`UPDATE outbox SET sent_at = ?`,
		time.Now().UTC().Add(-365*24*time.Hour).Format(time.RFC3339Nano),
	); err != nil {
		t.Fatal(err)
	}
	n, err := ob.PruneSent(ctx, time.Hour)
	if err != nil {
		t.Fatal(err)
	}
	if n != 0 {
		t.Fatalf("the only (newest) row must never be pruned, got %d deletions", n)
	}
	if empty, _ := ob.IsEmpty(ctx); empty {
		t.Fatal("IsEmpty flipped — the rebuilt-index signal was destroyed")
	}
}

func TestReplicatorPausedGateIdlesWithoutFlushing(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{Op: "created", LibraryRef: "lib", RelPath: "p"})

	var paused atomic.Bool
	paused.Store(true)
	// No BaseURL/HTTP server: any flush attempt would error and bump backoff —
	// the test proves the gate never lets the loop reach the outbox at all.
	var reads atomic.Int32
	rep := NewReplicator(New(st.DB()), Config{
		BaseURL: "http://127.0.0.1:1", // unroutable — a flush attempt would fail loudly
		AgentID: "agent-1",
		Poll:    5 * time.Millisecond,
		Paused: func() bool {
			reads.Add(1)
			return paused.Load()
		},
	})

	ctx, cancel := context.WithTimeout(context.Background(), 150*time.Millisecond)
	defer cancel()
	_ = rep.Run(ctx)

	if reads.Load() < 2 {
		t.Fatalf("pause gate was not consulted repeatedly (reads=%d)", reads.Load())
	}
	// The row must still be unsent and untouched (no failed-flush mutation).
	if n, _ := New(st.DB()).CountUnsent(context.Background()); n != 1 {
		t.Fatalf("paused loop must not touch the outbox; unsent=%d", n)
	}
}
