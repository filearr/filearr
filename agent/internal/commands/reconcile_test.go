package commands

// The `reconcile` command handler (2026-08-22). Structurally the rehash_sweep
// handler tests: payload passthrough, ok+counters on success, ok=false with
// partial counters on failure, clean degrade when the daemon seam is absent.

import (
	"context"
	"errors"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestReconcilePassesPayloadAndReportsCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{
		ID:      "c-reconcile",
		Kind:    KindReconcile,
		Payload: map[string]any{"force_reset": true},
	}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var got atomic.Value
	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunReconcile: func(_ context.Context, payload map[string]any) (map[string]any, error) {
			got.Store(payload)
			return map[string]any{
				"roots": 2, "matched": 1, "reconciled": 1, "rows": 4200, "reset": true,
			}, nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	payload, _ := got.Load().(map[string]any)
	if payload["force_reset"] != true {
		t.Fatalf("payload not passed through verbatim: %+v", payload)
	}
	rec, ok := m.completeFor("c-reconcile")
	if !ok || !rec.OK {
		t.Fatalf("reconcile must complete ok, got %+v (found=%v)", rec, ok)
	}
	if rec.Result["reconciled"] != float64(1) || rec.Result["rows"] != float64(4200) {
		t.Fatalf("counters not propagated: %+v", rec.Result)
	}
}

func TestReconcileFailureKeepsPartialCounters(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-reconcile-fail", Kind: KindReconcile}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunReconcile: func(context.Context, map[string]any) (map[string]any, error) {
			// One root reconciled before the second failed: durable work, report it.
			return map[string]any{"roots": 2, "reconciled": 1, "rows": 900},
				errors.New("root /mnt/b: session expired twice")
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-reconcile-fail")
	if !ok || rec.OK {
		t.Fatalf("a failed sweep must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil || rec.Result["reconciled"] != float64(1) {
		t.Fatalf("partial progress + error must both be reported: %+v", rec.Result)
	}
}

func TestReconcileMissingSeamDegrades(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-rec-noseam", Kind: KindReconcile}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{BaseURL: srv.URL, AgentID: "agent-1", HTTP: srv.Client()})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-rec-noseam")
	if !ok || rec.OK {
		t.Fatalf("nil RunReconcile must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil {
		t.Fatalf("degrade must carry an explanatory error: %+v", rec.Result)
	}
}
