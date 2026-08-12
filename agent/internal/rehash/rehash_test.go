package rehash

// The quick_hash migration sweep (QH-T6). Every test drives a REAL temp
// index.Store over REAL files on disk, because the semantics under test are
// precisely about the agreement between an index row and the bytes it describes.
// The HASHER, though, is a stub in most tests: the real one is
// scan.HashFile, and importing it here would make assertions about "did the row
// change" depend on xxh3 digests of fixture bytes, which is a test of the hasher
// (hash_test.go already owns that, against Python-precomputed parity fixtures)
// rather than of the sweep. TestSweepWithTheRealHasher is the one exception,
// wired through the same seam the daemon uses, to prove the two fit.
//
// The fixture scaffold is lifted from reextract_test.go on purpose — the sweeps
// are siblings and a reader comparing them should be reading the same shapes.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"strings"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/scan"
)

// fixture is one temp store plus the filesystem root its items live under.
type fixture struct {
	st     *index.Store
	root   string
	rootID string
}

func newFixture(t *testing.T) *fixture {
	t.Helper()
	dir := t.TempDir()
	st, err := index.Open(filepath.Join(dir, "index.db"))
	if err != nil {
		t.Fatal(err)
	}
	t.Cleanup(func() { st.Close() })

	root := filepath.Join(dir, "media")
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	ctx := context.Background()
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	rootID, err := index.EnsureRoot(ctx, tx, root)
	if err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return &fixture{st: st, root: root, rootID: rootID}
}

// add writes a real file of exactly size bytes and indexes it with the identity
// that file actually has plus the STALE hashes ("old-*") this sweep exists to
// replace. Size is the axis every band assertion turns on, so it is the
// parameter rather than the body.
func (f *fixture) add(t *testing.T, rel string, size int64) *index.Item {
	t.Helper()
	return f.addWith(t, rel, size, "old-quick-"+rel, "old-content-"+rel, false)
}

func (f *fixture) addWith(t *testing.T, rel string, size int64, quick, content string, sidecar bool) *index.Item {
	t.Helper()
	abs := filepath.Join(f.root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatal(err)
	}
	// Deterministic, non-uniform bytes: a run of identical bytes would hash the
	// same at every length and quietly weaken the band assertions.
	body := make([]byte, size)
	for i := range body {
		body[i] = byte(i * 31 % 251)
	}
	if err := os.WriteFile(abs, body, 0o644); err != nil {
		t.Fatal(err)
	}
	info, err := os.Stat(abs)
	if err != nil {
		t.Fatal(err)
	}
	id, _ := index.NewID()
	it := &index.Item{
		ID: id, RootID: f.rootID, RelPath: rel, Filename: filepath.Base(rel),
		Size: info.Size(), MtimeNs: info.ModTime().UnixNano(),
		QuickHash: quick, ContentHash: content,
		FileCategory: "image", Status: index.StatusActive, IsSidecar: sidecar,
		FirstSeen: time.Now(), LastSeen: time.Now(),
	}
	ctx := context.Background()
	tx, err := f.st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if err := index.InsertItem(ctx, tx, it); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	return it
}

// events decodes every outbox row written so far, in wire order.
func (f *fixture) events(t *testing.T) []map[string]any {
	t.Helper()
	rows, err := outbox.New(f.st.DB()).Unsent(context.Background(), 1000)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]map[string]any, 0, len(rows))
	for _, r := range rows {
		var m map[string]any
		if err := json.Unmarshal([]byte(r.Payload), &m); err != nil {
			t.Fatal(err)
		}
		out = append(out, m)
	}
	return out
}

