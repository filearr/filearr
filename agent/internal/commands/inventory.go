package commands

import (
	"bytes"
	"context"
	"encoding/json"
	"errors"
	"fmt"
	"io"
	"net/http"
	"time"

	"github.com/filearr/filearr/agent/internal/inventory"
)

// uploadRefused marks a 4xx from the inventory-results endpoint: central
// looked at the request and said no (too large, wrong command, not gzip…), so
// resending the same bytes cannot succeed and the retry loop stops early.
type uploadRefused struct {
	status int
	err    error
}

func (e *uploadRefused) Error() string { return e.err.Error() }
func (e *uploadRefused) Unwrap() error { return e.err }

// KindInventory is the W6-D3 extensible-inventory command kind.
const KindInventory = "inventory"

// decodeInventoryPayload narrows the JSONB command payload into an
// inventory.Command. It is tolerant of absent/typed-loosely fields (JSON numbers
// decode to float64) so a hand-authored or forward-compat payload does not fail
// the whole command; unknown keys are ignored (the vocabulary the agent honors is
// what it advertised).
func decodeInventoryPayload(raw map[string]any) inventory.Command {
	return inventory.Command{
		Collectors:   stringSlice(raw["collectors"]),
		Preset:       stringOf(raw["preset"]),
		Paths:        stringSlice(raw["paths"]),
		IncludeRegex: stringSlice(raw["include_regex"]),
		ExcludeRegex: stringSlice(raw["exclude_regex"]),
		MaxEntries:   intOf(raw["max_entries"]),
		MaxDepth:     intOf(raw["max_depth"]),
	}
}

func stringOf(v any) string {
	s, _ := v.(string)
	return s
}

func stringSlice(v any) []string {
	arr, ok := v.([]any)
	if !ok {
		return nil
	}
	out := make([]string, 0, len(arr))
	for _, e := range arr {
		if s, ok := e.(string); ok {
			out = append(out, s)
		}
	}
	return out
}

func intOf(v any) int {
	switch n := v.(type) {
	case float64:
		return int(n)
	case int:
		return n
	case int64:
		return int(n)
	case json.Number:
		i, _ := n.Int64()
		return int(i)
	default:
		return 0
	}
}

// processInventory runs an inventory command and reports its result, heartbeating
// the lease throughout (a broad walk can outlast one lease). A small result inlines
// its {summary, entries} in the command completion; a large result is gzipped and
// uploaded to the inventory-results endpoint, with the completion carrying only
// {summary, result_ref}. An agent build without an inventory Runner completes
// ok=false (graceful degradation).
func (p *Poller) processInventory(ctx context.Context, cmd commandOut) {
	if p.inv == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{"error": "inventory not supported by this agent"})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	defer cancel()

	started := p.clock().UTC()
	payload := decodeInventoryPayload(cmd.Payload)
	report := func(status string, extra map[string]any) {
		if p.invStatus == nil {
			return
		}
		st := map[string]any{
			"command_id": cmd.ID,
			"status":     status,
			"started_at": started.Format(time.RFC3339),
			"collectors": payload.Collectors,
			"paths":      payload.Paths,
			"preset":     payload.Preset,
			"updated_at": p.clock().UTC().Format(time.RFC3339),
		}
		for k, v := range extra {
			st[k] = v
		}
		p.invStatus(st)
	}
	report("running", nil)

	res, err := p.inv.Run(ctx, payload)
	if err != nil {
		cancel()
		p.log.Warn("inventory command failed", "command_id", cmd.ID, "err", err)
		report("failed", map[string]any{"error": err.Error()})
		p.complete(ctx, cmd.ID, false, map[string]any{"error": err.Error()})
		return
	}

	summary := summaryMap(res.Summary)
	if res.Inlineable() {
		cancel()
		report("finished", map[string]any{"summary": summary, "delivery": "inline"})
		p.complete(ctx, cmd.ID, true, map[string]any{"summary": summary, "entries": res.Inline})
		return
	}

	// Large result: upload the gzip NDJSON blob, then complete with a ref.
	report("uploading", map[string]any{"summary": summary, "blob_bytes": len(res.Blob)})
	ref, uerr := p.uploadInventoryResult(ctx, cmd.ID, res.Blob)
	cancel()
	if uerr != nil {
		p.log.Warn("inventory result upload failed", "command_id", cmd.ID, "err", uerr)
		report("failed", map[string]any{"summary": summary, "error": "upload: " + uerr.Error()})
		p.complete(ctx, cmd.ID, false, map[string]any{"summary": summary, "error": uerr.Error()})
		return
	}
	report("finished", map[string]any{"summary": summary, "delivery": "uploaded", "result_ref": ref})
	p.complete(ctx, cmd.ID, true, map[string]any{"summary": summary, "result_ref": ref})
}

