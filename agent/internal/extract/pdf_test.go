package extract

import (
	"context"
	"os"
	"os/exec"
	"path/filepath"
	"runtime"
	"strings"
	"testing"
)

// The PDF path is exercised WITHOUT poppler installed: neither CI nor the
// developer box has pdfinfo/pdftotext/pdftoppm/tesseract. Parity is therefore
// pinned where it actually lives — in the pure parsing/gating functions — and
// the subprocess shell around them is covered by fake binaries this file
// compiles itself (see buildFakePoppler), never by the real tools.

// --- pdfinfo report parsing -------------------------------------------------

// A realistic pdfinfo report, including the noise a real one carries: a value
// containing colons (the date), a blank value, and a key with a space in it.
const samplePDFInfo = `Title:          Quarterly Report
Subject:
Author:         A. Nonymous
Creator:        LaTeX with hyperref
Producer:       pdfTeX-1.40.25
CreationDate:   Tue Jan  2 15:04:05 2024
ModDate:        Wed Jan  3 09:30:00 2024
Custom Metadata: no
Tagged:         no
Pages:          42
Encrypted:      no
Page size:      612 x 792 pts (letter)
PDF version:    1.5
`

func TestParsePDFInfo(t *testing.T) {
	fields := parsePDFInfo(samplePDFInfo + "a line with no separator at all\n\r\n")

	for key, want := range map[string]string{
		"Title":        "Quarterly Report",
		"Subject":      "",
		"Author":       "A. Nonymous",
		"CreationDate": "Tue Jan  2 15:04:05 2024",
		"Pages":        "42",
		"Encrypted":    "no",
		"Page size":    "612 x 792 pts (letter)",
	} {
		if got := fields[key]; got != want {
			t.Errorf("%q = %q, want %q", key, got, want)
		}
	}
	if _, ok := fields["a line with no separator at all"]; ok {
		t.Error("a separator-less line became a field")
	}
}

func TestParsePDFInfoTolerance(t *testing.T) {
	// CRLF (poppler on Windows), a duplicate key, and a leading colon.
	fields := parsePDFInfo("Pages:\t7\r\nTitle: first\r\nTitle: second\r\n: orphan\r\n")
	if fields["Pages"] != "7" {
		t.Errorf("Pages = %q, want \"7\" (CRLF not trimmed?)", fields["Pages"])
	}
	if fields["Title"] != "first" {
		t.Errorf("Title = %q, want the FIRST occurrence", fields["Title"])
	}
	if len(fields) != 2 {
		t.Errorf("unexpected fields: %v", fields)
	}
}

func TestPDFInfoEncrypted(t *testing.T) {
	tests := map[string]bool{
		"no":                            false,
		"NO":                            false,
		"  no  ":                        false,
		"yes (print:yes copy:no)":       true,
		"yes (print:no algorithm:AES)":  true,
		"":                              true, // an unstated value is not "not encrypted"
		"maybe, the report was clipped": true,
	}
	for in, want := range tests {
		if got := pdfInfoEncrypted(in); got != want {
			t.Errorf("pdfInfoEncrypted(%q) = %v, want %v", in, got, want)
		}
	}
}

func TestPDFInfoInt(t *testing.T) {
	if n, ok := pdfInfoInt(" 42 "); !ok || n != 42 {
		t.Errorf("pdfInfoInt(\" 42 \") = %d, %v", n, ok)
	}
	for _, bad := range []string{"", "many", "-1", "4 2"} {
		if n, ok := pdfInfoInt(bad); ok {
			t.Errorf("pdfInfoInt(%q) accepted %d", bad, n)
		}
	}
}

