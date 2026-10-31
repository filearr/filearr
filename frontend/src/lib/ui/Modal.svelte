<!--
  Dialog shell on bits-ui Dialog, which owns the focus trap, Escape, outside
  click, scroll lock, focus return and aria-labelledby/-describedby.

    <Modal bind:open title="Edit library" onClose={reset}>
      …body…
      {#snippet footer()}<Button onclick={save}>Save</Button>{/snippet}
    </Modal>
-->
<script lang="ts">
  import type { Snippet } from "svelte";
  import { Dialog } from "bits-ui";

  type Size = "sm" | "md" | "lg" | "xl";

  let {
    open = $bindable(false),
    title,
    description,
    size = "md",
    onClose,
    class: className = "",
    children,
    footer,
  }: {
    open?: boolean;
    title: string;
    description?: string;
    size?: Size;
    /** Called when the user dismisses the dialog (Escape, outside click, Close). */
    onClose?: () => void;
    class?: string;
    children: Snippet;
    footer?: Snippet;
  } = $props();

  const widths: Record<Size, string> = {
    sm: "max-w-sm",
    md: "max-w-lg",
    lg: "max-w-2xl",
    xl: "max-w-4xl",
  };
</script>

<!-- onOpenChange only fires for closes bits-ui itself makes, so a parent
     setting `open = false` does not echo back through onClose. -->
<Dialog.Root bind:open onOpenChange={(v) => { if (!v) onClose?.(); }}>
  <Dialog.Portal>
    <!-- Every modal shares this one z-index and this one backdrop. -->
    <Dialog.Overlay class="fixed inset-0 z-50 bg-black/50" />
    <!-- The content props land on the panel (via `child`), not on the full-
         screen scroll container: bits-ui treats a click outside the content
         element as an outside click, so the area around the panel dismisses.
         px-4 keeps the panel off the screen edge on a phone. -->
    <Dialog.Content
      class="relative mx-auto my-8 w-full {widths[size]} rounded-panel bg-surface-2 p-5 shadow-xl {className}"
    >
      {#snippet child({ props })}
        <div class="fixed inset-0 z-50 overflow-y-auto px-4">
          <div {...props}>
            <Dialog.Title>
              {#snippet child({ props: titleProps })}
                <h2 {...titleProps} class="pr-8 text-lg font-semibold">{title}</h2>
              {/snippet}
            </Dialog.Title>
            {#if description}
              <Dialog.Description class="mt-1 text-sm text-muted">{description}</Dialog.Description>
            {/if}
            <div class="mt-4">{@render children()}</div>
            {#if footer}
              <div class="mt-5 flex flex-wrap justify-end gap-2">{@render footer()}</div>
            {/if}
            <!-- Last in DOM order so the initial focus lands on the body's first
                 control rather than on Close. -->
            <Dialog.Close
              aria-label="Close"
              class="absolute right-3 top-3 rounded-control px-1.5 py-0.5 leading-none text-muted hover:bg-surface-3 hover:text-fg"
            >
              <span aria-hidden="true">×</span>
            </Dialog.Close>
          </div>
        </div>
      {/snippet}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
