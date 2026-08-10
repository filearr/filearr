package extract

import (
	"archive/tar"
	"archive/zip"
	"bytes"
	"compress/gzip"
	"context"
	"image"
	"image/color"
	"image/png"
	"os"
	"path/filepath"
	"strings"
	"testing"
)

// Fixtures are BUILT IN CODE rather than checked in as binaries: a hand-built
// docx is readable in the diff, cannot bit-rot, and — for the zip-bomb cases —
// could not be committed safely anyway.

func writeFile(t *testing.T, dir, name string, data []byte) string {
	t.Helper()
	p := filepath.Join(dir, name)
	if err := os.WriteFile(p, data, 0o644); err != nil {
		t.Fatalf("write %s: %v", name, err)
	}
	return p
}

// tinyPNG encodes a w×h opaque PNG.
func tinyPNG(t *testing.T, w, h int) []byte {
	t.Helper()
	img := image.NewRGBA(image.Rect(0, 0, w, h))
	for x := 0; x < w; x++ {
		for y := 0; y < h; y++ {
			img.Set(x, y, color.RGBA{R: uint8(x), G: uint8(y), B: 0x40, A: 0xFF})
		}
	}
	var buf bytes.Buffer
	if err := png.Encode(&buf, img); err != nil {
		t.Fatalf("encode png: %v", err)
	}
	return buf.Bytes()
}

// zipOf builds a zip archive from an ordered list of {name, content} members.
func zipOf(t *testing.T, members [][2]string) []byte {
	t.Helper()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	for _, m := range members {
		w, err := zw.Create(m[0])
		if err != nil {
			t.Fatalf("zip create %s: %v", m[0], err)
		}
		if _, err := w.Write([]byte(m[1])); err != nil {
			t.Fatalf("zip write %s: %v", m[0], err)
		}
	}
	if err := zw.Close(); err != nil {
		t.Fatalf("zip close: %v", err)
	}
	return buf.Bytes()
}

const coreXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:dcterms="http://purl.org/dc/terms/">
  <dc:title>Quarterly Report</dc:title>
  <dc:creator>Ada Lovelace</dc:creator>
  <dc:subject>Analytics</dc:subject>
  <cp:keywords>finance, q3</cp:keywords>
  <cp:revision>4</cp:revision>
  <dcterms:created>2026-01-02T03:04:05Z</dcterms:created>
  <dcterms:modified>2026-02-03T04:05:06Z</dcterms:modified>
</cp:coreProperties>`

const documentXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:body>
    <w:p><w:r><w:t>Hello</w:t></w:r><w:r><w:t xml:space="preserve"> world</w:t></w:r></w:p>
    <w:p><w:r><w:t>Second paragraph.</w:t></w:r></w:p>
  </w:body>
</w:document>`

const workbookXML = `<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">
  <sheets>
    <sheet name="Summary" sheetId="1" r:id="rId1"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
    <sheet name="Raw Data" sheetId="2" r:id="rId2"
           xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"/>
  </sheets>
</workbook>`

const odfMetaXML = `<?xml version="1.0" encoding="UTF-8"?>
<office:document-meta
    xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:dc="http://purl.org/dc/elements/1.1/"
    xmlns:meta="urn:oasis:names:tc:opendocument:xmlns:meta:1.0">
  <office:meta>
    <dc:title>Open Notes</dc:title>
    <meta:initial-creator>Grace Hopper</meta:initial-creator>
    <dc:subject>Compilers</dc:subject>
    <meta:creation-date>2026-03-04T05:06:07</meta:creation-date>
    <dc:date>2026-04-05T06:07:08</dc:date>
  </office:meta>
</office:document-meta>`

const odfContentXML = `<?xml version="1.0" encoding="UTF-8"?>
<office:document-content
    xmlns:office="urn:oasis:names:tc:opendocument:xmlns:office:1.0"
    xmlns:text="urn:oasis:names:tc:opendocument:xmlns:text:1.0">
  <office:automatic-styles><style ignored="yes">NOTTEXT</style></office:automatic-styles>
  <office:body><office:text>
    <text:h>Chapter One</text:h>
    <text:p>The <text:span>quick</text:span> brown fox.</text:p>
  </office:text></office:body>
</office:document-content>`

