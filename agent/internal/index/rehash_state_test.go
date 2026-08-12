package index

// Quick_hash migration cursor accessors (QH-T6).

import (
	"context"
	"database/sql"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/outbox"
)

// insertSizedItem inserts one item with an explicit size/status/sidecar flag —
// the three dimensions RehashCandidates filters on.
func insertSizedItem(t *testing.T, st *Store, rootID, rel string, size int64, status string, sidecar bool) *Item {
	t.Helper()
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := NewID()
	it := &Item{
		ID: id, RootID: rootID, RelPath: rel, Filename: rel, Extension: "jpg",
		Size: size, MtimeNs: 1234, QuickHash: "q-" + rel, ContentHash: "c-" + rel,
		FileCategory: "image", FileGroup: "photo", Status: status, IsSidecar: sidecar,
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

func TestRehashStateDefaultsOnFreshStore(t *testing.T) {
	st, _ := openTemp(t)
	got, err := st.RehashState(context.Background())
	if err != nil {
		t.Fatalf("RehashState: %v", err)
	}
	// A never-swept store must read back the zero value: an empty fp (so the
	// first command invalidates rather than short-circuits) and a zero cursor.
	if got.FP != "" || got.CursorRowID != 0 || got.StartedAt != "" || got.FinishedAt != "" {
		t.Fatalf("fresh store must have an empty state, got %+v", got)
	}
	if got.Seen != 0 || got.Changed != 0 || got.Verified != 0 || got.Skipped != 0 || got.Failed != 0 {
		t.Fatalf("fresh store counters must be zero, got %+v", got)
	}
	if got.MinSize != 0 || got.MaxSize != 0 {
		t.Fatalf("fresh store must record no band, got %+v", got)
	}
}

func TestRehashStateRoundTrip(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	want := RehashState{
		FP: "h2-65537-131072", CursorRowID: 900,
		StartedAt: "2026-08-12T10:00:00Z", FinishedAt: "2026-08-12T11:00:00Z",
		Seen: 10, Changed: 7, Verified: 2, Skipped: 1, Failed: 0,
		MinSize: 65537, MaxSize: 131072,
	}
	if err := st.SaveRehashState(ctx, want); err != nil {
		t.Fatalf("SaveRehashState: %v", err)
	}
	got, err := st.RehashState(ctx)
	if err != nil {
		t.Fatalf("RehashState: %v", err)
	}
	if got != want {
		t.Fatalf("round trip mismatch:\n got %+v\nwant %+v", got, want)
	}

	// "" means SQL NULL for the two timestamps, and it must come BACK as "" —
	// the resume path keys entirely on FinishedAt == "".
	want.FinishedAt = ""
	if err := st.SaveRehashState(ctx, want); err != nil {
		t.Fatal(err)
	}
	if got, _ = st.RehashState(ctx); got.FinishedAt != "" {
		t.Fatalf("an unfinished sweep must read back as unfinished, got %q", got.FinishedAt)
	}
}

func TestSaveRehashStateTxCommitsWithItsRowsAndEvents(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	media := rootID(t, st, "/media")
	it := insertSizedItem(t, st, media, "a.jpg", 70000, StatusActive, false)

	// Commit path: the corrected row, the event it produced and the cursor that
	// covers both land together. This is stricter than the re-extract sweep's
	// equivalent, which only ever wrote events — a cursor that advanced past a
	// rolled-back UpdateItem would leave the stale hash in place with nothing
	// left to revisit it.
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	it.QuickHash = "corrected"
	if err := UpdateItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if _, err := outbox.Write(ctx, tx, outbox.Event{
		ItemID: it.ID, Op: outbox.OpModified, LibraryRef: "/media", RelPath: "a.jpg",
		QuickHash: "corrected",
	}); err != nil {
		t.Fatal(err)
	}
	if err := SaveRehashStateTx(ctx, tx, RehashState{FP: "h2-65537-131072", CursorRowID: 7}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	got, _ := st.RehashState(ctx)
	if got.CursorRowID != 7 || got.FP != "h2-65537-131072" {
		t.Fatalf("committed cursor not visible: %+v", got)
	}
	items, _ := st.LoadItems(ctx, media)
	if items["a.jpg"].QuickHash != "corrected" {
		t.Fatalf("committed row not visible: %+v", items["a.jpg"])
	}

	// Rollback path: none of the three survives.
	tx, err = st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	it.QuickHash = "rolled-back"
	if err := UpdateItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if _, err := outbox.Write(ctx, tx, outbox.Event{
		ItemID: it.ID, Op: outbox.OpModified, LibraryRef: "/media", RelPath: "a.jpg",
	}); err != nil {
		t.Fatal(err)
	}
	if err := SaveRehashStateTx(ctx, tx, RehashState{FP: "h2-65537-131072", CursorRowID: 99}); err != nil {
		t.Fatal(err)
	}
	if err := tx.Rollback(); err != nil {
		t.Fatal(err)
	}
	if got, _ = st.RehashState(ctx); got.CursorRowID != 7 {
		t.Fatalf("a rolled-back cursor must not advance, got %+v", got)
	}
	items, _ = st.LoadItems(ctx, media)
	if items["a.jpg"].QuickHash != "corrected" {
		t.Fatalf("a rolled-back row rewrite must not persist: %+v", items["a.jpg"])
	}
	if n := outboxRows(t, st); n != 1 {
		t.Fatalf("a rolled-back event must not persist, got %d rows", n)
	}
}

func TestRehashCandidatesFilterOnBandStatusAndSidecar(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	media := rootID(t, st, "/media")

	// Insertion order IS rowid order, which is the sweep's total order.
	below := insertSizedItem(t, st, media, "below.jpg", 65536, StatusActive, false)
	low := insertSizedItem(t, st, media, "lowedge.jpg", 65537, StatusActive, false)
	insertSizedItem(t, st, media, "gone.jpg", 70000, StatusMissing, false)
	insertSizedItem(t, st, media, "trash.jpg", 70000, StatusTrashed, false)
	insertSizedItem(t, st, media, "side.nfo", 70000, StatusActive, true)
	high := insertSizedItem(t, st, media, "highedge.jpg", 131072, StatusActive, false)
	above := insertSizedItem(t, st, media, "above.jpg", 131073, StatusActive, false)

	got, err := st.RehashCandidates(ctx, 0, 65537, 131072, 100)
	if err != nil {
		t.Fatalf("RehashCandidates: %v", err)
	}
	var ids []string
	for _, c := range got {
		ids = append(ids, c.Item.ID)
	}
	if len(ids) != 2 || ids[0] != low.ID || ids[1] != high.ID {
		t.Fatalf("expected exactly the two band edges in rowid order, got %v", ids)
	}
	_ = below
	_ = above

	// The whole Item is materialised, not a flat identity copy: the sweep hands
	// it straight to UpdateItem, which rewrites every mutable column.
	c := got[0]
	if c.RootPath != "/media" || c.Item.RelPath != "lowedge.jpg" ||
		c.Item.FileCategory != "image" || c.Item.FileGroup != "photo" ||
		c.Item.Extension != "jpg" || c.Item.QuickHash != "q-lowedge.jpg" {
		t.Fatalf("candidate not fully materialised: %+v", c.Item)
	}
	if c.RowID == 0 {
		t.Fatalf("the rowid cursor key must be populated: %+v", c)
	}

	// The cursor excludes everything at or below it.
	after, err := st.RehashCandidates(ctx, c.RowID, 65537, 131072, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(after) != 1 || after[0].Item.ID != high.ID {
		t.Fatalf("the cursor must exclude already-visited rows, got %+v", after)
	}

	// A widened band reaches the rows the default deliberately excludes.
	wide, err := st.RehashCandidates(ctx, 0, 1, 1<<30, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(wide) != 4 { // below + low + high + above; tombstones/sidecars still out
		t.Fatalf("a widened band must reach 4 active non-sidecar rows, got %d", len(wide))
	}

	// A zero limit reads nothing rather than the whole table.
	if none, err := st.RehashCandidates(ctx, 0, 1, 1<<30, 0); err != nil || none != nil {
		t.Fatalf("limit<=0 must return nothing, got %v / %v", none, err)
	}
}

// A NULL size must not be silently treated as 0 and swept into a band starting
// at 1: the row was never sized, so nothing can be said about which band it is
// in. (The scan's null-hash self-heal owns that case.)
func TestRehashCandidatesIgnoreNullSizes(t *testing.T) {
	st, _ := openTemp(t)
	ctx := context.Background()
	media := rootID(t, st, "/media")
	insertSizedItem(t, st, media, "sized.jpg", 70000, StatusActive, false)
	if _, err := st.DB().Exec(`UPDATE items SET size = NULL WHERE rel_path = ?`, "sized.jpg"); err != nil {
		t.Fatal(err)
	}
	got, err := st.RehashCandidates(ctx, 0, 1, 1<<30, 100)
	if err != nil {
		t.Fatal(err)
	}
	if len(got) != 0 {
		t.Fatalf("a NULL-size row is not a band candidate, got %+v", got)
	}
}

// THE upgrade guarantee. rehash_state was added WITHOUT bumping schemaVersion,
// precisely so an agent that upgrades in place keeps its index: a version bump
// makes integrity.go delete the store and rebuild it from a fresh walk, which on
// the live ~1.09M-item agent would mean re-walking everything and re-emitting
// every row to central. This test builds a store that predates the table, drops
// it to simulate that, and asserts the reopen adds it back with every item
// intact.
func TestRehashStateIsAddedInPlaceWithoutLosingTheIndex(t *testing.T) {
	path := filepath.Join(t.TempDir(), "index.db")
	ctx := context.Background()

	st, err := Open(path)
	if err != nil {
		t.Fatal(err)
	}
	media := rootID(t, st, "/media")
	it := insertSizedItem(t, st, media, "keep.jpg", 70000, StatusActive, false)
	// Roll the store back to the pre-QH-T6 shape.
	if _, err := st.DB().Exec(`DROP TABLE rehash_state`); err != nil {
		t.Fatal(err)
	}
	var version int
	if err := st.DB().QueryRow(`PRAGMA user_version`).Scan(&version); err != nil {
		t.Fatal(err)
	}
	if err := st.Close(); err != nil {
		t.Fatal(err)
	}

	// The new binary opens the old store.
	st2, err := Open(path)
	if err != nil {
		t.Fatalf("reopen: %v", err)
	}
	defer st2.Close()
	if st2.Rebuilt {
		t.Fatal("adding a local cursor table must NOT trigger an index rebuild")
	}
	var after int
	if err := st2.DB().QueryRow(`PRAGMA user_version`).Scan(&after); err != nil {
		t.Fatal(err)
	}
	if after != version {
		t.Fatalf("the schema version must be unchanged by this table (%d -> %d)", version, after)
	}
	items, err := st2.LoadItems(ctx, media)
	if err != nil {
		t.Fatal(err)
	}
	if got := items["keep.jpg"]; got == nil || got.ID != it.ID {
		t.Fatalf("the existing index must survive the upgrade, got %+v", items)
	}
	// And the table is there, seeded, and usable.
	state, err := st2.RehashState(ctx)
	if err != nil {
		t.Fatalf("rehash_state must exist after the in-place upgrade: %v", err)
	}
	if state.FP != "" {
		t.Fatalf("the fresh cursor must be empty, got %+v", state)
	}
	var seeded int
	if err := st2.DB().QueryRow(`SELECT COUNT(*) FROM rehash_state WHERE id = 1`).Scan(&seeded); err != nil {
		t.Fatal(err)
	}
	if seeded != 1 {
		t.Fatalf("the singleton row must be seeded, got %d rows", seeded)
	}
	// The singleton is a SCHEMA guarantee, not a convention.
	if _, err := st2.DB().Exec(`INSERT INTO rehash_state(id) VALUES(2)`); err == nil {
		t.Fatal("the id CHECK must reject a second row")
	} else if err == sql.ErrNoRows {
		t.Fatal("unexpected error kind")
	}
}