func (f *fixture) state(t *testing.T) index.RehashState {
	t.Helper()
	st, err := f.st.RehashState(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return st
}

// row reads one item back from the store by rel_path, so a test can assert what
// the sweep actually WROTE rather than what it reported writing.
func (f *fixture) row(t *testing.T, rel string) *index.Item {
	t.Helper()
	items, err := f.st.LoadItems(context.Background(), f.rootID)
	if err != nil {
		t.Fatal(err)
	}
	it := items[rel]
	if it == nil {
		t.Fatalf("no item at %q", rel)
	}
	return it
}

// stubHash returns a deterministic "corrected" pair for every file, standing in
// for the real hashers.
func stubHash(absPath string, size int64) (string, string) {
	return "new-quick-" + filepath.Base(absPath), "new-content-" + filepath.Base(absPath)
}

func baseOpts() Options {
	return Options{FP: Fingerprint(2, DefaultMinSize, DefaultMaxSize), Hash: stubHash}
}

// --- band targeting ---------------------------------------------------------

// The band's edges ARE the specification: 65536 was fully hashed by the old code
// and is already correct, 65537 is the first byte that could be lost, 131072 is
// the last (the tail branch fired only ABOVE it), and 131073 was always sampled
// head+tail and is unchanged by QH-T1.
func TestBandTargetingHonoursBothEdges(t *testing.T) {
	f := newFixture(t)
	f.add(t, "below.bin", 65536)
	f.add(t, "lowedge.bin", 65537)
	f.add(t, "highedge.bin", 131072)
	f.add(t, "above.bin", 131073)

	res, err := Run(context.Background(), f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Changed != 2 {
		t.Fatalf("only the two in-band files may be visited: %+v", res)
	}
	got := map[string]bool{}
	for _, ev := range f.events(t) {
		got[ev["rel_path"].(string)] = true
	}
	if !got["lowedge.bin"] || !got["highedge.bin"] {
		t.Fatalf("both band edges must be swept, got %v", got)
	}
	if got["below.bin"] {
		t.Fatal("a 65536-byte file was hashed IN FULL by the old code — sweeping it is waste")
	}
	if got["above.bin"] {
		t.Fatal("a 131073-byte file was sampled head+tail before AND after QH-T1 — sweeping it is waste")
	}
	// The rows outside the band must be untouched, hashes included.
	if f.row(t, "below.bin").QuickHash != "old-quick-below.bin" {
		t.Fatal("an out-of-band row was rewritten")
	}
}

func TestBandIsOverridablePerRun(t *testing.T) {
	f := newFixture(t)
	f.add(t, "small.bin", 4096)
	f.add(t, "inband.bin", 70000)

	// The opt-in wide backfill: everything up to the ceiling, including the files
	// the default deliberately excludes.
	opts := baseOpts()
	opts.MinSize, opts.MaxSize = 1, DefaultMaxSize
	opts.FP = Fingerprint(2, 1, DefaultMaxSize)
	res, err := Run(context.Background(), f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Changed != 2 {
		t.Fatalf("a widened band must reach the small file too: %+v", res)
	}
	if res.MinSize != 1 || res.MaxSize != DefaultMaxSize {
		t.Fatalf("the result must echo the band it swept: %+v", res)
	}
	if got := f.state(t); got.MinSize != 1 || got.MaxSize != DefaultMaxSize {
		t.Fatalf("the cursor must record its band: %+v", got)
	}
}

func TestInvalidBandRefusesRatherThanStampingDone(t *testing.T) {
	f := newFixture(t)
	f.add(t, "inband.bin", 70000)

	// An inverted band would select zero rows and then stamp the fingerprint
	// FINISHED — permanently short-circuiting the real sweep at that band until
	// someone thinks to force it. Refusing is the only safe answer, and the agent
	// does it independently of central's own 422 because a payload can also come
	// from an older or a hand-crafted caller.
	opts := baseOpts()
	opts.MinSize, opts.MaxSize = 131072, 65537
	if _, err := Run(context.Background(), f.st, opts); err == nil {
		t.Fatal("an inverted band must refuse")
	}
	if got := f.state(t); got.FinishedAt != "" || got.FP != "" {
		t.Fatalf("a refused run must not touch the cursor: %+v", got)
	}

	// A non-positive knob is ABSENT, not invalid: payloadInt yields 0 for a
	// missing or malformed JSON number, and the daemon's house rule is that a
	// malformed knob falls back to the default rather than killing the command.
	// So 0 (and any negative) means "the defect band", and only a POSITIVE min
	// that exceeds max can reach the guard above.
	for _, minSize := range []int64{0, -1} {
		opts := baseOpts()
		opts.MinSize, opts.MaxSize = minSize, 0
		res, err := Run(context.Background(), f.st, opts)
		if err != nil {
			t.Fatalf("min_size=%d means the default band, not an error: %v", minSize, err)
		}
		if res.MinSize != DefaultMinSize || res.MaxSize != DefaultMaxSize {
			t.Fatalf("min_size=%d must default to the defect band: %+v", minSize, res)
		}
	}
}

func TestSidecarsAndTombstonesAreNotCandidates(t *testing.T) {
	f := newFixture(t)
	f.addWith(t, "poster.nfo", 70000, "", "", true) // sidecars are never hashed
	tomb := f.addWith(t, "gone.bin", 70000, "old-quick", "old-content", false)

	ctx := context.Background()
	tx, err := f.st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	tomb.Status = index.StatusMissing
	if err := index.UpdateItem(ctx, tx, tomb); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}

	res, err := Run(ctx, f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if res.Seen != 0 {
		t.Fatalf("neither a sidecar nor a tombstone is a candidate: %+v", res)
	}
	if n := len(f.events(t)); n != 0 {
		t.Fatalf("nothing may be emitted, got %d events", n)
	}
}

// --- emit-only-on-change ----------------------------------------------------

func TestCorrectRowIsVerifiedWithNoWriteAndNoEvent(t *testing.T) {
	f := newFixture(t)
	// Already carries exactly what the hasher will produce: an ordinary rescan
	// touched this file after the QH-T1 binary landed and repaired it.
	it := f.addWith(t, "fixed.bin", 70000, "new-quick-fixed.bin", "new-content-fixed.bin", false)
	seqBefore := it.LocalSeqNo

	res, err := Run(context.Background(), f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 1 || res.Verified != 1 || res.Changed != 0 {
		t.Fatalf("an already-correct row is verified, not changed: %+v", res)
	}
	if n := len(f.events(t)); n != 0 {
		t.Fatalf("a verified row must produce no outbox event, got %d", n)
	}
	// No UpdateItem: the local sequence number is the observable proof, since
	// UpdateItem stamps a fresh one on every call. Central defers a Meilisearch
	// sync job per applied batch, so a needless event is not free anywhere.
	if got := f.row(t, "fixed.bin"); got.LocalSeqNo != seqBefore {
		t.Fatalf("a verified row must not be rewritten (seq %d -> %d)", seqBefore, got.LocalSeqNo)
	}
	if got := f.state(t); got.Verified != 1 || got.Changed != 0 {
		t.Fatalf("verified must be counted SEPARATELY from changed: %+v", got)
	}
}

func TestStaleRowProducesExactlyOneModifiedWithNewHashesAndNoExtraction(t *testing.T) {
	f := newFixture(t)
	it := f.add(t, "stale.bin", 70000)

	res, err := Run(context.Background(), f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 1 || res.Changed != 1 || res.Verified != 0 {
		t.Fatalf("unexpected result: %+v", res)
	}

	evs := f.events(t)
	if len(evs) != 1 {
		t.Fatalf("expected exactly one event, got %d", len(evs))
	}
	ev := evs[0]
	if ev["event_type"] != "modified" {
		t.Fatalf("a re-hash is a modified event, got %v", ev["event_type"])
	}
	if ev["library_ref"] != f.root || ev["rel_path"] != "stale.bin" {
		t.Fatalf("wrong location: %+v", ev)
	}
	// The NEW hashes — this is the inversion from reextract, which re-emits the
	// index's stored values verbatim.
	if ev["quick_hash"] != "new-quick-stale.bin" || ev["content_hash"] != "new-content-stale.bin" {
		t.Fatalf("the corrected hashes must be on the wire: %+v", ev)
	}
	// Identity that did NOT change must be re-stated unchanged: this sweep is not
	// allowed to move size or mtime (a file whose bytes moved was skipped above).
	if ev["size"] != float64(it.Size) || ev["mtime"] != float64(it.MtimeNs)/1e9 {
		t.Fatalf("size/mtime must be the index's, verbatim: %+v", ev)
	}
	// THE containment. apply_batch merges metadata_ only when `extracted` is
	// present, and every applied batch defers a Meili sync job — attaching an
	// extraction here would turn a hash correction into a fleet-wide
	// re-extraction of ~99k items.
	if _, present := ev["extracted"]; present {
		t.Fatalf("a re-hash must NEVER carry an extraction payload: %+v", ev)
	}

	// And the local row is genuinely repaired, not merely reported.
	got := f.row(t, "stale.bin")
	if got.QuickHash != "new-quick-stale.bin" || got.ContentHash != "new-content-stale.bin" {
		t.Fatalf("the local index row must be corrected: %+v", got)
	}
	if got.LocalSeqNo == it.LocalSeqNo {
		t.Fatal("a rewritten row must take a fresh local_seq_no")
	}
	if got.Status != index.StatusActive || got.FileCategory != "image" {
		t.Fatalf("UpdateItem must not blank the columns the sweep does not own: %+v", got)
	}
}

// --- stat guard -------------------------------------------------------------

func TestStatDriftAndVanishedFilesAreSkippedWithoutEvents(t *testing.T) {
	f := newFixture(t)
	f.add(t, "stable.bin", 70000)
	f.add(t, "resized.bin", 70000)
	f.add(t, "retouched.bin", 70000)
	f.add(t, "gone.bin", 70000)
	f.add(t, "nowdir.bin", 70000)

	// Grown on disk since it was indexed: the ordinary scan's job. Repairing it
	// here would write a fresh hash next to a size this sweep may not update.
	if err := os.WriteFile(filepath.Join(f.root, "resized.bin"), make([]byte, 70001), 0o644); err != nil {
		t.Fatal(err)
	}
	// Same size, different mtime — the other half of the identity guard.
	future := time.Now().Add(2 * time.Hour)
	if err := os.Chtimes(filepath.Join(f.root, "retouched.bin"), future, future); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(f.root, "gone.bin")); err != nil {
		t.Fatal(err)
	}
	// Replaced by a directory: indexed, present, but not a regular file.
	if err := os.Remove(filepath.Join(f.root, "nowdir.bin")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(f.root, "nowdir.bin"), 0o755); err != nil {
		t.Fatal(err)
	}

	res, err := Run(context.Background(), f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 5 || res.Changed != 1 || res.Skipped != 4 {
		t.Fatalf("unexpected result: %+v", res)
	}
	evs := f.events(t)
	if len(evs) != 1 || evs[0]["rel_path"] != "stable.bin" {
		t.Fatalf("only the unchanged file may be emitted, got %+v", evs)
	}
	if got := f.row(t, "retouched.bin"); got.QuickHash != "old-quick-retouched.bin" {
		t.Fatal("a file whose mtime moved must be left entirely to the scan")
	}
}

func TestHashFailureLeavesTheRowAloneAndIsCounted(t *testing.T) {
	f := newFixture(t)
	f.add(t, "poison.bin", 70000)
	f.add(t, "ok.bin", 70000)

	opts := baseOpts()
	opts.Hash = func(absPath string, size int64) (string, string) {
		if filepath.Base(absPath) == "poison.bin" {
			return "", "" // an open/read error or the per-file hash timeout
		}
		return stubHash(absPath, size)
	}
	res, err := Run(context.Background(), f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Failed != 1 || res.Changed != 1 {
		t.Fatalf("a hash failure is counted, not fatal: %+v", res)
	}
	// The crucial part: an empty digest must never be WRITTEN. It would destroy a
	// merely-suspect value and then read as a null-hash row the scan self-heals.
	if got := f.row(t, "poison.bin"); got.QuickHash != "old-quick-poison.bin" {
		t.Fatalf("a failed hash must leave the stored value intact: %+v", got)
	}
	if n := len(f.events(t)); n != 1 {
		t.Fatalf("only the successful item may be emitted, got %d", n)
	}
}

func TestEmptyContentHashNeverBlanksAStoredOne(t *testing.T) {
	f := newFixture(t)
	// A widened band can reach a file the policy declines to content-hash. Its
	// stored content_hash was computed when the policy DID allow it and the file
	// has not changed since, so it must survive.
	f.addWith(t, "big.bin", 200000, "old-quick", "keep-me", false)

	opts := baseOpts()
	opts.MinSize, opts.MaxSize = 1, 1<<30
	opts.FP = Fingerprint(2, 1, 1<<30)
	opts.Hash = func(absPath string, size int64) (string, string) {
		return "new-quick", "" // content hashing declined by policy/ceiling
	}
	if _, err := Run(context.Background(), f.st, opts); err != nil {
		t.Fatal(err)
	}
	got := f.row(t, "big.bin")
	if got.QuickHash != "new-quick" {
		t.Fatalf("the quick hash must still be corrected: %+v", got)
	}
	if got.ContentHash != "keep-me" {
		t.Fatalf("an absent content hash must not blank a stored one: %+v", got)
	}
	if evs := f.events(t); len(evs) != 1 || evs[0]["content_hash"] != "keep-me" {
		t.Fatalf("the preserved content hash must ride the event: %+v", evs)
	}
}

// --- cursor / idempotence ---------------------------------------------------

func TestAlreadyDoneShortCircuitsUnlessForced(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)
	f.add(t, "b.bin", 70000)
	ctx := context.Background()

	if _, err := Run(ctx, f.st, baseOpts()); err != nil {
		t.Fatal(err)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("first sweep must emit both items, got %d", n)
	}

	// Same scheme and band, already finished: a no-op, and above all NOT a second
	// read of every file in the band.
	res, err := Run(ctx, f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 0 || res.Reason != reasonAlreadyDone {
		t.Fatalf("a repeat at the same fingerprint must short-circuit: %+v", res)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("the short-circuit must emit nothing, got %d events", n)
	}

	// Force re-sweeps from the beginning. The rows are now correct, so the
	// forced run VERIFIES them and still emits nothing — the emit-only-on-change
	// rule holds even under force, which is what makes force safe to press.
	opts := baseOpts()
	opts.Force = true
	res, err = Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Verified != 2 || res.Changed != 0 || res.Resumed {
		t.Fatalf("a forced sweep must re-examine everything: %+v", res)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("a forced sweep over correct rows must add no events, got %d", n)
	}
}

func TestFingerprintChangeInvalidatesTheCursor(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)
	ctx := context.Background()

	if _, err := Run(ctx, f.st, baseOpts()); err != nil {
		t.Fatal(err)
	}
	before := f.state(t)
	if before.Seen != 1 || before.Changed != 1 {
		t.Fatalf("setup: %+v", before)
	}

	// A future HashSchemeVersion bump: every stored digest in the band is stale
	// again, fleet-wide, with no operator action.
	opts := baseOpts()
	opts.FP = Fingerprint(3, DefaultMinSize, DefaultMaxSize)
	opts.Hash = func(absPath string, size int64) (string, string) {
		return "scheme3-quick", "scheme3-content"
	}
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Resumed || res.Seen != 1 || res.Changed != 1 {
		t.Fatalf("a changed fingerprint must re-sweep from scratch: %+v", res)
	}
	after := f.state(t)
	if after.FP != opts.FP || after.StartedAt == before.StartedAt {
		t.Fatalf("state must be re-stamped for the new fingerprint: %+v", after)
	}
	if after.Seen != 1 {
		t.Fatalf("counters must be zeroed on invalidation, got %+v", after)
	}
}

func TestMaxItemsStopsWithAResumableCursor(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)
	f.add(t, "b.bin", 70000)
	f.add(t, "c.bin", 70000)
	ctx := context.Background()

	opts := baseOpts()
	opts.MaxItems = 2
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if res.Completed || res.Seen != 2 || res.Reason != reasonMaxItems || res.Cursor == 0 {
		t.Fatalf("a bounded run must stop resumably: %+v", res)
	}
	if got := f.state(t); got.FinishedAt != "" {
		t.Fatalf("an incomplete sweep must not be stamped finished: %+v", got)
	}

	// The operator sends the command again: it picks up where it stopped and
	// re-reads nothing it already covered.
	res2, err := Run(ctx, f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res2.Resumed || !res2.Completed || res2.Seen != 1 || res2.Changed != 1 {
		t.Fatalf("the second chunk must resume and finish: %+v", res2)
	}
	evs := f.events(t)
	if len(evs) != 3 {
		t.Fatalf("each item must be emitted exactly once across the chunks, got %d", len(evs))
	}
	seen := map[string]bool{}
	for _, ev := range evs {
		rel, _ := ev["rel_path"].(string)
		if seen[rel] {
			t.Fatalf("item %q emitted twice across chunks", rel)
		}
		seen[rel] = true
	}
	if got := f.state(t); got.Seen != 3 || got.Changed != 3 || got.FinishedAt == "" {
		t.Fatalf("cumulative state wrong: %+v", got)
	}
}

// A restart is not the same as a second call in one process: the resume path has
// to work from what is on DISK, with no in-memory state carried across.
func TestCursorResumesAcrossAProcessRestart(t *testing.T) {
	dir := t.TempDir()
	dbPath := filepath.Join(dir, "index.db")
	root := filepath.Join(dir, "media")
	ctx := context.Background()

	open := func() *index.Store {
		st, err := index.Open(dbPath)
		if err != nil {
			t.Fatal(err)
		}
		return st
	}

	st := open()
	f := &fixture{st: st, root: root}
	if err := os.MkdirAll(root, 0o755); err != nil {
		t.Fatal(err)
	}
	tx, err := st.Begin(ctx)
	if err != nil {
		t.Fatal(err)
	}
	if f.rootID, err = index.EnsureRoot(ctx, tx, root); err != nil {
		t.Fatal(err)
	}
	if err := tx.Commit(); err != nil {
		t.Fatal(err)
	}
	f.add(t, "a.bin", 70000)
	f.add(t, "b.bin", 70000)

	opts := baseOpts()
	opts.MaxItems = 1
	if _, err := Run(ctx, st, opts); err != nil {
		t.Fatal(err)
	}
	cursor := f.state(t).CursorRowID
	if cursor == 0 {
		t.Fatal("the first chunk must leave a durable cursor")
	}
	st.Close()

	// New process, new *Store, same file.
	st2 := open()
	defer st2.Close()
	f2 := &fixture{st: st2, root: root, rootID: f.rootID}
	res, err := Run(ctx, st2, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res.Resumed || !res.Completed || res.Seen != 1 {
		t.Fatalf("a reopened store must resume from the durable cursor: %+v", res)
	}
	if n := len(f2.events(t)); n != 2 {
		t.Fatalf("expected exactly two events across the restart, got %d", n)
	}
}

func TestPausedStopsBetweenBatchesAndResumes(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)
	f.add(t, "b.bin", 70000)
	f.add(t, "c.bin", 70000)
	ctx := context.Background()

	batches := 0
	opts := baseOpts()
	opts.BatchSize = 1
	opts.Paused = func() bool {
		// Not paused for the first batch, paused from the second on (the agent was
		// suspended, or central entered maintenance, mid-sweep).
		batches++
		return batches > 1
	}
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if res.Completed || res.Seen != 1 || res.Reason != reasonPaused {
		t.Fatalf("pause must stop the sweep cleanly: %+v", res)
	}
	if n := len(f.events(t)); n != 1 {
		t.Fatalf("only the pre-pause batch may be emitted, got %d", n)
	}

	res2, err := Run(ctx, f.st, baseOpts())
	if err != nil {
		t.Fatal(err)
	}
	if !res2.Resumed || !res2.Completed || res2.Seen != 2 {
		t.Fatalf("resume after pause wrong: %+v", res2)
	}
	if n := len(f.events(t)); n != 3 {
		t.Fatalf("expected 3 events in total, got %d", n)
	}
}

func TestContextCancellationStopsAndKeepsCommittedWork(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)
	f.add(t, "b.bin", 70000)
	f.add(t, "c.bin", 70000)

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	opts := baseOpts()
	opts.BatchSize = 250 // one batch: cancellation must be honoured WITHIN it
	opts.Hash = func(absPath string, size int64) (string, string) {
		cancel() // shutdown arrives while the first file is being hashed
		return stubHash(absPath, size)
	}
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatalf("cancellation is a stop, not an error: %v", err)
	}
	if res.Completed || res.Reason != reasonCancelled {
		t.Fatalf("a cancelled run must report it: %+v", res)
	}
	if res.Seen != 1 || res.Changed != 1 {
		t.Fatalf("the in-flight item finishes, the rest are abandoned: %+v", res)
	}
	// The commit is deliberately detached from cancellation: the read was already
	// paid for, so the corrected row, its event and the cursor are durable.
	if n := len(f.events(t)); n != 1 {
		t.Fatalf("the finished item's event must survive the cancel, got %d", n)
	}
	got := f.state(t)
	if got.FinishedAt != "" || got.CursorRowID == 0 || got.Seen != 1 {
		t.Fatalf("an abandoned batch must leave a resumable, unfinished cursor: %+v", got)
	}
}

func TestRunWithoutHashingSeamRefuses(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.bin", 70000)

	opts := Options{FP: Fingerprint(2, DefaultMinSize, DefaultMaxSize)}
	if _, err := Run(context.Background(), f.st, opts); err == nil {
		t.Fatal("a sweep with no hasher must refuse rather than stamp the scheme done")
	}
	if got := f.state(t); got.FP != "" || got.FinishedAt != "" {
		t.Fatalf("a refused run must not touch the cursor: %+v", got)
	}
}

// --- fingerprint ------------------------------------------------------------

func TestFingerprintIsReadableAndSensitive(t *testing.T) {
	base := Fingerprint(2, DefaultMinSize, DefaultMaxSize)
	// Readable by eye on the console — the reason this one is not a hash digest.
	if base != "h2-65537-131072" {
		t.Fatalf("unexpected fingerprint shape %q", base)
	}
	if !strings.HasPrefix(base, "h") {
		t.Fatalf("the scheme prefix is part of the contract: %q", base)
	}
	for what, got := range map[string]string{
		"scheme bump": Fingerprint(3, DefaultMinSize, DefaultMaxSize),
		"wider floor": Fingerprint(2, 1, DefaultMaxSize),
		"raised ceil": Fingerprint(2, DefaultMinSize, 1<<20),
	} {
		if got == base {
			t.Fatalf("%s must change the fingerprint (both %q)", what, got)
		}
	}
}

// --- end to end with the production hasher ----------------------------------

// The one test that uses scan.HashFile, wired exactly as the daemon wires it.
// It proves the seam fits and, more importantly, that the sweep's whole premise
// holds: two DIFFERENT in-band files that share their first 64 KiB — the precise
// shape of the defect — come out with DIFFERENT quick hashes.
func TestSweepWithTheRealHasher(t *testing.T) {
	f := newFixture(t)
	root := f.root
	shared := make([]byte, 65536)
	for i := range shared {
		shared[i] = byte(i % 251)
	}
	for name, tailByte := range map[string]byte{"twinA.bin": 0x01, "twinB.bin": 0x02} {
		body := append(append([]byte{}, shared...), make([]byte, 4464)...)
		for i := 65536; i < len(body); i++ {
			body[i] = tailByte
		}
		if len(body) != 70000 {
			t.Fatalf("fixture size %d", len(body))
		}
		if err := os.WriteFile(filepath.Join(root, name), body, 0o644); err != nil {
			t.Fatal(err)
		}
		info, err := os.Stat(filepath.Join(root, name))
		if err != nil {
			t.Fatal(err)
		}
		id, _ := index.NewID()
		it := &index.Item{
			ID: id, RootID: f.rootID, RelPath: name, Filename: name,
			Size: info.Size(), MtimeNs: info.ModTime().UnixNano(),
			// The stale value the old hasher produced: identical for both files,
			// because it only ever saw the shared first 64 KiB. This IS the bug.
			QuickHash: "stale-head-only", ContentHash: "",
			FileCategory: "image", Status: index.StatusActive,
			FirstSeen: time.Now(), LastSeen: time.Now(),
		}
		ctx := context.Background()
		tx, err := f.st.Begin(ctx)
		if err != nil {
			t.Fatal(err)
		}
		if err := index.InsertItem(ctx, tx, it); err != nil {
			t.Fatal(err)
		}
		if err := tx.Commit(); err != nil {
			t.Fatal(err)
		}
	}

	policy := scan.DefaultHashPolicy()
	opts := baseOpts()
	opts.FP = Fingerprint(scan.HashSchemeVersion, DefaultMinSize, DefaultMaxSize)
	opts.Hash = func(absPath string, size int64) (string, string) {
		return scan.HashFile(absPath, size, policy)
	}
	res, err := Run(context.Background(), f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Changed != 2 {
		t.Fatalf("both twins must be corrected: %+v", res)
	}
	a, b := f.row(t, "twinA.bin"), f.row(t, "twinB.bin")
	if a.QuickHash == "stale-head-only" || b.QuickHash == "stale-head-only" {
		t.Fatal("the stale head-only digest survived the sweep")
	}
	if a.QuickHash == b.QuickHash {
		t.Fatalf("THE DEFECT: two different in-band files still share a quick hash (%q)", a.QuickHash)
	}
	// QH-T2: an in-band file always gets a real content hash, regardless of
	// policy — so the sweep grants one to a row that had none.
	if len(a.ContentHash) != 32 || len(b.ContentHash) != 32 {
		t.Fatalf("in-band rows must gain a 32-hex xxh3-128 content hash: %q / %q", a.ContentHash, b.ContentHash)
	}
	if a.ContentHash == b.ContentHash {
		t.Fatal("different bytes must yield different content hashes")
	}
}
