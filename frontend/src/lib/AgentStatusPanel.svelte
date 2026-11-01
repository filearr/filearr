<script lang="ts">
  // P10-T10 — hosting-agent identity, online status, and inline Verify for an
  // agent-hosted item. Shows the owning agent's name, an online/offline badge
  // (with a relative "last seen"), the "last verified …" freshness, and a split
  // Verify button (fast stat / full rehash). Issuing a Verify polls
  // ``/agent-status`` until ``last_verified_at`` advances (freshness updates in
  // place — NO full transfer) or a timeout, which then messages clearly (offline
  // agents queue the check; never a bare spinner). Mirrors RetrievePanel's tone.
  import { relTime } from "./format";
  import { onDestroy } from "svelte";
  import {
    friendlyError,
    itemAgentStatus,
    verifyItem,
    type ItemAgentStatus,
  } from "./api";
  import { Alert, Badge, Button, toneForStatus } from "./ui";

  let { itemId }: { itemId: string } = $props();

  let status = $state<ItemAgentStatus | null>(null);
  let verifying = $state(false);
  let message = $state<string | null>(null);
  // A failed Verify request; shown as a dismissable Alert under the buttons.
  let verifyError = $state<string | null>(null);

  // Bumped whenever the item changes; async loops capture it and bail if stale.
  let token = 0;

  const online = $derived(status?.online === true);
  const agentStatus = $derived(status?.agent_status ?? "active");
  const inFlight = $derived(status?.verify_in_flight === true);
  const lastVerifiedAt = $derived(status?.last_verified_at ?? null);
  const lastSeenAt = $derived(status?.last_seen_at ?? null);

  const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

  async function refresh(): Promise<ItemAgentStatus | null> {
    const mine = token;
    try {
      const s = await itemAgentStatus(itemId);
      if (mine !== token) return null; // a newer item superseded this fetch
      status = s;
      return s;
    } catch {
      return null; // a transient failure just leaves the last-known status
    }
  }

  // Poll agent-status until last_verified_at moves past ``baseline`` (the check
  // completed) or the budget lapses. Offline / lapsed → a clear queued message.
  async function pollUntilVerified(baseline: string | null): Promise<void> {
    const mine = token;
    const deadline = Date.now() + 60_000; // ~1 min budget, then message + stop
    while (Date.now() < deadline) {
      await sleep(3000);
      if (mine !== token) return;
      const s = await refresh();
      if (mine !== token) return;
      if (s && s.agent_hosted && (s.last_verified_at ?? null) !== baseline) {
        message = `Verified ${relTime(s.last_verified_at)}`;
        return;
      }
    }
    if (mine !== token) return;
    message = online
      ? "Still verifying — the agent hasn't reported back yet. It will update shortly."
      : "Agent offline — the check is queued and will run once it reconnects.";
  }

  async function runVerify(mode: "stat" | "rehash") {
    if (verifying || inFlight) return;
    const mine = token;
    verifying = true;
    const baseline = lastVerifiedAt;
    message = mode === "stat" ? "Verification requested…" : "Full rehash requested…";
    verifyError = null;
    try {
      await verifyItem(itemId, mode);
      await refresh(); // reflect verify_in_flight immediately
      await pollUntilVerified(baseline);
    } catch (e) {
      if (mine === token) {
        message = null;
        verifyError = friendlyError(e);
      }
    } finally {
      // A newer item reset `verifying` already and may have its own run going.
      if (mine === token) verifying = false;
    }
  }

  $effect(() => {
    itemId; // track
    token += 1;
    status = null;
    verifying = false;
    message = null;
    verifyError = null;
    void refresh();
  });

  // M18: stop the verify poll loop (and drop in-flight results) on unmount.
  onDestroy(() => {
    token += 1;
  });
</script>

{#if status && status.agent_hosted}
  <div class="flex flex-wrap items-center gap-2 border-b border-border pb-3 text-sm">
    <span class="font-medium text-muted">Agent-hosted</span>
    {#if status.agent_name}
      <span class="font-mono text-muted">{status.agent_name}</span>
    {/if}

    <!-- Online / offline badge. -->
    {#if online}
      <Badge tone="ok" class="gap-1">
        <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true"></span>online
      </Badge>
    {:else}
      <span title={lastSeenAt ?? "never seen"}>
        <Badge class="gap-1">
          <span class="h-1.5 w-1.5 rounded-full bg-current" aria-hidden="true"></span>
          offline{#if lastSeenAt} · seen {relTime(lastSeenAt)}{/if}
        </Badge>
      </span>
    {/if}

    <!-- Lifecycle chip for a non-active agent. -->
    {#if agentStatus === "revoked" || agentStatus === "pending"}
      <Badge tone={toneForStatus(agentStatus)}>{agentStatus}</Badge>
    {/if}

    <!-- Freshness. -->
    {#if lastVerifiedAt}
      <span class="text-muted" title={lastVerifiedAt}>last verified {relTime(lastVerifiedAt)}</span>
    {:else}
      <span class="text-muted">never verified</span>
    {/if}

    <span class="grow"></span>
    <div class="inline-flex gap-1">
      <Button
        size="sm"
        disabled={verifying || inFlight || agentStatus === "revoked"}
        onclick={() => runVerify("stat")}
        title="Ask the agent to confirm the file still exists (fast)">Verify</Button>
      <Button
        size="sm"
        disabled={verifying || inFlight || agentStatus === "revoked"}
        onclick={() => runVerify("rehash")}
        title="Ask the agent to re-hash the file's contents (slower, detects silent changes)"
        >Full rehash</Button>
    </div>
  </div>
  {#if verifyError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (verifyError = null)}>{verifyError}</Alert>
  {/if}
  {#if message}
    <p class="mt-2 text-xs text-accent-text" role="status">{message}</p>
  {/if}
{/if}
