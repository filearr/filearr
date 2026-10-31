<script lang="ts">
  import type { Snippet } from "svelte";
  import type { HTMLAnchorAttributes, HTMLButtonAttributes } from "svelte/elements";

  type Variant = "primary" | "secondary" | "danger" | "ghost";
  type Size = "sm" | "md";

  // One prop surface for both renderings: rest props land on whichever
  // element is drawn, so either element's attributes are accepted.
  type Props = Omit<HTMLButtonAttributes, "type" | "disabled" | "class" | "children"> &
    Omit<HTMLAnchorAttributes, "type" | "href" | "class" | "children"> & {
      variant?: Variant;
      size?: Size;
      type?: "button" | "submit" | "reset";
      disabled?: boolean;
      href?: string;
      class?: string;
      children?: Snippet;
    };

  let {
    variant = "secondary",
    size = "md",
    type = "button",
    disabled = false,
    href,
    class: className = "",
    children,
    onclick,
    ...rest
  }: Props = $props();

  const variants: Record<Variant, string> = {
    primary: "bg-accent text-accent-fg hover:opacity-90",
    secondary: "border border-border-strong bg-transparent text-fg hover:bg-surface-3",
    // The dark danger token is a pale red, so white text drops to ~1.9:1; dark surface text gives ~11:1.
    danger: "bg-danger text-white dark:text-surface hover:opacity-90",
    ghost: "text-muted hover:bg-surface-3 hover:text-fg",
  };
  // min-h-8 keeps the md hit area at 32px even when the label is a lone icon.
  const sizes: Record<Size, string> = {
    md: "min-h-8 px-3 py-1.5 text-sm",
    sm: "px-2 py-1 text-xs",
  };

  // aria-disabled:* mirrors disabled:* for the <a> rendering, which has no
  // native disabled state.
  const classes = $derived(
    `rounded-control inline-flex items-center gap-1.5 font-medium disabled:opacity-50 aria-disabled:opacity-50 ${sizes[size]} ${variants[variant]} ${className}`,
  );
</script>

{#if href !== undefined}
  <!-- A disabled link drops its href (so it cannot navigate) and its click
       handler, and leaves the tab order; aria-disabled tells assistive tech why
       it does nothing, and role="link" restores the role an href-less <a> loses. -->
  <a
    {...rest}
    href={disabled ? undefined : href}
    role={disabled ? "link" : rest.role}
    onclick={disabled ? undefined : onclick}
    aria-disabled={disabled ? "true" : undefined}
    tabindex={disabled ? -1 : rest.tabindex}
    class={classes}
  >
    {@render children?.()}
  </a>
{:else}
  <button {...rest} {type} {disabled} onclick={disabled ? undefined : onclick} class={classes}>
    {@render children?.()}
  </button>
{/if}
