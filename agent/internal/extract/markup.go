package extract

import (
	"fmt"
	"io"
	"os"
	"strings"

	"golang.org/x/net/html"
)

// extractMarkupBody reduces an HTML/XML file to its visible text (2026-08-20,
// mirror of documents.py _markup_body): size-capped read, tags stripped with
// the x/net/html tokenizer (script/style contents dropped, entities resolved).
// Nothing is ever fetched; hostile markup at worst yields odd text.
func extractMarkupBody(path string, opts Options, res *Result) error {
	readCap := int64(opts.MaxBodyChars)*8 + 8 // markup is mostly tags
	f, err := os.Open(path)
	if err != nil {
		return fmt.Errorf("cannot read markup file: %w", err)
	}
	defer f.Close()
	raw, err := io.ReadAll(io.LimitReader(f, readCap+1))
	if err != nil {
		return fmt.Errorf("cannot read markup file: %w", err)
	}
	hitCap := int64(len(raw)) > readCap
	if hitCap {
		raw = raw[:readCap]
	}
	text := markupToText(string(raw))
	body, truncated := normalizeBodyText(text, opts.MaxBodyChars, hitCap)
	if body != "" {
		res.setBody(body, truncated)
	}
	return nil
}

// markupToText walks the token stream keeping text outside script/style/head.
func markupToText(src string) string {
	tok := html.NewTokenizer(strings.NewReader(src))
	var b strings.Builder
	skip := 0
	for {
		switch tok.Next() {
		case html.ErrorToken:
			return b.String()
		case html.StartTagToken:
			name, _ := tok.TagName()
			switch string(name) {
			case "script", "style", "head":
				skip++
			case "br", "p", "div", "tr", "li", "h1", "h2", "h3", "h4":
				b.WriteByte('\n')
			}
		case html.EndTagToken:
			name, _ := tok.TagName()
			switch string(name) {
			case "script", "style", "head":
				if skip > 0 {
					skip--
				}
			case "p", "div", "tr", "li":
				b.WriteByte('\n')
			}
		case html.TextToken:
			if skip == 0 {
				b.Write(tok.Text())
				b.WriteByte(' ')
			}
		}
	}
}