func bodyOpts() Options {
	return Options{BodyText: true}
}

// --- image ------------------------------------------------------------------

func TestExtractImage(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "pic.png", tinyPNG(t, 7, 3))

	res, err := Extract(context.Background(), p, CategoryImage, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil {
		t.Fatal("no result for a valid PNG")
	}
	if res.Meta["width"] != 7 || res.Meta["height"] != 3 {
		t.Errorf("dimensions = %v x %v, want 7 x 3", res.Meta["width"], res.Meta["height"])
	}
	// Central stores Pillow's uppercase format token.
	if res.Meta["format"] != "PNG" {
		t.Errorf("format = %v, want PNG", res.Meta["format"])
	}
	if res.Meta["mode"] != "RGBA" {
		t.Errorf("mode = %v, want RGBA", res.Meta["mode"])
	}
	if len(res.Errors) != 0 {
		t.Errorf("unexpected errors: %v", res.Errors)
	}
}

func TestExtractImageCorruptIsRecordedNotFatal(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "broken.png", []byte("this is not a png"))

	res, err := Extract(context.Background(), p, CategoryImage, Options{})
	if err != nil {
		t.Fatalf("a corrupt file must not fail the pass: %v", err)
	}
	if res == nil || res.Errors["image"] == "" {
		t.Fatalf("corrupt image produced no recorded error: %+v", res)
	}
}

// --- documents --------------------------------------------------------------

func TestExtractTextBody(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "notes.md", []byte("# Title\n\nSome\tbody   text.\x00\n"))

	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil {
		t.Fatal("no result for a text file")
	}
	// Control chars stripped, whitespace runs collapsed — central's discipline.
	if res.BodyText != "# Title Some body text." {
		t.Errorf("body = %q", res.BodyText)
	}
	if res.BodyTextTruncated {
		t.Error("short body reported truncated")
	}
}

func TestExtractTextBodyRequiresPolicy(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "notes.txt", []byte("content"))

	// extract_body_text off: a txt file has nothing else to offer, so the whole
	// result is empty and the event carries no `extracted` object at all.
	res, err := Extract(context.Background(), p, CategoryDocument, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("body text disabled but got a result: %+v", res)
	}
}

func TestExtractTextBodyTruncates(t *testing.T) {
	dir := t.TempDir()
	// 300 chars of content against a 100-char cap.
	p := writeFile(t, dir, "big.txt", []byte(strings.Repeat("a", 300)))

	opts := bodyOpts()
	opts.MaxBodyChars = 100
	res, err := Extract(context.Background(), p, CategoryDocument, opts)
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if got := len([]rune(res.BodyText)); got != 100 {
		t.Errorf("body length = %d, want 100", got)
	}
	if !res.BodyTextTruncated {
		t.Error("clipped body not flagged truncated")
	}
}

func TestExtractDocx(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "report.docx", zipOf(t, [][2]string{
		{"[Content_Types].xml", `<Types/>`},
		{"docProps/core.xml", coreXML},
		{"word/document.xml", documentXML},
	}))

	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res == nil {
		t.Fatal("no result for a docx")
	}
	for key, want := range map[string]any{
		"title":    "Quarterly Report",
		"author":   "Ada Lovelace",
		"subject":  "Analytics",
		"keywords": "finance, q3",
		"created":  "2026-01-02T03:04:05Z",
		"modified": "2026-02-03T04:05:06Z",
		"revision": 4,
	} {
		if res.Meta[key] != want {
			t.Errorf("%s = %v, want %v", key, res.Meta[key], want)
		}
	}
	if res.Meta["paragraphs"] != 2 {
		t.Errorf("paragraphs = %v, want 2", res.Meta["paragraphs"])
	}
	if res.BodyText != "Hello world Second paragraph." {
		t.Errorf("body = %q", res.BodyText)
	}
}

