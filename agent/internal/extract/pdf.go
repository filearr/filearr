package extract

import (
	"context"
	"fmt"
	"os"
	"os/exec"
	"path/filepath"
	"strconv"
	"strings"
	"time"
	"unicode/utf8"
)

// PDF extraction, mirroring central's documents.py extract_pdf / _pdf_body and
// ocr.py run_ocr — but with a DIFFERENT engine on purpose.
//
// Central links pypdf in-process. The agent does not: the parity design's rule
// is that heavy capabilities are HOST TOOLS (see the package doc), so a PDF is
// read through poppler-utils — pdfinfo for properties, pdftotext for the text
// layer, pdftoppm for the rasterisation a scanned-page OCR needs. The three
// binaries are detected independently (Options.PDFInfoPath / PDFToTextPath /
// PDFToPPMPath) because a host can ship a partial poppler, and an absent binary
// is an absent capability, never an error.
//
// The engine difference is invisible in the OUTPUT: every key written here is
// the key extract_pdf writes, with the same types and the same omit-when-empty
// rule, so an agent-extracted PDF and a centrally-extracted one are the same
// object.

const (
	// pdfInfoTimeout / pdfTextTimeout / pdfInfoMaxOutputBytes are AGENT-SIDE
	// choices, not mirrored central values: documents.py runs pypdf in-process
	// and therefore has no subprocess budget to copy. They are sized against
	// this package's existing subprocess posture — FFprobeTimeout (30s) for the
	// cheap metadata read, ocr_timeout_s (120s, see OCRTimeout) for the
	// expensive whole-document text pass, and an output ceiling one order below
	// FFprobeMaxOutputBytes because pdfinfo emits ~20 short lines and anything
	// approaching a megabyte is a hostile document, not a verbose one.
	pdfInfoTimeout        = 30 * time.Second
	pdfTextTimeout        = 120 * time.Second
	pdfInfoMaxOutputBytes = 1 << 20

	// pdfStderrMaxBytes bounds the diagnostic buffer. ffprobe.go can use an
	// unbounded bytes.Buffer because `-v error` silences the child; poppler is
	// noisier (pdfinfo has no quiet switch we rely on — see pdfProperties), so
	// the error channel gets its own ceiling rather than a promise of brevity.
	pdfStderrMaxBytes = 8 << 10

	// pdfProbeMaxOutputBytes bounds the first-page text probe ocrPDF runs purely
	// to MEASURE the native text layer. It only has to distinguish "under
	// OCRMinTextChars" from "over it", so 64 KiB is already far more than the
	// decision can consume.
	pdfProbeMaxOutputBytes = 64 << 10

	// ocrPageMaxBytes refuses a single rasterised page image larger than this
	// before it is handed to tesseract. A 200-DPI letter page is a few MiB; a
	// PDF declaring an absurd MediaBox can make pdftoppm write far more, and the
	// bytes are both disk we allocated and memory tesseract would map. Sized at
	// MemberReadMax, the ceiling this package already applies to any single
	// decompressed blob.
	ocrPageMaxBytes int64 = MemberReadMax
)

// pdfInfoStringKeys maps pdfinfo's output labels onto central's key vocabulary
// (documents.py extract_pdf reads the same five DocumentInformation fields).
// Kept as an ordered slice rather than a map so the emission order — and thus
// any future ordering-sensitive diff of two results — is deterministic.
var pdfInfoStringKeys = [][2]string{
	{"title", "Title"},
	{"author", "Author"},
	{"subject", "Subject"},
	{"creator", "Creator"},
	{"producer", "Producer"},
}

