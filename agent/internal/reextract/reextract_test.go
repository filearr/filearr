package reextract

// The re-extraction sweep (agent parity phase 3). Every test drives a REAL temp
// index.Store over REAL files on disk, because the semantics under test are
// precisely about the agreement between an index row and the bytes it describes.
// Extraction itself is always a stub: invoking the real extractors would drag
// host tools (ffprobe/tesseract/poppler) into the unit suite, and this package
// owns none of that behaviour.

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"testing"
	"time"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
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

// add writes a real file and indexes it with the identity that file actually
// has, which is the steady state the sweep expects to find.
func (f *fixture) add(t *testing.T, rel, body string) *index.Item {
	t.Helper()
	abs := filepath.Join(f.root, filepath.FromSlash(rel))
	if err := os.MkdirAll(filepath.Dir(abs), 0o755); err != nil {
		t.Fatal(err)
	}
	if err := os.WriteFile(abs, []byte(body), 0o644); err != nil {
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
		QuickHash: "q-" + rel, ContentHash: "c-" + rel,
		FileCategory: "document", Status: index.StatusActive,
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

func (f *fixture) state(t *testing.T) index.ExtractState {
	t.Helper()
	st, err := f.st.ExtractState(context.Background())
	if err != nil {
		t.Fatal(err)
	}
	return st
}

// stubExtract returns a deterministic result for every file, standing in for the
// real pass. A nil return is the "nothing found" case tested separately.
func stubExtract(ctx context.Context, absPath, category string) *outbox.Extracted {
	return &outbox.Extracted{
		Schema: 1,
		Meta:   map[string]any{"title": filepath.Base(absPath), "category": category},
	}
}

func baseOpts(fp string) Options {
	return Options{FP: fp, Extract: stubExtract}
}

func TestSweepEmitsIndexIdentityVerbatimWithExtraction(t *testing.T) {
	f := newFixture(t)
	it := f.add(t, "Docs/manual.pdf", "hello")

	res, err := Run(context.Background(), f.st, baseOpts("fp-a"))
	if err != nil {
		t.Fatalf("Run: %v", err)
	}
	if !res.Completed || res.Resumed || res.Seen != 1 || res.Extracted != 1 || res.Skipped != 0 {
		t.Fatalf("unexpected result: %+v", res)
	}

	evs := f.events(t)
	if len(evs) != 1 {
		t.Fatalf("expected exactly one event, got %d", len(evs))
	}
	ev := evs[0]
	if ev["event_type"] != "modified" {
		t.Fatalf("a re-extract is a modified event, got %v", ev["event_type"])
	}
	if ev["library_ref"] != f.root || ev["rel_path"] != "Docs/manual.pdf" {
		t.Fatalf("wrong location: %+v", ev)
	}
	// Identity must be the INDEX's, byte for byte — not re-derived.
	if ev["size"] != float64(it.Size) {
		t.Fatalf("size not verbatim: %v want %v", ev["size"], it.Size)
	}
	if ev["mtime"] != float64(it.MtimeNs)/1e9 {
		t.Fatalf("mtime not verbatim: %v", ev["mtime"])
	}
	if ev["quick_hash"] != it.QuickHash || ev["content_hash"] != it.ContentHash {
		t.Fatalf("hashes not verbatim: %+v", ev)
	}
	ext, ok := ev["extracted"].(map[string]any)
	if !ok {
		t.Fatalf("extracted payload missing: %+v", ev)
	}
	meta, _ := ext["meta"].(map[string]any)
	if ext["schema"] != float64(1) || meta["title"] != "manual.pdf" || meta["category"] != "document" {
		t.Fatalf("extraction payload wrong: %+v", ext)
	}

	// The completed sweep is durably stamped, cursor and all.
	got := f.state(t)
	if got.FP != "fp-a" || got.FinishedAt == "" || got.StartedAt == "" || got.CursorRowID == 0 {
		t.Fatalf("state not stamped: %+v", got)
	}
	if got.Seen != 1 || got.Extracted != 1 {
		t.Fatalf("state counters wrong: %+v", got)
	}
}

func TestAlreadyDoneShortCircuitsUnlessForced(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")
	f.add(t, "b.pdf", "bbb")
	ctx := context.Background()

	if _, err := Run(ctx, f.st, baseOpts("fp-a")); err != nil {
		t.Fatal(err)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("first sweep must emit both items, got %d", n)
	}

	// Same configuration, already finished: a no-op, and above all NOT a second
	// walk of the whole index.
	res, err := Run(ctx, f.st, baseOpts("fp-a"))
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 0 || res.Reason != reasonAlreadyDone {
		t.Fatalf("repeat at the same configuration must short-circuit: %+v", res)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("short-circuit must emit nothing, got %d events", n)
	}

	// Force overrides the short-circuit and re-sweeps from the beginning.
	opts := baseOpts("fp-a")
	opts.Force = true
	res, err = Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Extracted != 2 || res.Resumed {
		t.Fatalf("forced sweep must redo everything: %+v", res)
	}
	if n := len(f.events(t)); n != 4 {
		t.Fatalf("forced sweep must re-emit both items, got %d events", n)
	}
}

func TestFingerprintChangeInvalidatesTheCursor(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")
	ctx := context.Background()

	if _, err := Run(ctx, f.st, baseOpts("fp-a")); err != nil {
		t.Fatal(err)
	}
	before := f.state(t)

	// A new configuration (policy change, or a host tool appeared) means the
	// previous sweep's results no longer describe what extraction would produce.
	res, err := Run(ctx, f.st, baseOpts("fp-b"))
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Resumed || res.Seen != 1 || res.Extracted != 1 {
		t.Fatalf("a changed fingerprint must re-sweep from scratch: %+v", res)
	}
	if n := len(f.events(t)); n != 2 {
		t.Fatalf("expected a second event for the new configuration, got %d", n)
	}
	after := f.state(t)
	if after.FP != "fp-b" || after.StartedAt == before.StartedAt {
		t.Fatalf("state must be re-stamped for the new fingerprint: %+v", after)
	}
	if after.Seen != 1 {
		t.Fatalf("counters must be zeroed on invalidation, got %+v", after)
	}
}

func TestMaxItemsStopsWithAResumableCursor(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")
	f.add(t, "b.pdf", "bbb")
	f.add(t, "c.pdf", "ccc")
	ctx := context.Background()

	opts := baseOpts("fp-a")
	opts.MaxItems = 2
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if res.Completed || res.Seen != 2 || res.Reason != reasonMaxItems || res.Cursor == 0 {
		t.Fatalf("bounded run must stop resumably: %+v", res)
	}
	if got := f.state(t); got.FinishedAt != "" {
		t.Fatalf("an incomplete sweep must not be stamped finished: %+v", got)
	}

	// The operator sends the command again: it picks up where it stopped and
	// re-emits nothing it already covered.
	res2, err := Run(ctx, f.st, baseOpts("fp-a"))
	if err != nil {
		t.Fatal(err)
	}
	if !res2.Resumed || !res2.Completed || res2.Seen != 1 || res2.Extracted != 1 {
		t.Fatalf("second chunk must resume and finish: %+v", res2)
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
	// The persisted counters accumulate across the chunks of ONE sweep.
	if got := f.state(t); got.Seen != 3 || got.Extracted != 3 || got.FinishedAt == "" {
		t.Fatalf("cumulative state wrong: %+v", got)
	}
}

func TestChangedAndVanishedFilesAreSkippedWithoutEvents(t *testing.T) {
	f := newFixture(t)
	f.add(t, "stable.pdf", "aaa")
	f.add(t, "changed.pdf", "aaa")
	f.add(t, "gone.pdf", "aaa")
	f.add(t, "nowdir.pdf", "aaa")

	// Grown on disk since it was indexed: the scan's job, never the sweep's.
	if err := os.WriteFile(filepath.Join(f.root, "changed.pdf"), []byte("aaaaaaaaaaaa"), 0o644); err != nil {
		t.Fatal(err)
	}
	if err := os.Remove(filepath.Join(f.root, "gone.pdf")); err != nil {
		t.Fatal(err)
	}
	// Replaced by a directory: indexed, present, but not a regular file.
	if err := os.Remove(filepath.Join(f.root, "nowdir.pdf")); err != nil {
		t.Fatal(err)
	}
	if err := os.Mkdir(filepath.Join(f.root, "nowdir.pdf"), 0o755); err != nil {
		t.Fatal(err)
	}

	res, err := Run(context.Background(), f.st, baseOpts("fp-a"))
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 4 || res.Extracted != 1 || res.Skipped != 3 {
		t.Fatalf("unexpected result: %+v", res)
	}
	evs := f.events(t)
	if len(evs) != 1 || evs[0]["rel_path"] != "stable.pdf" {
		t.Fatalf("only the unchanged file may be emitted, got %+v", evs)
	}
}

func TestNilExtractionResultCountsAsSkipped(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")
	f.add(t, "b.pdf", "bbb")

	opts := baseOpts("fp-a")
	opts.Extract = func(_ context.Context, absPath, _ string) *outbox.Extracted {
		if filepath.Base(absPath) == "a.pdf" {
			return nil // unsupported category / over the cap / nothing found
		}
		return &outbox.Extracted{Schema: 1, Meta: map[string]any{"k": "v"}}
	}
	res, err := Run(context.Background(), f.st, opts)
	if err != nil {
		t.Fatal(err)
	}
	if !res.Completed || res.Seen != 2 || res.Extracted != 1 || res.Skipped != 1 {
		t.Fatalf("a nil extraction result is a skip, not a failure: %+v", res)
	}
	if evs := f.events(t); len(evs) != 1 || evs[0]["rel_path"] != "b.pdf" {
		t.Fatalf("only the extracted item may be emitted, got %+v", evs)
	}
}

func TestPausedStopsMidSweepAndResumes(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")
	f.add(t, "b.pdf", "bbb")
	f.add(t, "c.pdf", "ccc")
	ctx := context.Background()

	batches := 0
	opts := baseOpts("fp-a")
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

	// Unpaused, the sweep resumes from the durable cursor.
	res2, err := Run(ctx, f.st, baseOpts("fp-a"))
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
	f.add(t, "a.pdf", "aaa")
	f.add(t, "b.pdf", "bbb")
	f.add(t, "c.pdf", "ccc")

	ctx, cancel := context.WithCancel(context.Background())
	defer cancel()

	opts := baseOpts("fp-a")
	opts.BatchSize = 250 // one batch: cancellation must be honoured WITHIN it
	opts.Extract = func(_ context.Context, absPath, category string) *outbox.Extracted {
		// Shutdown arrives while the first file is being extracted.
		cancel()
		return stubExtract(ctx, absPath, category)
	}
	res, err := Run(ctx, f.st, opts)
	if err != nil {
		t.Fatalf("cancellation is a stop, not an error: %v", err)
	}
	if res.Completed || res.Reason != reasonCancelled {
		t.Fatalf("cancelled run must report it: %+v", res)
	}
	if res.Seen != 1 || res.Extracted != 1 {
		t.Fatalf("the in-flight item finishes, the rest are abandoned: %+v", res)
	}
	// The commit is deliberately detached from cancellation: the extraction was
	// already paid for, so its event and its cursor are durable.
	if n := len(f.events(t)); n != 1 {
		t.Fatalf("the finished item's event must survive the cancel, got %d", n)
	}
	got := f.state(t)
	if got.FinishedAt != "" {
		t.Fatalf("an abandoned batch must not be stamped finished: %+v", got)
	}
	if got.CursorRowID == 0 {
		t.Fatalf("the cursor must cover the committed event: %+v", got)
	}
	if got.Seen != 1 {
		t.Fatalf("state counters wrong: %+v", got)
	}
}

func TestRunWithoutExtractionSeamRefuses(t *testing.T) {
	f := newFixture(t)
	f.add(t, "a.pdf", "aaa")

	opts := Options{FP: "fp-a"} // no Extract seam: extraction is off on this agent
	if _, err := Run(context.Background(), f.st, opts); err == nil {
		t.Fatal("a sweep with no extractor must refuse rather than stamp the configuration done")
	}
	if got := f.state(t); got.FP != "" || got.FinishedAt != "" {
		t.Fatalf("a refused run must not touch the cursor: %+v", got)
	}
}

func TestFingerprintIsStableAndSensitive(t *testing.T) {
	tools := map[string]bool{"ffprobe": true, "exiftool": true, "tesseract": false}
	base := Fingerprint(1, false, false, true, 1<<30, tools)

	// Map iteration order must not leak into the value.
	for i := 0; i < 8; i++ {
		if got := Fingerprint(1, false, false, true, 1<<30, map[string]bool{
			"exiftool": true, "tesseract": false, "ffprobe": true,
		}); got != base {
			t.Fatalf("fingerprint unstable across map orders: %q vs %q", got, base)
		}
	}
	// The human-readable prefix carries the schema version an operator can act on.
	if len(base) < 4 || base[:3] != "e1-" {
		t.Fatalf("expected a schema-prefixed fingerprint, got %q", base)
	}

	// Anything that changes what an extraction would PRODUCE must change it.
	cases := map[string]string{
		"schema":        Fingerprint(2, false, false, true, 1<<30, tools),
		"body text":     Fingerprint(1, true, false, true, 1<<30, tools),
		"ocr":           Fingerprint(1, false, true, true, 1<<30, tools),
		"exif":          Fingerprint(1, false, false, false, 1<<30, tools),
		"max bytes":     Fingerprint(1, false, false, true, 1<<20, tools),
		"tool appeared": Fingerprint(1, false, false, true, 1<<30, map[string]bool{"ffprobe": true, "exiftool": true, "tesseract": true}),
		"tool lost":     Fingerprint(1, false, false, true, 1<<30, map[string]bool{"exiftool": true}),
	}
	for what, got := range cases {
		if got == base {
			t.Fatalf("%s must change the fingerprint (both %q)", what, got)
		}
	}
}
