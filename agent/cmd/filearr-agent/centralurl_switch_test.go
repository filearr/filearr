package main

import (
	"log/slog"
	"os"
	"path/filepath"
	"strings"
	"testing"

	"github.com/filearr/filearr/agent/internal/enroll"
)

// The mTLS-migration seam (live 2026-08-08): every daemon loop runs off
// state.json's enrollment-time central_url, so the resolved config
// (flag > env > sidecar) must be adopted INTO state at startup or repointing
// an enrolled agent at agents.<domain> silently does nothing.
func TestAdoptConfiguredCentralURL(t *testing.T) {
	log := slog.New(slog.NewTextHandler(os.Stderr, nil))
	newID := func() *enroll.Identity {
		return &enroll.Identity{State: enroll.State{
			AgentID:    "a-1",
			CentralURL: "https://filearr.example.com",
		}}
	}

	t.Run("differing config switches and persists", func(t *testing.T) {
		store := enroll.NewCertStore(t.TempDir())
		id := newID()
		cfg := &config{CentralURL: "https://agents.example.com/"}
		adoptConfiguredCentralURL(cfg, store, id, log)
		if id.State.CentralURL != "https://agents.example.com" {
			t.Fatalf("in-memory state not switched: %q", id.State.CentralURL)
		}
		b, err := os.ReadFile(filepath.Join(store.Dir, "state.json"))
		if err != nil {
			t.Fatalf("state.json not persisted: %v", err)
		}
		if !strings.Contains(string(b), "agents.example.com") {
			t.Fatalf("persisted state lacks the new URL: %s", b)
		}
	})

	t.Run("same URL (modulo trailing slash) is a no-op", func(t *testing.T) {
		store := enroll.NewCertStore(t.TempDir())
		id := newID()
		adoptConfiguredCentralURL(&config{CentralURL: "https://filearr.example.com/"}, store, id, log)
		if id.State.CentralURL != "https://filearr.example.com" && id.State.CentralURL != "https://filearr.example.com/" {
			t.Fatalf("state must be unchanged: %q", id.State.CentralURL)
		}
		if _, err := os.Stat(filepath.Join(store.Dir, "state.json")); err == nil {
			t.Fatalf("no-op must not write state.json")
		}
	})

	t.Run("empty config leaves enrollment URL", func(t *testing.T) {
		store := enroll.NewCertStore(t.TempDir())
		id := newID()
		adoptConfiguredCentralURL(&config{}, store, id, log)
		if id.State.CentralURL != "https://filearr.example.com" {
			t.Fatalf("state must be unchanged: %q", id.State.CentralURL)
		}
	})
}