// extractPDF fills central's PDF vocabulary: encrypted, pages, title, author,
// subject, creator, producer, created, modified (+ body_text/body_text_truncated
// when the body pass is enabled).
//
// The properties pass and the body pass are INDEPENDENT, exactly as they are in
// central (extract_pdf and _pdf_body are separate calls with separate failure
// handling). Both run; if only one fails, its error is returned AFTER the other
// has stored its keys, so a PDF whose text layer defeats pdftotext still lands
// its page count and title.
func extractPDF(ctx context.Context, path string, opts Options, res *Result) error {
	if opts.PDFInfoPath == "" && opts.PDFToTextPath == "" {
		return nil // no poppler on this host: the capability is simply absent
	}

	var propErr, bodyErr error
	locked := false
	if opts.PDFInfoPath != "" {
		locked, propErr = pdfProperties(ctx, path, opts, res)
	}
	// A PDF that stays locked under the empty password yields NO body text —
	// _pdf_body returns ("", False) for exactly this case, and reading text out
	// of a document we could not open would be the agent claiming a capability
	// central deliberately declines.
	if opts.BodyText && opts.PDFToTextPath != "" && !locked {
		bodyErr = pdfBodyText(ctx, path, opts, res)
	}

	switch {
	case propErr != nil && bodyErr != nil:
		return fmt.Errorf("%v; %v", propErr, bodyErr)
	case propErr != nil:
		return propErr
	}
	return bodyErr
}

// pdfProperties runs pdfinfo and writes the property keys. It reports locked=true
// when the document is encrypted AND could not be opened, which is central's
// "return only {encrypted: true} and stop" branch.
func pdfProperties(ctx context.Context, path string, opts Options, res *Result) (bool, error) {
	cctx, cancel := context.WithTimeout(ctx, pdfInfoTimeout)
	defer cancel()

	// `-enc UTF-8` is pdfinfo's documented text-output encoding switch; naming it
	// explicitly means a poppler built with a different default still hands us
	// the encoding we decode as. No `-q`: pdfinfo's stderr is what tells us a
	// document is password-locked (see isPDFPasswordError), and silencing it
	// would turn that verdict into an anonymous exit status.
	argv := []string{"-enc", "UTF-8", popplerPath(path)}
	cmd := exec.CommandContext(cctx, opts.PDFInfoPath, argv...)
	out := &capBuffer{limit: pdfInfoMaxOutputBytes}
	stderr := &capBuffer{limit: pdfStderrMaxBytes}
	cmd.Stdout = out
	cmd.Stderr = stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return false, fmt.Errorf("pdfinfo timed out after %s", pdfInfoTimeout)
		}
		// pypdf reports is_encrypted before it fails; poppler just refuses the
		// document. Recognising that refusal is what keeps the two sides
		// agreeing that an unopenable encrypted PDF is {encrypted: true}, not an
		// extraction error.
		if isPDFPasswordError(stderr.buf.String()) {
			res.Meta["encrypted"] = true
			return true, nil
		}
		if msg := lastLine(stderr.buf.String()); msg != "" {
			return false, fmt.Errorf("pdfinfo failed: %s", msg)
		}
		return false, fmt.Errorf("pdfinfo failed: %w", err)
	}

	return applyPDFInfo(parsePDFInfo(out.buf.String()), res), nil
}

// applyPDFInfo writes central's key vocabulary from parsed pdfinfo fields and
// reports whether the document is locked. Pure over the parsed map so the whole
// key contract is testable without poppler installed.
func applyPDFInfo(fields map[string]string, res *Result) bool {
	encRaw, hasEnc := fields["Encrypted"]
	encrypted := hasEnc && pdfInfoEncrypted(encRaw)
	if hasEnc {
		// extract_pdf stores `encrypted` unconditionally (True or False), so the
		// key is written whenever pdfinfo stated a value — not only for the
		// interesting case.
		res.Meta["encrypted"] = encrypted
	}

	pages, hasPages := pdfInfoInt(fields["Pages"])
	if encrypted && !hasPages {
		// Encrypted with no readable page tree is central's locked branch: the
		// result is {encrypted: true} and NOTHING else, because pypdf would raise
		// FileNotDecrypted on the first metadata access.
		return true
	}
	if hasPages {
		res.Meta["pages"] = pages
	}
	for _, kv := range pdfInfoStringKeys {
		res.set(kv[0], fields[kv[1]]) // res.set drops blanks, like _clean_str
	}
	// Dates: the ISO string when the printed date parses, the RAW string when it
	// does not. That fallback is central's, and its reason is central's too —
	// integrity over tidiness, keep what the file states (extract_pdf's
	// creation_date_raw / modification_date_raw branch).
	if v := pdfInfoDate(fields["CreationDate"]); v != "" {
		res.Meta["created"] = v
	}
	if v := pdfInfoDate(fields["ModDate"]); v != "" {
		res.Meta["modified"] = v
	}
	return false
}

