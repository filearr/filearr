package commands

import "context"

// KindSelfUpdate is the operator-triggered "apply an update at next check-in"
// command (console button, 2026-08-05). Agent-scoped: central enqueues it with
// no item_id. The heavy lifting lives in internal/update; this handler only
// bridges the command lifecycle onto Updater.TriggerNow via Config.TriggerUpdate.
const KindSelfUpdate = "self_update"

// processSelfUpdate runs one immediate update check-and-apply.
//
// Completion ordering is the delicate part: a SUCCESSFUL apply exits the
// process (service-managed restart) and can never report afterwards, so the
// trigger invokes beforeApply — where we post the terminal "applying" result —
// once the manifest+artifact have passed every check but before the swap.
// Central then confirms the outcome out-of-band from the version the agent
// reports on its next manifest poll (the §6.3 confirmed-version signal). An
// apply failure AFTER that completion can only be logged; the version central
// observes stays unchanged, so the console keeps showing the update as
// available.
func (p *Poller) processSelfUpdate(ctx context.Context, cmd commandOut) {
	if p.updateTrigger == nil {
		p.complete(ctx, cmd.ID, false, map[string]any{
			"error": "self-update unavailable (disabled by FILEARR_AGENT_SELF_UPDATE or not running as the daemon)",
		})
		return
	}
	completed := false
	version, applying, err := p.updateTrigger(ctx, func(v string) {
		completed = true
		p.complete(ctx, cmd.ID, true, map[string]any{"status": "applying", "version": v})
	})
	switch {
	case err != nil && !completed:
		p.complete(ctx, cmd.ID, false, map[string]any{"error": err.Error()})
	case err != nil:
		p.log.Error("self_update: apply failed after the applying result was posted",
			"version", version, "err", err)
	case !applying:
		res := map[string]any{"status": "up-to-date"}
		if version != "" {
			res["central_offers"] = version
		}
		p.complete(ctx, cmd.ID, false, res)
	default:
		// applying: the process is exiting (or a stubbed test exit returned) —
		// the completion was already posted in beforeApply.
	}
}