func TestExtractDocxPropertiesOnlyWithoutBodyPolicy(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "report.docx", zipOf(t, [][2]string{
		{"docProps/core.xml", coreXML},
		{"word/document.xml", documentXML},
	}))

	res, err := Extract(context.Background(), p, CategoryDocument, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.BodyText != "" {
		t.Errorf("body text extracted despite the policy being off: %q", res.BodyText)
	}
	if res.Meta["title"] != "Quarterly Report" {
		t.Errorf("properties should still be extracted: %v", res.Meta)
	}
}

func TestExtractXlsxHasNoBodyText(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "book.xlsx", zipOf(t, [][2]string{
		{"docProps/core.xml", coreXML},
		{"xl/workbook.xml", workbookXML},
		{"xl/sharedStrings.xml", `<sst><si><t>SECRET CELL</t></si></sst>`},
	}))

	// Body text is ON, and xlsx must STILL produce none: central's xlsx
	// extractor is structure-only and the two sides must agree.
	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.BodyText != "" {
		t.Errorf("xlsx produced body text: %q", res.BodyText)
	}
	sheets, ok := res.Meta["sheets"].([]string)
	if !ok || len(sheets) != 2 || sheets[0] != "Summary" || sheets[1] != "Raw Data" {
		t.Errorf("sheets = %v, want [Summary, Raw Data]", res.Meta["sheets"])
	}
	if res.Meta["sheet_count"] != 2 {
		t.Errorf("sheet_count = %v, want 2", res.Meta["sheet_count"])
	}
	if res.Meta["author"] != "Ada Lovelace" {
		t.Errorf("author = %v", res.Meta["author"])
	}
}

func TestExtractODF(t *testing.T) {
	dir := t.TempDir()
	odt := writeFile(t, dir, "notes.odt", zipOf(t, [][2]string{
		{"mimetype", "application/vnd.oasis.opendocument.text"},
		{"meta.xml", odfMetaXML},
		{"content.xml", odfContentXML},
	}))

	res, err := Extract(context.Background(), odt, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.Meta["title"] != "Open Notes" {
		t.Errorf("title = %v", res.Meta["title"])
	}
	if res.Meta["author"] != "Grace Hopper" {
		t.Errorf("author = %v", res.Meta["author"])
	}
	if res.Meta["created"] != "2026-03-04T05:06:07" {
		t.Errorf("created = %v", res.Meta["created"])
	}
	if res.BodyText != "Chapter One The quick brown fox." {
		t.Errorf("body = %q", res.BodyText)
	}
	if strings.Contains(res.BodyText, "NOTTEXT") {
		t.Error("style content leaked into the body text")
	}
}

func TestExtractODSHasNoBodyText(t *testing.T) {
	dir := t.TempDir()
	ods := writeFile(t, dir, "sheet.ods", zipOf(t, [][2]string{
		{"meta.xml", odfMetaXML},
		{"content.xml", odfContentXML},
	}))

	res, err := Extract(context.Background(), ods, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res.BodyText != "" {
		t.Errorf("ods produced body text: %q", res.BodyText)
	}
	if res.Meta["title"] != "Open Notes" {
		t.Errorf("ods properties missing: %v", res.Meta)
	}
}

func TestDispatchPDFWithoutPopplerIsSkipped(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "paper.pdf", []byte("%PDF-1.7\n"))

	// PDF support is a HOST TOOL capability (poppler-utils). bodyOpts() resolves
	// no tool paths, which is the "poppler is not installed here" configuration:
	// it must be a SKIP (nil, nil), not a recurring per-file error on every scan
	// of a document library.
	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("pdf produced a result: %+v", res)
	}
}

// --- archives ---------------------------------------------------------------

