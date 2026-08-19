package extract

import (
	"os"
	"path/filepath"
	"testing"
)

func TestMarkupBodyStripsTagsAndScript(t *testing.T) {
	dir := t.TempDir()
	p := filepath.Join(dir, "page.html")
	os.WriteFile(p, []byte(`<html><head><style>p{}</style><title>T</title></head>`+
		`<body><h1>Heading</h1><p>Real &amp; visible</p><script>evil()</script></body></html>`), 0o644)
	res := &Result{Meta: map[string]any{}}
	if err := extractMarkupBody(p, Options{MaxBodyChars: 1000}.withDefaults(), res); err != nil {
		t.Fatal(err)
	}
	body := res.BodyText
	if body == "" || !contains(body, "Heading") || !contains(body, "Real & visible") {
		t.Fatalf("body = %q", body)
	}
	if contains(body, "evil") || contains(body, "style") {
		t.Fatalf("script/style leaked: %q", body)
	}

	x := filepath.Join(dir, "m.xml")
	os.WriteFile(x, []byte(`<movie><title>Heat</title><plot>Bank robbers.</plot></movie>`), 0o644)
	res = &Result{Meta: map[string]any{}}
	if err := extractMarkupBody(x, Options{MaxBodyChars: 1000}.withDefaults(), res); err != nil {
		t.Fatal(err)
	}
	if !contains(res.BodyText, "Heat") || !contains(res.BodyText, "Bank robbers.") {
		t.Fatalf("xml body = %q", res.BodyText)
	}
}

func contains(s, sub string) bool {
	return len(s) >= len(sub) && (func() bool {
		for i := 0; i+len(sub) <= len(s); i++ {
			if s[i:i+len(sub)] == sub {
				return true
			}
		}
		return false
	})()
}
