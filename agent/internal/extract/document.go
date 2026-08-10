package extract

import (
	"archive/zip"
	"context"
	"encoding/xml"
	"fmt"
	"io"
	"os"
	"strconv"
	"strings"
	"unicode/utf8"
)

// Extension sets, mirroring central's dispatch (documents.py extract_body /
// _PARSERS). txt/md are the ONLY extensions central gives a plain-text body to;
// xlsx gets properties + structure but explicitly NO body text, and the agent
// keeps that asymmetry so the two sides produce the same shape for the same file.
var (
	textExts = map[string]bool{"txt": true, "md": true}
	// ODF part names are shared across the family; the body rule is not.
	// Text/presentation documents get body text, spreadsheets (ods/ots) do not —
	// the same "structure only for spreadsheets" rule as xlsx.
	odfTextExts  = map[string]bool{"odt": true, "ott": true, "odp": true, "otp": true, "fodt": true}
	odfSheetExts = map[string]bool{"ods": true, "ots": true, "fods": true}
)

// extractDocument dispatches one document by extension. An extension with no
// reader here returns nil WITHOUT writing anything: PDF text is explicitly out
// of scope for this phase, and an unreadable-by-design format must not manifest
// as a per-file error on every scan.
func extractDocument(ctx context.Context, path, ext string, opts Options, res *Result) error {
	switch {
	case textExts[ext]:
		if !opts.BodyText {
			return nil // properties-only mode: a txt file has no properties
		}
		return extractTextBody(path, opts, res)
	case ext == "docx" || ext == "docm":
		return extractDocx(ctx, path, opts, res)
	case ext == "xlsx" || ext == "xlsm":
		return extractXlsx(ctx, path, opts, res)
	case odfTextExts[ext] || odfSheetExts[ext]:
		return extractODF(ctx, path, odfTextExts[ext] && opts.BodyText, res)
	}
	return nil
}

// extractTextBody reads a plain-text/markdown body under a byte ceiling, a port
// of documents.py _text_body: at most max_chars*4 bytes (UTF-8 worst case) so a
// multi-gigabyte log is never slurped whole, and hitting that cap marks the body
// truncated.
func extractTextBody(path string, opts Options, res *Result) error {
	readCap := int64(opts.MaxBodyChars)*4 + 8
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read text file: %w", err)
	}
	defer f.Close()

	raw, err := io.ReadAll(io.LimitReader(f, readCap+1))
	if err != nil {
		return fmt.Errorf("cannot read text file: %w", err)
	}
	hitCap := int64(len(raw)) > readCap
	if hitCap {
		raw = raw[:readCap]
	}
	// errors="replace" equivalent: invalid UTF-8 becomes U+FFFD rather than
	// failing the file (central decodes the same way).
	text := strings.ToValidUTF8(string(raw), "�")
	res.setBody(normalizeBodyText(text, opts.MaxBodyChars, hitCap))
	return nil
}

// setBody stores the body-text pair, dropping an empty body entirely (central
// returns {} rather than storing an empty string).
func (r *Result) setBody(text string, truncated bool) {
	if text == "" {
		return
	}
	r.BodyText = text
	r.BodyTextTruncated = truncated
}

// openGuardedZip opens a zip-based document with the central-directory
// bomb guard applied FIRST — the same order central uses (guard, then parse).
func openGuardedZip(path string) (*zip.ReadCloser, error) {
	zr, err := zip.OpenReader(path)
	if err != nil {
		return nil, fmt.Errorf("not a valid zip container: %w", err)
	}
	if err := guardDecompression(&zr.Reader); err != nil {
		zr.Close()
		return nil, err
	}
	return zr, nil
}

// extractDocx reads OOXML core properties and (optionally) the document body
// straight from the package with archive/zip + encoding/xml — no OOXML library.
// Emits central's DOCX vocabulary: title/author/subject/keywords/created/
// modified/revision (+ body_text/body_text_truncated).
//
// paragraphs (which central reports from python-docx) is counted from the <w:p>
// elements we already walk when body text is enabled; without a body pass there
// is nothing to count and the key is omitted rather than guessed.
func extractDocx(ctx context.Context, path string, opts Options, res *Result) error {
	zr, err := openGuardedZip(path)
	if err != nil {
		return err
	}
	defer zr.Close()

	if err := readCoreProps(&zr.Reader, res); err != nil {
		return err
	}
	if !opts.BodyText {
		return nil
	}
	rc, ok := openMember(&zr.Reader, "word/document.xml")
	if !ok {
		return nil // no main part: not a word processing package
	}
	defer rc.Close()
	text, paragraphs, stopped, err := docxBody(ctx, rc, opts.MaxBodyChars)
	if err != nil {
		return err
	}
	if paragraphs > 0 {
		res.Meta["paragraphs"] = paragraphs
	}
	res.setBody(normalizeBodyText(text, opts.MaxBodyChars, stopped))
	return nil
}

