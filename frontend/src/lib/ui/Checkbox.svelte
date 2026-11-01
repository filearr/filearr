<script lang="ts">
  import { getContext } from "svelte";
  import type { HTMLInputAttributes } from "svelte/elements";
  import { controlAttrs, FIELD_CTX, type FieldCtx } from "./fieldContext.ts";

  type Props = Omit<HTMLInputAttributes, "type" | "checked" | "indeterminate" | "size" | "class" | "children"> & {
    checked?: boolean;
    /** Visible label; the wrapping <label> makes the whole row clickable. */
    label: string;
    description?: string;
    indeterminate?: boolean;
    disabled?: boolean;
    invalid?: boolean;
    size?: "sm" | "md";
    class?: string;
  };

  let {
    checked = $bindable(false),
    label,
    description,
    indeterminate = $bindable(false),
    disabled,
    invalid,
    size = "md",
    class: className = "",
    ...rest
  }: Props = $props();

  // The label text inherits this size; the description stays at text-2xs.
  const sizes: Record<"sm" | "md", string> = { md: "text-sm", sm: "text-xs" };

  // Same wiring as Input: a surrounding <Field> supplies id, hint/error
  // description, invalid and required; explicit props still win.
  const field = getContext<FieldCtx | undefined>(FIELD_CTX);
  const { invalid: _invalid, ...attrs } = $derived(
    controlAttrs(field, {
      id: rest.id ?? undefined,
      "aria-describedby": rest["aria-describedby"] ?? undefined,
      "aria-invalid": rest["aria-invalid"] ?? undefined,
      required: rest.required ?? undefined,
      invalid,
    }),
  );
</script>

<!-- No focus-visible:outline-hidden: the global :focus-visible outline in
     app.css is the kit's only keyboard focus indicator (see control.ts). -->
<label class="inline-flex items-start gap-2 {sizes[size]} {className}">
  <input
    {...rest}
    type="checkbox"
    bind:checked
    bind:indeterminate
    {disabled}
    {...attrs}
    class="mt-0.5 size-4 shrink-0 rounded border-border-strong accent-[var(--accent)]"
  />
  <span>
    <span class="text-fg">{label}</span>
    {#if description}<span class="block text-2xs text-muted">{description}</span>{/if}
  </span>
</label>
