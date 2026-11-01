<script lang="ts">
  import { getContext } from "svelte";
  import type { HTMLButtonAttributes } from "svelte/elements";
  import { controlAttrs, FIELD_CTX, type FieldCtx } from "./fieldContext.ts";

  type Props = Omit<HTMLButtonAttributes, "type" | "role" | "aria-checked" | "disabled" | "class" | "children"> & {
    checked?: boolean;
    /** Visible label; a <label> wrapping a <button> names it and forwards clicks. */
    label: string;
    description?: string;
    disabled?: boolean;
    invalid?: boolean;
    required?: boolean;
    class?: string;
  };

  let {
    checked = $bindable(false),
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
</script>

<!-- A real <button>, so Space/Enter toggle natively. The caller's onclick
     runs after the toggle rather than replacing it. No
     focus-visible:outline-hidden: the global :focus-visible outline in app.css
     is the kit's only keyboard focus indicator (see control.ts). -->
<label class="inline-flex items-center gap-2 {className}">
  <button
    {...rest}
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
    class="relative h-5 w-9 shrink-0 rounded-full border border-border-strong transition-colors disabled:opacity-50 {checked
      ? 'bg-accent'
      : 'bg-surface-3'}"
  >
    <!-- top-px centres the 16px knob in the 18px box inside the 1px border. -->
    <span
      class="absolute top-px size-4 rounded-full bg-surface shadow transition-transform {checked
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
