package extract

import (
	"testing"
)

func TestCleanProvenanceURL(t *testing.T) {
	cases := map[string]string{
		"https://example.com/a?b=1":    "https://example.com/a?b=1",
		"  http://x.test/\x00bad\x07 ": "http://x.test/bad",
		"javascript:alert(1)":          "",
		"file:///etc/passwd":           "",
		"":                             "",
		"ftp://h.test/f":               "ftp://h.test/f",
	}
	for in, want := range cases {
		if got := cleanProvenanceURL(in); got != want {
			t.Errorf("clean(%q) = %q, want %q", in, got, want)
		}
	}
}

func TestSetProvenanceDropsDuplicateReferrer(t *testing.T) {
	res := &Result{Meta: map[string]any{}}
	setProvenance(res, "https://a.test/f", "https://a.test/f")
	if res.Meta[keyOriginURL] != "https://a.test/f" || res.Meta[keyReferrerURL] != nil {
		t.Fatalf("got %v", res.Meta)
	}
	res = &Result{Meta: map[string]any{}}
	setProvenance(res, "https://a.test/f", "https://b.test/")
	if res.Meta[keyReferrerURL] != "https://b.test/" {
		t.Fatalf("got %v", res.Meta)
	}
}

// Generated with python3: plistlib.dumps(["https://cdn.example.org/x.dmg",
// "https://example.org/"], fmt=plistlib.FMT_BINARY)
var bplistTwo = []byte("bplist00\xa2\x01\x02_\x10\x1dhttps://cdn.example.org/x.dmg_\x10\x14https://example.org/\x08\x0b+\x00\x00\x00\x00\x00\x00\x01\x01\x00\x00\x00\x00\x00\x00\x00\x03\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00\x00B")

func TestDecodeWhereFroms(t *testing.T) {
	o, r := decodeWhereFroms(bplistTwo)
	if o != "https://cdn.example.org/x.dmg" || r != "https://example.org/" {
		t.Fatalf("got %q %q", o, r)
	}
	if o, r := decodeWhereFroms([]byte("garbage")); o != "" || r != "" {
		t.Fatalf("garbage decoded to %q %q", o, r)
	}
	// truncated trailer must not panic
	if o, _ := decodeWhereFroms(bplistTwo[:20]); o != "" {
		t.Fatalf("truncated decoded to %q", o)
	}
}
