<!-- Renders the active confirm() or promptText() request. Mounted once, in App.svelte. -->
<script lang="ts">
  import ConfirmDialog from "./ConfirmDialog.svelte";
  import { activeConfirm, settleConfirm } from "./confirm.svelte.ts";

  const req = $derived(activeConfirm());
</script>

{#if req}
  <!-- Keyed so each request gets a fresh dialog: open state, typed text and
       the initial focus all reset between queued requests. confirm() and
       promptText() requests share the one dialog; a prompt answers with its
       value (or null on Cancel/Escape). -->
  {#key req}
    <ConfirmDialog
      open
      title={req.title}
      message={req.message}
      details={req.kind === "confirm" ? req.details : undefined}
      confirmLabel={req.confirmLabel}
      cancelLabel={req.cancelLabel}
      tone={req.kind === "confirm" ? req.tone : undefined}
      typedName={req.kind === "confirm" ? req.typedName : undefined}
      prompt={req.kind === "prompt" ? req : undefined}
      onConfirm={(value) => settleConfirm(req, req.kind === "prompt" ? (value ?? null) : true)}
      onCancel={() => settleConfirm(req, req.kind === "prompt" ? null : false)}
    />
  {/key}
{/if}
