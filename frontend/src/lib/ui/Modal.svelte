<!--
  Dialog shell on bits-ui Dialog, which owns the focus trap, Escape, outside
  click, scroll lock, focus return and aria-labelledby/-describedby.

    <Modal bind:open title="Edit library" onClose={reset}>
      …body…
      {#snippet footer()}<Button onclick={save}>Save</Button>{/snippet}
    </Modal>

  Dirty-check every way out. The check gets the reason, so a save that closes
  with `open = false` ("caller") is not asked about the edits it just saved:
    <Modal bind:open title="Edit" onBeforeClose={(r) => r === "caller" || !dirty || confirm({ title: "Discard changes?" })}>
  A Cancel button that sets `open = false` is then a "caller" close too, so it
  runs the discard check itself before closing.
  Use `bind:open` together with `onBeforeClose`: after a veto the dialog
  writes `open = true` back, which a one-way `open` cannot receive, so the
  parent would still hold `false` and could not close it again.
-->
<script lang="ts">
  import { untrack, type Snippet } from "svelte";
  import { Dialog } from "bits-ui";

  type Size = "sm" | "md" | "lg" | "xl";
  /** Why a close was requested: Escape, an outside click, the × button, or the
   *  parent setting `open = false`. */
  type CloseReason = "escape" | "outside" | "close-button" | "caller";

  let {
    open = $bindable(false),
    title,
    description,
    size = "md",
    onClose,
    dismissible = true,
    onBeforeClose,
    initialFocus,
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
    /** False: Escape and outside clicks do nothing; Close and `open = false` still close. */
    dismissible?: boolean;
    /** Asked before every close, including `open = false` from the caller
     *  (reason "caller"); returning false keeps the dialog open. A throw counts
     *  as false. Pair it with `bind:open`. */
    onBeforeClose?: (reason: CloseReason) => boolean | Promise<boolean>;
    /** The element to focus on open instead of the body's first control. */
    initialFocus?: () => HTMLElement | null | undefined;
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

  // bits-ui closes by writing its `open` before any hook can veto it, so it is
  // driven from `inner` and never closes on its own: Escape and outside clicks
  // are prevented in the layer callbacks and Close is a plain button, all
  // routed through requestClose(). `inner` follows the prop (via the effect
  // below) except while a close is being decided.
  let inner = $state(false);
  let deciding = false;
  let panel = $state<HTMLElement | null>(null);

  /** "caller": the parent set `open = false`. That path skips onClose, which
   *  is for user dismissals only. */
  async function requestClose(reason: CloseReason) {
    if (deciding) return;
    const fromCaller = reason === "caller";
    if (onBeforeClose) {
      deciding = true;
      let ok = false;
      try {
        ok = await onBeforeClose(reason);
      } catch (e) {
        // A failing check must not discard what it was guarding.
        if (import.meta.env.DEV) console.error("Modal: onBeforeClose threw; keeping the dialog open.", e);
      } finally {
        deciding = false;
      }
      if (!ok) {
        // Re-assert open on both sides, including a caller close made while
        // the check was pending.
        inner = true;
        open = true;
        return;
      }
      // The parent reopened while the check was pending: it wins.
      if (fromCaller && open) return;
    }
    // inner first, so the effect sees the prop and inner agree and does not
    // run the check a second time.
    inner = false;
    open = false;
    if (!fromCaller) onClose?.();
  }

  $effect(() => {
    const want = open;
    untrack(() => {
      if (want && !inner) inner = true;
      else if (!want && inner) void requestClose("caller");
    });
  });

  const focusable =
    'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';
</script>

<!-- Every close above goes through requestClose, so bits-ui should never
     report one; if it does (a future path), undo it and ask first. It is a
     user dismissal, so it goes in as "outside" (never "caller", which a
     dirty check may wave through). -->
<Dialog.Root
  bind:open={inner}
  onOpenChange={(v) => {
    if (v) return;
    inner = true;
    void requestClose("outside");
  }}
>
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
      bind:ref={panel}
      interactOutsideBehavior={dismissible ? "close" : "ignore"}
      escapeKeydownBehavior={dismissible ? "close" : "ignore"}
      onInteractOutside={(e) => {
        e.preventDefault();
        // A click on a toast over the modal (e.g. its Undo) is not a dismissal.
        if ((e.target as Element | null)?.closest?.("[data-toast-region]")) return;
        void requestClose("outside");
      }}
      onEscapeKeydown={(e) => {
        e.preventDefault();
        void requestClose("escape");
      }}
      onOpenAutoFocus={(e) => {
        if (!initialFocus) return;
        // Same frame bits-ui's default focus waits for, so a bind:this target
        // inside the body is attached by then. Nothing returned: fall back to
        // the default (first control, else the panel).
        e.preventDefault();
        requestAnimationFrame(() => {
          const el = initialFocus() ?? panel?.querySelector<HTMLElement>(focusable) ?? panel;
          el?.focus();
        });
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
            <!-- A plain button, not Dialog.Close: that one closes before
                 onBeforeClose could veto it. -->
            <button
              type="button"
              aria-label="Close"
              class="absolute right-3 top-3 rounded-control px-1.5 py-0.5 leading-none text-muted hover:bg-surface-3 hover:text-fg"
              onclick={() => void requestClose("close-button")}
            >
              <span aria-hidden="true">×</span>
            </button>
          </div>
        </div>
      {/snippet}
    </Dialog.Content>
  </Dialog.Portal>
</Dialog.Root>
