<script lang="ts">
  // P10-T6/T7 — the browser-side retrieve UX for an agent-hosted item.
  //
  // Flow: Retrieve → POST initiate → drive a progress component from the
  // /transfers/{id}/events SSE stream. States map to clear human phases:
  //   waiting for the agent (offline is NORMAL, never a spinner) → agent
  //   preparing → uploading (progress bar) → verifying → ready → Download.
  // Failure modes get explicit messaging, never a spinner: failed integrity
  // verification, an offline_timeout (agent stayed offline past the window), and
  // an expired staged file. A 409 "active transfer" from initiate attaches to the
  // EXISTING transfer (its id parsed from the error) rather than erroring.
  import {
    cancelTransfer,
    errorMessage,
    friendlyError,
    initiateTransfer,
    probeDownload,
    transferDownloadUrl,
    transferEventsUrl,
    mintTransferEventsToken,
    type TransferEvent,
  } from "./api";
  import { Alert, Button } from "./ui";

  let { itemId, filename }: { itemId: string; filename: string } = $props();

  type Phase =
    | "idle"
    | "starting"
    | "waiting" // agent offline / not yet polled — the P10-T7 normal case
    | "preparing" // command picked up, upload not yet flowing
    | "uploading"
    | "verifying"
    | "ready" // staged + verified — downloadable
    | "downloaded"
    | "failed" // integrity verification failed
    | "offline" // offline_timeout — TTL lapsed while the agent never came
    | "expired" // staged file TTL lapsed
    | "cancelled";

  let phase = $state<Phase>("idle");
  let transferId = $state<string | null>(null);
  let bytes = $state(0);
  let total = $state<number | null>(null);
  let message = $state("");
  let downloading = $state(false);
  // A29: a failed Download (probe 409/401/403/network) — one shared slot under
  // the action row, shown whatever the phase.
  let downloadError = $state("");
  let es: EventSource | null = null;
  // A29: the 2 s reconnect timer, cleared on close / item change / unmount so
  // a pending retry never reopens a stream on a destroyed panel.
  let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
  // A29: Cancel pressed while initiate is still in flight (no id yet); honoured
  // as soon as initiateTransfer resolves. State (item 18): Cancel reads
  // "Cancelling…" and is disabled until then.
  let cancelRequested = $state(false);
  // Bumped by the reset effect's cleanup (item change / unmount): an initiate
  // that resolves for an older generation is dropped.
  let generation = 0;

  const pct = $derived(
    total && total > 0 ? Math.min(100, Math.round((bytes / total) * 100)) : 0,
  );
  const active = $derived(
    phase === "starting" ||
      phase === "waiting" ||
      phase === "preparing" ||
      phase === "uploading" ||
      phase === "verifying",
  );
  const canDownload = $derived(phase === "ready" || phase === "downloaded");
  const failedPhase = $derived(
    phase === "failed" ||
      phase === "offline" ||
      phase === "expired" ||
      phase === "cancelled",
  );

  const PHASE_LABEL: Record<Phase, string> = {
    idle: "",
    starting: "Requesting retrieve…",
    waiting: "Waiting for the agent to come online…",
    preparing: "Agent is preparing the file…",
    uploading: "Uploading from agent…",
    verifying: "Verifying integrity…",
    ready: "Ready to download",
    downloaded: "Downloaded",
    failed: "Retrieve failed",
    offline: "Agent offline",
    expired: "Retrieve expired",
    cancelled: "Retrieve cancelled",
  };

  function closeStream() {
    clearTimeout(reconnectTimer);
    reconnectTimer = undefined;
    es?.close();
    es = null;
  }

  function applyEvent(d: TransferEvent) {
    if (typeof d.bytes_transferred === "number") bytes = d.bytes_transferred;
    if (d.total_bytes != null) total = d.total_bytes;
    switch (d.state) {
      case "pending":
        phase = d.waiting_for_agent ? "waiting" : "preparing";
        break;
      case "uploading":
        phase = "uploading";
        break;
      case "staged":
        phase = d.verified ? "ready" : "verifying";
        break;
      case "downloaded":
        phase = "downloaded";
        break;
      case "failed":
        phase = "failed";
        message = "The file failed integrity verification and was not staged.";
        break;
      case "expired":
        if (d.reason === "offline_timeout") {
          phase = "offline";
          message =
            "The agent stayed offline past the retrieve window. Try again once it reconnects.";
        } else if (d.reason === "cancelled") {
          phase = "cancelled";
          message = "This retrieve was cancelled.";
        } else {
          phase = "expired";
          message = "The staged file expired. Start a new retrieve to download it.";
        }
        break;
      default:
        // An error frame for an unknown id carries only { detail }.
        if (d.detail && !d.state) {
          phase = "failed";
          message = d.detail;
        }
    }
  }

  let opening = false;

  async function openStream(id: string) {
    if (opening) return;
    opening = true;
    closeStream();
    const gen = generation;
    // EventSource can't set headers, so mint a single-use scoped stream token
    // (?stream_token=) per connect — the real API key never rides the URL. A
    // failed mint tries tokenless (works with auth off; 401s into the error
    // path with auth on).
    let token = "";
    try {
      token = (await mintTransferEventsToken(id)).token;
    } catch {
      /* tokenless attempt below */
    }
    opening = false;
    // Unmounted / item changed during the mint: open nothing.
    if (gen !== generation) return;
    es = new EventSource(transferEventsUrl(id, token));
    let terminated = false;
    const onFrame = (ev: MessageEvent) => {
      try {
        applyEvent(JSON.parse(ev.data));
      } catch {
        /* ignore a malformed frame */
      }
    };
    es.addEventListener("progress", onFrame);
    es.addEventListener("offline_timeout", onFrame);
    es.addEventListener("done", (ev) => {
      terminated = true;
      onFrame(ev as MessageEvent);
      closeStream();
    });
    es.addEventListener("error", (ev) => {
      // A named server `error` frame carries `.data` (failed transfer / unknown
      // id); a bare connection drop does not. The browser's native reconnect
      // would replay the SAME (already-consumed) stream token and 401, so we
      // reconnect ourselves with a freshly-minted token instead.
      if ((ev as MessageEvent).data) {
        onFrame(ev as MessageEvent);
      } else if (terminated) {
        closeStream();
      } else {
        closeStream();
        reconnectTimer = setTimeout(() => {
          reconnectTimer = undefined;
          if (transferId === id && !opening) openStream(id);
        }, 2000);
      }
    });
  }

  async function retrieve() {
    if (active) return;
    phase = "starting";
    message = "";
    downloadError = "";
    bytes = 0;
    total = null;
    cancelRequested = false;
    const gen = generation;
    try {
      const r = await initiateTransfer(itemId);
      // The item changed (or the panel unmounted) while initiating: the reset
      // effect owns the state now.
      if (gen !== generation) return;
      transferId = r.transfer_id;
      if (cancelRequested) {
        await cancel();
        cancelRequested = false;
        return;
      }
      openStream(r.transfer_id);
    } catch (e) {
      if (gen !== generation) return;
      cancelRequested = false;
      phase = "failed";
      message = friendlyError(e, "retrieve");
    }
  }

  async function cancel() {
    if (!transferId) {
      // Initiate still in flight: cancel the transfer the moment it has an id.
      if (phase === "starting") cancelRequested = true;
      return;
    }
    try {
      await cancelTransfer(transferId);
    } catch {
      /* already terminal — the stream/refresh will reflect the real state */
    }
    closeStream();
    phase = "cancelled";
    message = "This retrieve was cancelled.";
  }

  async function download() {
    if (!transferId || downloading) return;
    const id = transferId;
    downloading = true;
    downloadError = ""; // a retry clears the previous attempt's error
    try {
      await probeDownload(id);
      if (id !== transferId) return;
      // The browser streams the file to disk from this navigation (cookie
      // auth, Content-Disposition attachment): no blob, nothing in memory.
      const a = document.createElement("a");
      a.href = transferDownloadUrl(id);
      a.download = filename || `transfer-${id}`;
      a.hidden = true;
      document.body.appendChild(a);
      a.click();
      a.remove();
      phase = "downloaded";
    } catch (e) {
      if (id === transferId) downloadError = errorMessage(e, { verb: "download" });
    } finally {
      downloading = false;
    }
  }

  // Reset + tear down the stream whenever the item changes / the panel unmounts.
  $effect(() => {
    itemId; // track
    phase = "idle";
    transferId = null;
    bytes = 0;
    total = null;
    message = "";
    downloadError = "";
    downloading = false;
    cancelRequested = false;
    closeStream();
    return () => {
      generation += 1;
      closeStream();
    };
  });
