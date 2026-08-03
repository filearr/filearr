package schedule

import (
	"testing"
	"time"
)

func at(s string) time.Time {
	t, err := time.Parse("2006-01-02 15:04", s)
	if err != nil {
		panic(err)
	}
	return t
}

func TestParseErrors(t *testing.T) {
	for _, expr := range []string{
		"", "* * * *", "* * * * * *", "60 * * * *", "* 24 * * *",
		"* * 0 * *", "* * 32 * *", "* * * 13 *", "* * * * 8",
		"a * * * *", "*/0 * * * *", "5-1 * * * *", "1--2 * * * *",
	} {
		if _, err := Parse(expr); err == nil {
			t.Errorf("Parse(%q): want error, got nil", expr)
		}
	}
}

func TestMatches(t *testing.T) {
	cases := []struct {
		expr string
		when string
		want bool
	}{
		{"* * * * *", "2026-08-03 12:34", true},
		{"0 3 * * *", "2026-08-03 03:00", true},
		{"0 3 * * *", "2026-08-03 03:01", false},
		{"0 3 * * *", "2026-08-03 04:00", false},
		{"*/15 * * * *", "2026-08-03 12:45", true},
		{"*/15 * * * *", "2026-08-03 12:50", false},
		{"30 2-4 * * *", "2026-08-03 04:30", true},
		{"30 2-4 * * *", "2026-08-03 05:30", false},
		{"0 0 1,15 * *", "2026-08-15 00:00", true},
		{"0 0 1,15 * *", "2026-08-14 00:00", false},
		{"0 12 * * 0", "2026-08-02 12:00", true},  // Sunday
		{"0 12 * * 7", "2026-08-02 12:00", true},  // 7 ≡ Sunday
		{"0 12 * * 1", "2026-08-02 12:00", false}, // Monday field, Sunday time
		{"0 4 * 12 *", "2026-12-25 04:00", true},
		{"0 4 * 12 *", "2026-11-25 04:00", false},
		{"10-20/5 * * * *", "2026-08-03 12:15", true},
		{"10-20/5 * * * *", "2026-08-03 12:16", false},
	}
	for _, c := range cases {
		cr, err := Parse(c.expr)
		if err != nil {
			t.Fatalf("Parse(%q): %v", c.expr, err)
		}
		if got := cr.Matches(at(c.when)); got != c.want {
			t.Errorf("%q at %s = %v, want %v", c.expr, c.when, got, c.want)
		}
	}
}

func TestVixieEitherOr(t *testing.T) {
	// Both dom and dow restricted: fires on the 1st AND on every Monday.
	cr, err := Parse("0 0 1 * 1")
	if err != nil {
		t.Fatal(err)
	}
	if !cr.Matches(at("2026-08-01 00:00")) { // Saturday the 1st: dom hit
		t.Error("want dom match on the 1st")
	}
	if !cr.Matches(at("2026-08-03 00:00")) { // Monday the 3rd: dow hit
		t.Error("want dow match on Monday")
	}
	if cr.Matches(at("2026-08-04 00:00")) { // Tuesday the 4th: neither
		t.Error("want no match on a plain Tuesday")
	}
}
