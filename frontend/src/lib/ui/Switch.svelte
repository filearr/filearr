<script lang="ts">
  import { getContext } from "svelte";
  import type { HTMLButtonAttributes } from "svelte/elements";
  import { controlAttrs, FIELD_CTX, warnFieldIdMismatch, type FieldCtx } from "./fieldContext.ts";

  type Props = Omit<HTMLButtonAttributes, "type" | "role" | "aria-checked" | "disabled" | "class" | "children"> & {
    checked?: boolean;
    /** Visible label; a <label> wrapping a <button> names it and forwards clicks. */
    label: string;
    description?: string;
    disabled?: boolean;
    invalid?: boolean;
    required?: boolean;
    class?: string;
    /** The <button role="switch"> element, for focus() and the like: `bind:ref`. */
    ref?: HTMLButtonElement | null;
  };

  let {
    checked = $bindable(false),
    ref = $bindable<HTMLButtonElement | null>(null),
    label,
    description,
    disabled = false,
    invalid,
    required,
    class: className = "",
    onclick,
    ...rest
  }: Props = $props();

  // Same wiring as Input. A <button> has no `required` attribute, so the
  // merged value goes out as aria-required instead.
  const field = getContext<FieldCtx | undefined>(FIELD_CTX);
  const merged = $derived(
    controlAttrs(field, {
      id: rest.id ?? undefined,
      "aria-describedby": rest["aria-describedby"] ?? undefined,
      "aria-invalid": rest["aria-invalid"] ?? undefined,
      required,
      invalid,
    }),
  );

  // An own id inside a Field orphans the Field's <label for>.
  $effect(() => warnFieldIdMismatch(field, rest.id ?? undefined, import.meta.env.DEV));
</script>

<!-- A real <button>, so Space/Enter toggle natively. The caller's onclick
     runs after the toggle rather than replacing it. No
     focus-visible:outline-hidden: the global :focus-visible outline in app.css
     is the kit's only keyboard focus indicator (see control.ts). Disabled
     dims the whole row (track and label) from the wrapper, once. -->
<label class="inline-flex items-center gap-2 {className}" class:opacity-50={disabled}>
  <button
    {...rest}
    bind:this={ref}
    type="button"
    role="switch"
    aria-checked={checked}
    {disabled}
    id={merged.id}
    aria-describedby={merged["aria-describedby"]}
    aria-invalid={merged["aria-invalid"]}
    aria-required={merged.required ? "true" : undefined}
    onclick={(e) => {
      checked = !checked;
      onclick?.(e);
    }}
    class="relative h-5 w-9 shrink-0 rounded-full border border-border-strong transition-colors {checked
      ? 'bg-accent'
      : 'bg-surface-3'}"
  >
    <!-- top-px centres the 16px knob in the 18px box inside the 1px border;
         left-0 anchors it to the padding edge (without it the absolute knob
         starts from the centred content box), so the translate is the travel. -->
    <span
      class="absolute left-0 top-px size-4 rounded-full bg-surface shadow transition-transform {checked
        ? 'translate-x-4'
        : 'translate-x-0.5'}"
      aria-hidden="true"
    ></span>
  </button>
  <span>
    <span class="text-sm text-fg">{label}</span>
    {#if description}<span class="block text-2xs text-muted">{description}</span>{/if}
  </span>
</label>