func TestListZipArchive(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "bundle.zip", zipOf(t, [][2]string{
		{"a.txt", "aaaa"},
		{"nested/b.txt", "bbbbbb"},
		{"../evil.txt", "x"},
	}))

	res, err := Extract(context.Background(), p, CategoryArchive, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	arch, ok := res.Meta["archive"].(map[string]any)
	if !ok {
		t.Fatalf("archive object missing: %v", res.Meta)
	}
	if arch["member_count"] != 3 {
		t.Errorf("member_count = %v, want 3", arch["member_count"])
	}
	if arch["total_uncompressed"] != int64(11) {
		t.Errorf("total_uncompressed = %v, want 11", arch["total_uncompressed"])
	}
	if arch["format"] != "zip" {
		t.Errorf("format = %v, want zip", arch["format"])
	}
	if arch["truncated"] != false {
		t.Errorf("truncated = %v, want false", arch["truncated"])
	}
	members, _ := arch["members"].([]map[string]any)
	if len(members) != 3 {
		t.Fatalf("members = %v", members)
	}
	// A traversal-shaped member name is stored VERBATIM: it is a display/search
	// string that is never resolved as a path.
	if members[2]["name"] != "../evil.txt" {
		t.Errorf("member name was normalised: %v", members[2]["name"])
	}
}

func TestListTarGzArchive(t *testing.T) {
	dir := t.TempDir()

	var raw bytes.Buffer
	gw := gzip.NewWriter(&raw)
	tw := tar.NewWriter(gw)
	for _, m := range []struct {
		name string
		body string
	}{{"one.txt", "12345"}, {"two.txt", "678"}} {
		if err := tw.WriteHeader(&tar.Header{
			Name: m.name, Mode: 0o644, Size: int64(len(m.body)), Typeflag: tar.TypeReg,
		}); err != nil {
			t.Fatalf("tar header: %v", err)
		}
		if _, err := tw.Write([]byte(m.body)); err != nil {
			t.Fatalf("tar write: %v", err)
		}
	}
	// A directory entry must be skipped from the member list.
	if err := tw.WriteHeader(&tar.Header{Name: "adir/", Mode: 0o755, Typeflag: tar.TypeDir}); err != nil {
		t.Fatalf("tar dir header: %v", err)
	}
	tw.Close()
	gw.Close()

	p := writeFile(t, dir, "bundle.tar.gz", raw.Bytes())
	res, err := Extract(context.Background(), p, CategoryArchive, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	arch := res.Meta["archive"].(map[string]any)
	if arch["member_count"] != 2 {
		t.Errorf("member_count = %v, want 2 (the dir entry is not a member)", arch["member_count"])
	}
	if arch["total_uncompressed"] != int64(8) {
		t.Errorf("total_uncompressed = %v, want 8", arch["total_uncompressed"])
	}
	// The compound suffix must win over the bare ".gz".
	if arch["format"] != "tar.gz" {
		t.Errorf("format = %v, want tar.gz", arch["format"])
	}
}

func TestListArchiveUnsupportedFamilyIsSkipped(t *testing.T) {
	dir := t.TempDir()
	// .txz has no pure-Go stdlib decoder — a documented gap, and a SKIP rather
	// than a per-scan error.
	p := writeFile(t, dir, "bundle.txz", []byte("\xfd7zXZ\x00"))
	res, err := Extract(context.Background(), p, CategoryArchive, Options{})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("xz archive produced a result: %+v", res)
	}
}

// --- guards -----------------------------------------------------------------

// TestZipBombRejected builds a genuine ratio bomb (a highly compressible payload
// past the ratio floor) and asserts the guard rejects it from the CENTRAL
// DIRECTORY, before anything is decompressed.
func TestZipBombRejected(t *testing.T) {
	dir := t.TempDir()
	var buf bytes.Buffer
	zw := zip.NewWriter(&buf)
	w, err := zw.Create("bomb.bin")
	if err != nil {
		t.Fatalf("zip create: %v", err)
	}
	// 12 MiB of zeros compresses to a few KB: > 100:1 ratio AND past the 10 MiB
	// ratio floor, so both gate conditions are satisfied.
	if _, err := w.Write(make([]byte, 12<<20)); err != nil {
		t.Fatalf("zip write: %v", err)
	}
	zw.Close()
	p := writeFile(t, dir, "bomb.docx", buf.Bytes())

	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("a bomb must be recorded, not raised: %v", err)
	}
	if res == nil || !strings.Contains(res.Errors["document"], "decompression guard") {
		t.Fatalf("bomb not rejected by the guard: %+v", res)
	}
	if res.BodyText != "" {
		t.Error("bomb produced body text")
	}
}