</script>

<div class="flex flex-col gap-2">
  <div class="flex flex-wrap items-center gap-2">
    <span class="text-sm font-medium text-muted">Retrieve file</span>
    <span class="grow"></span>
    {#if !active}
      <!-- One primary action at a time: once a download is ready it is the
           primary, and Retrieve again steps back to secondary. -->
      <Button
        variant={canDownload ? "secondary" : "primary"}
        size="sm"
        onclick={retrieve}
        title="Pull this file from the hosting agent to the server, then download it"
      >{phase === "idle" ? "Retrieve" : "Retrieve again"}</Button>
    {:else}
      <Button size="sm" onclick={cancel} disabled={cancelRequested}
        >{cancelRequested ? "Cancelling…" : "Cancel"}</Button>
    {/if}
    {#if canDownload}
      <Button
        variant="primary"
        size="sm"
        disabled={downloading}
        onclick={download}>{downloading ? "Downloading…" : "Download"}</Button>
    {/if}
  </div>

  {#if downloadError}
    <Alert tone="danger" onDismiss={() => (downloadError = "")}>{downloadError}</Alert>
  {/if}

  {#if active}
    <div class="flex flex-col gap-1">
      <div class="flex items-center justify-between text-xs text-muted">
        <span>{PHASE_LABEL[phase]}</span>
        {#if phase === "uploading" && total}
          <span>{pct}%</span>
        {/if}
      </div>
      <div class="h-2 w-full overflow-hidden rounded-control bg-surface-3">
        {#if phase === "uploading" && total}
          <div class="h-full bg-accent transition-all" style="width: {pct}%"></div>
        {:else}
          <!-- Indeterminate stripe for waiting / preparing / verifying (no % yet). -->
          <div class="h-full w-1/3 animate-pulse bg-accent"></div>
        {/if}
      </div>
    </div>
  {:else if canDownload}
    <p class="text-xs text-ok" role="status">
      {PHASE_LABEL[phase]}{#if phase === "downloaded"} — you can download it again until it expires.{/if}
    </p>
  {:else if failedPhase && message}
    <!-- Why the retrieve ended, under the Retrieve again button. An integrity
         failure is an error; offline / expired / cancelled are warnings. -->
    <Alert tone={phase === "failed" ? "danger" : "warn"} onDismiss={() => (message = "")}>{message}</Alert>
  {/if}
</div>