func TestPDFInfoDate(t *testing.T) {
	tests := []struct {
		name string
		in   string
		want string
	}{
		{"poppler ctime default", "Tue Jan  2 15:04:05 2024", "2024-01-02T15:04:05"},
		{"double-digit day", "Fri Nov 15 08:00:00 2024", "2024-11-15T08:00:00"},
		{"iso build", "2024-01-02T15:04:05", "2024-01-02T15:04:05"},
		{"rfc3339 with zone", "2024-01-02T15:04:05Z", "2024-01-02T15:04:05"},
		{"space separated", "2024-01-02 15:04:05", "2024-01-02T15:04:05"},
		{"raw pdf date object", "D:20240102150405+01'00'", "2024-01-02T15:04:05"},
		// Central keeps the raw string when the date will not parse (integrity
		// over tidiness) — the agent must not quietly drop it either.
		{"unparseable falls back to raw", "2006/05/24 21:06", "2006/05/24 21:06"},
		{"garbage falls back to raw", "not a date", "not a date"},
		{"empty stays empty", "   ", ""},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			if got := pdfInfoDate(tc.in); got != tc.want {
				t.Errorf("pdfInfoDate(%q) = %q, want %q", tc.in, got, tc.want)
			}
		})
	}
}

// --- the emitted key vocabulary (central's extract_pdf) ---------------------

func TestApplyPDFInfoVocabulary(t *testing.T) {
	res := &Result{Meta: map[string]any{}}
	if locked := applyPDFInfo(parsePDFInfo(samplePDFInfo), res); locked {
		t.Fatal("an unencrypted report reported locked")
	}
	want := map[string]any{
		"encrypted": false,
		"pages":     42,
		"title":     "Quarterly Report",
		"author":    "A. Nonymous",
		"creator":   "LaTeX with hyperref",
		"producer":  "pdfTeX-1.40.25",
		"created":   "2024-01-02T15:04:05",
		"modified":  "2024-01-03T09:30:00",
	}
	for key, w := range want {
		if got := res.Meta[key]; got != w {
			t.Errorf("%s = %v (%T), want %v (%T)", key, got, got, w, w)
		}
	}
	// Blank values are omitted, not stored as "" (documents.py _clean_str).
	if _, ok := res.Meta["subject"]; ok {
		t.Errorf("blank Subject was stored: %v", res.Meta["subject"])
	}
	// Nothing outside central's vocabulary leaks in from the report.
	for key := range res.Meta {
		if _, expected := want[key]; !expected {
			t.Errorf("unexpected key %q = %v", key, res.Meta[key])
		}
	}
}

func TestApplyPDFInfoLockedEmitsEncryptedOnly(t *testing.T) {
	// pdfinfo can report the encryption dictionary while refusing the page tree;
	// central returns {"encrypted": True} and NOTHING else for that document.
	report := "Title:      Secret\nEncrypted:  yes (print:no copy:no change:no addNotes:no algorithm:AES)\n"
	res := &Result{Meta: map[string]any{}}
	if locked := applyPDFInfo(parsePDFInfo(report), res); !locked {
		t.Fatal("encrypted report without a page count did not report locked")
	}
	if len(res.Meta) != 1 || res.Meta["encrypted"] != true {
		t.Fatalf("locked PDF emitted %v, want only {encrypted: true}", res.Meta)
	}
}

func TestApplyPDFInfoEncryptedButReadable(t *testing.T) {
	// Permissions-only encryption (the empty password opens it): central reports
	// encrypted alongside the full property set rather than stopping.
	report := "Title: Open\nPages: 3\nEncrypted: yes (print:yes copy:yes)\n"
	res := &Result{Meta: map[string]any{}}
	if locked := applyPDFInfo(parsePDFInfo(report), res); locked {
		t.Fatal("a readable encrypted PDF reported locked")
	}
	if res.Meta["encrypted"] != true || res.Meta["pages"] != 3 || res.Meta["title"] != "Open" {
		t.Fatalf("readable encrypted PDF emitted %v", res.Meta)
	}
}

func TestApplyPDFInfoGarbageReport(t *testing.T) {
	res := &Result{Meta: map[string]any{}}
	if locked := applyPDFInfo(parsePDFInfo("\x00\x01 not a report at all\n"), res); locked {
		t.Fatal("garbage reported locked")
	}
	if len(res.Meta) != 0 {
		t.Fatalf("garbage produced keys: %v", res.Meta)
	}
}

