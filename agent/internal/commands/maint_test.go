package commands

// suspend / agent_maintenance command handlers + the X-Filearr-Maintenance
// poll-header advertisement (2026-08-09).

import (
	"context"
	"errors"
	"net/http/httptest"
	"sync/atomic"
	"testing"
)

func TestMaintenanceHeaderDrivesCallbackBothWays(t *testing.T) {
	m := newMockCentral()
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var last atomic.Value // bool
	p := NewPoller(Config{
		BaseURL:       srv.URL,
		AgentID:       "agent-1",
		HTTP:          srv.Client(),
		OnMaintenance: func(active bool) { last.Store(active) },
	})

	m.maint = true
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if v, _ := last.Load().(bool); !v {
		t.Fatal("header present: callback must observe active=true")
	}

	// Level-triggered: the next poll WITHOUT the header must report false.
	m.mu.Lock()
	m.maint = false
	m.mu.Unlock()
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if v, _ := last.Load().(bool); v {
		t.Fatal("header absent: callback must observe active=false")
	}
}

func TestSuspendCommandAppliesAndReports(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{
		{ID: "c-susp", Kind: KindSuspend, Payload: map[string]any{"suspended": true}},
	}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var applied atomic.Value
	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		SetSuspended: func(_ context.Context, s bool) error {
			applied.Store(s)
			return nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if v, _ := applied.Load().(bool); !v {
		t.Fatal("SetSuspended(true) was not applied")
	}
	rec, ok := m.completeFor("c-susp")
	if !ok || !rec.OK || rec.Result["suspended"] != true {
		t.Fatalf("complete: want ok with suspended=true, got %+v (found=%v)", rec, ok)
	}
}

func TestSuspendCommandBadPayloadAndMissingSeam(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{
		{ID: "c-bad", Kind: KindSuspend, Payload: map[string]any{}},
	}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL:      srv.URL,
		AgentID:      "agent-1",
		HTTP:         srv.Client(),
		SetSuspended: func(context.Context, bool) error { return nil },
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if rec, ok := m.completeFor("c-bad"); !ok || rec.OK {
		t.Fatalf("missing payload.suspended must complete ok=false, got %+v", rec)
	}

	// Nil seam (build without daemon wiring) degrades to ok=false, not a hang.
	m.queued = []commandOut{
		{ID: "c-noseam", Kind: KindSuspend, Payload: map[string]any{"suspended": true}},
	}
	p2 := NewPoller(Config{BaseURL: srv.URL, AgentID: "agent-1", HTTP: srv.Client()})
	if _, err := p2.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	if rec, ok := m.completeFor("c-noseam"); !ok || rec.OK {
		t.Fatalf("nil SetSuspended must complete ok=false, got %+v", rec)
	}
}

func TestAgentMaintenanceCommandReportsResult(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-maint", Kind: KindAgentMaintenance}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunMaintenance: func(context.Context) (map[string]any, error) {
			return map[string]any{"outbox_pruned_rows": 42}, nil
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-maint")
	if !ok || !rec.OK {
		t.Fatalf("maintenance must complete ok, got %+v (found=%v)", rec, ok)
	}
	if rec.Result["outbox_pruned_rows"] != float64(42) { // JSON round-trip
		t.Fatalf("result not propagated: %+v", rec.Result)
	}
}

func TestAgentMaintenanceCommandFailureKeepsPartialResult(t *testing.T) {
	m := newMockCentral()
	m.queued = []commandOut{{ID: "c-fail", Kind: KindAgentMaintenance}}
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	p := NewPoller(Config{
		BaseURL: srv.URL,
		AgentID: "agent-1",
		HTTP:    srv.Client(),
		RunMaintenance: func(context.Context) (map[string]any, error) {
			return map[string]any{"temp_files_removed": 3}, errors.New("vacuum blew up")
		},
	})
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatalf("PollOnce: %v", err)
	}
	rec, ok := m.completeFor("c-fail")
	if !ok || rec.OK {
		t.Fatalf("failed maintenance must complete ok=false, got %+v", rec)
	}
	if rec.Result["error"] == nil || rec.Result["temp_files_removed"] != float64(3) {
		t.Fatalf("partial progress + error must both be reported: %+v", rec.Result)
	}
}
