<script lang="ts">
  // IN-T4 — the bulk action bar. Appears above the results whenever the search
  // selection is non-empty; collects ONE set of operations and hands them to the
  // page, which resolves each item's current tags, computes a DIFFERENT patch
  // per item and submits in chunks of 500.
  //
  // Division of labour on purpose: this component owns the controls and their
  // validation; ./bulkEdit.ts owns every rule worth testing (tag set arithmetic,
  // applicable-field intersection, coercion, chunking, result reading); the page
  // owns the network. Nothing here fetches items or patches anything.
  //
  // Notably ABSENT: title. Setting one title on N files is almost always a
  // mistake, so title editing is single-item only (ItemDetail). That is a design
  // decision, not an omission.
  import { onMount } from "svelte";
  import CustomFieldInput from "./CustomFieldInput.svelte";
  import { Alert, Button, Field, Input, Select } from "./ui";
  import type { CustomField } from "./api";
  import { cachedCustomFields } from "./configCache";
  import {
    applicableFields,
    coerceCustomValue,
    opsAreEmpty,
    type BulkOps,
    type FieldTarget,
  } from "./bulkEdit";

  let {
    count,
    targets,
    busy = false,
    cancelling = false,
    progress = "",
    onApply,
    onClear,
  }: {
    /** How many items are selected (may exceed ``targets.length`` only if a
     *  caller passes a sample — today they match). */
    count: number;
    /** The category/library of every selected item, for the applicability
     *  intersection. */
    targets: FieldTarget[];
    busy?: boolean;
    /** A cancelled run is still settling: Apply reads "Cancelling…" (and the
     *  caller keeps `busy` on) until its in-flight chunk returns. */
    cancelling?: boolean;
    /** Chunk progress of a multi-request run ("1/3"), shown on the button. */
    progress?: string;
    onApply: (ops: BulkOps) => void;
    onClear: () => void;
  } = $props();

  // ---- tag chips ----------------------------------------------------------
  // Two independent lists: ADD (union'd into each item's tags) and REMOVE
  // (filtered out). They are separate controls rather than one "tags" box
  // because the wire format REPLACES the whole list — the only way to express
  // "add one tag to 300 differently-tagged files" is per-item arithmetic, and
  // the UI has to ask for the operation, not the result.
  let tagsAdd = $state<string[]>([]);
  let tagsRemove = $state<string[]>([]);
  let addDraft = $state("");
  let removeDraft = $state("");

  function commit(draft: string, list: string[]): { list: string[]; draft: string } {
    // Commas split, so pasting "hdr, remux, 4k" does the obvious thing.
    const parts = draft.split(",").map((s) => s.trim()).filter(Boolean);
    const out = [...list];
    for (const p of parts) {
      if (!out.some((t) => t.toLowerCase() === p.toLowerCase())) out.push(p);
    }
    return { list: out, draft: "" };
  }
  function commitAdd() {
    ({ list: tagsAdd, draft: addDraft } = commit(addDraft, tagsAdd));
  }
  function commitRemove() {
    ({ list: tagsRemove, draft: removeDraft } = commit(removeDraft, tagsRemove));
  }
  function tagKey(e: KeyboardEvent, which: "add" | "remove") {
    if (e.key !== "Enter" && e.key !== ",") return;
    e.preventDefault();
    if (which === "add") commitAdd();
    else commitRemove();
  }

  // ---- year ---------------------------------------------------------------
  // Three-state, not a text box: "" (untouched) has to be distinguishable from
  // an explicit CLEAR, because those are `absent` and `null` on the wire and
  // they do completely different things.
  let yearMode = $state<"none" | "set" | "clear">("none");
  let yearText = $state("");

  // ---- custom field -------------------------------------------------------
  let defs = $state<CustomField[]>([]);
  let defsError = $state("");
  let fieldName = $state("");
  let fieldMode = $state<"none" | "set" | "clear">("none");
  let fieldRaw = $state<string | boolean>("");

  onMount(async () => {
    try {
      defs = await cachedCustomFields();
    } catch {
      // Non-fatal: tags + year still work; the custom-field row explains itself.
      defsError = "Custom-field definitions are unavailable (read scope?).";
    }
  });

  // Only fields applicable to EVERY selected item. Offering the union would
  // guarantee that some items 422 — see applicableFields() for the reasoning.
  const usable = $derived(applicableFields(defs, targets));
  const chosen = $derived(usable.find((d) => d.name === fieldName) ?? null);

  /** Pick a field: the value and action restart from the NEW field's
   *  defaults, so a value typed for the previous field never rides along. */
  function pickField(name: string) {
    fieldName = name;
    fieldMode = "none";
    const def = usable.find((d) => d.name === name);
    fieldRaw = def?.data_type === "boolean" ? false : "";
  }

  // A selection change can make the chosen field inapplicable; drop it rather
  // than silently submit a field the new selection does not accept.
  $effect(() => {
    if (fieldName && !usable.some((d) => d.name === fieldName)) {
      fieldName = "";
      fieldMode = "none";
      fieldRaw = "";
    }
  });

  // ---- validation + submit ------------------------------------------------
  let error = $state("");

  const ops = $derived<BulkOps>({
    tagsAdd,
    tagsRemove,
    yearMode,
    year: yearMode === "set" && yearText.trim() ? Number(yearText.trim()) : null,
    fieldName,
    fieldMode,
    fieldValue: null, // filled in by apply() after coercion
  });

  // A half-typed tag counts: apply() folds it in, so Apply works in ONE click
  // (the input's blur would otherwise enable the button only after the click).
  const nothingToDo = $derived(opsAreEmpty(ops) && !addDraft.trim() && !removeDraft.trim());

  function apply() {
    error = "";
    // Fold any half-typed chip in, so a user who types a tag and hits Apply
    // without pressing Enter still gets what they meant.
    if (addDraft.trim()) commitAdd();
    if (removeDraft.trim()) commitRemove();
    // A draft of only separators ("," ) folds to nothing: still a no-op.
    if (opsAreEmpty({ ...ops, tagsAdd, tagsRemove })) return;

    let fieldValue: unknown = null;
    if (fieldName && fieldMode === "set") {
      if (!chosen) {
        error = "That custom field does not apply to every selected item.";
        return;
      }
      const res = coerceCustomValue(chosen, fieldRaw);
      if (!res.ok) {
        error = res.error;
        return;
      }
      fieldValue = res.value;
    }
    if (yearMode === "set") {
      const y = Number(yearText.trim());
      if (!yearText.trim() || !Number.isInteger(y) || y < 1 || y > 9999) {
        error = "Year must be a whole number between 1 and 9999.";
        return;
      }
    }
    onApply({ ...ops, tagsAdd, tagsRemove, fieldValue });
  }

  // There is deliberately no reset(): a FULL success clears the selection, which
  // unmounts this bar and drops its state; a PARTIAL failure keeps both, so the
  // operator can hit "Select only the failed" and re-apply the very same
  // operations without retyping them.

  // Segmented toggles (year / custom-field action): the pressed option is
  // accent-filled.
  const SEG_ON = "bg-accent text-accent-fg";
