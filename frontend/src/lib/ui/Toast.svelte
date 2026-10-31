<script lang="ts">
  import Alert from "./Alert.svelte";
  import Button from "./Button.svelte";
  import type { ToastTone } from "./toast.svelte.ts";

  let {
    message,
    tone = "info",
    durationMs = null,
    paused = false,
    actionLabel,
    onAction,
    onDismiss,
    class: className = "",
  }: {
    message: string;
    tone?: ToastTone;
    /** Auto-dismiss after this long; null stays until dismissed. */
    durationMs?: number | null;
    /** Freezes the countdown (the host sets it while hovered or focused). */
    paused?: boolean;
    actionLabel?: string;
    onAction?: () => void;
    onDismiss: () => void;
    class?: string;
  } = $props();

  // Time left on the countdown, carried across pauses so hovering does not
  // restart the full duration. Plain variable: only the effect reads it.
  let remaining: number | undefined;

  $effect(() => {
    if (durationMs === null || paused) return;
    remaining ??= durationMs;
    const startedAt = Date.now();
    const timer = setTimeout(onDismiss, remaining);
    return () => {
      clearTimeout(timer);
      remaining = Math.max(0, (remaining ?? 0) - (Date.now() - startedAt));
    };
  });

  function act() {
    onAction?.();
    onDismiss();
  }
</script>

<!-- live={false}: toast() already spoke this through announce(). -->
<Alert {tone} {onDismiss} live={false} class="shadow-lg {className}">
  <p>{message}</p>
  {#if actionLabel}
    <Button variant="ghost" size="sm" class="-ml-2 mt-1" onclick={act}>{actionLabel}</Button>
  {/if}
</Alert>
