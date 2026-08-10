package extract

import (
	"strings"
	"time"
	"unicode/utf8"
)

// Resource ceilings, every one of them copied from central so an agent-extracted
// item and a centrally-extracted item are bounded IDENTICALLY. Drift here is a
// parity bug, not a tuning choice: cite the central symbol when you change one.
const (
	// BodyTextMaxChars mirrors backend/filearr/config.py Settings.body_text_max_chars
	// (= 100_000, also documents.py DEFAULT_BODY_TEXT_MAX_CHARS). It is the cap on
	// the text STORED in metadata_.body_text — central's SMALLER
	// body_text_index_chars cap applies later, when build_doc projects into Meili,
	// and is none of the agent's business.
	BodyTextMaxChars = 100_000

	// OCRMaxChars mirrors config.py Settings.ocr_max_chars (= 100_000): the cap on
	// OCR text stored in metadata_ (index-bloat + row-size control).
	OCRMaxChars = 100_000

	// DecompressedMax is the zip-bomb total-uncompressed ceiling checked against
	// the central directory BEFORE any member is read. It mirrors the EFFECTIVE
	// central value, config.py Settings.doc_decompressed_max (= 1 GiB), not the
	// stale 200 MiB module default in documents.py — central raised it on
	// 2026-07-24 after a legitimate ~954 MiB-declared docx was rejected live, and
	// an agent that rejects what central accepts is a parity bug.
	DecompressedMax int64 = 1_073_741_824

	// DecompressionRatio / DecompressionRatioMinBytes mirror
	// config.py doc_decompression_ratio (100:1) and
	// doc_decompression_ratio_min_bytes (10 MiB). The ratio gate is what actually
	// catches a bomb; the min-bytes floor is what stops an ordinary tiny,
	// highly-compressible office file from being falsely rejected.
	DecompressionRatio               = 100.0
	DecompressionRatioMinBytes int64 = 10_485_760

	// MemberReadMax bounds the decompressed bytes pulled from ANY SINGLE zip
	// member (word/document.xml, content.xml, …). The central-directory guard is
	// a DECLARED-size check and a crafted member can lie, so the streaming read
	// carries its own hard ceiling. Sized from config.py archive_scan_max_bytes
	// (64 MiB) — the same order of magnitude central allows itself to stream.
	MemberReadMax int64 = 67_108_864

	// ArchiveMaxMembers / ArchiveMembersStored mirror config.py
	// archive_max_members (10_000 — the enumeration cap) and
	// archive_members_stored (1_000 — the {name,size} entries persisted).
	ArchiveMaxMembers    = 10_000
	ArchiveMembersStored = 1_000

	// ArchiveScanMaxBytes mirrors config.py archive_scan_max_bytes (64 MiB): the
	// COMPRESSED-stream ceiling for tar listing, past which enumeration stops
	// cleanly and the listing is marked truncated. tar has no central directory,
	// so this bound plus the member-count cap are the only bomb protection.
	ArchiveScanMaxBytes int64 = 67_108_864

	// MemberNameCap mirrors archives.py _MEMBER_NAME_CAP (1024): a hostile
	// archive can declare arbitrarily long member names, and the name is a
	// display/search STRING that is never resolved as a path.
	MemberNameCap = 1024

	// FFprobeTimeout / FFprobeMaxOutputBytes mirror config.py's ffprobe posture
	// (tasks/ffprobe.py probe(): timeout_s=30.0, max_output_bytes=8_388_608).
	FFprobeTimeout        = 30 * time.Second
	FFprobeMaxOutputBytes = 8_388_608

	// OCRTimeout / OCRLang / OCRPSM mirror config.py ocr_timeout_s (120s),
	// ocr_lang ("eng") and ocr.py _PSM ("3" — fully automatic page segmentation,
	// tesseract's own default).
	OCRTimeout = 120 * time.Second
	OCRLang    = "eng"
	OCRPSM     = "3"

	// OCRMaxPixels mirrors config.py ocr_max_pixels (~40 MP): an image declaring
	// more pixels than this is not handed to tesseract at all.
	OCRMaxPixels = 40_000_000

	// OCRDPI / OCRMaxPages / OCRMinTextChars mirror config.py ocr_dpi (200),
	// ocr_max_pages (10) and ocr_min_text_chars (100) — the scanned-PDF rules of
	// ocr.should_ocr. MinTextChars is the "cheap native text first" gate every
	// PDF-touching tool applies: a PDF whose pdftotext layer already yields that
	// many characters is NOT rasterised, because OCR would only produce a worse
	// copy of text we already have.
	OCRDPI          = 200
	OCRMaxPages     = 10
	OCRMinTextChars = 100
)

