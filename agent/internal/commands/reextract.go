package commands

// The `reextract` command (agent parity phase 3): an operator-triggered sweep of
// the EXISTING local index that re-runs extraction and re-emits the results.
// Extraction otherwise only ever runs over files a scan reports as new or
// changed, so an item catalogued before extract_enabled was turned on — or before
// its host gained ffprobe/exiftool/poppler — is never enriched. Like
// suspend/agent_maintenance, this bridges onto a daemon-provided seam
// (Config.RunReextract) so the package stays free of daemon imports.

import "context"

const KindReextract = "reextract"

// processReextract runs the sweep, heartbeating the lease while it runs. The
// heartbeat is not optional here the way it is for a stat_check: a sweep over a
// million-item index outlasts the 300s lease by hours, and without the ack
// central's redelivery sweep would reclaim the command and enqueue it again on
// top of the run already in progress.
func (p *Poller) processReextract(ctx context.Context, cmd commandOut) {
	if p.runReextract == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "reextract unavailable on this agent build",
		})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	// The payload ({"force": bool, "max_items": int}) is passed through verbatim:
	// its keys are the SWEEP's vocabulary, and parsing them here would put the
	// contract in two places.
	result, err := p.runReextract(ctx, cmd.Payload)
	cancel()
	if err != nil {
		p.log.Warn("re-extraction sweep failed", "command_id", cmd.ID, "err", err)
		out := map[string]any{"error": err.Error()}
		// Partial progress is still worth reporting (processAgentMaintenance takes
		// the same posture): a sweep that emitted 40k events before failing has
		// done real, durable work, and an operator resending the command needs to
		// know it resumes rather than restarts.
		for k, v := range result {
			out[k] = v
		}
		p.complete(ctx, cmd.ID, false, out)
		return
	}
	if result == nil {
		result = map[string]any{}
	}
	p.log.Info("re-extraction sweep finished", "command_id", cmd.ID)
	p.complete(ctx, cmd.ID, true, result)
}
