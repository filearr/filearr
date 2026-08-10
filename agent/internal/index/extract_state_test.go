package index

// Re-extraction cursor accessors (agent parity phase 3).

import (
	"context"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/outbox"
)

// insertExtractItem inserts one item with an explicit status/sidecar flag — the
// two dimensions ExtractCandidates filters on.
func insertExtractItem(t *testing.T, st *Store, rootID, rel, status string, sidecar bool) *Item {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := NewID()
	it := &Item{
		ID: id, RootID: rootID, RelPath: rel, Filename: rel,
		Size: 42, MtimeNs: 1234, QuickHash: "q-" + rel, ContentHash: "c-" + rel,
		FileCategory: "document", Status: status, IsSidecar: sidecar,
		FirstSeen: time.Now(), LastSeen: time.Now(),
	}
	if err := InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return it
}

func TestExtractStateDefaultsOnFreshStore(t *testing.T) {
	st, _ := openTemp(t)
	got, err := st.ExtractState(context.Background())
	if err != nil {
		t.Fatalf("ExtractState: %v", err)
	}
	// A never-swept store must read back the zero value: empty fp (so the first
	// command invalidates rather than short-circuits) and a zero cursor.
	if got.FP != "" || got.CursorRowID != 0 || got.StartedAt != "" || got.FinishedAt != "" {
		t.Fatalf("fresh store must have an empty state, got %+v", got)
	}
	if got.Seen != 0 || got.Extracted != 0 || got.Skipped != 0 || got.Failed != 0 {
		t.Fatalf("fresh store counters must be zero, got %+v", got)
	}
}

