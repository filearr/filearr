package main

import (
	"context"
	"strings"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

// seedCLIIndex writes a one-item local index (Movies/Film.mkv, kind video) under
// path for the query CLI integration tests.
func seedCLIIndex(t *testing.T, path string) {
	t.Helper()
	st, err := index.Open(path)
	if err != nil {
		t.Fatal(err)
	}
	defer st.Close()
	ctx := context.Background()
	tx, _ := st.Begin(ctx)
	rid, err := index.EnsureRoot(ctx, tx, "/media")
	if err != nil {
		t.Fatal(err)
	}
	tx.Commit()
	now := time.Now()
	tx, _ = st.Begin(ctx)
	id, _ := index.NewID()
	it := &index.Item{ID: id, RootID: rid, RelPath: "Movies/Film.mkv", Filename: "Film.mkv",
		Extension: "mkv", Size: 10, MtimeNs: now.UnixNano(), FileCategory: "video",
		Status: index.StatusActive, FirstSeen: now, LastSeen: now}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	tx.Commit()
}

func nonEmptyLines(s string) []string {
	var out []string
	for _, l := range strings.Split(s, "\n") {
		if strings.TrimSpace(l) != "" {
			out = append(out, l)
		}
	}
	return out
}
