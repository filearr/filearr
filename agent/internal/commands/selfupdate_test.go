package commands

import (
	"context"
	"errors"
	"net/http/httptest"
	"testing"
)

func selfUpdatePoller(srv *httptest.Server, trigger func(context.Context, func(string)) (string, bool, error)) *Poller {
	return NewPoller(Config{
		BaseURL:       srv.URL,
		AgentID:       "agent-1",
		AuthFn:        func() string { return "fp" },
		HTTP:          srv.Client(),
		TriggerUpdate: trigger,
	})
}

func queueSelfUpdate(m *mockCentral, id string) {
	m.mu.Lock()
	m.queued = append(m.queued, commandOut{ID: id, Kind: KindSelfUpdate})
	m.mu.Unlock()
}

func TestSelfUpdateAppliesAndCompletesBeforeSwap(t *testing.T) {
	m := newMockCentral()
	srv := httptest.NewServer(m.handler())
	defer srv.Close()

	var completedWhenApplyRan bool
	p := selfUpdatePoller(srv, func(_ context.Context, beforeApply func(string)) (string, bool, error) {
		beforeApply("2.0.0")
		// the handler must have posted the terminal result already — a real
		// apply exits the process right after this returns
		_, completedWhenApplyRan = m.completeFor("cmd-up")
		return "2.0.0", true, nil
	})
	queueSelfUpdate(m, "cmd-up")
	if _, err := p.PollOnce(context.Background()); err != nil {
		t.Fatal(err)
	}
	rec, ok := m.completeFor("cmd-up")
	if !ok || !rec.OK {
		t.Fatalf("expected ok completion, got %+v (ok=%v)", rec, ok)
	}
	if rec.Result["status"] != "applying" || rec.Result["version"] != "2.0.0" {
		t.Fatalf("wrong result: %+v", rec.Result)
	}
	if !completedWhenApplyRan {
		t.Fatal("completion was not posted before the apply phase")
	}
}

func TestSelfUpdateUpToDate(t *testing.T) {
	m := newMockCentral()
	srv := httptest.NewServer(m.handler())
	defer srv.Close()
	p := selfUpdatePoller(srv, func(context.Context, func(string)) (string, bool, error) {
		return "", false, nil
	})
	queueSelfUpdate(m, "cmd-utd")
	_, _ = p.PollOnce(context.Background())
	rec, ok := m.completeFor("cmd-utd")
	if !ok || rec.OK {
		t.Fatalf("expected ok=false up-to-date completion: %+v", rec)
	}
	if rec.Result["status"] != "up-to-date" {
		t.Fatalf("wrong result: %+v", rec.Result)
	}
}

func TestSelfUpdateCheckErrorCompletesFailed(t *testing.T) {
	m := newMockCentral()
	srv := httptest.NewServer(m.handler())
	defer srv.Close()
	p := selfUpdatePoller(srv, func(context.Context, func(string)) (string, bool, error) {
		return "", false, errors.New("manifest fetch: central returned 503")
	})
	queueSelfUpdate(m, "cmd-err")
	_, _ = p.PollOnce(context.Background())
	rec, ok := m.completeFor("cmd-err")
	if !ok || rec.OK {
		t.Fatalf("expected failed completion: %+v", rec)
	}
	if rec.Result["error"] == "" {
		t.Fatalf("expected error detail: %+v", rec.Result)
	}
}

func TestSelfUpdateUnavailableWithoutTrigger(t *testing.T) {
	m := newMockCentral()
	srv := httptest.NewServer(m.handler())
	defer srv.Close()
	p := selfUpdatePoller(srv, nil)
	queueSelfUpdate(m, "cmd-na")
	_, _ = p.PollOnce(context.Background())
	rec, ok := m.completeFor("cmd-na")
	if !ok || rec.OK {
		t.Fatalf("expected ok=false completion: %+v", rec)
	}
}