// extractXlsx reads OOXML core properties plus the sheet names from
// xl/workbook.xml. Body text is deliberately NOT extracted — central's xlsx
// extractor is structure-only, and a cell-value dump would make agent items
// searchable in a way central items are not.
func extractXlsx(ctx context.Context, path string, opts Options, res *Result) error {
	zr, err := openGuardedZip(path)
	if err != nil {
		return err
	}
	defer zr.Close()

	if err := readCoreProps(&zr.Reader, res); err != nil {
		return err
	}
	rc, ok := openMember(&zr.Reader, "xl/workbook.xml")
	if !ok {
		return nil
	}
	defer rc.Close()
	sheets, err := workbookSheets(ctx, rc)
	if err != nil {
		return err
	}
	if len(sheets) > 0 {
		res.Meta["sheets"] = sheets
		res.Meta["sheet_count"] = len(sheets)
	}
	return nil
}

// extractODF reads an OpenDocument package's meta.xml properties and, for text/
// presentation documents, its content.xml body.
func extractODF(ctx context.Context, path string, wantBody bool, res *Result) error {
	zr, err := openGuardedZip(path)
	if err != nil {
		return err
	}
	defer zr.Close()

	if rc, ok := openMember(&zr.Reader, "meta.xml"); ok {
		err := readDublinCore(ctx, rc, res)
		rc.Close()
		if err != nil {
			return err
		}
	}
	if !wantBody {
		return nil
	}
	rc, ok := openMember(&zr.Reader, "content.xml")
	if !ok {
		return nil
	}
	defer rc.Close()
	text, stopped, err := odfBody(ctx, rc, BodyTextMaxChars)
	if err != nil {
		return err
	}
	res.setBody(normalizeBodyText(text, BodyTextMaxChars, stopped))
	return nil
}

// readCoreProps parses docProps/core.xml (optional in OOXML — absence is not an
// error) into central's property keys.
func readCoreProps(zr *zip.Reader, res *Result) error {
	rc, ok := openMember(zr, "docProps/core.xml")
	if !ok {
		return nil
	}
	defer rc.Close()
	return readDublinCore(context.Background(), rc, res)
}

// readDublinCore walks a Dublin-Core-ish property part (OOXML docProps/core.xml
// AND ODF meta.xml — both carry dc:title/dc:creator/dc:subject and a created/
// modified pair) and writes central's key names.
//
// Matching is on LOCAL element names, ignoring namespace prefixes and URIs. That
// is deliberately tolerant: producers disagree about namespace URIs (dcterms vs
// dc for dates, ODF's meta:creation-date), and a strict namespace match silently
// drops properties from perfectly ordinary files.
func readDublinCore(ctx context.Context, r io.Reader, res *Result) error {
	dec := xml.NewDecoder(r)
	dec.Strict = false
	for {
		if err := ctx.Err(); err != nil {
			return err
		}
		tok, err := dec.Token()
		if err == io.EOF {
			return nil
		}
		if err != nil {
			return fmt.Errorf("malformed property XML: %w", err)
		}
		se, ok := tok.(xml.StartElement)
		if !ok {
			continue
		}
		var key string
		switch se.Name.Local {
		case "title":
			key = "title"
		case "creator", "initial-creator":
			key = "author"
		case "subject":
			key = "subject"
		case "keywords", "keyword":
			key = "keywords"
		case "created", "creation-date":
			key = "created"
		case "modified", "date":
			key = "modified"
		case "revision", "editing-cycles":
			key = "revision"
		default:
			continue
		}
		text, err := elementText(dec, &se)
		if err != nil {
			return err
		}
		if key == "revision" {
			// central stores revision as an int and omits a zero.
			if n, err := strconv.Atoi(strings.TrimSpace(text)); err == nil && n != 0 {
				res.Meta["revision"] = n
			}
			continue
		}
		// First writer wins: OOXML lists dc:creator before cp:lastModifiedBy, and
		// ODF lists meta:initial-creator before dc:creator — the earlier element
		// is the authored value in both.
		if _, exists := res.Meta[key]; !exists {
			res.set(key, text)
		}
	}
}

