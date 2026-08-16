<script lang="ts">
  // Collapsible raw-data section of the library-diagnosis dialog. Closed by
  // default; the header carries a small count so the operator can see at a
  // glance whether it is worth opening.
  import type { Snippet } from "svelte";

  let {
    title,
    count = null,
    children,
  }: { title: string; count?: number | string | null; children: Snippet } = $props();

  let open = $state(false);
</script>

<div class="rounded-lg border border-slate-200 dark:border-slate-700">
  <button
    type="button"
    class="flex w-full items-center justify-between px-3 py-2 text-left text-sm font-medium hover:bg-slate-50 dark:hover:bg-slate-800/60"
    aria-expanded={open}
    onclick={() => (open = !open)}>
    <span>{title}</span>
    <span class="flex items-center gap-2 text-xs text-slate-500">
      {#if count !== null && count !== undefined}
        <span class="rounded bg-slate-200 px-1.5 py-0.5 dark:bg-slate-700 dark:text-slate-300">{count}</span>
      {/if}
      <span aria-hidden="true">{open ? "▾" : "▸"}</span>
    </span>
  </button>
  {#if open}
    <div class="border-t border-slate-200 px-3 py-2 text-xs dark:border-slate-700">
      {@render children()}
    </div>
  {/if}
</div>
