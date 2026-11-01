<!--
  Anchored panel on bits-ui Popover, which owns positioning, Escape and
  outside-click dismiss, focus handling and aria-expanded/-controls.

    <Popover label="Filters">…panel body…</Popover>

  Custom trigger (ariaLabel is then required):
    <Popover bind:open ariaLabel="Filters">
      {#snippet trigger({ props })}<Button {...props}>Filters</Button>{/snippet}
      …panel body…
    </Popover>
-->
<script lang="ts">
  import type { ComponentProps, Snippet } from "svelte";
  import { Popover } from "bits-ui";
  import Button from "./Button.svelte";

  type ButtonProps = ComponentProps<typeof Button>;
  type TriggerSnippet = Snippet<[{ props: Record<string, unknown> }]>;

  type Props = {
    open?: boolean;
    side?: Popover.ContentProps["side"];
    align?: Popover.ContentProps["align"];
    /** For the `label` button trigger. */
    variant?: ButtonProps["variant"];
    size?: ButtonProps["size"];
    class?: string;
    children: Snippet;
  } & (
    | {
        /** Text of a Button trigger with a ▾; also the panel's default name. */
        label: string;
        /** Accessible name for the panel (role=dialog, set here: bits-ui adds none). */
        ariaLabel?: string;
        trigger?: TriggerSnippet;
      }
    | {
        label?: string;
        ariaLabel: string;
        /** Spread `props` onto the caller's Button so it is the trigger itself
         *  (aria-expanded, ref, click handler), not a button nested in a button. */
        trigger: TriggerSnippet;
      }
  );

  let {
    open = $bindable(false),
    side = "bottom",
    align = "start",
    label,
    variant,
    size,
    ariaLabel,
    class: className = "",
    trigger,
    children,
  }: Props = $props();

  // The union type already rejects neither; this covers untyped callers.
  $effect(() => {
    if (import.meta.env.DEV && !label && !trigger) console.warn("Popover: pass `label` or a `trigger` snippet.");
  });
</script>

<Popover.Root bind:open>
  <Popover.Trigger>
    {#snippet child({ props })}
      {#if trigger}
        {@render trigger({ props })}
      {:else}
        <Button {variant} {size} {...props}>{label}<span aria-hidden="true"> ▾</span></Button>
      {/if}
    {/snippet}
  </Popover.Trigger>
  <Popover.Portal>
    <!-- collisionPadding + the viewport-relative max width keep the panel
         fully on screen down to 360 px, whatever width the caller sets.
         role="dialog" is explicit: bits-ui 2.18 gives the trigger
         aria-haspopup="dialog" but renders Content with no role, and an
         aria-label on a role-less div is ignored by screen readers. -->
    <Popover.Content
      {side}
      {align}
      sideOffset={6}
      collisionPadding={8}
      role="dialog"
      aria-label={ariaLabel ?? label}
      class="z-50 max-w-[calc(100vw-1rem)] rounded-panel border border-border bg-surface-2 p-3 shadow-lg {className}"
    >
      {@render children()}
    </Popover.Content>
  </Popover.Portal>
</Popover.Root>
