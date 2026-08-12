package commands

// The `rehash_sweep` command (QH-T6, 2026-08-12): an operator-triggered sweep of
// the EXISTING local index that re-reads every file in the 64-128 KiB defect
// band, recomputes its hashes under the post-QH-T1 rules, and re-emits the ones
// that changed. Like suspend/agent_maintenance/reextract, it bridges onto a
// daemon-provided seam (Config.RunRehashSweep) so this package stays free of
// daemon imports.
//
// NOT `rehash_check`. The two are different things and the names are close
// enough to be dangerous:
//
//	rehash_check  — ITEM-scoped, one file, on demand. Central asks "what does
//	                this specific item hash to right now?" and the executor
//	                (executor.go) answers with a CommandResult; nothing is
//	                written locally and nothing is replicated. It is a verify.
//	rehash_sweep  — AGENT-scoped, no item_id, hours long. A resumable migration
//	                over a whole size band that WRITES corrected rows to the
//	                local index and emits replication events for them.
//
// A verify answers a question; a sweep changes the world. Keep the names apart.

import "context"

const KindRehashSweep = "rehash_sweep"

// processRehashSweep runs the sweep, heartbeating the lease while it runs. The
// heartbeat is not optional here, for the same reason it is not for reextract: a
// sweep over ~99k files re-read end to end outlasts the 300s lease by hours, and
// without the ack central's redelivery sweep would reclaim the command and
// enqueue it again on top of the run already in progress — two sweeps fighting
// over one cursor, double-emitting the rows they raced on.
func (p *Poller) processRehashSweep(ctx context.Context, cmd commandOut) {
	if p.runRehashSweep == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "rehash_sweep unavailable on this agent build",
		})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	// The payload ({"force", "max_items", "min_size", "max_size"}) is passed
	// through verbatim: its keys are the SWEEP's vocabulary, and parsing them
	// here would put the contract in two places.
	result, err := p.runRehashSweep(ctx, cmd.Payload)
	cancel()
	if err != nil {
		p.log.Warn("quick_hash migration sweep failed", "command_id", cmd.ID, "err", err)
		out := map[string]any{"error": err.Error()}
		// Partial progress is still worth reporting (processReextract and
		// processAgentMaintenance take the same posture): a sweep that corrected
		// 40k rows before failing has done real, durable work, and an operator
		// resending the command needs to know it resumes rather than restarts.
		for k, v := range result {
			out[k] = v
		}
		p.complete(ctx, cmd.ID, false, out)
		return
	}
	if result == nil {
		result = map[string]any{}
	}
	p.log.Info("quick_hash migration sweep finished", "command_id", cmd.ID)
	p.complete(ctx, cmd.ID, true, result)
}