// elementText consumes an element's character data (concatenating nested runs)
// and returns it. The decoder is left positioned after the closing tag.
func elementText(dec *xml.Decoder, start *xml.StartElement) (string, error) {
	var b strings.Builder
	depth := 1
	for depth > 0 {
		tok, err := dec.Token()
		if err != nil {
			if err == io.EOF {
				return b.String(), nil
			}
			return "", fmt.Errorf("malformed XML in <%s>: %w", start.Name.Local, err)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			depth++
		case xml.EndElement:
			depth--
		case xml.CharData:
			if b.Len() < MemberNameCap*8 { // property values are short by nature
				b.Write(t)
			}
		}
	}
	return b.String(), nil
}

// docxBody streams word/document.xml, concatenating <w:t> runs and breaking
// paragraphs on </w:p>. It stops as soon as maxChars characters have been
// collected (the same time/space box central applies per paragraph), reporting
// stopped=true so the caller can flag the body truncated.
func docxBody(ctx context.Context, r io.Reader, maxChars int) (text string, paragraphs int, stopped bool, err error) {
	dec := xml.NewDecoder(r)
	dec.Strict = false
	var b strings.Builder
	total := 0
	inText := false
	for {
		if cerr := ctx.Err(); cerr != nil {
			return "", 0, false, cerr
		}
		tok, terr := dec.Token()
		if terr == io.EOF {
			break
		}
		if terr != nil {
			// A member read cut short by MemberReadMax surfaces as an XML error
			// mid-document. Whatever text we already have is still good, so treat
			// it as a clean truncation rather than losing the whole document.
			if b.Len() > 0 {
				return b.String(), paragraphs, true, nil
			}
			return "", 0, false, fmt.Errorf("malformed document.xml: %w", terr)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			switch t.Name.Local {
			case "t":
				inText = true
			case "tab", "br", "cr":
				b.WriteByte(' ')
			}
		case xml.EndElement:
			switch t.Name.Local {
			case "t":
				inText = false
			case "p":
				paragraphs++
				b.WriteByte('\n')
			}
		case xml.CharData:
			if !inText {
				continue
			}
			b.Write(t)
			total += utf8.RuneCount(t)
			if total >= maxChars {
				return b.String(), paragraphs, true, nil
			}
		}
	}
	return b.String(), paragraphs, false, nil
}

// odfBody streams an ODF content.xml, collecting the text of <text:p> and
// <text:h> elements. Bounded exactly like docxBody.
func odfBody(ctx context.Context, r io.Reader, maxChars int) (text string, stopped bool, err error) {
	dec := xml.NewDecoder(r)
	dec.Strict = false
	var b strings.Builder
	total := 0
	depth := 0 // nesting depth inside a paragraph/heading
	for {
		if cerr := ctx.Err(); cerr != nil {
			return "", false, cerr
		}
		tok, terr := dec.Token()
		if terr == io.EOF {
			break
		}
		if terr != nil {
			if b.Len() > 0 {
				return b.String(), true, nil
			}
			return "", false, fmt.Errorf("malformed content.xml: %w", terr)
		}
		switch t := tok.(type) {
		case xml.StartElement:
			if depth > 0 {
				depth++
			} else if t.Name.Local == "p" || t.Name.Local == "h" {
				depth = 1
			}
			if t.Name.Local == "tab" || t.Name.Local == "line-break" {
				b.WriteByte(' ')
			}
		case xml.EndElement:
			if depth > 0 {
				depth--
				if depth == 0 {
					b.WriteByte('\n')
				}
			}
		case xml.CharData:
			if depth == 0 {
				continue
			}
			b.Write(t)
			total += utf8.RuneCount(t)
			if total >= maxChars {
				return b.String(), true, nil
			}
		}
	}
	return b.String(), false, nil
}

// workbookSheets reads the sheet names from xl/workbook.xml in workbook order
// (the same order openpyxl's sheetnames reports).
func workbookSheets(ctx context.Context, r io.Reader) ([]string, error) {
	dec := xml.NewDecoder(r)
	dec.Strict = false
	var out []string
	for {
		if err := ctx.Err(); err != nil {
			return nil, err
		}
		tok, err := dec.Token()
		if err == io.EOF {
			return out, nil
		}
		if err != nil {
			if len(out) > 0 {
				return out, nil
			}
			return nil, fmt.Errorf("malformed workbook.xml: %w", err)
		}
		se, ok := tok.(xml.StartElement)
		if !ok || se.Name.Local != "sheet" {
			continue
		}
		for _, attr := range se.Attr {
			if attr.Name.Local == "name" {
				if n := cleanStr(attr.Value); n != "" {
					out = append(out, n)
				}
				break
			}
		}
		if len(out) >= ArchiveMaxMembers {
			return out, nil // a workbook cannot honestly have 10k sheets
		}
	}
}
