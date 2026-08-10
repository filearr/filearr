package outbox

import (
	"context"
	"encoding/json"
	"testing"
)

// sampleExtracted is the canonical `extracted` object the agent emits — one key
// from each vocabulary family, so a shape regression shows up here.
func sampleExtracted() *Extracted {
	return &Extracted{
		Schema: 1,
		Meta: map[string]any{
			"width":  1920,
			"height": 1080,
			"format": "PNG",
		},
		BodyText:          "the quick brown fox",
		BodyTextTruncated: true,
	}
}

// TestExtractedAttachedOnUpsert pins the exact wire shape from the agent-parity
// design contract: {schema, meta, body_text, body_text_truncated} under an
// `extracted` key on a created/modified event.
func TestExtractedAttachedOnUpsert(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/m", RelPath: "a.png",
		Size: 1, MtimeNs: 1, QuickHash: "q",
		Extracted: sampleExtracted(),
	})

	m := rawPayload(t, st)
	ex, ok := m["extracted"].(map[string]any)
	if !ok {
		t.Fatalf("extracted missing/not an object: %v", m["extracted"])
	}
	if ex["schema"] != float64(1) {
		t.Errorf("schema = %v, want 1", ex["schema"])
	}
	if ex["body_text"] != "the quick brown fox" {
		t.Errorf("body_text = %v", ex["body_text"])
	}
	if ex["body_text_truncated"] != true {
		t.Errorf("body_text_truncated = %v, want true", ex["body_text_truncated"])
	}
	meta, ok := ex["meta"].(map[string]any)
	if !ok {
		t.Fatalf("meta missing/not an object: %v", ex["meta"])
	}
	if meta["width"] != float64(1920) || meta["format"] != "PNG" {
		t.Errorf("meta shape wrong: %+v", meta)
	}
	// Only the four documented sub-keys — an extra one would be a contract drift
	// central is not prepared for.
	if len(ex) != 4 {
		t.Errorf("extracted has %d keys, want 4: %+v", len(ex), ex)
	}
}

// TestExtractedOmittedWhenAbsent is the additive-field contract: with no
// extraction the KEY is entirely absent, so a central that predates the field —
// and the overwhelmingly common default-off configuration — is unaffected.
func TestExtractedOmittedWhenAbsent(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/m", RelPath: "a.png",
		Size: 1, MtimeNs: 1, QuickHash: "q", // Extracted left nil
	})
	if _, present := rawPayload(t, st)["extracted"]; present {
		t.Fatal("extracted must be OMITTED when absent")
	}
}

// TestExtractedEmptySubFieldsOmitted keeps a metadata-only result minimal: an
// empty body must not ship as "" plus a false flag.
func TestExtractedEmptySubFieldsOmitted(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpCreated, LibraryRef: "/m", RelPath: "a.png",
		Size: 1, MtimeNs: 1, QuickHash: "q",
		Extracted: &Extracted{Schema: 1, Meta: map[string]any{"width": 4}},
	})
	ex := rawPayload(t, st)["extracted"].(map[string]any)
	if _, present := ex["body_text"]; present {
		t.Error("empty body_text should be omitted")
	}
	if _, present := ex["body_text_truncated"]; present {
		t.Error("false body_text_truncated should be omitted")
	}
}

// TestExtractedNotEmittedOnDelete: a tombstone describes a file that is gone —
// there is nothing to have extracted.
func TestExtractedNotEmittedOnDelete(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpDeleted, LibraryRef: "/m", RelPath: "gone.png",
		Extracted: sampleExtracted(),
	})
	if _, present := rawPayload(t, st)["extracted"]; present {
		t.Fatal("a deleted event must not carry extracted")
	}
}

// TestExtractedRoundTripsThroughTheDrain is the compatibility guard that
// matters operationally: the replicator UNMARSHALS a stored payload into
// wireEvent and re-MARSHALS it into the batch. A field it did not model would be
// silently dropped between the outbox and central.
func TestExtractedRoundTripsThroughTheDrain(t *testing.T) {
	st := openStore(t)
	writeOne(t, st, Event{
		ItemID: "i1", Op: OpModified, LibraryRef: "/m", RelPath: "a.png",
		Size: 1, MtimeNs: 1, QuickHash: "q",
		Extracted: sampleExtracted(),
	})
	rows, err := New(st.DB()).Unsent(context.Background(), 10)
	if err != nil {
		t.Fatal(err)
	}

	var we wireEvent
	if err := json.Unmarshal([]byte(rows[0].Payload), &we); err != nil {
		t.Fatalf("decode stored payload: %v", err)
	}
	if we.Extracted == nil || we.Extracted.Schema != 1 {
		t.Fatalf("extracted lost on decode: %+v", we.Extracted)
	}
	we.SeqNo = rows[0].SeqNo

	buf, err := json.Marshal(we)
	if err != nil {
		t.Fatalf("re-marshal: %v", err)
	}
	var out map[string]any
	if err := json.Unmarshal(buf, &out); err != nil {
		t.Fatal(err)
	}
	ex, ok := out["extracted"].(map[string]any)
	if !ok || ex["body_text"] != "the quick brown fox" {
		t.Fatalf("extracted lost on re-marshal: %v", out["extracted"])
	}
}

// TestOldRowWithoutExtractedStillDrains is the backward-compatibility half: a
// payload written by an agent build that predates the field must decode and
// re-encode cleanly, with `extracted` still absent.
func TestOldRowWithoutExtractedStillDrains(t *testing.T) {
	oldPayload := `{"event_type":"created","library_ref":"/m","rel_path":"a.mkv",` +
		`"from_rel_path":null,"size":42,"mtime":1.5,"quick_hash":"q","content_hash":null}`

	var we wireEvent
	if err := json.Unmarshal([]byte(oldPayload), &we); err != nil {
		t.Fatalf("an old row must still decode: %v", err)
	}
	if we.Extracted != nil {
		t.Fatalf("old row produced a non-nil extracted: %+v", we.Extracted)
	}
	we.SeqNo = 7

	buf, err := json.Marshal(we)
	if err != nil {
		t.Fatal(err)
	}
	var out map[string]any
	if err := json.Unmarshal(buf, &out); err != nil {
		t.Fatal(err)
	}
	if _, present := out["extracted"]; present {
		t.Fatalf("re-marshalling an old row invented an extracted key: %v", out)
	}
	// The pre-existing contract fields must survive untouched.
	if out["event_type"] != "created" || out["rel_path"] != "a.mkv" || out["size"] != float64(42) {
		t.Fatalf("old-row round trip changed the body: %v", out)
	}
}