// uploadInventoryResult POSTs the gzip NDJSON blob to central's inventory-results
// endpoint (a dedicated small-blob channel mirroring agent_thumbs' write-if-absent
// posture — NOT the staging plane, which is sized for multi-GB media and re-hashes
// against a catalog row). Returns the stored ref central echoes back.
//
// 2026-08-25: the shared client's 30 s Timeout is right for a poll and wrong
// for this — a 100k-entry permissions blob is megabytes over a home uplink,
// and central used to ingest it INSIDE the request (minutes; live: "Client.
// Timeout exceeded while awaiting headers", result discarded). The upload
// now runs on a copy of the client with uploadTimeout, and retries: the
// endpoint is write-if-absent, so a redelivery of the same bytes is a no-op.
func (p *Poller) uploadInventoryResult(ctx context.Context, commandID string, blob []byte) (string, error) {
	var lastErr error
	for attempt := 0; attempt < uploadAttempts; attempt++ {
		if attempt > 0 {
			select {
			case <-ctx.Done():
				return "", ctx.Err()
			case <-time.After(uploadBackoff(attempt)):
			}
		}
		ref, err := p.uploadInventoryResultOnce(ctx, commandID, blob)
		if err == nil {
			return ref, nil
		}
		lastErr = err
		var refused *uploadRefused
		if errors.As(err, &refused) {
			return "", err // central refused the blob outright; retrying cannot help
		}
		p.log.Warn("inventory result upload failed; will retry",
			"command_id", commandID, "attempt", attempt+1, "of", uploadAttempts, "err", err)
	}
	return "", lastErr
}

// uploadTimeout bounds one upload attempt end-to-end (dial, send the blob,
// wait for central's ack). Central acks as soon as the blob is stored and
// ingests it in a worker, so this is dominated by the transfer itself.
const uploadTimeout = 10 * time.Minute

// uploadAttempts is the total number of tries; uploadBackoff spaces them.
const uploadAttempts = 4

func uploadBackoff(attempt int) time.Duration {
	return time.Duration(attempt*attempt) * 15 * time.Second // 15s, 60s, 135s
}

func (p *Poller) uploadInventoryResultOnce(ctx context.Context, commandID string, blob []byte) (string, error) {
	url := fmt.Sprintf("%s/api/v1/agents/%s/inventory-results", p.baseURL, p.agentID)
	req, err := http.NewRequestWithContext(ctx, http.MethodPost, url, bytes.NewReader(blob))
	if err != nil {
		return "", err
	}
	req.Header.Set("Content-Type", "application/gzip")
	req.Header.Set("Accept", "application/json")
	req.Header.Set("X-Filearr-Command-Id", commandID)
	if tok := p.authFn(); tok != "" {
		req.Header.Set("Authorization", "Bearer "+tok)
	}
	// A shallow copy shares the transport (connection pool, TLS/mTLS config)
	// and only lengthens the overall deadline for this one request.
	client := *p.http
	client.Timeout = uploadTimeout
	resp, err := client.Do(req)
	if err != nil {
		return "", err
	}
	defer resp.Body.Close()
	body, _ := io.ReadAll(io.LimitReader(resp.Body, 1<<20))
	if resp.StatusCode != http.StatusOK && resp.StatusCode != http.StatusCreated {
		err := p.statusError("inventory-results", resp.StatusCode, body)
		if resp.StatusCode >= 400 && resp.StatusCode < 500 &&
			resp.StatusCode != http.StatusRequestTimeout && resp.StatusCode != http.StatusTooManyRequests {
			return "", &uploadRefused{status: resp.StatusCode, err: err}
		}
		return "", err
	}
	var env struct {
		ResultRef string `json:"result_ref"`
	}
	if err := json.Unmarshal(body, &env); err != nil {
		return "", fmt.Errorf("inventory-results: decode body: %w", err)
	}
	return env.ResultRef, nil
}

// summaryMap converts the inventory Summary to the JSON map central stores in the
// completion result. json round-trips the struct's tags (the canonical shape).
func summaryMap(s inventory.Summary) map[string]any {
	b, _ := json.Marshal(s)
	var m map[string]any
	_ = json.Unmarshal(b, &m)
	return m
}