// --- absent-tool skips ------------------------------------------------------

func TestExtractPDFWithoutPopplerIsSkipped(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "doc.pdf", []byte("%PDF-1.4\n% not a real document\n"))

	res, err := Extract(context.Background(), p, CategoryDocument, Options{BodyText: true, OCR: true})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil && len(res.Errors) != 0 {
		t.Errorf("absent poppler recorded an error: %v", res.Errors)
	}
	if res != nil {
		if _, ok := res.Meta["pages"]; ok {
			t.Errorf("PDF keys produced without poppler: %v", res.Meta)
		}
	}
}

func TestOCRPDFWithoutToolsIsSkipped(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "scan.pdf", []byte("%PDF-1.4\n"))

	for _, opts := range []Options{
		{TesseractPath: "tesseract"}, // no pdftoppm
		{PDFToPPMPath: "pdftoppm"},   // no tesseract
		{},                           // neither
	} {
		res := &Result{Meta: map[string]any{}}
		if err := ocrPDF(context.Background(), p, opts.withDefaults(), res); err != nil {
			t.Errorf("%+v: %v", opts, err)
		}
		if len(res.Meta) != 0 {
			t.Errorf("%+v produced %v", opts, res.Meta)
		}
	}
}

// --- the should_ocr gate (ocr.py) -------------------------------------------

// ocrGateOptions points the OCR tools at paths that CANNOT exist, so a gate that
// lets a document through fails at the exec with a recognisable error and a gate
// that stops it returns nil having spawned nothing. That difference is the
// observation these tests make — no binary is ever run.
func ocrGateOptions() Options {
	missing := filepath.Join(string(filepath.Separator), "filearr-no-such-dir", "pdftoppm")
	return Options{PDFToPPMPath: missing, TesseractPath: missing}.withDefaults()
}

// runOCRGate reports whether the gate ALLOWED the OCR attempt.
func runOCRGate(t *testing.T, res *Result, opts Options) bool {
	t.Helper()
	dir := t.TempDir()
	p := writeFile(t, dir, "scan.pdf", []byte("%PDF-1.4\n"))
	err := ocrPDF(context.Background(), p, opts, res)
	if err == nil {
		return false
	}
	if !strings.Contains(err.Error(), "pdftoppm") {
		t.Fatalf("gate passed but failed unexpectedly: %v", err)
	}
	return true
}

func TestOCRPDFNativeTextGate(t *testing.T) {
	opts := ocrGateOptions() // OCRMinTextChars == 100
	tests := []struct {
		name      string
		chars     int
		wantAllow bool
	}{
		{"just below the threshold", OCRMinTextChars - 1, true},
		{"exactly at the threshold", OCRMinTextChars, false},
		{"just above the threshold", OCRMinTextChars + 1, false},
		{"no text layer at all", 0, true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			// PDFToTextPath stays empty, so the length comes from the body pass's
			// result exactly as ocr_run._native_text_len reads meta["body_text"].
			res := &Result{Meta: map[string]any{}, BodyText: strings.Repeat("a", tc.chars)}
			if got := runOCRGate(t, res, opts); got != tc.wantAllow {
				t.Errorf("allowed = %v, want %v for %d chars", got, tc.wantAllow, tc.chars)
			}
		})
	}
}

func TestOCRPDFNativeTextGateCountsRunes(t *testing.T) {
	// Central compares len() of a Python str, which counts CODE POINTS; counting
	// UTF-8 bytes here would skip OCR on a short CJK text layer.
	opts := ocrGateOptions()
	res := &Result{Meta: map[string]any{}, BodyText: strings.Repeat("測", OCRMinTextChars-1)}
	if !runOCRGate(t, res, opts) {
		t.Error("a 99-character CJK text layer was counted as sufficient")
	}
}