// parsePDFInfo turns pdfinfo's `Key: value` report into a map. Untrusted input:
// a line without a colon is skipped rather than guessed at, a repeated key keeps
// the FIRST occurrence (pdfinfo prints the document's own values before its
// derived ones), and values are taken verbatim for the caller to clean.
func parsePDFInfo(out string) map[string]string {
	fields := map[string]string{}
	for _, line := range strings.Split(out, "\n") {
		line = strings.TrimRight(line, "\r") // poppler on Windows emits CRLF
		key, value, found := strings.Cut(line, ":")
		if !found {
			continue
		}
		key = strings.TrimSpace(key)
		if key == "" {
			continue
		}
		if _, exists := fields[key]; exists {
			continue
		}
		fields[key] = strings.TrimSpace(value)
	}
	return fields
}

// pdfInfoEncrypted reads pdfinfo's `Encrypted:` line, which is either "no" or
// "yes (print:yes copy:no …)". Anything that is not the literal "no" prefix is
// encryption — an unparseable permission suffix must never downgrade the answer.
func pdfInfoEncrypted(value string) bool {
	return !strings.HasPrefix(strings.ToLower(strings.TrimSpace(value)), "no")
}

// pdfInfoInt parses a non-negative integer field, returning ok=false for
// anything else (a hostile or truncated report must not produce a negative page
// count that later gates read as "small").
func pdfInfoInt(value string) (int, bool) {
	n, err := strconv.Atoi(strings.TrimSpace(value))
	if err != nil || n < 0 {
		return 0, false
	}
	return n, true
}

// pdfInfoDateLayouts are the shapes pdfinfo's date lines are seen in. The first
// is poppler's own default (it formats the parsed PDF date with ctime(3), so the
// day is space-padded — Go's `_2`); the ISO-ish forms cover poppler builds that
// print ISO dates; the trailing `D:`-stripped forms cover the case where poppler
// gives up converting and echoes the PDF's raw date object.
var pdfInfoDateLayouts = []string{
	"Mon Jan _2 15:04:05 2006",
	"Mon Jan _2 15:04:05 2006 MST",
	time.RFC3339,
	"2006-01-02T15:04:05",
	"2006-01-02 15:04:05",
	"2006-01-02",
	"20060102150405",
	"20060102",
}

// pdfInfoDate coerces a printed date to ISO-8601, falling back to the cleaned
// raw string when no layout matches. This is _iso()'s contract: a datetime
// becomes isoformat(), and an unparseable value is kept verbatim rather than
// dropped.
func pdfInfoDate(raw string) string {
	s := cleanStr(raw)
	if s == "" {
		return ""
	}
	// A PDF date object is "D:YYYYMMDDHHmmSSOHH'mm'"; strip the marker and the
	// timezone tail so the numeric layouts below can see the date itself.
	candidate := s
	if rest, ok := strings.CutPrefix(candidate, "D:"); ok {
		candidate = rest
		if i := strings.IndexAny(candidate, "+-Zz"); i > 0 {
			candidate = candidate[:i]
		}
	}
	for _, layout := range pdfInfoDateLayouts {
		if t, err := time.Parse(layout, candidate); err == nil {
			return t.Format("2006-01-02T15:04:05")
		}
	}
	return s
}

// isPDFPasswordError recognises poppler's refusal to open an encrypted document.
// Matching on the message is unavoidable: poppler collapses several conditions
// onto exit status 1, so the status alone cannot separate "locked" from "corrupt".
func isPDFPasswordError(stderr string) bool {
	l := strings.ToLower(stderr)
	return strings.Contains(l, "incorrect password") || strings.Contains(l, "encrypted")
}

