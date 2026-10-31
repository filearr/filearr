<!--
  Tab strip on bits-ui Tabs, which owns role=tablist/tab/tabpanel,
  aria-selected, aria-controls/-labelledby and arrow-key navigation.

    <Tabs bind:value tabs={[{ value: "files", label: "Files", badge: 12 }]} ariaLabel="Item sections">
      {#snippet children(active)}
        {#if active === "files"}<FileList />{/if}
      {/snippet}
    </Tabs>
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  import { Tabs } from "bits-ui";
  import Badge from "./Badge.svelte";

  type Tab = { value: string; label: string; badge?: string | number; disabled?: boolean };

  let {
    tabs,
    // Default to the first enabled tab: an empty value makes bits-ui give every
    // trigger tabindex=0 and leaves the panel unnamed.
    value = $bindable(tabs.find((t) => !t.disabled)?.value ?? ""),
    ariaLabel,
    class: className = "",
    children,
  }: {
    tabs: Tab[];
    value?: string;
    ariaLabel: string;
    class?: string;
    /** Renders the active panel's body; called with the active tab's value. */
    children: Snippet<[string]>;
  } = $props();
</script>

<Tabs.Root bind:value class={className}>
  <Tabs.List aria-label={ariaLabel} class="flex flex-wrap gap-1 border-b border-border pb-2">
    {#each tabs as tab (tab.value)}
      <Tabs.Trigger
        value={tab.value}
        disabled={tab.disabled}
        class="inline-flex items-center gap-1.5 rounded-control px-3 py-1.5 text-sm text-muted hover:text-fg disabled:opacity-50 data-[state=active]:bg-accent data-[state=active]:text-accent-fg data-[state=active]:font-semibold"
      >
        {tab.label}
        <!-- Neutral even on the active tab: an accent badge would vanish
             against the accent-filled trigger. -->
        {#if tab.badge !== undefined}<Badge>{tab.badge}</Badge>{/if}
      </Tabs.Trigger>
    {/each}
  </Tabs.List>
  <!-- One panel, keyed to the active value, so callers render only the body
       they need while it still carries role=tabpanel + aria-labelledby. -->
  <Tabs.Content {value} class="pt-3">
    {@render children(value)}
  </Tabs.Content>
</Tabs.Root>
