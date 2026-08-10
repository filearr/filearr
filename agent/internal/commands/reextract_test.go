package commands

// The `reextract` command handler (agent parity phase 3).

import (
	"context"
	"errors"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestReextractCommandPassesPayloadAndReportsCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{
		ID:      "c-reex",
		Kind:    KindReextract,
		Payload: map[string]any{"force": true, "max_items": float64(500)},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var got atomic.Value // map[string]any
	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunReextract: func(_ context.Context, payload map[string]any) (map[string]any, error) {
			got.Store(payload)
			return map[string]any{"seen": 500, "extracted": 120, "completed": false}, nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}

	// The payload reaches the seam verbatim — the sweep owns its own vocabulary.
	payload, _ := got.Load().(map[string]any)
	if payload["force"] != true || payload["max_items"] != float64(500) {
		t.Fatalf("payload not passed through verbatim: %+v", payload)
	}
	rec, ok := m.completeFor("c-reex")
	if !ok || !rec.OK {
		t.Fatalf("reextract must complete ok, got %+v (found=%v)", rec, ok)
	}
	if rec.Result["seen"] != float64(500) || rec.Result["extracted"] != float64(120) {
		t.Fatalf("counters not propagated: %+v", rec.Result)
	}
}

func TestReextractCommandFailureKeepsPartialCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-reex-fail", Kind: KindReextract}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunReextract: func(context.Context, map[string]any) (map[string]any, error) {
			// A sweep that emitted real, durable events before dying still has
			// progress worth reporting (processAgentMaintenance's posture).
			return map[string]any{"seen": 40_000, "cursor": 88_123}, errors.New("disk went away")
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-reex-fail")
	if !ok || rec.OK {
		t.Fatalf("failed reextract must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil || rec.Result["seen"] != float64(40_000) || rec.Result["cursor"] != float64(88_123) {
		t.Fatalf("partial progress + error must both be reported: %+v", rec.Result)
	}
}

func TestReextractCommandMissingSeamDegrades(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-noseam", Kind: KindReextract}}
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
		t.Fatalf("nil RunReextract must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil {
		t.Fatalf("the unavailable reason must be reported: %+v", rec.Result)
	}
}