// pdfBodyText streams the document's text layer through pdftotext.
//
// `-q` suppresses poppler's per-object syntax warnings (the same noise-control
// intent as ffprobe's `-v error`), `-enc UTF-8` fixes the output encoding,
// `-nopgbrk` drops the U+000C page separators that would only be collapsed to
// spaces by normalizeBodyText anyway, and the final `-` writes to stdout instead
// of a temp file we would have to create, bound and delete.
func pdfBodyText(ctx context.Context, path string, opts Options, res *Result) error {
	cctx, cancel := context.WithTimeout(ctx, pdfTextTimeout)
	defer cancel()

	argv := []string{"-q", "-enc", "UTF-8", "-nopgbrk", popplerPath(path), "-"}
	cmd := exec.CommandContext(cctx, opts.PDFToTextPath, argv...)
	// Same ceiling as the OCR read: the stored-character cap times UTF-8's worst
	// case. Past it the text would be truncated anyway, so buffering more of a
	// thousand-page document buys nothing.
	out := &capBuffer{limit: opts.MaxBodyChars*4 + 8}
	stderr := &capBuffer{limit: pdfStderrMaxBytes}
	cmd.Stdout = out
	cmd.Stderr = stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return fmt.Errorf("pdftotext timed out after %s", pdfTextTimeout)
		}
		if msg := lastLine(stderr.buf.String()); msg != "" {
			return fmt.Errorf("pdftotext failed: %s", msg)
		}
		// `-q` means an ordinary failure usually arrives with an empty stderr;
		// the exit status is then the whole diagnosis, which is the price of not
		// buffering a warning storm from a malformed document.
		return fmt.Errorf("pdftotext failed: %w", err)
	}

	res.setBody(normalizeBodyText(out.buf.String(), opts.MaxBodyChars, out.overflowed))
	return nil
}

// ocrPDF OCRs a SCANNED PDF: rasterise the first pages with pdftoppm, run
// tesseract over each page image, concatenate. It is the PDF half of central's
// ocr_run.ocr_metadata + ocr.run_ocr(is_pdf=True), and it writes the same
// ocr_text / ocr_text_truncated keys ocrImage writes for images.
func ocrPDF(ctx context.Context, path string, opts Options, res *Result) error {
	// pdftoppm rasterises and tesseract reads; without BOTH there is no scanned
	// -PDF capability on this host. A silent skip, like every absent tool.
	if opts.PDFToPPMPath == "" || opts.TesseractPath == "" {
		return nil
	}

	// The should_ocr gate (ocr.py), in central's order: native text first, then
	// the page ceiling. Central's third gate — max_pixels — is not evaluated
	// here for the same reason it is not evaluated centrally: ocr_run._pixels
	// reads width/height, which a PDF item does not carry. It reappears per
	// rasterised page below, which is what ocr.py's docstring means by
	// "rasterised-page pixel count".
	if pdfNativeTextChars(ctx, path, opts, res) >= opts.OCRMinTextChars {
		return nil // a usable text layer already exists; OCR would only be worse
	}
	if pages, ok := pdfPageCount(res); ok && pages > opts.OCRMaxPages {
		return nil // over the page ceiling this document is not OCR'd at all
	}

	// rasterize_pdf's temp dir, same prefix so the two sides leave recognisably
	// identical debris behind if a process is killed mid-pass.
	tmp, err := os.MkdirTemp("", "filearr-ocr-")
	if err != nil {
		return fmt.Errorf("cannot create OCR scratch directory: %w", err)
	}
	defer os.RemoveAll(tmp)

	pages, err := rasterizePDF(ctx, path, tmp, opts)
	if err != nil {
		return err
	}

	// Page loop mirrors run_ocr: accumulate page texts, stop as soon as the
	// running length reaches the char cap, join with "\n". A page that defeats
	// tesseract fails the whole OCR (run_ocr propagates OcrError) rather than
	// silently yielding a partial document.
	var parts []string
	total := 0
	hardStopped := false
	for _, page := range pages {
		if err := ctx.Err(); err != nil {
			return err
		}
		text, overflowed, err := ocrPage(ctx, page, opts, opts.MaxOCRChars*4+8)
		if err != nil {
			return err
		}
		if text != "" {
			parts = append(parts, text)
			total += utf8.RuneCountInString(text)
		}
		if overflowed || total >= opts.MaxOCRChars {
			hardStopped = true
			break
		}
	}

	text, truncated := normalizeBodyText(strings.Join(parts, "\n"), opts.MaxOCRChars, hardStopped)
	if text == "" {
		return nil // a blank scan is a legitimate result, not a failure
	}
	res.Meta["ocr_text"] = text
	res.Meta["ocr_text_truncated"] = truncated
	return nil
}

