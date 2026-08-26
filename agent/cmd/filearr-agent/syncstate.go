package main

// Sync-with-central status for the local web UI (2026-08-25, user request:
// "show sync status including any backpressure if the central server is
// overloaded or not responding"). Each central-facing loop — policy poll,
// command poll, replication push — reports every attempt's outcome here; the
// Status page renders one row per channel with the last success, the current
// failure streak, the backoff in effect and a plain-language classification.

import (
	"errors"
	"net"
	"os"
	"strings"
	"sync"
	"time"
)

// syncChannel is one central-facing loop's rolling status.
type syncChannel struct {
	LastSuccess time.Time
	LastAttempt time.Time
	LastError   string
	Failures    int
	NextRetry   time.Time
	Backoff     time.Duration
	Class       string // "", unreachable, timeout, overloaded, maintenance, auth, error
}

// syncTracker is the process-wide registry. Cheap to report into (one mutex);
// snapshotted on every Status page load.
type syncTracker struct {
	mu       sync.Mutex
	channels map[string]*syncChannel
	now      func() time.Time
}

var syncStatus = &syncTracker{channels: map[string]*syncChannel{}, now: time.Now}

// report records one attempt's outcome for channel. A nil err is a success
// (resets the streak); otherwise the error and the backoff about to be slept.
func (t *syncTracker) report(channel string, err error, backoff time.Duration) {
	t.mu.Lock()
	defer t.mu.Unlock()
	c := t.channels[channel]
	if c == nil {
		c = &syncChannel{}
		t.channels[channel] = c
	}
	now := t.now()
	c.LastAttempt = now
	if err == nil {
		c.LastSuccess = now
		c.LastError, c.Failures, c.Backoff, c.Class = "", 0, 0, ""
		c.NextRetry = time.Time{}
		return
	}
	c.Failures++
	c.LastError = err.Error()
	c.Backoff = backoff
	c.NextRetry = now.Add(backoff)
	c.Class = classifySyncError(err)
}

// reporter adapts report() to the hook signature the loops accept.
func (t *syncTracker) reporter(channel string) func(err error, backoff time.Duration) {
	return func(err error, backoff time.Duration) { t.report(channel, err, backoff) }
}

// snapshot renders the channels for the settings JSON. Times are RFC 3339;
// absent channels (a loop that has not run yet) are omitted.
func (t *syncTracker) snapshot() map[string]any {
	t.mu.Lock()
	defer t.mu.Unlock()
	out := map[string]any{}
	now := t.now()
	for name, c := range t.channels {
		row := map[string]any{
			"ok":       c.Failures == 0 && !c.LastSuccess.IsZero(),
			"failures": c.Failures,
		}
		if !c.LastSuccess.IsZero() {
			row["last_success_at"] = c.LastSuccess.UTC().Format(time.RFC3339)
			row["since_success_s"] = int(now.Sub(c.LastSuccess).Seconds())
		}
		if !c.LastAttempt.IsZero() {
			row["last_attempt_at"] = c.LastAttempt.UTC().Format(time.RFC3339)
		}
		if c.Failures > 0 {
			row["last_error"] = c.LastError
			row["class"] = c.Class
			row["backoff_s"] = int(c.Backoff.Seconds())
			if !c.NextRetry.IsZero() {
				row["next_retry_at"] = c.NextRetry.UTC().Format(time.RFC3339)
				if left := c.NextRetry.Sub(now); left > 0 {
					row["retry_in_s"] = int(left.Seconds())
				}
			}
		}
		out[name] = row
	}
	return out
}

// classifySyncError buckets a transport/central error into the handful of
// situations an operator acts on differently. Backpressure — central alive but
// shedding load (429/503) or in maintenance — is distinct from central being
// down (connection refused / DNS) or slow (timeouts), and both from a rejected
// credential (which no amount of waiting fixes).
func classifySyncError(err error) string {
	if err == nil {
		return ""
	}
	msg := strings.ToLower(err.Error())
	switch {
	case strings.Contains(msg, "maintenance"):
		return "maintenance"
	case strings.Contains(msg, " 503") || strings.Contains(msg, "(503)") ||
		strings.Contains(msg, " 429") || strings.Contains(msg, "(429)") ||
		strings.Contains(msg, "too many requests") || strings.Contains(msg, "service unavailable") ||
		strings.Contains(msg, " 502") || strings.Contains(msg, "(502)") || strings.Contains(msg, "bad gateway"):
		return "overloaded"
	case strings.Contains(msg, "401") || strings.Contains(msg, "403") ||
		strings.Contains(msg, "rejected the agent") || strings.Contains(msg, "unauthorized") || strings.Contains(msg, "forbidden"):
		return "auth"
	}
	var nerr net.Error
	if errors.As(err, &nerr) && nerr.Timeout() {
		return "timeout"
	}
	if errors.Is(err, os.ErrDeadlineExceeded) || strings.Contains(msg, "timeout") || strings.Contains(msg, "deadline exceeded") {
		return "timeout"
	}
	if strings.Contains(msg, "connection refused") || strings.Contains(msg, "no such host") ||
		strings.Contains(msg, "network is unreachable") || strings.Contains(msg, "no route to host") ||
		strings.Contains(msg, "connection reset") || strings.Contains(msg, "eof") {
		return "unreachable"
	}
	return "error"
}
