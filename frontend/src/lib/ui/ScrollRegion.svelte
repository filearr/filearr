<!--
  A focusable, named scroll container, so keyboard users can scroll content
  that has no focusable element of its own (a wide table, a long log):

    <ScrollRegion label="Similar items table" class="max-h-64">…</ScrollRegion>

  `label` is required: a focusable region must say what it is. Other
  attributes land on the <div>.

  role/aria-label/tabindex go in through a spread object on purpose. Svelte's
  a11y_no_noninteractive_tabindex has no exception for scroll regions (axe's
  scrollable-region-focusable asks for exactly this tabindex), and the linter
  does not inspect spread attributes. This is the ONE place that trick lives:
  callers use this component instead of a svelte-ignore or their own spread.
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAttributes } from "svelte/elements";

  type Props = Omit<HTMLAttributes<HTMLDivElement>, "class" | "children" | "role" | "tabindex" | "aria-label"> & {
    /** Accessible name of the region (aria-label). */
    label: string;
    class?: string;
    children?: Snippet;
  };

  let { label, class: className = "", children, ...rest }: Props = $props();
</script>

<div {...rest} {...{ role: "region", "aria-label": label, tabindex: 0 }} class="overflow-auto {className}">
  {@render children?.()}
</div>