// rasterizePDF renders the first opts.OCRMaxPages pages to PNGs in dir and
// returns them in filename order. The argv is ocr.py rasterize_pdf's, flag for
// flag: `pdftoppm -png -r <dpi> -f 1 -l <max_pages> <pdf> <dir>/page`.
func rasterizePDF(ctx context.Context, path, dir string, opts Options) ([]string, error) {
	cctx, cancel := context.WithTimeout(ctx, opts.OCRTimeout)
	defer cancel()

	argv := []string{
		"-png",
		"-r", strconv.Itoa(opts.OCRDPI),
		"-f", "1",
		"-l", strconv.Itoa(opts.OCRMaxPages),
		popplerPath(path),
		filepath.Join(dir, "page"),
	}
	cmd := exec.CommandContext(cctx, opts.PDFToPPMPath, argv...)
	stderr := &capBuffer{limit: pdfStderrMaxBytes}
	cmd.Stderr = stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return nil, fmt.Errorf("pdftoppm timed out after %s", opts.OCRTimeout)
		}
		if msg := lastLine(stderr.buf.String()); msg != "" {
			return nil, fmt.Errorf("pdftoppm failed: %s", msg)
		}
		return nil, fmt.Errorf("pdftoppm failed: %w", err)
	}

	entries, err := os.ReadDir(dir) // already sorted by filename
	if err != nil {
		return nil, fmt.Errorf("cannot list rasterised pages: %w", err)
	}
	var out []string
	for _, e := range entries {
		if e.IsDir() || !strings.EqualFold(filepath.Ext(e.Name()), ".png") {
			continue
		}
		p := filepath.Join(dir, e.Name())
		if !rasterPageUsable(p) {
			continue // over a ceiling; skipping one page beats OOMing the scan
		}
		out = append(out, p)
		if len(out) >= opts.OCRMaxPages {
			break // pdftoppm was already bounded by -l; this is the belt to that brace
		}
	}
	if len(out) == 0 {
		// run_ocr treats "no page images" as an OcrError, so an operator sees
		// that OCR was attempted and produced nothing usable.
		return nil, fmt.Errorf("pdftoppm produced no usable page images")
	}
	return out, nil
}

// rasterPageUsable applies the two ceilings a rasterised page must clear before
// tesseract sees it: the on-disk byte cap, and OCRMaxPixels — the same declared
// -pixel gate ocrImage applies to a source image, here against the page poppler
// actually produced. Both are cheap (a stat and a header read); a page that
// fails either is skipped, never fatal.
func rasterPageUsable(path string) bool {
	info, err := os.Stat(path)
	if err != nil || info.Size() > ocrPageMaxBytes {
		return false
	}
	if w, h, err := imageDimensions(path); err == nil && int64(w)*int64(h) > OCRMaxPixels {
		return false
	}
	return true
}

