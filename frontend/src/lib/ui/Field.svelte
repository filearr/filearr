<!--
  Labelled form row: visible label, the control, then optional hint and error.
  Field owns the ids; the control receives them through the children snippet:

    <Field label="Name" error={err}>
      {#snippet children({ id, describedBy, invalid })}
        <Input {id} aria-describedby={describedBy} {invalid} bind:value={name} />
      {/snippet}
    </Field>

  Labels are always visible — never use a placeholder as the label.
-->
<script lang="ts">
  import type { Snippet } from "svelte";

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
    children: Snippet<[{ id: string; describedBy: string | undefined; invalid: boolean }]>;
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
</script>

<div class="flex flex-col gap-1 {className}">
  <label for={id} class="text-xs font-medium text-fg">
    {label}
    {#if required}<span class="text-danger" aria-hidden="true">*</span><span class="sr-only">required</span>{/if}
  </label>
  {@render children({ id, describedBy, invalid })}
  {#if hint}<p id={hintId} class="text-2xs text-muted">{hint}</p>{/if}
  {#if error}<p id={errorId} class="text-2xs text-danger" role="alert">{error}</p>{/if}
</div>
