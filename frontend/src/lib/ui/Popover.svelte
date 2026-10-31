<!--
  Anchored panel on bits-ui Popover, which owns positioning, Escape and
  outside-click dismiss, focus handling and aria-expanded/-controls.

    <Popover bind:open>
      {#snippet trigger({ props })}<Button {...props}>Filters</Button>{/snippet}
      …panel body…
    </Popover>
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  import { Popover } from "bits-ui";

  let {
    open = $bindable(false),
    side = "bottom",
    align = "start",
    class: className = "",
    trigger,
    children,
  }: {
    open?: boolean;
    side?: Popover.ContentProps["side"];
    align?: Popover.ContentProps["align"];
    class?: string;
    /** Spread `props` onto the caller's Button so it is the trigger itself
     *  (aria-expanded, ref, click handler), not a button nested in a button. */
    trigger: Snippet<[{ props: Record<string, unknown> }]>;
    children: Snippet;
  } = $props();
</script>

<Popover.Root bind:open>
  <Popover.Trigger>
    {#snippet child({ props })}
      {@render trigger({ props })}
    {/snippet}
  </Popover.Trigger>
  <Popover.Portal>
    <!-- collisionPadding + the viewport-relative max width keep the panel
         fully on screen down to 360 px, whatever width the caller sets. -->
    <Popover.Content
      {side}
      {align}
      sideOffset={6}
      collisionPadding={8}
      class="z-50 max-w-[calc(100vw-1rem)] rounded-panel border border-border bg-surface-2 p-3 shadow-lg {className}"
    >
      {@render children()}
    </Popover.Content>
  </Popover.Portal>
</Popover.Root>
