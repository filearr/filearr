// Package schedule evaluates 5-field cron expressions at minute granularity
// for the in-daemon scan scheduler. Deliberately tiny: numeric fields with
// `*`, lists, ranges, and steps — no names, no @keywords, no seconds field —
// matching the subset central validates for `library.scan_cron` (cronsim).
// Standard (vixie) day semantics: when BOTH day-of-month and day-of-week are
// restricted, a time matches if EITHER matches.
package schedule

import (
	"fmt"
	"strconv"
	"strings"
	"time"
)

// Cron is a parsed 5-field expression. Fields are bitmasks (minute 0-59,
// hour 0-23, dom 1-31, month 1-12, dow 0-6 with 7 normalized to 0).
type Cron struct {
	min, hour, dom, mon, dow uint64
	domStar, dowStar         bool
}

// Parse parses "min hour dom month dow". Returns an error naming the bad
// field so an operator sees exactly what to fix.
func Parse(expr string) (*Cron, error) {
	fields := strings.Fields(strings.TrimSpace(expr))
	if len(fields) != 5 {
		return nil, fmt.Errorf("cron %q: want 5 fields, got %d", expr, len(fields))
	}
	c := &Cron{}
	specs := []struct {
		name     string
		src      string
		lo, hi   int
		dst      *uint64
		star     *bool
		wrapHigh bool // dow: 7 ≡ 0
	}{
		{"minute", fields[0], 0, 59, &c.min, nil, false},
		{"hour", fields[1], 0, 23, &c.hour, nil, false},
		{"day-of-month", fields[2], 1, 31, &c.dom, &c.domStar, false},
		{"month", fields[3], 1, 12, &c.mon, nil, false},
		{"day-of-week", fields[4], 0, 7, &c.dow, &c.dowStar, true},
	}
	for _, s := range specs {
		mask, star, err := parseField(s.src, s.lo, s.hi)
		if err != nil {
			return nil, fmt.Errorf("cron %q: %s: %w", expr, s.name, err)
		}
		if s.wrapHigh && mask&(1<<7) != 0 { // dow 7 → 0 (Sunday)
			mask = (mask &^ (1 << 7)) | 1
		}
		*s.dst = mask
		if s.star != nil {
			*s.star = star
		}
	}
	return c, nil
}

// parseField parses one field into a bitmask. star reports a bare "*" (no
// step), which day matching needs for the vixie either-or rule.
func parseField(src string, lo, hi int) (mask uint64, star bool, err error) {
	if src == "*" {
		for i := lo; i <= hi; i++ {
			mask |= 1 << uint(i)
		}
		return mask, true, nil
	}
	for _, part := range strings.Split(src, ",") {
		rng, step := part, 1
		if base, st, found := strings.Cut(part, "/"); found {
			rng = base
			step, err = strconv.Atoi(st)
			if err != nil || step < 1 {
				return 0, false, fmt.Errorf("bad step %q", part)
			}
		}
		start, end := lo, hi
		switch {
		case rng == "*":
			// full range with step
		case strings.Contains(rng, "-"):
			a, b, _ := strings.Cut(rng, "-")
			if start, err = strconv.Atoi(a); err != nil {
				return 0, false, fmt.Errorf("bad range %q", part)
			}
			if end, err = strconv.Atoi(b); err != nil {
				return 0, false, fmt.Errorf("bad range %q", part)
			}
		default:
			if start, err = strconv.Atoi(rng); err != nil {
				return 0, false, fmt.Errorf("bad value %q", part)
			}
			end = start
		}
		if start < lo || end > hi || start > end {
			return 0, false, fmt.Errorf("value %q out of range %d-%d", part, lo, hi)
		}
		for i := start; i <= end; i += step {
			mask |= 1 << uint(i)
		}
	}
	if mask == 0 {
		return 0, false, fmt.Errorf("empty field %q", src)
	}
	return mask, false, nil
}

// Matches reports whether t's minute satisfies the expression (seconds are
// ignored; callers fire at most once per matching minute).
func (c *Cron) Matches(t time.Time) bool {
	if c.min&(1<<uint(t.Minute())) == 0 ||
		c.hour&(1<<uint(t.Hour())) == 0 ||
		c.mon&(1<<uint(int(t.Month()))) == 0 {
		return false
	}
	domHit := c.dom&(1<<uint(t.Day())) != 0
	dowHit := c.dow&(1<<uint(int(t.Weekday()))) != 0
	// Vixie rule: both restricted → either matches; otherwise both must hold
	// (a star field always holds).
	if !c.domStar && !c.dowStar {
		return domHit || dowHit
	}
	return domHit && dowHit
}
