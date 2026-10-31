<!-- Renders the active confirm() request. Mounted once, in App.svelte. -->
<script lang="ts">
  import ConfirmDialog from "./ConfirmDialog.svelte";
  import { activeConfirm, settleConfirm } from "./confirm.svelte.ts";

  const req = $derived(activeConfirm());
</script>

{#if req}
  <!-- Keyed so each request gets a fresh dialog: open state, typed text and
       the initial Cancel focus all reset between queued requests. -->
  {#key req}
    <ConfirmDialog
      open
      title={req.title}
      message={req.message}
      confirmLabel={req.confirmLabel}
      cancelLabel={req.cancelLabel}
      tone={req.tone}
      typedName={req.typedName}
      onConfirm={() => settleConfirm(req, true)}
      onCancel={() => settleConfirm(req, false)}
    />
  {/key}
{/if}