// normalizeBodyText sanitises, whitespace-normalises and char-caps untrusted
// text. It is a behavioural port of documents.py _normalize_body_text: C0/C1
// control characters are dropped (the whitespace controls collapse to a space),
// whitespace runs collapse to one space, ends are trimmed, and the result is
// truncated to maxChars CHARACTERS (runes — central counts Python str length,
// which is code points). hardStopped propagates a truncation the CALLER already
// caused (a read/member ceiling) into the returned flag.
func normalizeBodyText(raw string, maxChars int, hardStopped bool) (string, bool) {
	var b strings.Builder
	b.Grow(len(raw))
	for _, ch := range raw {
		switch {
		case ch == '\t' || ch == '\n' || ch == '\v' || ch == '\f' || ch == '\r':
			b.WriteRune(' ')
		case ch < 0x20 || (ch >= 0x7F && ch <= 0x9F):
			// strip other control chars (ANSI/NUL/etc.) — untrusted content
		default:
			b.WriteRune(ch)
		}
	}
	collapsed := strings.TrimSpace(collapseSpaces(b.String()))
	n := utf8.RuneCountInString(collapsed)
	truncated := hardStopped || n > maxChars
	if n > maxChars {
		collapsed = strings.TrimRight(truncateRunes(collapsed, maxChars), " ")
	}
	return collapsed, truncated
}

// collapseSpaces reduces every run of whitespace to a single space (the Go
// equivalent of central's re.sub(r"\s+", " ", …); the control pass above already
// turned tabs/newlines into spaces).
func collapseSpaces(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	prevSpace := false
	for _, ch := range s {
		if ch == ' ' || ch == 0x85 || ch == 0xA0 || isUnicodeSpace(ch) {
			if !prevSpace {
				b.WriteByte(' ')
			}
			prevSpace = true
			continue
		}
		prevSpace = false
		b.WriteRune(ch)
	}
	return b.String()
}

// isUnicodeSpace covers the non-ASCII separators Python's \s matches on str
// (ideographic space, the EN/EM quad family, …) so a CJK document normalises the
// same on both sides.
func isUnicodeSpace(ch rune) bool {
	switch {
	case ch == 0x1680, ch == 0x2028, ch == 0x2029, ch == 0x202F, ch == 0x205F, ch == 0x3000:
		return true
	case ch >= 0x2000 && ch <= 0x200A:
		return true
	}
	return false
}

// stripControl drops C0/C1 control characters without touching anything else —
// the archives.py _clean_member_name discipline, used for member names and for
// recorded error messages (both reach the search index and the UI).
func stripControl(s string) string {
	var b strings.Builder
	b.Grow(len(s))
	for _, ch := range s {
		if ch < 0x20 || (ch >= 0x7F && ch <= 0x9F) {
			continue
		}
		b.WriteRune(ch)
	}
	return b.String()
}

// truncateRunes cuts s to at most n runes (never splitting a code point).
func truncateRunes(s string, n int) string {
	if n <= 0 {
		return ""
	}
	count := 0
	for i := range s {
		if count == n {
			return s[:i]
		}
		count++
	}
	return s
}

// cleanStr trims and control-strips a metadata string value, returning "" for
// anything that normalises to nothing (mirrors documents.py _clean_str, which
// stores None rather than an empty string).
func cleanStr(s string) string {
	return strings.TrimSpace(stripControl(s))
}