// TestZipSmallHighRatioAccepted is the other half of the guard contract: an
// ordinary tiny, highly-compressible office file (well under the ratio floor)
// must NOT be falsely rejected.
func TestZipSmallHighRatioAccepted(t *testing.T) {
	dir := t.TempDir()
	filler := strings.Repeat("A", 200_000) // ~200 KB, compresses ~1000:1
	p := writeFile(t, dir, "small.docx", zipOf(t, [][2]string{
		{"docProps/core.xml", coreXML},
		{"word/document.xml", `<w:document xmlns:w="x"><w:body><w:p><w:r><w:t>` +
			filler + `</w:t></w:r></w:p></w:body></w:document>`},
	}))

	res, err := Extract(context.Background(), p, CategoryDocument, bodyOpts())
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if got := res.Errors["document"]; got != "" {
		t.Fatalf("ordinary compressible file rejected: %s", got)
	}
	if res.Meta["title"] != "Quarterly Report" {
		t.Errorf("properties missing: %v", res.Meta)
	}
}

func TestExtractHonorsMaxBytes(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "pic.png", tinyPNG(t, 32, 32))

	res, err := Extract(context.Background(), p, CategoryImage, Options{MaxBytes: 8})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("oversized file was extracted anyway: %+v", res)
	}
}

func TestExtractHonorsCancellation(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "pic.png", tinyPNG(t, 4, 4))

	ctx, cancel := context.WithCancel(context.Background())
	cancel()
	if _, err := Extract(ctx, p, CategoryImage, Options{}); err == nil {
		t.Fatal("cancelled context did not stop extraction")
	}
}

func TestExtractUnknownCategoryIsSkipped(t *testing.T) {
	dir := t.TempDir()
	p := writeFile(t, dir, "thing.bin", []byte("data"))

	res, err := Extract(context.Background(), p, "development", Options{BodyText: true})
	if err != nil {
		t.Fatalf("Extract: %v", err)
	}
	if res != nil {
		t.Fatalf("unknown category produced a result: %+v", res)
	}
}

func TestExtractMissingFileErrors(t *testing.T) {
	if _, err := Extract(context.Background(), filepath.Join(t.TempDir(), "nope"), CategoryImage, Options{}); err == nil {
		t.Fatal("a missing file should surface a framing error")
	}
}

func TestNormalizeBodyText(t *testing.T) {
	tests := []struct {
		name          string
		in            string
		maxChars      int
		hardStopped   bool
		want          string
		wantTruncated bool
	}{
		{name: "collapses whitespace", in: "a  \t\n b", maxChars: 100, want: "a b"},
		{name: "strips control chars", in: "a\x00\x1bb", maxChars: 100, want: "ab"},
		{name: "trims ends", in: "   padded   ", maxChars: 100, want: "padded"},
		{name: "caps length", in: "abcdef", maxChars: 3, want: "abc", wantTruncated: true},
		{name: "propagates a hard stop", in: "abc", maxChars: 100, hardStopped: true, want: "abc", wantTruncated: true},
		{name: "counts runes not bytes", in: "héllo wörld", maxChars: 5, want: "héllo", wantTruncated: true},
	}
	for _, tc := range tests {
		t.Run(tc.name, func(t *testing.T) {
			got, truncated := normalizeBodyText(tc.in, tc.maxChars, tc.hardStopped)
			if got != tc.want {
				t.Errorf("text = %q, want %q", got, tc.want)
			}
			if truncated != tc.wantTruncated {
				t.Errorf("truncated = %v, want %v", truncated, tc.wantTruncated)
			}
		})
	}
}

func TestFormats(t *testing.T) {
	if got := Formats(false); strings.Contains(strings.Join(got, ","), "video") {
		t.Errorf("video advertised without ffprobe: %v", got)
	}
	if got := Formats(true); !strings.Contains(strings.Join(got, ","), "video") {
		t.Errorf("video not advertised with ffprobe: %v", got)
	}
}