</script>

<!-- Same chip-bar visual convention as the filter rows above it, one step more
     prominent (accent-tinted panel) because it WRITES. -->
<div
  class="mt-2 rounded-panel border border-accent/40 bg-accent/5 p-3"
  role="region"
  aria-label="Bulk edit selected items">
  <div class="flex flex-wrap items-center gap-2">
    <span class="rounded-full bg-accent px-3 py-1 text-sm font-medium text-accent-fg">
      {count} selected
    </span>
    <Button size="sm" onclick={onClear}>Clear selection</Button>
    <span class="grow"></span>
    <Button
      variant="primary"
      disabled={busy || nothingToDo}
      title={nothingToDo ? "Choose a tag, year or custom-field change first" : "Apply to every selected item"}
      onclick={apply}>{cancelling ? "Cancelling…" : busy ? `Applying…${progress ? ` ${progress}` : ""}` : `Apply to ${count}`}</Button>
  </div>
  <!-- Validation of the operations above, right under the Apply button. -->
  {#if error}
    <Alert tone="danger" class="mt-2" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  <!-- TAGS -->
  <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-muted">
    <span class="mt-1.5 w-14 font-medium">Tags</span>
    <div class="flex flex-col gap-1">
      <div class="flex flex-wrap items-center gap-1">
        <span class="w-44">
          <Input
            size="sm"
            aria-label="Tags to add"
            placeholder="add tag…"
            bind:value={addDraft}
            onkeydown={(e) => tagKey(e, "add")}
            onblur={commitAdd} />
        </span>
        {#each tagsAdd as t (t)}
          <span class="inline-flex items-center gap-1 rounded-full bg-ok-bg px-2 py-1 text-ok">
            +{t}
            <button
              type="button"
              class="rounded-full px-1 leading-none"
              aria-label={`Don't add ${t}`}
              onclick={() => (tagsAdd = tagsAdd.filter((x) => x !== t))}>×</button>
          </span>
        {/each}
      </div>
      <div class="flex flex-wrap items-center gap-1">
        <span class="w-44">
          <Input
            size="sm"
            aria-label="Tags to remove"
            placeholder="remove tag…"
            bind:value={removeDraft}
            onkeydown={(e) => tagKey(e, "remove")}
            onblur={commitRemove} />
        </span>
        {#each tagsRemove as t (t)}
          <span class="inline-flex items-center gap-1 rounded-full bg-danger-bg px-2 py-1 text-danger">
            −{t}
            <button
              type="button"
              class="rounded-full px-1 leading-none"
              aria-label={`Don't remove ${t}`}
              onclick={() => (tagsRemove = tagsRemove.filter((x) => x !== t))}>×</button>
          </span>
        {/each}
      </div>
      <span>
        Each item keeps its own other tags — add/remove are computed per file.
      </span>
    </div>
  </div>

  <!-- YEAR -->
  <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted">
    <span class="w-14 font-medium">Year</span>
    <div class="flex items-center overflow-hidden rounded-full border border-border-strong" role="group" aria-label="Year action">
      {#each [["none", "No change"], ["set", "Set"], ["clear", "Clear"]] as [mode, lbl] (mode)}
        <button
          type="button"
          class="px-3 py-1 {yearMode === mode ? SEG_ON : ''}"
          aria-pressed={yearMode === mode}
          onclick={() => (yearMode = mode as "none" | "set" | "clear")}>{lbl}</button>
      {/each}
    </div>
    {#if yearMode === "set"}
      <!-- value/oninput, NOT bind:value: Svelte's number binding rewrites the
           bound variable to a `number | null`, which would turn `yearText` into
           a number behind its own type and blow up the `.trim()` in apply(). -->
      <span class="w-24">
        <Input
          size="sm"
          aria-label="Year"
          type="number"
          min="1"
          max="9999"
          step="1"
          placeholder="1999"
          value={yearText}
          oninput={(e) => (yearText = e.currentTarget.value)} />
      </span>
    {:else if yearMode === "clear"}
      <span>Removes the year from every selected item.</span>
    {/if}
  </div>

  <!-- CUSTOM FIELD -->
  <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-muted">
    <span class="w-14 font-medium">Field</span>
    {#if defsError}
      <span>{defsError}</span>
    {:else if !usable.length}
      <span>
        No custom field applies to all {count} selected items
        {defs.length ? " (narrow the selection to one category/library)" : ""}.
      </span>
    {:else}
      <span class="w-48">
        <Select
          size="sm"
          aria-label="Custom field"
          value={fieldName}
          onchange={(e) => pickField(e.currentTarget.value)}>
          <option value="">— no change —</option>
          {#each usable as d (d.id)}
            <option value={d.name}>{d.label}</option>
          {/each}
        </Select>
      </span>
      {#if fieldName}
        <div class="flex items-center overflow-hidden rounded-full border border-border-strong" role="group" aria-label="Custom field action">
          {#each [["set", "Set"], ["clear", "Clear"]] as [mode, lbl] (mode)}
            <button
              type="button"
              class="px-3 py-1 {fieldMode === mode ? SEG_ON : ''}"
              aria-pressed={fieldMode === mode}
              onclick={() => (fieldMode = mode as "set" | "clear")}>{lbl}</button>
          {/each}
        </div>
        {#if fieldMode === "set" && chosen}
          <!-- CustomFieldInput's kit controls read this Field's context. -->
          <Field label={`${chosen.label} value`}>
            <CustomFieldInput def={chosen} bind:value={fieldRaw} />
          </Field>
        {:else if fieldMode === "clear"}
          <span>Removes this key from every selected item.</span>
        {/if}
      {/if}
    {/if}
  </div>
</div>
