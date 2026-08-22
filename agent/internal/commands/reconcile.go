package commands

// The `reconcile` command (2026-08-22): central asks the agent for one
// immediate full-manifest consistency sweep. Like reextract/rehash_sweep it is
// AGENT-scoped (no item_id) and bridges onto a daemon-provided seam
// (Config.RunReconcile → reconcile.Supervisor.RunNow) so this package stays
// free of reconcile imports. The seam routes through the supervisor's
// single-flight gate, so a command can never interleave with a periodic or
// reconnect-triggered sweep already in flight.
//
// Why central needs this at all: the agent's own triggers are its 24h uptime
// ticker, a >24h replication outage, and a cursor dead-end — none of which
// ever fire on a desktop-pattern machine that sleeps or restarts daily (agent
// XENON showed "Last reconcile: never" permanently). The daemon's startup
// catch-up fixes the steady state; this command is the operator's immediate
// handle from the console.

import "context"

const KindReconcile = "reconcile"

// processReconcile runs the sweep, heartbeating the lease while it runs — a
// manifest digest over a million-row index plus a possible full row stream
// can outlast the 300s lease, and without the ack central's redelivery sweep
// would reclaim the command and enqueue a second sweep on top of the first.
func (p *Poller) processReconcile(ctx context.Context, cmd commandOut) {
	if p.runReconcile == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "reconcile unavailable on this agent build",
		})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	// Payload ({"force_reset": bool}) passes through verbatim — its keys are
	// the sweep's vocabulary (reconcile.Options), not this package's.
	result, err := p.runReconcile(ctx, cmd.Payload)
	cancel()
	if err != nil {
		p.log.Warn("reconcile sweep failed", "command_id", cmd.ID, "err", err)
		out := map[string]any{"error": err.Error()}
		// Partial progress is durable (roots that reconciled stay reconciled)
		// and worth reporting — same posture as reextract/rehash_sweep.
		for k, v := range result {
			out[k] = v
		}
		p.complete(ctx, cmd.ID, false, out)
		return
	}
	if result == nil {
		result = map[string]any{}
	}
	p.log.Info("reconcile sweep finished", "command_id", cmd.ID)
	p.complete(ctx, cmd.ID, true, result)
}
