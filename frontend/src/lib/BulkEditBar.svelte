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
  import { listCustomFields, type CustomField } from "./api";
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
      defs = await listCustomFields();
    } catch {
      // Non-fatal: tags + year still work; the custom-field row explains itself.
      defsError = "Custom-field definitions are unavailable (read scope?).";
    }
  });

  // Only fields applicable to EVERY selected item. Offering the union would
  // guarantee that some items 422 — see applicableFields() for the reasoning.
  const usable = $derived(applicableFields(defs, targets));
  const chosen = $derived(usable.find((d) => d.name === fieldName) ?? null);

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

  const nothingToDo = $derived(opsAreEmpty(ops));

  function apply() {
    error = "";
    // Fold any half-typed chip in, so a user who types a tag and hits Apply
    // without pressing Enter still gets what they meant.
    if (addDraft.trim()) commitAdd();
    if (removeDraft.trim()) commitRemove();

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

  const CHIP = "rounded-full border px-3 py-1 text-sm";
  const ON = "border-transparent bg-[var(--accent)] text-white";
  const OFF = "border-slate-300 dark:border-slate-700";
  const BOX =
    "rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm outline-none " +
    "focus:border-[var(--accent)] dark:border-slate-700";
</script>

<!-- Same chip-bar visual convention as the filter rows above it, one step more
     prominent (accent-tinted panel) because it WRITES. -->
<div
  class="mt-2 rounded-lg border border-[var(--accent)]/40 bg-[var(--accent)]/5 p-3"
  role="region"
  aria-label="Bulk edit selected items">
  <div class="flex flex-wrap items-center gap-2">
    <span class="rounded-full bg-[var(--accent)] px-3 py-1 text-sm font-medium text-white">
      {count} selected
    </span>
    <button type="button" class="{CHIP} {OFF}" onclick={onClear}>Clear selection</button>
    <span class="grow"></span>
    {#if error}<span class="text-xs text-red-500">{error}</span>{/if}
    <button
      type="button"
      class="rounded-lg bg-[var(--accent)] px-4 py-1.5 text-sm text-white disabled:opacity-50"
      disabled={busy || nothingToDo}
      title={nothingToDo ? "Choose a tag, year or custom-field change first" : "Apply to every selected item"}
      onclick={apply}>{busy ? "Applying…" : `Apply to ${count}`}</button>
  </div>

  <!-- TAGS -->
  <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-slate-500">
    <span class="mt-1.5 w-14 font-medium">Tags</span>
    <div class="flex flex-col gap-1">
      <div class="flex flex-wrap items-center gap-1">
        <label class="sr-only" for="bulk-tag-add">Tags to add</label>
        <input
          id="bulk-tag-add"
          class="{BOX} w-44"
          placeholder="add tag…"
          bind:value={addDraft}
          onkeydown={(e) => tagKey(e, "add")}
          onblur={commitAdd} />
        {#each tagsAdd as t (t)}
          <span class="inline-flex items-center gap-1 rounded-full bg-emerald-500/15 px-2 py-1 text-emerald-700 dark:text-emerald-300">
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
        <label class="sr-only" for="bulk-tag-remove">Tags to remove</label>
        <input
          id="bulk-tag-remove"
          class="{BOX} w-44"
          placeholder="remove tag…"
          bind:value={removeDraft}
          onkeydown={(e) => tagKey(e, "remove")}
          onblur={commitRemove} />
        {#each tagsRemove as t (t)}
          <span class="inline-flex items-center gap-1 rounded-full bg-red-500/15 px-2 py-1 text-red-700 dark:text-red-300">
            −{t}
            <button
              type="button"
              class="rounded-full px-1 leading-none"
              aria-label={`Don't remove ${t}`}
              onclick={() => (tagsRemove = tagsRemove.filter((x) => x !== t))}>×</button>
          </span>
        {/each}
      </div>
      <span class="opacity-70">
        Each item keeps its own other tags — add/remove are computed per file.
      </span>
    </div>
  </div>

  <!-- YEAR -->
  <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
    <span class="w-14 font-medium">Year</span>
    <div class="flex items-center overflow-hidden rounded-full border border-slate-300 dark:border-slate-700" role="group" aria-label="Year action">
      {#each [["none", "No change"], ["set", "Set"], ["clear", "Clear"]] as [mode, lbl] (mode)}
        <button
          type="button"
          class="px-3 py-1 {yearMode === mode ? 'bg-[var(--accent)] text-white' : ''}"
          aria-pressed={yearMode === mode}
          onclick={() => (yearMode = mode as "none" | "set" | "clear")}>{lbl}</button>
      {/each}
    </div>
    {#if yearMode === "set"}
      <label class="sr-only" for="bulk-year">Year</label>
      <!-- value/oninput, NOT bind:value: Svelte's number binding rewrites the
           bound variable to a `number | null`, which would turn `yearText` into
           a number behind its own type and blow up the `.trim()` in apply(). -->
      <input
        id="bulk-year"
        class="{BOX} w-24"
        type="number"
        min="1"
        max="9999"
        step="1"
        placeholder="1999"
        value={yearText}
        oninput={(e) => (yearText = e.currentTarget.value)} />
    {:else if yearMode === "clear"}
      <span class="opacity-70">Removes the year from every selected item.</span>
    {/if}
  </div>

  <!-- CUSTOM FIELD -->
  <div class="mt-3 flex flex-wrap items-center gap-2 text-xs text-slate-500">
    <span class="w-14 font-medium">Field</span>
    {#if defsError}
      <span class="opacity-70">{defsError}</span>
    {:else if !usable.length}
      <span class="opacity-70">
        No custom field applies to all {count} selected items
        {defs.length ? " (narrow the selection to one category/library)" : ""}.
      </span>
    {:else}
      <label class="sr-only" for="bulk-field">Custom field</label>
      <select id="bulk-field" class="{BOX} w-48" bind:value={fieldName}>
        <option value="">— no change —</option>
        {#each usable as d (d.id)}
          <option value={d.name}>{d.label}</option>
        {/each}
      </select>
      {#if fieldName}
        <div class="flex items-center overflow-hidden rounded-full border border-slate-300 dark:border-slate-700" role="group" aria-label="Custom field action">
          {#each [["set", "Set"], ["clear", "Clear"]] as [mode, lbl] (mode)}
            <button
              type="button"
              class="px-3 py-1 {fieldMode === mode ? 'bg-[var(--accent)] text-white' : ''}"
              aria-pressed={fieldMode === mode}
              onclick={() => (fieldMode = mode as "set" | "clear")}>{lbl}</button>
          {/each}
        </div>
        {#if fieldMode === "set" && chosen}
          <CustomFieldInput def={chosen} bind:value={fieldRaw} id="bulk-field-value" />
        {:else if fieldMode === "clear"}
          <span class="opacity-70">Removes this key from every selected item.</span>
        {/if}
      {/if}
    {/if}
  </div>
</div>
