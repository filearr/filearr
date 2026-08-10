package main

// Daemon wiring for the `reextract` command (agent parity phase 3).
//
// Extraction runs INSIDE the scan, over the files a scan reports as new or
// changed. That is the right default — it costs nothing on an unchanged tree —
// but it leaves a permanent gap: an item catalogued before `extract_enabled` was
// turned on, or before its host gained ffprobe/exiftool/poppler/tesseract, is
// never enriched, because nothing about it will ever change again. The sweep in
// internal/reextract closes that gap by walking the EXISTING index and re-
// emitting those items; this file is the seam that hands it the live policy, the
// resolved host tools and the daemon's pause state.

import (
	"context"
	"fmt"
	"log/slog"

	agentcfg "github.com/filearr/filearr/agent/internal/config"
	"github.com/filearr/filearr/agent/internal/extract"
	"github.com/filearr/filearr/agent/internal/index"
	"github.com/filearr/filearr/agent/internal/inventory"
	"github.com/filearr/filearr/agent/internal/outbox"
	"github.com/filearr/filearr/agent/internal/reextract"
)

// extractFingerprint renders the extraction CONFIGURATION this host would apply
// right now. The sweep stores it and short-circuits a repeat run at the same
// value, so the fingerprint must cover everything that changes what an
// extraction would PRODUCE — the schema, the four policy knobs, and which host
// tools resolved. Installing exiftool therefore makes the next sweep real work
// again, which is exactly the "my agent gained a capability" case phase 3 exists
// for.
func extractFingerprint(pol agentcfg.Policy) string {
	return reextract.Fingerprint(
		extract.Schema,
		pol.ExtractBodyTextValue(),
		pol.ExtractOCRValue(),
		pol.ExtractEXIFValue(),
		pol.ExtractMaxBytesOr(agentcfg.DefaultExtractMaxBytes),
		inventory.Tools(),
	)
}

// reextractRunner builds the commands.Config.RunReextract seam.
//
// The policy is read at COMMAND time, not at daemon start: an operator's normal
// sequence is "enable extraction, then re-extract", and a runner holding the
// policy from process start would sweep a million files with the pass disabled
// and report a confident zero.
func reextractRunner(idx *index.Store, dataDir string, ops *opState, log *slog.Logger) func(context.Context, map[string]any) (map[string]any, error) {
	return func(ctx context.Context, payload map[string]any) (map[string]any, error) {
		pol, ok, err := agentcfg.LoadCachedPolicy(dataDir)
		if err != nil || !ok {
			// No cached policy means this agent has never been told anything,
			// and a zero Policy has extraction off. Refusing loudly beats
			// sweeping the whole index to produce nothing.
			return nil, fmt.Errorf("no cached central policy on this agent yet")
		}
		if !pol.ExtractEnabledValue() {
			return nil, fmt.Errorf("extract_enabled is off in the effective policy — nothing to re-extract")
		}
		// The same ignored-setting verdict the scan logs, so an operator who
		// triggers a sweep on a host missing a tool sees WHY the result is
		// thinner than they expected, in the log they are already watching.
		agentcfg.NewIgnoredLogger(log).Log(agentcfg.IgnoredSettings(pol, hostExtractCapabilities()))

		fn := newExtractFn(pol, log)
		if fn == nil {
			return nil, fmt.Errorf("extraction pass unavailable for the effective policy")
		}

		res, err := reextract.Run(ctx, idx, reextract.Options{
			Force:    payloadBool(payload, "force"),
			MaxItems: payloadInt(payload, "max_items"),
			FP:       extractFingerprint(pol),
			Extract:  func(c context.Context, absPath, category string) *outbox.Extracted { return fn(c, absPath, category) },
			// A sweep is ordinary replication work: it must stop while the agent
			// is suspended or central is in maintenance, for the same reason the
			// replicator does — the events it produces would only pile up.
			Paused: func() bool { return ops != nil && ops.ReplicationPaused() },
			Log:    log,
		})
		out := map[string]any{
			"seen":      res.Seen,
			"extracted": res.Extracted,
			"skipped":   res.Skipped,
			"failed":    res.Failed,
			"completed": res.Completed,
			"resumed":   res.Resumed,
			"cursor":    res.Cursor,
		}
		if res.Reason != "" {
			out["reason"] = res.Reason
		}
		if err != nil {
			return out, err // partial counters still travel with the failure
		}
		return out, nil
	}
}

// payloadBool / payloadInt read the command payload's optional knobs. JSON
// numbers arrive as float64 through the generic map, so an int knob has to be
// coerced rather than type-asserted; anything malformed falls back to the
// default rather than failing the command, because central already validated
// the body and a stricter agent would only turn a schema drift into a dead
// command.
func payloadBool(p map[string]any, key string) bool {
	v, _ := p[key].(bool)
	return v
}

func payloadInt(p map[string]any, key string) int {
	switch v := p[key].(type) {
	case float64:
		return int(v)
	case int:
		return v
	case int64:
		return int(v)
	}
	return 0
}