// ocrPage runs ONE tesseract subprocess over a rasterised page. The argv is
// ocrImage's, which is ocr.py run_tesseract's: `<img> stdout -l <lang> --psm 3`.
// It returns the raw text plus whether the read hit its ceiling, so the caller
// can propagate a truncation the way normalizeBodyText expects.
func ocrPage(ctx context.Context, img string, opts Options, limit int) (string, bool, error) {
	cctx, cancel := context.WithTimeout(ctx, opts.OCRTimeout)
	defer cancel()

	argv := []string{img, "stdout", "-l", opts.OCRLang, "--psm", OCRPSM}
	cmd := exec.CommandContext(cctx, opts.TesseractPath, argv...)
	out := &capBuffer{limit: limit}
	stderr := &capBuffer{limit: pdfStderrMaxBytes}
	cmd.Stdout = out
	cmd.Stderr = stderr

	if err := cmd.Run(); err != nil {
		if cctx.Err() == context.DeadlineExceeded {
			return "", false, fmt.Errorf("tesseract timed out after %s", opts.OCRTimeout)
		}
		if msg := lastLine(stderr.buf.String()); msg != "" {
			return "", false, fmt.Errorf("tesseract failed: %s", msg)
		}
		return "", false, fmt.Errorf("tesseract failed: %w", err)
	}
	return out.buf.String(), out.overflowed, nil
}

// pdfNativeTextChars measures the document's existing text layer for the
// should_ocr gate, in CHARACTERS (central compares len() of a Python str, which
// counts code points).
//
// When the body pass already ran, its result IS the measurement — free, and
// exactly what ocr_run._native_text_len reads. When it did not (body text is off
// by policy) the length is probed with one bounded pdftotext run whose output is
// deliberately thrown away: it is gate input, not a result, and storing it would
// smuggle body text past the policy that disabled it. A probe failure counts as
// zero rather than raising, mirroring central's behaviour when nothing extracted
// native text — and it must not be recorded as an OCR error, because the OCR it
// gates may still succeed.
//
// DIVERGENCE, deliberate: the probe reads only the FIRST page (`-l 1`), so a PDF
// whose page 1 is a scanned cover over a real text body is OCR'd here and not
// centrally. Measuring the whole document would cost a second full text pass on
// every scanned PDF; a first-page sample is the cheap proxy, and over-OCRing is
// the harmless direction of the error.
func pdfNativeTextChars(ctx context.Context, path string, opts Options, res *Result) int {
	if res.BodyText != "" {
		return utf8.RuneCountInString(res.BodyText)
	}
	if opts.PDFToTextPath == "" {
		return 0 // nothing can read the text layer, so treat it as absent
	}

	cctx, cancel := context.WithTimeout(ctx, pdfTextTimeout)
	defer cancel()

	argv := []string{"-q", "-enc", "UTF-8", "-nopgbrk", "-l", "1", popplerPath(path), "-"}
	cmd := exec.CommandContext(cctx, opts.PDFToTextPath, argv...)
	out := &capBuffer{limit: pdfProbeMaxOutputBytes}
	cmd.Stdout = out
	cmd.Stderr = &capBuffer{limit: pdfStderrMaxBytes}
	if err := cmd.Run(); err != nil {
		return 0
	}
	// Whitespace-only output is not a text layer: an empty scanned page prints
	// newlines, and counting those would suppress the OCR that page needs.
	return utf8.RuneCountInString(strings.TrimSpace(out.buf.String()))
}

// pdfPageCount reads the page count the properties pass stored. Typed loosely
// because Meta is a wire object other extractors also write into.
func pdfPageCount(res *Result) (int, bool) {
	switch v := res.Meta["pages"].(type) {
	case int:
		return v, true
	case int64:
		return int(v), true
	case float64:
		return int(v), true
	}
	return 0, false
}

// popplerPath keeps an untrusted filename from being read as an option. poppler's
// argument parser has no `--` terminator (ffprobe does, see runFFprobe), so a
// RELATIVE path beginning with "-" is disarmed with an explicit "./" prefix,
// which cannot change the meaning of any other path — and an absolute path, what
// the walk actually hands us, can never begin with "-".
func popplerPath(p string) string {
	if strings.HasPrefix(p, "-") {
		return "." + string(filepath.Separator) + p
	}
	return p
}
