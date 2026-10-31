<!-- Renders the toast stack. Mounted once, in App.svelte. -->
<script lang="ts">
  import Toast from "./Toast.svelte";
  import { dismissToast, visibleToasts } from "./toast.svelte.ts";

  let hovered = $state(false);
  let focused = $state(false);
  let stack = $state<HTMLElement | null>(null);
  const toasts = $derived(visibleToasts());

  // A removed node fires no focusout/pointerleave, so a dismissed or evicted
  // toast that held focus would leave the flags stuck and the survivors paused
  // for good. Re-read focus from the DOM whenever the stack changes instead.
  $effect(() => {
    const count = toasts.length;
    focused = !!stack && stack.contains(document.activeElement);
    if (count === 0) hovered = false;
  });

  function dismiss(id: number) {
    dismissToast(id);
    // The clicked Dismiss button is about to leave the DOM with its toast.
    focused = false;
  }
</script>

<!-- A labelled landmark rather than a live region: toast() already announced
     the message, and the toasts' Alerts carry no live role, so nothing is read
     twice while the Dismiss/action buttons stay reachable and named.
     Countdowns pause while the pointer or keyboard focus is in the stack, so a
     toast cannot vanish while it is being read or its action reached. z-[60]
     keeps toasts above modals; pointer-events-auto because an open bits-ui
     dialog sets `pointer-events: none` on <body>, which this fixed region
     would otherwise inherit. Modal reads data-toast-region to treat clicks
     here as inside, not as an outside-click dismissal. -->
{#if toasts.length}
  <div
    bind:this={stack}
    role="region"
    aria-label="Notifications"
    data-toast-region
    class="pointer-events-auto fixed bottom-4 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2"
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
        onDismiss={() => dismiss(t.id)}
      />
    {/each}
  </div>
{/if}
