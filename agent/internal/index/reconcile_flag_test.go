package index

// The durable last-reconcile watermark (2026-08-22): what makes the daemon's
// startup catch-up possible — before it, the periodic ticker restarted from
// zero every boot and a daily-rebooting machine never reconciled (agent XENON).

import (
	"context"
	"testing"
	"time"
)

func TestLastReconcileAtRoundTripAndZeroDefault(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()

	got, err := st.LastReconcileAt(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !got.IsZero() {
		t.Fatalf("fresh store must report zero time, got %v", got)
	}

	stamp := time.Date(2026, 8, 22, 10, 30, 0, 0, time.UTC)
	if err := st.SetLastReconcileAt(ctx, stamp); err != nil {
		t.Fatal(err)
	}
	got, err = st.LastReconcileAt(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if !got.Equal(stamp) {
		t.Fatalf("round trip: want %v got %v", stamp, got)
	}

	// Upsert: a later sweep overwrites.
	later := stamp.Add(24 * time.Hour)
	if err := st.SetLastReconcileAt(ctx, later); err != nil {
		t.Fatal(err)
	}
	got, _ = st.LastReconcileAt(ctx)
	if !got.Equal(later) {
		t.Fatalf("upsert: want %v got %v", later, got)
	}
}
