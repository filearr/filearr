package commands

// The `rehash_sweep` command handler (QH-T6). Structurally the reextract handler
// tests, plus the one assertion neither of the two sweeps needs on its own: that
// `rehash_sweep` and the long-standing item-scoped `rehash_check` are routed to
// completely different code.

import (
	"context"
	"errors"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestRehashSweepPassesPayloadAndReportsCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{
		ID:   "c-rehash",
		Kind: KindRehashSweep,
		Payload: map[string]any{
			"force": false, "max_items": float64(1000),
			"min_size": float64(65537), "max_size": float64(131072),
		},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var got atomic.Value // map[string]any
	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunRehashSweep: func(_ context.Context, payload map[string]any) (map[string]any, error) {
			got.Store(payload)
			return map[string]any{
				"seen": 1000, "changed": 940, "verified": 55, "skipped": 5,
				"completed": false, "cursor": 88_123,
			}, nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}

	// The payload reaches the seam verbatim — the sweep owns its own vocabulary,
	// band knobs included.
	payload, _ := got.Load().(map[string]any)
	if payload["max_items"] != float64(1000) ||
		payload["min_size"] != float64(65537) || payload["max_size"] != float64(131072) {
		t.Fatalf("payload not passed through verbatim: %+v", payload)
	}
	rec, ok := m.completeFor("c-rehash")
	if !ok || !rec.OK {
		t.Fatalf("rehash_sweep must complete ok, got %+v (found=%v)", rec, ok)
	}
	// changed and verified must BOTH survive the round trip: collapsing them is
	// what makes a converged agent look like a broken sweep.
	if rec.Result["changed"] != float64(940) || rec.Result["verified"] != float64(55) {
		t.Fatalf("counters not propagated: %+v", rec.Result)
	}
}

func TestRehashSweepFailureKeepsPartialCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-rehash-fail", Kind: KindRehashSweep}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunRehashSweep: func(context.Context, map[string]any) (map[string]any, error) {
			// A sweep that corrected 40k rows before dying has done real, durable
			// work and an operator resending the command needs to know it resumes.
			return map[string]any{"seen": 40_000, "changed": 39_500, "cursor": 88_123},
				errors.New("mount went away")
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-rehash-fail")
	if !ok || rec.OK {
		t.Fatalf("a failed sweep must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil || rec.Result["changed"] != float64(39_500) ||
		rec.Result["cursor"] != float64(88_123) {
		t.Fatalf("partial progress + error must both be reported: %+v", rec.Result)
	}
}

func TestRehashSweepMissingSeamDegrades(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-noseam", Kind: KindRehashSweep}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	// An agent build without the daemon wiring must answer the command rather
	// than leave it dangling for central's redelivery sweep.
	p := NewPoller(Config{BaseURL: srv.URL, AgentID: "agent-1", HTTP: srv.Client()})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-noseam")
	if !ok || rec.OK {
		t.Fatalf("nil RunRehashSweep must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil {
		t.Fatalf("the unavailable reason must be reported: %+v", rec.Result)
	}
}

// The names are one underscore apart and they do opposite things: rehash_check
// verifies ONE item and writes nothing, rehash_sweep migrates a whole size band
// and rewrites rows. A dispatch mix-up would be silent — the sweep seam would
// simply never fire, or a verify would kick off hours of I/O — so it is pinned.
func TestRehashCheckAndRehashSweepAreDifferentKinds(t *testing.T) {
	if KindRehashCheck == KindRehashSweep {
		t.Fatal("the two rehash kinds must be distinct wire values")
	}
	m := newMockCentral()
	m.queued = []commandOut{{
		ID: "c-verify", Kind: KindRehashCheck,
		Payload: map[string]any{"item_id": "nope"},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	sweepRan := false
	p := NewPoller(Config{
		BaseURL: srv.URL, AgentID: "agent-1", HTTP: srv.Client(),
		// No Executor is wired, so the verify path fails — which is fine and is
		// exactly what proves the routing: the ONE thing that must not happen is
		// the sweep seam firing for an item-scoped verify.
		RunRehashSweep: func(context.Context, map[string]any) (map[string]any, error) {
			sweepRan = true
			return nil, nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if sweepRan {
		t.Fatal("a rehash_check must NEVER reach the rehash_sweep seam")
	}
}
