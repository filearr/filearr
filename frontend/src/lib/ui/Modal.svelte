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
         screen wrapper: bits-ui treats a click outside the content element as
         an outside click, so the area around the panel dismisses. The panel
         scrolls, not the wrapper: the body scroll lock leaves the wrapper at
         `pointer-events: none`, so a wrapper scrollbar could not be dragged and
         clicking it fell through to the overlay and closed the modal. Title
         and footer stay put while only the body scrolls, which also keeps the
         Close button in view. px-4 keeps the panel off the screen edge on a phone. -->
    <Dialog.Content
      class="relative flex max-h-[calc(100dvh-4rem)] w-full flex-col overflow-y-auto {widths[size]} rounded-panel bg-surface-2 p-5 shadow-xl {className}"
      onInteractOutside={(e) => {
        // A click on a toast over the modal (e.g. its Undo) is not a dismissal.
        if ((e.target as Element | null)?.closest?.("[data-toast-region]")) e.preventDefault();
      }}
    >
      {#snippet child({ props })}
        <div class="fixed inset-0 z-50 flex items-start justify-center px-4 py-8">
          <div {...props}>
            <div class="shrink-0">
              <Dialog.Title>
                {#snippet child({ props: titleProps })}
                  <h2 {...titleProps} class="pr-8 text-lg font-semibold">{title}</h2>
                {/snippet}
              </Dialog.Title>
              {#if description}
                <Dialog.Description class="mt-1 text-sm text-muted">{description}</Dialog.Description>
              {/if}
            </div>
            <!-- Bleeds into the panel padding (-mx-5 px-5, py-1) so the global
                 focus outline of a control at the body's edge is not clipped
                 by the scroll container. -->
            <div class="-mx-5 mt-3 min-h-0 overflow-y-auto px-5 py-1">{@render children()}</div>
            {#if footer}
              <div class="mt-4 flex shrink-0 flex-wrap justify-end gap-2">{@render footer()}</div>
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