func TestExtractStateRoundTrip(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	want := ExtractState{
		FP: "e1-abc123", CursorRowID: 900,
		StartedAt: "2026-08-09T10:00:00Z", FinishedAt: "2026-08-09T11:00:00Z",
		Seen: 10, Extracted: 4, Skipped: 6, Failed: 0,
	}
	if err := st.SaveExtractState(ctx, want); err != nil {
		t.Fatalf("SaveExtractState: %v", err)
	}
	got, err := st.ExtractState(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if got != want {
		t.Fatalf("round-trip mismatch:\n got %+v\nwant %+v", got, want)
	}

	// The write is an upsert on the singleton, so a second save replaces rather
	// than duplicates — and an empty FinishedAt goes back to NULL/"" (the "sweep
	// restarted, no longer complete" transition).
	want.FinishedAt = ""
	want.CursorRowID = 0
	if err := st.SaveExtractState(ctx, want); err != nil {
		t.Fatal(err)
	}
	got, _ = st.ExtractState(ctx)
	if got != want {
		t.Fatalf("re-save mismatch:\n got %+v\nwant %+v", got, want)
	}
	var rows int
	if err := st.DB().QueryRow(`SELECT COUNT(*) FROM extract_state`).Scan(&rows); err != nil {
		t.Fatal(err)
	}
	if rows != 1 {
		t.Fatalf("extract_state must stay a singleton, got %d rows", rows)
	}
}

func TestSaveExtractStateTxCommitsWithItsEvents(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()

	// Commit path: the cursor and the event it covers land together.
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := outbox.Write(ctx, tx, outbox.Event{
		ItemID: "i1", Op: outbox.OpModified, LibraryRef: "/media", RelPath: "a.pdf",
	}); err != nil {
		t.Fatal(err)
	}
	if err := SaveExtractStateTx(ctx, tx, ExtractState{FP: "fp1", CursorRowID: 7}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	got, _ := st.ExtractState(ctx)
	if got.CursorRowID != 7 || got.FP != "fp1" {
		t.Fatalf("committed cursor not visible: %+v", got)
	}
	if n := outboxRows(t, st); n != 1 {
		t.Fatalf("expected the committed event, got %d rows", n)
	}

	// Rollback path: neither the further-advanced cursor nor its event survives —
	// the atomicity the resumable sweep rests on.
	tx, err = st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if _, err := outbox.Write(ctx, tx, outbox.Event{
		ItemID: "i2", Op: outbox.OpModified, LibraryRef: "/media", RelPath: "b.pdf",
	}); err != nil {
		t.Fatal(err)
	}
	if err := SaveExtractStateTx(ctx, tx, ExtractState{FP: "fp1", CursorRowID: 99}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	got, _ = st.ExtractState(ctx)
	if got.CursorRowID != 7 {
		t.Fatalf("rolled-back cursor must not advance, got %+v", got)
	}
	if n := outboxRows(t, st); n != 1 {
		t.Fatalf("rolled-back event must not persist, got %d rows", n)
	}
}

func outboxRows(t *testing.T, st *Store) int {
	t.Helper()
	var n int
	if err := st.DB().QueryRow(`SELECT COUNT(*) FROM outbox`).Scan(&n); err != nil {
		t.Fatal(err)
	}
	return n
}

func TestExtractCandidatesFiltersOrderAndCursor(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	media := rootID(t, st, "/media")
	other := rootID(t, st, "/other")

	// Insertion order IS rowid order, which is the sweep's total order.
	a := insertExtractItem(t, st, media, "a.pdf", StatusActive, false)
	insertExtractItem(t, st, media, "gone.pdf", StatusMissing, false)
	insertExtractItem(t, st, media, "trash.pdf", StatusTrashed, false)
	insertExtractItem(t, st, media, "a.nfo", StatusActive, true)
	b := insertExtractItem(t, st, media, "b.pdf", StatusActive, false)
	c := insertExtractItem(t, st, other, "c.pdf", StatusActive, false)

	all, err := st.ExtractCandidates(ctx, 0, 100)
	if err != nil {
		t.Fatalf("ExtractCandidates: %v", err)
	}
	var gotIDs []string
	for _, cand := range all {
		gotIDs = append(gotIDs, cand.ID)
	}
	want := []string{a.ID, b.ID, c.ID}
	if len(gotIDs) != len(want) {
		t.Fatalf("tombstoned/trashed/sidecar rows must be excluded, got %d: %+v", len(gotIDs), all)
	}
	for i := range want {
		if gotIDs[i] != want[i] {
			t.Fatalf("candidates must be ordered by rowid:\n got %v\nwant %v", gotIDs, want)
		}
	}
	// Rowids must be strictly increasing — the property the cursor relies on.
	for i := 1; i < len(all); i++ {
		if all[i].RowID <= all[i-1].RowID {
			t.Fatalf("rowids not increasing: %+v", all)
		}
	}

	// The root join supplies the absolute path (and the event's library_ref).
	if all[0].RootPath != "/media" || all[2].RootPath != "/other" {
		t.Fatalf("root path join wrong: %+v", all)
	}
	// Identity travels with the candidate so the sweep can re-emit it verbatim.
	if all[0].RelPath != "a.pdf" || all[0].Size != 42 || all[0].MtimeNs != 1234 ||
		all[0].QuickHash != "q-a.pdf" || all[0].ContentHash != "c-a.pdf" ||
		all[0].FileCategory != "document" {
		t.Fatalf("candidate identity wrong: %+v", all[0])
	}

	// after = the cursor: strictly greater, so a resumed sweep never revisits.
	rest, err := st.ExtractCandidates(ctx, all[0].RowID, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(rest) != 2 || rest[0].ID != b.ID {
		t.Fatalf("cursor must exclude everything at or below it, got %+v", rest)
	}

	// limit bounds the read.
	one, err := st.ExtractCandidates(ctx, 0, 1)
	if err != nil {
		t.Fatal(err)
	}
	if len(one) != 1 || one[0].ID != a.ID {
		t.Fatalf("limit not honoured, got %+v", one)
	}
	// A non-positive limit reads nothing rather than everything.
	if none, err := st.ExtractCandidates(ctx, 0, 0); err != nil || len(none) != 0 {
		t.Fatalf("limit<=0 must read nothing, got %d (%v)", len(none), err)
	}
}