func TestOCRPDFPageCeiling(t *testing.T) {
	opts := ocrGateOptions() // OCRMaxPages == 10
	for _, tc := range []struct {
		pages     int
		wantAllow bool
	}{
		{OCRMaxPages - 1, true},
		{OCRMaxPages, true},
		{OCRMaxPages + 1, false},
	} {
		res := &Result{Meta: map[string]any{"pages": tc.pages}}
		if got := runOCRGate(t, res, opts); got != tc.wantAllow {
			t.Errorf("%d pages: allowed = %v, want %v", tc.pages, got, tc.wantAllow)
		}
	}
	// An unknown page count is not a ceiling breach — central's should_ocr only
	// applies max_pages when `pages` is actually an int.
	if !runOCRGate(t, &Result{Meta: map[string]any{"pages": "lots"}}, opts) {
		t.Error("an unparseable page count blocked OCR")
	}
}

func TestPDFPageCountTypeTolerance(t *testing.T) {
	for _, v := range []any{7, int64(7), float64(7)} {
		if n, ok := pdfPageCount(&Result{Meta: map[string]any{"pages": v}}); !ok || n != 7 {
			t.Errorf("pdfPageCount(%T) = %d, %v", v, n, ok)
		}
	}
	if _, ok := pdfPageCount(&Result{Meta: map[string]any{}}); ok {
		t.Error("a missing page count was reported present")
	}
}

func TestPopplerPathDisarmsLeadingDash(t *testing.T) {
	if got := popplerPath("-rf.pdf"); got != "."+string(filepath.Separator)+"-rf.pdf" {
		t.Errorf("popplerPath(\"-rf.pdf\") = %q", got)
	}
	for _, p := range []string{"/data/media/a.pdf", "C:\\media\\a.pdf", "sub/dir/a.pdf"} {
		if got := popplerPath(p); got != p {
			t.Errorf("popplerPath(%q) rewrote to %q", p, got)
		}
	}
}

// --- subprocess shell, against fake binaries this test compiles -------------

// fakePopplerSource is a stand-in for pdfinfo/pdftotext: it is compiled once,
// copied under each tool's name, and driven by env vars keyed on that name, so a
// single test can make pdfinfo fail while pdftotext succeeds. It also records
// its argv, which is what pins the flags this package promises poppler.
const fakePopplerSource = `package main

import (
	"os"
	"path/filepath"
	"strconv"
	"strings"
)

func main() {
	name := filepath.Base(os.Args[0])
	name = strings.ToUpper(strings.TrimSuffix(name, filepath.Ext(name)))
	env := func(key string) string { return os.Getenv("FILEARR_FAKE_" + name + "_" + key) }

	if f := env("ARGV"); f != "" {
		os.WriteFile(f, []byte(strings.Join(os.Args[1:], "\n")), 0o644)
	}
	out := env("STDOUT")
	if n, err := strconv.Atoi(env("REPEAT")); err == nil && n > 1 {
		out = strings.Repeat(out, n)
	}
	os.Stdout.WriteString(out)
	os.Stderr.WriteString(env("STDERR"))
	code, _ := strconv.Atoi(env("EXIT"))
	os.Exit(code)
}
`

