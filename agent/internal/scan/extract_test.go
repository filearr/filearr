package scan

import (
	"context"
	"encoding/json"
	"os"
	"path/filepath"
	"sync"
	"testing"

	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/outbox"
)

// recordingExtractor is a test double for the extraction seam: it records every
// (path, category) it is asked about and returns a fixed object. It keeps this
// test about the WIRING (which files get extracted, and whether the result
// reaches the event) rather than about any real parser.
type recordingExtractor struct {
	mu    sync.Mutex
	calls []call
	// result, when nil, simulates an extractor that produced nothing.
	result *outbox.Extracted
}

type call struct{ path, category string }

func (r *recordingExtractor) fn(_ context.Context, absPath, category string) *outbox.Extracted {
	r.mu.Lock()
	defer r.mu.Unlock()
	r.calls = append(r.calls, call{path: absPath, category: category})
	return r.result
}

func (r *recordingExtractor) categories() map[string]int {
	r.mu.Lock()
	defer r.mu.Unlock()
	out := map[string]int{}
	for _, c := range r.calls {
		out[c.category]++
	}
	return out
}

// payloads returns every outbox payload written so far, decoded.
func payloads(t *testing.T, st *index.Store) []map[string]any {
	t.Helper()
	rows, err := outbox.New(st.DB()).Unsent(context.Background(), 1000)
	if err != nil {
		t.Fatal(err)
	}
	out := make([]map[string]any, 0, len(rows))
	for _, row := range rows {
		var m map[string]any
		if err := json.Unmarshal([]byte(row.Payload), &m); err != nil {
			t.Fatal(err)
		}
		out = append(out, m)
	}
	return out
}

func TestScanAttachesExtractionToEvents(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"song.mp3", "clip.mkv"})
	st := newStore(t)

	rec := &recordingExtractor{result: &outbox.Extracted{
		Schema: 1, Meta: map[string]any{"title": "Cathedral"},
	}}
	r := mustScan(t, st, Options{Root: root, Extract: rec.fn})
	if r.New != 2 {
		t.Fatalf("scan: %+v", r)
	}

	// Extraction is driven by the category the walk already computed — the scan
	// never re-classifies.
	cats := rec.categories()
	if cats["audio"] != 1 || cats["video"] != 1 {
		t.Fatalf("categories passed to the extractor = %v", cats)
	}

	for _, p := range payloads(t, st) {
		ex, ok := p["extracted"].(map[string]any)
		if !ok {
			t.Fatalf("event %v carries no extracted object", p["rel_path"])
		}
		meta := ex["meta"].(map[string]any)
		if meta["title"] != "Cathedral" {
			t.Errorf("meta not carried through: %v", meta)
		}
	}
}

func TestScanOmitsExtractedWhenExtractorReturnsNil(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"song.mp3"})
	st := newStore(t)

	rec := &recordingExtractor{result: nil} // extractor ran, produced nothing
	mustScan(t, st, Options{Root: root, Extract: rec.fn})

	for _, p := range payloads(t, st) {
		if _, present := p["extracted"]; present {
			t.Fatalf("extracted key present for an empty result: %v", p)
		}
	}
}

func TestScanWithoutExtractorIsUnchanged(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"song.mp3"})
	st := newStore(t)

	// The default configuration (extract_enabled false → nil seam) must produce
	// byte-identical events to a build that never had the feature.
	mustScan(t, st, Options{Root: root})
	for _, p := range payloads(t, st) {
		if _, present := p["extracted"]; present {
			t.Fatalf("extracted key present with no extractor configured: %v", p)
		}
	}
}

// TestScanReExtractsOnlyChangedFiles pins the cost model: a steady-state rescan
// must not re-read content, and a CHANGED file must be re-extracted so central
// overwrites its now-stale metadata.
func TestScanReExtractsOnlyChangedFiles(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"a.mp3", "b.mp3"})
	st := newStore(t)

	rec := &recordingExtractor{result: &outbox.Extracted{Schema: 1, Meta: map[string]any{"k": "v"}}}
	opts := Options{Root: root, Extract: rec.fn}

	mustScan(t, st, opts)
	if got := len(rec.calls); got != 2 {
		t.Fatalf("first scan extracted %d files, want 2", got)
	}

	// Unchanged rescan: no extraction at all.
	mustScan(t, st, opts)
	if got := len(rec.calls); got != 2 {
		t.Fatalf("steady-state rescan re-extracted (%d calls total)", got)
	}

	// Change one file's bytes; only it is re-extracted.
	if err := os.WriteFile(filepath.Join(root, "a.mp3"), []byte("longer content"), 0o644); err != nil {
		t.Fatal(err)
	}
	mustScan(t, st, opts)
	if got := len(rec.calls); got != 3 {
		t.Fatalf("changed-file rescan made %d total calls, want 3", got)
	}
	if last := rec.calls[2].path; filepath.Base(last) != "a.mp3" {
		t.Fatalf("re-extracted %q, want a.mp3", last)
	}
}

// TestScanSkipsSidecarExtraction: sidecars are pointers to a primary item, not
// content in their own right — the same reason they are not hashed.
func TestScanSkipsSidecarExtraction(t *testing.T) {
	root := t.TempDir()
	mktree(t, root, []string{"movie.mkv", "movie.nfo"})
	st := newStore(t)

	rec := &recordingExtractor{result: &outbox.Extracted{Schema: 1}}
	mustScan(t, st, Options{Root: root, Extract: rec.fn})

	for _, c := range rec.calls {
		if filepath.Ext(c.path) == ".nfo" {
			t.Fatalf("sidecar %s was handed to the extractor", c.path)
		}
	}
}
