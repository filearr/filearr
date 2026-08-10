package config

import (
	"log/slog"
	"strings"
	"sync"
)

// IgnoredSetting is one effective policy key this agent will NOT honour, with
// the operator-actionable reason why. It exists because the parity design
// deliberately degrades PER CAPABILITY rather than per build: an agent whose
// host lacks tesseract still runs everything else, and the only unacceptable
// outcome is that nobody can tell. Central's console renders the same list, so
// an operator never has to shell into a machine to answer "what is this agent
// actually doing".
type IgnoredSetting struct {
	Key    string `json:"key"`
	Reason string `json:"reason"`
}

// ExtractCapabilities is the slice of the agent's capability advertisement the
// ignored-setting rules evaluate against (a plain struct, not the inventory
// package's map, so internal/config stays free of that dependency and the rules
// stay unit-testable with hand-written inputs).
type ExtractCapabilities struct {
	// Extract is whether this BUILD carries the extraction pass at all.
	Extract bool
	// FFprobe / Tesseract are host-tool presence on THIS machine.
	FFprobe   bool
	Tesseract bool
	// Exiftool backs the deep-EXIF pass over images (camera/lens/exposure/GPS).
	Exiftool bool
	// PDFInfo / PDFToText / PDFToPPM are the poppler-utils binaries: PDF
	// properties, PDF body text, and page rasterisation for scanned-PDF OCR.
	// Separate flags because a partial poppler install is a real configuration
	// and "PDFs work here" is only true per capability.
	PDFInfo   bool
	PDFToText bool
	PDFToPPM  bool
}

// IgnoredSettings computes, in a stable order, every extraction policy key p
// sets that caps cannot satisfy. Rules:
//
//  1. extract_enabled true on a build with no extraction pass — nothing runs.
//  2. any extract_* sub-key SET while extract_enabled is false/absent — the
//     master switch gates them, so they are inert. Only keys the policy actually
//     specifies are reported; a defaulted key is not an ignored setting.
//  3. a tool-backed opt-in (extract_ocr, extract_exif) true with the tool absent.
//  4. extract_enabled true with no ffprobe — extraction still runs, but the
//     technical probe (video codec/resolution/duration, audio duration/bitrate/
//     samplerate/channels) is skipped. Reported as a PARTIAL non-application,
//     which is the honest thing an operator needs to see.
func IgnoredSettings(p Policy, caps ExtractCapabilities) []IgnoredSetting {
	var out []IgnoredSetting
	add := func(key, reason string) { out = append(out, IgnoredSetting{Key: key, Reason: reason}) }

	enabled := p.ExtractEnabledValue()

	if enabled && !caps.Extract {
		add("extract_enabled", "this agent build has no extraction pass (upgrade the agent)")
		return out // every sub-key is moot; one clear line beats four
	}

	if !enabled {
		// Report in a fixed order so the fingerprint below is stable.
		for _, sub := range []struct {
			key string
			set bool
		}{
			{"extract_body_text", p.ExtractBodyText != nil},
			{"extract_ocr", p.ExtractOCR != nil},
			{"extract_exif", p.ExtractEXIF != nil},
			{"extract_max_bytes", p.ExtractMaxBytes != nil},
		} {
			if sub.set {
				add(sub.key, "extract_enabled is false — the extraction pass never runs (set extract_enabled to use this)")
			}
		}
		return out
	}

	if p.ExtractOCRValue() && !caps.Tesseract {
		add("extract_ocr", "no tesseract on this host (install it or unset extract_ocr)")
	}
	if p.ExtractOCRValue() && caps.Tesseract && !caps.PDFToPPM {
		add("extract_ocr", "no pdftoppm (poppler-utils) on this host — scanned PDFs are not OCR'd; images still are (install poppler-utils)")
	}
	if !caps.FFprobe {
		add("extract_enabled", "no ffprobe on this host — video/audio technical probe (codec, resolution, duration, bitrate) is skipped; the rest of extraction still runs (install ffmpeg/ffprobe)")
	}
	if p.ExtractEXIFValue() && !caps.Exiftool {
		add("extract_exif", "no exiftool on this host (install it or unset extract_exif)")
	}
	if !caps.PDFInfo {
		add("extract_enabled", "no pdfinfo (poppler-utils) on this host — PDF page count and document properties are skipped (install poppler-utils)")
	}
	// Only worth saying when body text was actually asked for: without
	// extract_body_text, pdftotext would not be run for text anyway.
	if p.ExtractBodyTextValue() && !caps.PDFToText {
		add("extract_body_text", "no pdftotext (poppler-utils) on this host — PDF body text is skipped; other document formats still extract text (install poppler-utils)")
	}
	return out
}

// IgnoredLogger logs an ignored-setting set at WARN ONCE per distinct change,
// not once per poll. The policy poller re-applies on every version bump and the
// scan process starts fresh constantly; without the fingerprint an unfixable
// condition (no tesseract on a NAS) would fill the log forever and drown the
// signal it is meant to carry.
//
// Zero value is not usable — build one with NewIgnoredLogger. Safe for
// concurrent use.
type IgnoredLogger struct {
	log *slog.Logger

	mu   sync.Mutex
	seen bool   // whether a fingerprint has ever been recorded
	last string // fingerprint of the last-logged set
}

// NewIgnoredLogger returns a logger that suppresses repeats of an unchanged set.
func NewIgnoredLogger(log *slog.Logger) *IgnoredLogger {
	return &IgnoredLogger{log: log}
}

// Log emits one WARN line per ignored setting when the set differs from the
// last one logged. A transition back to "nothing ignored" logs a single INFO so
// an operator who installs the missing tool sees the fix land.
func (l *IgnoredLogger) Log(items []IgnoredSetting) {
	if l == nil || l.log == nil {
		return
	}
	fp := fingerprint(items)

	l.mu.Lock()
	unchanged := l.seen && fp == l.last
	hadPrevious := l.seen && l.last != ""
	l.last = fp
	l.seen = true
	l.mu.Unlock()

	if unchanged {
		return
	}
	if len(items) == 0 {
		if hadPrevious {
			l.log.Info("policy: all extraction settings now apply")
		}
		return
	}
	for _, it := range items {
		// Matches the established convention for a policy value the agent
		// declines to honour (cf. "scan scheduler: invalid scan cron ignored").
		l.log.Warn("policy: "+it.Key+" ignored", "reason", it.Reason)
	}
}

// fingerprint renders a set of ignored settings into a comparable string. Order
// is already deterministic (IgnoredSettings evaluates rules in a fixed order),
// so no sort is needed and the reason text is part of the identity — a changed
// reason for the same key IS a change worth re-logging.
func fingerprint(items []IgnoredSetting) string {
	if len(items) == 0 {
		return ""
	}
	parts := make([]string, 0, len(items))
	for _, it := range items {
		parts = append(parts, it.Key+"="+it.Reason)
	}
	return strings.Join(parts, "\n")
}