// buildFakePoppler compiles the stand-in and returns the paths it was installed
// under (pdfinfo, pdftotext). Skips — never fails — when no Go toolchain is
// usable, so the pure-function coverage above remains the load-bearing part.
func buildFakePoppler(t *testing.T) (pdfinfo, pdftotext string) {
	t.Helper()
	if _, err := exec.LookPath("go"); err != nil {
		t.Skip("no go toolchain available to build the fake poppler tools")
	}
	dir := t.TempDir()
	writeFile(t, dir, "main.go", []byte(fakePopplerSource))
	writeFile(t, dir, "go.mod", []byte("module filearrfakepoppler\n\ngo 1.21\n"))

	suffix := ""
	if runtime.GOOS == "windows" {
		suffix = ".exe"
	}
	built := filepath.Join(dir, "fake"+suffix)
	cmd := exec.Command("go", "build", "-o", built, ".")
	cmd.Dir = dir
	// Clear GOFLAGS: an inherited -mod=vendor would look for a vendor tree this
	// throwaway module does not have.
	cmd.Env = append(os.Environ(), "GOFLAGS=")
	if out, err := cmd.CombinedOutput(); err != nil {
		t.Skipf("cannot build the fake poppler tools: %v\n%s", err, out)
	}
	blob, err := os.ReadFile(built)
	if err != nil {
		t.Fatalf("read built fake: %v", err)
	}
	install := func(name string) string {
		p := filepath.Join(dir, name+suffix)
		if err := os.WriteFile(p, blob, 0o755); err != nil {
			t.Fatalf("install fake %s: %v", name, err)
		}
		return p
	}
	return install("pdfinfo"), install("pdftotext")
}

