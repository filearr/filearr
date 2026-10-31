<script lang="ts">
  import type { Snippet } from "svelte";
  import { toneClasses, type Tone } from "./tone.ts";

  let {
    tone = "info",
    title,
    onDismiss,
    class: className = "",
    children,
  }: {
    tone?: Exclude<Tone, "neutral" | "accent">;
    title?: string;
    onDismiss?: () => void;
    class?: string;
    children?: Snippet;
  } = $props();

  // Only problems interrupt the screen reader; ok/info announce politely.
  const role = $derived(tone === "warn" || tone === "danger" ? "alert" : "status");
</script>

<div {role} class="flex items-start gap-2 rounded-control px-3 py-2 text-sm {toneClasses(tone)} {className}">
  <div class="min-w-0 flex-1">
    {#if title}<p class="font-semibold">{title}</p>{/if}
    {@render children?.()}
  </div>
  {#if onDismiss}
    <button
      type="button"
      aria-label="Dismiss"
      class="-my-0.5 shrink-0 rounded-control px-1 leading-none opacity-70 hover:opacity-100"
      onclick={onDismiss}
    >
      <span aria-hidden="true">×</span>
    </button>
  {/if}
</div>
