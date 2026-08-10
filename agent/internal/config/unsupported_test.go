package config

import (
	"bytes"
	"encoding/json"
	"log/slog"
	"strings"
	"testing"
)

// policyFrom builds a Policy from a JSON body, so a test expresses exactly which
// keys central SET (pointer presence is the whole point of these rules).
func policyFrom(t *testing.T, body string) Policy {
	t.Helper()
	p, err := ParsePolicy(json.RawMessage(body))
	if err != nil {
		t.Fatalf("parse policy %s: %v", body, err)
	}
	return p
}

func TestIgnoredSettings(t *testing.T) {
	full := ExtractCapabilities{Extract: true, FFprobe: true, Tesseract: true}

	tests := []struct {
		name   string
		body   string
		caps   ExtractCapabilities
		want   []IgnoredSetting
		reason string
	}{
		{
			name: "everything satisfied",
			body: `{"extract_enabled":true,"extract_ocr":true,"extract_body_text":true}`,
			caps: full,
			want: nil,
		},
		{
			name: "empty policy ignores nothing",
			body: `{}`,
			caps: ExtractCapabilities{Extract: true},
			// extract_enabled defaults false and no sub-key is set, so there is
			// nothing an operator asked for that we are declining.
			want: nil,
		},
		{
			name: "ocr without tesseract",
			body: `{"extract_enabled":true,"extract_ocr":true}`,
			caps: ExtractCapabilities{Extract: true, FFprobe: true},
			want: []IgnoredSetting{{Key: "extract_ocr", Reason: "no tesseract on this host (install it or unset extract_ocr)"}},
		},
		{
			name: "no ffprobe degrades the technical probe",
			body: `{"extract_enabled":true}`,
			caps: ExtractCapabilities{Extract: true, Tesseract: true},
			want: []IgnoredSetting{{Key: "extract_enabled", Reason: "no ffprobe on this host — video/audio technical probe (codec, resolution, duration, bitrate) is skipped; the rest of extraction still runs (install ffmpeg/ffprobe)"}},
		},
		{
			name: "sub-keys set while the master switch is off",
			body: `{"extract_body_text":true,"extract_ocr":false,"extract_max_bytes":1024}`,
			caps: full,
			want: []IgnoredSetting{
				{Key: "extract_body_text"},
				{Key: "extract_ocr"},
				{Key: "extract_max_bytes"},
			},
			reason: "extract_enabled is false",
		},
		{
			name: "explicit disable with no sub-keys is silent",
			body: `{"extract_enabled":false}`,
			caps: full,
			want: nil,
		},
		{
			name: "build without the pass collapses to one line",
			body: `{"extract_enabled":true,"extract_ocr":true}`,
			caps: ExtractCapabilities{},
			want: []IgnoredSetting{{Key: "extract_enabled", Reason: "this agent build has no extraction pass (upgrade the agent)"}},
		},
	}

	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got := IgnoredSettings(policyFrom(t, tc.body), tc.caps)
			if len(got) != len(tc.want) {
				t.Fatalf("got %d ignored settings %v, want %d %v", len(got), got, len(tc.want), tc.want)
			}
			for i, want := range tc.want {
				if got[i].Key != want.Key {
					t.Errorf("[%d] key = %q, want %q", i, got[i].Key, want.Key)
				}
				switch {
				case want.Reason != "" && got[i].Reason != want.Reason:
					t.Errorf("[%d] reason = %q, want %q", i, got[i].Reason, want.Reason)
				case tc.reason != "" && !strings.Contains(got[i].Reason, tc.reason):
					t.Errorf("[%d] reason %q does not mention %q", i, got[i].Reason, tc.reason)
				}
			}
		})
	}
}

// TestIgnoredSettingsDeterministicOrder guards the fingerprint: an unstable
// order would make the once-per-change logger fire on every poll.
func TestIgnoredSettingsDeterministicOrder(t *testing.T) {
	pol := policyFrom(t, `{"extract_body_text":true,"extract_ocr":true,"extract_max_bytes":99}`)
	first := IgnoredSettings(pol, ExtractCapabilities{Extract: true})
	for i := 0; i < 20; i++ {
		if fingerprint(IgnoredSettings(pol, ExtractCapabilities{Extract: true})) != fingerprint(first) {
			t.Fatal("ignored-setting order is not deterministic")
		}
	}
}

// captureLogger returns a WARN-capable logger writing into buf.
func captureLogger(buf *bytes.Buffer) *slog.Logger {
	return slog.New(slog.NewTextHandler(buf, &slog.HandlerOptions{Level: slog.LevelInfo}))
}

