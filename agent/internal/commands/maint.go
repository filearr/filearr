package commands

// Agent-scoped lifecycle commands (2026-08-09): `suspend` pauses/resumes the
// agent's own processing (scan scheduling + replication push — command polling
// and health reporting continue, or the operator could never resume it), and
// `agent_maintenance` runs the local cleanup pass (index VACUUM, outbox prune,
// temp sweep). Both bridge onto daemon-provided seams (Config.SetSuspended /
// Config.RunMaintenance) exactly like self_update bridges onto TriggerUpdate,
// keeping this package free of daemon imports.

import (
	"context"
	"fmt"
)

const (
	KindSuspend          = "suspend"
	KindAgentMaintenance = "agent_maintenance"
)

// processSuspend applies the desired suspended state from the command payload
// ({"suspended": bool}) via the daemon seam and reports the applied value.
func (p *Poller) processSuspend(ctx context.Context, cmd commandOut) {
	if p.setSuspended == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "suspend unavailable on this agent build",
		})
		return
	}
	want, ok := cmd.Payload["suspended"].(bool)
	if !ok {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "payload.suspended (bool) required",
		})
		return
	}
	if err := p.setSuspended(ctx, want); err != nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": fmt.Sprintf("apply suspend: %v", err),
		})
		return
	}
	p.log.Info("agent processing state changed by central command", "suspended", want)
	p.complete(ctx, cmd.ID, true, map[string]any{"suspended": want})
}

// processAgentMaintenance runs the local maintenance pass, heartbeating the
// lease while it runs (a VACUUM over a large index can outlast the lease).
func (p *Poller) processAgentMaintenance(ctx context.Context, cmd commandOut) {
	if p.runMaint == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "agent maintenance unavailable on this agent build",
		})
		return
	}
	hbCtx, cancel := context.WithCancel(ctx)
	go p.heartbeat(hbCtx, cmd.ID)
	result, err := p.runMaint(ctx)
	cancel()
	if err != nil {
		p.log.Warn("agent maintenance failed", "command_id", cmd.ID, "err", err)
		out := map[string]any{"error": err.Error()}
		for k, v := range result { // partial progress is still worth reporting
			out[k] = v
		}
		p.complete(ctx, cmd.ID, false, out)
		return
	}
	if result == nil {
		result = map[string]any{}
	}
	p.log.Info("agent maintenance finished", "command_id", cmd.ID)
	p.complete(ctx, cmd.ID, true, result)
}
