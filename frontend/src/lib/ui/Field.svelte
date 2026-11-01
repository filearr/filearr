<!--
  Labelled form row: visible label, the control, then optional hint and error.
  Field owns the ids and shares them, with `invalid` and `required`, through
  context (fieldContext.ts), so the kit's controls wire themselves up:

    <Field label="Name" error={err} required><Input bind:value={name} /></Field>

  Input, Select, Textarea, Checkbox and Switch read that context; explicit
  props on the control still win. For a custom control that does not read the
  context, take the values from the snippet arguments instead:

    <Field label="Name" error={err}>
      {#snippet children({ id, describedBy, invalid, required })}
        <MyControl {id} aria-describedby={describedBy} {invalid} {required} />
      {/snippet}
    </Field>

  One control per Field: the context carries a single id, so a second control
  would duplicate it and steal the label. Checkbox and Switch carry their own
  label; wrap one in a Field only to wire a hint or error to it — the Field
  label then names the group:

    <Field label="Notifications" hint="Sent to your account email.">
      <Checkbox label="Email me when a scan fails" bind:checked />
    </Field>

  Labels are always visible — never use a placeholder as the label.
-->
<script lang="ts">
  import { setContext, type Snippet } from "svelte";
  import { FIELD_CTX, type FieldCtx } from "./fieldContext.ts";

  let {
    label,
    hint,
    error,
    required = false,
    id: idProp,
    class: className = "",
    children,
  }: {
    label: string;
    hint?: string;
    error?: string;
    required?: boolean;
    id?: string;
    class?: string;
    children?: Snippet<[{ id: string; describedBy: string | undefined; invalid: boolean; required: boolean }]>;
  } = $props();

  // $props.id() is stable across SSR/hydration and unique per instance, so
  // label/hint/error wiring works without callers inventing ids.
  const generatedId = $props.id();
  const id = $derived(idProp ?? generatedId);
  const hintId = $derived(`${id}-hint`);
  const errorId = $derived(`${id}-error`);
  const invalid = $derived(!!error);
  // Only reference nodes that are actually rendered; a dangling id in
  // aria-describedby is ignored at best and confusing at worst.
  const describedBy = $derived([hint ? hintId : "", error ? errorId : ""].filter(Boolean).join(" ") || undefined);

  // Getters, not a snapshot: context is set once, so a plain object would
  // freeze the first values. Reading through getters lets a control's
  // $derived track the current id/error/required.
  setContext<FieldCtx>(FIELD_CTX, {
    get id() {
      return id;
    },
    get describedBy() {
      return describedBy;
    },
    get invalid() {
      return invalid;
    },
    get required() {
      return required;
    },
  });
</script>

<div class="flex flex-col gap-1 {className}">
  <label for={id} class="text-xs font-medium text-fg">
    {label}
    {#if required}<span class="text-danger" aria-hidden="true">*</span><span class="sr-only">required</span>{/if}
  </label>
  {#if children}{@render children({ id, describedBy, invalid, required })}{/if}
  {#if hint}<p id={hintId} class="text-2xs text-muted">{hint}</p>{/if}
  {#if error}<p id={errorId} class="text-2xs text-danger" role="alert">{error}</p>{/if}
</div>
