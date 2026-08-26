package main

import (
	"errors"
	"testing"
	"time"
)

func TestSyncTrackerStreaksAndSnapshot(t *testing.T) {
	now := time.Date(2026, 8, 25, 12, 0, 0, 0, time.UTC)
	tr := &syncTracker{channels: map[string]*syncChannel{}, now: func() time.Time { return now }}
	tr.report("replication", nil, 0)
	snap := tr.snapshot()["replication"].(map[string]any)
	if snap["ok"] != true || snap["failures"] != 0 {
		t.Fatalf("healthy channel: %+v", snap)
	}
	now = now.Add(time.Minute)
	tr.report("replication", errors.New("Post \"https://c/x\": dial tcp: connection refused"), 30*time.Second)
	now = now.Add(10 * time.Second)
	snap = tr.snapshot()["replication"].(map[string]any)
	if snap["ok"] != false || snap["failures"] != 1 || snap["class"] != "unreachable" {
		t.Fatalf("failing channel: %+v", snap)
	}
	if snap["retry_in_s"] != 20 || snap["since_success_s"] != 70 {
		t.Fatalf("timing: %+v", snap)
	}
	tr.report("replication", nil, 0)
	if s := tr.snapshot()["replication"].(map[string]any); s["failures"] != 0 || s["ok"] != true {
		t.Fatalf("success must reset the streak: %+v", s)
	}
}

func TestClassifySyncError(t *testing.T) {
	cases := map[string]string{
		"commands poll: 503 — central: maintenance":                 "maintenance",
		"replication-batch: 503 Service Unavailable":                 "overloaded",
		"commands poll: central returned 429 Too Many Requests":      "overloaded",
		"commands poll: central rejected the agent bearer token (401)": "auth",
		"Post \"https://c\": context deadline exceeded (Client.Timeout exceeded while awaiting headers)": "timeout",
		"dial tcp 10.0.0.5:443: connect: connection refused":         "unreachable",
		"lookup agents.example.com: no such host":                     "unreachable",
		"something odd":                                               "error",
	}
	for msg, want := range cases {
		if got := classifySyncError(errors.New(msg)); got != want {
			t.Errorf("%q: got %q want %q", msg, got, want)
		}
	}
	if classifySyncError(nil) != "" {
		t.Fatal("nil error must classify as empty")
	}
}
