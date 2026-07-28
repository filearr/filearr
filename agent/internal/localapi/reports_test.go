package localapi

import (
	"context"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
)

func seedReportIndex(t *testing.T) *index.Store {
	t.Helper()
	st, err := index.Open(filepath.Join(t.TempDir(), "index.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })
	db := st.DB()
	mustExec := func(q string, args ...any) {
		t.Helper()
		if _, err := db.Exec(q, args...); err != nil {
			t.Fatalf("%s: %v", q, err)
		}
	}
	mustExec(`INSERT INTO roots (id, path, added_at) VALUES ('r1', '/data/media', '2026-01-01')`)
	ins := `INSERT INTO items (id, root_id, rel_path, filename, extension, size, mtime_ns,
		quick_hash, content_hash, file_category, status, first_seen, last_seen)
		VALUES (?, 'r1', ?, ?, ?, ?, ?, ?, ?, ?, 'active', '2026-01-01', '2026-01-01')`
	now := time.Date(2026, 7, 27, 0, 0, 0, 0, time.UTC).UnixNano()
	mustExec(ins, "i1", "a/big.mkv", "big.mkv", "mkv", 5000, now, "q1", "c1", "video")
	mustExec(ins, "i2", "b/copy1.mkv", "copy1.mkv", "mkv", 3000, now, "qd", "cd", "video")
	mustExec(ins, "i3", "c/copy2.mkv", "copy2.mkv", "mkv", 3000, now, "qd", "cd", "video")
	mustExec(ins, "i4", "d/odd.xyz", "odd.xyz", "xyz", 10, now, "q4", "c4", "")
	mustExec(ins, "i5", "e/tomorrow.txt", "tomorrow.txt", "txt", 20, now+int64(72*time.Hour), "q5", "c5", "document")
	return st
}

func TestReportsRegistryAndPages(t *testing.T) {
	st := seedReportIndex(t)
	now := func() time.Time { return time.Date(2026, 7, 27, 0, 0, 0, 0, time.UTC) }
	run := NewReportsFn(st.DB(), now)
	ctx := context.Background()

	if got := len(ReportSpecs()); got < 5 {
		t.Fatalf("registry has %d reports, want >=5", got)
	}

	page, err := run(ctx, "categories", 100, 0)
	if err != nil || page == nil {
		t.Fatalf("categories: %v %v", page, err)
	}
	if page.Total < 2 || len(page.Rows) < 2 {
		t.Fatalf("categories rows = %d total = %d, want >=2", len(page.Rows), page.Total)
	}

	page, err = run(ctx, "largest_files", 1, 0)
	if err != nil {
		t.Fatal(err)
	}
	if page.Total != 5 || len(page.Rows) != 1 {
		t.Fatalf("largest: total=%d rows=%d, want 5/1", page.Total, len(page.Rows))
	}
	if p, _ := page.Rows[0][0].(string); p != "/data/media/a/big.mkv" {
		t.Fatalf("largest path = %v", page.Rows[0][0])
	}

	page, err = run(ctx, "duplicate_files", 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	if page.Total != 1 || len(page.Rows) != 1 {
		t.Fatalf("duplicates: total=%d, want exactly the cd content-hash pair", page.Total)
	}

	page, err = run(ctx, "future_dated", 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	if page.Total != 1 {
		t.Fatalf("future_dated total=%d, want 1 (only the +72h item)", page.Total)
	}

	page, err = run(ctx, "unmapped_extensions", 100, 0)
	if err != nil {
		t.Fatal(err)
	}
	if page.Total != 1 {
		t.Fatalf("unmapped total=%d, want 1 (xyz)", page.Total)
	}

	if page, err = run(ctx, "nope", 10, 0); err != nil || page != nil {
		t.Fatalf("unknown id should yield nil page, nil err; got %v %v", page, err)
	}
}