func TestIgnoredLoggerLogsOncePerChange(t *testing.T) {
	var buf bytes.Buffer
	lg := NewIgnoredLogger(captureLogger(&buf))

	noTesseract := []IgnoredSetting{{Key: "extract_ocr", Reason: "no tesseract on this host"}}

	lg.Log(noTesseract)
	if n := strings.Count(buf.String(), "extract_ocr ignored"); n != 1 {
		t.Fatalf("first log emitted %d lines, want 1: %s", n, buf.String())
	}
	// Repeats of an UNCHANGED set are the common case (every poll) and must be
	// silent — that suppression is the whole reason this type exists.
	for i := 0; i < 5; i++ {
		lg.Log(noTesseract)
	}
	if n := strings.Count(buf.String(), "extract_ocr ignored"); n != 1 {
		t.Fatalf("repeat logs emitted %d lines, want 1: %s", n, buf.String())
	}

	// A genuinely different set logs again.
	buf.Reset()
	lg.Log([]IgnoredSetting{{Key: "extract_enabled", Reason: "no ffprobe"}})
	if !strings.Contains(buf.String(), "extract_enabled ignored") {
		t.Fatalf("changed set did not log: %s", buf.String())
	}

	// Clearing the condition reports the fix exactly once.
	buf.Reset()
	lg.Log(nil)
	if !strings.Contains(buf.String(), "all extraction settings now apply") {
		t.Fatalf("clearing did not log the recovery: %s", buf.String())
	}
	buf.Reset()
	lg.Log(nil)
	if buf.Len() != 0 {
		t.Fatalf("repeat clear logged again: %s", buf.String())
	}
}

// TestIgnoredLoggerFirstEmptySetIsSilent: a healthy agent's very first apply
// must not announce anything (there is no fix to report).
func TestIgnoredLoggerFirstEmptySetIsSilent(t *testing.T) {
	var buf bytes.Buffer
	NewIgnoredLogger(captureLogger(&buf)).Log(nil)
	if buf.Len() != 0 {
		t.Fatalf("first empty set logged: %s", buf.String())
	}
}

func TestExtractPolicyAccessors(t *testing.T) {
	tests := []struct {
		name         string
		body         string
		wantEnabled  bool
		wantBody     bool
		wantOCR      bool
		wantMaxBytes int64
	}{
		{
			name:         "absent keys take the never-contacted defaults",
			body:         `{}`,
			wantMaxBytes: DefaultExtractMaxBytes,
		},
		{
			name:         "explicit values win",
			body:         `{"extract_enabled":true,"extract_body_text":true,"extract_ocr":true,"extract_max_bytes":4096}`,
			wantEnabled:  true,
			wantBody:     true,
			wantOCR:      true,
			wantMaxBytes: 4096,
		},
		{
			name:         "zero max bytes is treated as absent, never as unlimited",
			body:         `{"extract_max_bytes":0}`,
			wantMaxBytes: DefaultExtractMaxBytes,
		},
		{
			name:         "negative max bytes is treated as absent",
			body:         `{"extract_max_bytes":-1}`,
			wantMaxBytes: DefaultExtractMaxBytes,
		},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			p := policyFrom(t, tc.body)
			if p.ExtractEnabledValue() != tc.wantEnabled {
				t.Errorf("ExtractEnabledValue = %v, want %v", p.ExtractEnabledValue(), tc.wantEnabled)
			}
			if p.ExtractBodyTextValue() != tc.wantBody {
				t.Errorf("ExtractBodyTextValue = %v, want %v", p.ExtractBodyTextValue(), tc.wantBody)
			}
			if p.ExtractOCRValue() != tc.wantOCR {
				t.Errorf("ExtractOCRValue = %v, want %v", p.ExtractOCRValue(), tc.wantOCR)
			}
			if got := p.ExtractMaxBytesOr(DefaultExtractMaxBytes); got != tc.wantMaxBytes {
				t.Errorf("ExtractMaxBytesOr = %d, want %d", got, tc.wantMaxBytes)
			}
		})
	}
}

// TestExtractKeysRoundTripUnknownContract re-asserts the package invariant for
// the new keys: the RAW body is what persists, so an older central's policy
// without them (and a newer central's policy with extra ones) survives intact.
func TestExtractKeysSurviveRawRoundTrip(t *testing.T) {
	raw := json.RawMessage(`{"extract_enabled":true,"extract_future_key":"x"}`)
	doc := PolicyDoc{Policy: raw}
	pol, err := doc.Parsed()
	if err != nil {
		t.Fatalf("parse: %v", err)
	}
	if !pol.ExtractEnabledValue() {
		t.Fatal("extract_enabled did not parse")
	}
	keys := doc.PolicyKeys()
	if len(keys) != 2 || keys[0] != "extract_enabled" || keys[1] != "extract_future_key" {
		t.Fatalf("unknown key lost from the raw body: %v", keys)
	}
}