func TestPDFHostToolShell(t *testing.T) {
	pdfinfo, pdftotext := buildFakePoppler(t)
	dir := t.TempDir()
	doc := writeFile(t, dir, "doc.pdf", []byte("%PDF-1.4\n"))

	t.Run("properties and body both land", func(t *testing.T) {
		argvFile := filepath.Join(t.TempDir(), "argv")
		t.Setenv("FILEARR_FAKE_PDFINFO_STDOUT", samplePDFInfo)
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "  Hello\n\n  world  ")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_ARGV", argvFile)

		res := &Result{Meta: map[string]any{}}
		opts := Options{BodyText: true, PDFInfoPath: pdfinfo, PDFToTextPath: pdftotext}.withDefaults()
		if err := extractPDF(context.Background(), doc, opts, res); err != nil {
			t.Fatalf("extractPDF: %v", err)
		}
		if res.Meta["pages"] != 42 || res.Meta["title"] != "Quarterly Report" {
			t.Errorf("properties missing: %v", res.Meta)
		}
		if res.BodyText != "Hello world" || res.BodyTextTruncated {
			t.Errorf("body = %q truncated=%v", res.BodyText, res.BodyTextTruncated)
		}
		// The path must arrive as ONE argument among the documented flags — the
		// whole point of the argv-list discipline.
		argv, err := os.ReadFile(argvFile)
		if err != nil {
			t.Fatalf("read argv: %v", err)
		}
		want := strings.Join([]string{"-q", "-enc", "UTF-8", "-nopgbrk", doc, "-"}, "\n")
		if string(argv) != want {
			t.Errorf("pdftotext argv =\n%s\nwant\n%s", argv, want)
		}
	})

	t.Run("body text truncation flag propagates", func(t *testing.T) {
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "abcdefghijklmnopqrstuvwxyz")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_REPEAT", "200") // past the read ceiling too

		res := &Result{Meta: map[string]any{}}
		opts := Options{BodyText: true, MaxBodyChars: 10, PDFToTextPath: pdftotext}.withDefaults()
		if err := extractPDF(context.Background(), doc, opts, res); err != nil {
			t.Fatalf("extractPDF: %v", err)
		}
		if res.BodyText != "abcdefghij" {
			t.Errorf("body = %q, want the first 10 characters", res.BodyText)
		}
		if !res.BodyTextTruncated {
			t.Error("body_text_truncated not set for a clipped body")
		}
	})

	t.Run("body text is not extracted unless enabled", func(t *testing.T) {
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "secret body")
		res := &Result{Meta: map[string]any{}}
		opts := Options{PDFInfoPath: pdfinfo, PDFToTextPath: pdftotext}.withDefaults()
		if err := extractPDF(context.Background(), doc, opts, res); err != nil {
			t.Fatalf("extractPDF: %v", err)
		}
		if res.BodyText != "" {
			t.Errorf("body text extracted with BodyText off: %q", res.BodyText)
		}
	})

	t.Run("locked document reports encrypted only", func(t *testing.T) {
		t.Setenv("FILEARR_FAKE_PDFINFO_EXIT", "1")
		t.Setenv("FILEARR_FAKE_PDFINFO_STDERR", "Command Line Error: Incorrect password\n")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "text that must not be read")

		res := &Result{Meta: map[string]any{}}
		opts := Options{BodyText: true, PDFInfoPath: pdfinfo, PDFToTextPath: pdftotext}.withDefaults()
		if err := extractPDF(context.Background(), doc, opts, res); err != nil {
			t.Fatalf("a locked PDF must not be an error: %v", err)
		}
		if len(res.Meta) != 1 || res.Meta["encrypted"] != true {
			t.Errorf("locked PDF emitted %v", res.Meta)
		}
		if res.BodyText != "" {
			t.Errorf("body text extracted from a locked PDF: %q", res.BodyText)
		}
	})

	t.Run("a failed properties pass still lets the body land", func(t *testing.T) {
		t.Setenv("FILEARR_FAKE_PDFINFO_EXIT", "1")
		t.Setenv("FILEARR_FAKE_PDFINFO_STDERR", "Syntax Error: Couldn't read xref table\n")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "salvaged text")

		res := &Result{Meta: map[string]any{}}
		opts := Options{BodyText: true, PDFInfoPath: pdfinfo, PDFToTextPath: pdftotext}.withDefaults()
		err := extractPDF(context.Background(), doc, opts, res)
		if err == nil || !strings.Contains(err.Error(), "pdfinfo failed") {
			t.Fatalf("err = %v, want a pdfinfo failure", err)
		}
		if res.BodyText != "salvaged text" {
			t.Errorf("body = %q; the body pass must be independent", res.BodyText)
		}
	})

	t.Run("both passes failing reports both", func(t *testing.T) {
		t.Setenv("FILEARR_FAKE_PDFINFO_EXIT", "1")
		t.Setenv("FILEARR_FAKE_PDFINFO_STDERR", "Syntax Error: Couldn't read xref table\n")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_EXIT", "1")
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDERR", "Syntax Error: Couldn't read xref table\n")

		res := &Result{Meta: map[string]any{}}
		opts := Options{BodyText: true, PDFInfoPath: pdfinfo, PDFToTextPath: pdftotext}.withDefaults()
		err := extractPDF(context.Background(), doc, opts, res)
		if err == nil {
			t.Fatal("both passes failed but no error was returned")
		}
		if !strings.Contains(err.Error(), "pdfinfo failed") || !strings.Contains(err.Error(), "pdftotext failed") {
			t.Errorf("err = %v, want both failures named", err)
		}
	})

	t.Run("native text probe measures without storing", func(t *testing.T) {
		// Body text is OFF, so the gate probes pdftotext itself; the probe's
		// output must never reach the result.
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", strings.Repeat("a", OCRMinTextChars))
		opts := Options{PDFToTextPath: pdftotext, PDFToPPMPath: "x", TesseractPath: "x"}.withDefaults()
		res := &Result{Meta: map[string]any{}}
		if n := pdfNativeTextChars(context.Background(), doc, opts, res); n != OCRMinTextChars {
			t.Errorf("probe measured %d chars, want %d", n, OCRMinTextChars)
		}
		if res.BodyText != "" || len(res.Meta) != 0 {
			t.Errorf("the probe stored something: body=%q meta=%v", res.BodyText, res.Meta)
		}

		// Whitespace-only output is not a text layer.
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_STDOUT", "\n\n   \n")
		if n := pdfNativeTextChars(context.Background(), doc, opts, res); n != 0 {
			t.Errorf("blank page measured %d chars, want 0", n)
		}

		// A probe that cannot run counts as "no text", never as an error.
		t.Setenv("FILEARR_FAKE_PDFTOTEXT_EXIT", "1")
		if n := pdfNativeTextChars(context.Background(), doc, opts, res); n != 0 {
			t.Errorf("failed probe measured %d chars, want 0", n)
		}
	})
}
