<!-- Renders the toast stack. Mounted once, in App.svelte. -->
<script lang="ts">
  import Toast from "./Toast.svelte";
  import { dismissToast, visibleToasts } from "./toast.svelte.ts";

  let hovered = $state(false);
  let focused = $state(false);
  const toasts = $derived(visibleToasts());

  // The stack unmounts when it empties, and a removed node fires no
  // pointerleave/focusout, so clear the flags here or the next toast would
  // start paused.
  $effect(() => {
    if (toasts.length === 0) {
      hovered = false;
      focused = false;
    }
  });
</script>

<!-- aria-hidden: every toast() already went through announce(), so exposing
     the stack too would read each message twice. Countdowns pause while the
     pointer or keyboard focus is in the stack, so a toast cannot vanish while
     it is being read or its action reached. z-[60] keeps toasts above modals. -->
{#if toasts.length}
  <div
    aria-hidden="true"
    class="fixed bottom-4 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
    onpointerenter={() => (hovered = true)}
    onpointerleave={() => (hovered = false)}
    onfocusin={() => (focused = true)}
    onfocusout={(e) => (focused = e.currentTarget.contains(e.relatedTarget as Node | null))}
  >
    {#each toasts as t (t.id)}
      <Toast
        message={t.message}
        tone={t.tone}
        durationMs={t.durationMs}
        paused={hovered || focused}
        actionLabel={t.actionLabel}
        onAction={t.onAction}
        onDismiss={() => dismissToast(t.id)}
      />
    {/each}
  </div>
{/if}
