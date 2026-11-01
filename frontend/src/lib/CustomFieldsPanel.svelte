<script lang="ts">
  // P4-T3 — admin surface for custom-field DEFINITIONS. List + create + edit +
  // soft-delete. Values written under these keys live in items.user_metadata and
  // are type-checked on edit (P4-T4); this panel only manages the definitions.
  //
  // name + data_type are IMMUTABLE server-side (the API 422s a change), so the
  // edit form exposes only the mutable fields. facetable/sortable show a note
  // that they take effect after an index rebuild (P4-T6). All dynamic strings
  // render as text (no {@html}).
  import { onMount } from "svelte";
  import {
    CUSTOM_FIELD_TYPES,
    createCustomField,
    deleteCustomField,
    listCustomFields,
    updateCustomField,
    type CustomField,
    type CustomFieldType,
    type Library,
    errorMessage,
  } from "./api";
  import { invalidateCustomFields } from "./configCache";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { Alert, Button, Checkbox, DataTable, Field, Input, Modal, Select, Textarea, confirm } from "./ui";

  let { libraries = [] }: { libraries?: Library[] } = $props();

  let fields = $state<CustomField[]>([]);
  let loadError = $state("");
  let createError = $state("");
  let editError = $state(""); // the edit dialog's Save
  let deleteError = $state(""); // row delete
  let loading = $state(true);

  // create form
  let cName = $state("");
  let cLabel = $state("");
  let cType = $state<CustomFieldType>("string");
  let cLibraryIds = $state<string[]>([]);
  let cFacetable = $state(false);
  let cSortable = $state(false);
  let cRequired = $state(false);
  let cSelectOptions = $state(""); // newline-joined, only for select
  let creating = $state(false);

  // inline edit target (mutable fields only)
  let editing = $state<CustomField | null>(null);
  let eLabel = $state("");
  let eLibraryIds = $state<string[]>([]);
  let eFacetable = $state(false);
  let eSortable = $state(false);
  let eRequired = $state(false);
  let eSelectOptions = $state("");
  let saving = $state(false);

  const libName = (id: string) => libraries.find((l) => l.id === id)?.name ?? id;

  async function refresh() {
    loading = true;
    try {
      fields = await listCustomFields();
      loadError = "";
    } catch (e) {
      loadError = errorMessage(e);
    } finally {
      loading = false;
    }
  }

  onMount(refresh);

  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  async function create(e: Event) {
    e.preventDefault();
    creating = true;
    createError = "";
    try {
      await createCustomField({
        name: cName,
        label: cLabel || cName,
        data_type: cType,
        library_ids: cLibraryIds,
        facetable: cFacetable,
        sortable: cSortable,
        required: cRequired,
        select_options:
          cType === "select"
            ? cSelectOptions.split("\n").map((s) => s.trim()).filter(Boolean)
            : null,
      });
      cName = "";
      cLabel = "";
      cType = "string";
      cLibraryIds = [];
      cFacetable = cSortable = cRequired = false;
      cSelectOptions = "";
      invalidateCustomFields(); // A44: item details and the bulk bar refetch
      await refresh();
    } catch (err) {
      createError = errorMessage(err, { verb: "manage custom fields" });
    } finally {
      creating = false;
    }
  }

  function startEdit(f: CustomField) {
    editError = "";
    editing = f;
    eLabel = f.label;
    eLibraryIds = [...f.library_ids];
    eFacetable = f.facetable;
    eSortable = f.sortable;
    eRequired = f.required;
    eSelectOptions = (f.select_options ?? []).join("\n");
  }

  const sameIds = (a: string[], b: string[]) => a.length === b.length && a.every((x) => b.includes(x));
  // Unsaved edits in the dialog, for its "Discard changes?" check.
  const editDirty = $derived(
    !!editing &&
      (eLabel !== editing.label ||
        !sameIds(eLibraryIds, editing.library_ids) ||
        eFacetable !== editing.facetable ||
        eSortable !== editing.sortable ||
        eRequired !== editing.required ||
        eSelectOptions !== (editing.select_options ?? []).join("\n")),
  );

  async function saveEdit() {
    if (!editing) return;
    saving = true;
    editError = "";
    try {
      await updateCustomField(editing.id, {
        label: eLabel,
        library_ids: eLibraryIds,
        facetable: eFacetable,
        sortable: eSortable,
        required: eRequired,
        select_options:
          editing.data_type === "select"
            ? eSelectOptions.split("\n").map((s) => s.trim()).filter(Boolean)
            : null,
      });
      editing = null;
      invalidateCustomFields(); // A44: item details and the bulk bar refetch
      await refresh();
    } catch (err) {
      editError = errorMessage(err, { verb: "manage custom fields" });
    } finally {
      saving = false;
    }
  }

  async function remove(f: CustomField) {
    const ok = await confirm({
      title: `Delete custom field "${f.label}" (${f.name})?`,
      message:
        "The definition is removed but existing values already saved under this " +
        "key on any item are kept untouched.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    deleteError = "";
    try {
      await deleteCustomField(f.id);
      invalidateCustomFields(); // A44: item details and the bulk bar refetch
      await refresh();
    } catch (err) {
      deleteError = errorMessage(err, { verb: "manage custom fields" });
    }
  }
</script>

{#snippet nameCell(f: CustomField)}<span class="font-mono text-xs">{f.name}</span>{/snippet}
{#snippet typeCell(f: CustomField)}<span class="text-xs text-muted">{f.data_type}</span>{/snippet}
{#snippet librariesCell(f: CustomField)}
  <span class="text-xs text-muted">{f.library_ids.length === 0 ? "all" : f.library_ids.map(libName).join(", ")}</span>
{/snippet}
{#snippet facetCell(f: CustomField)}{f.facetable ? "✓" : "—"}{/snippet}
{#snippet sortCell(f: CustomField)}{f.sortable ? "✓" : "—"}{/snippet}
{#snippet reqCell(f: CustomField)}{f.required ? "✓" : "—"}{/snippet}
{#snippet actionsCell(f: CustomField)}
  <div class="flex justify-end gap-1">
    <Button size="sm" onclick={() => startEdit(f)}>Edit</Button>
    <Button size="sm" variant="danger" onclick={() => remove(f)}>Delete</Button>
  </div>
{/snippet}

<!-- Library toggle chips (create form and edit dialog): bespoke toggles, not
     a kit control; aria-pressed carries the state the colour shows. -->
{#snippet libraryChips(selected: string[], onToggle: (id: string) => void)}
  <div class="flex flex-wrap gap-2">
    {#each libraries as l (l.id)}
      <button type="button" aria-pressed={selected.includes(l.id)}
        class="rounded-full border px-3 py-1 text-sm {selected.includes(l.id) ? 'border-transparent bg-accent text-accent-fg' : 'border-border-strong text-fg'}"
        onclick={() => onToggle(l.id)}>{l.name}</button>
    {/each}
    <span class="self-center text-xs text-muted">(none = all libraries)</span>
  </div>
{/snippet}

<section class="mt-8">
  <h2 class="text-lg font-semibold">Custom fields</h2>
  <p class="mt-1 max-w-2xl text-xs text-slate-500">
    Admin-defined metadata fields. Values are stored per item under
    <code>user_metadata</code> and type-checked on edit. Deleting a definition keeps
    existing values.
  </p>

  <DataTable
    class="mt-3"
    caption="Custom fields"
    captionHidden
    columns={[
      { key: "name", label: "Name", cell: nameCell },
      { key: "label", label: "Label" },
      { key: "data_type", label: "Type", cell: typeCell },
      { key: "library_ids", label: "Libraries", cell: librariesCell },
      { key: "facetable", label: "Facet", cell: facetCell },
      { key: "sortable", label: "Sort", cell: sortCell },
      { key: "required", label: "Req", cell: reqCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={fields}
    rowKey={(f) => f.id}
    {loading}
    error={loadError || null}
    onRetry={refresh}
    emptyMessage="No custom fields yet — add one below."
  />
  {#if deleteError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (deleteError = "")}>{deleteError}</Alert>
  {/if}

  <h3 class="mt-6 text-sm font-semibold">Add custom field</h3>
  <!-- Help sits beside each Field/Checkbox, not inside its label, so the
       labels stay plain strings and the "?" button is not nested in a label. -->
  <form class="mt-3 flex max-w-xl flex-col gap-3" onsubmit={create}>
    <div class="flex items-start gap-2">
      <Field label="Name" required class="flex-1">
        <Input class="font-mono" placeholder="e.g. shelf_location" bind:value={cName} />
      </Field>
      <span class="pt-0.5"><Help text={HELP.cf_name} label="custom field name" /></span>
    </div>

    <Field label="Label">
      <Input placeholder="Human-readable label" bind:value={cLabel} />
    </Field>

    <div class="flex items-start gap-2">
      <Field label="Type" class="w-48">
        <Select bind:value={cType}>
          {#each CUSTOM_FIELD_TYPES as t}<option value={t}>{t}</option>{/each}
        </Select>
      </Field>
      <span class="pt-0.5"><Help text={HELP.cf_data_type} label="custom field type" /></span>
    </div>

    {#if cType === "select"}
      <Field label="Options (one per line)">
        <Textarea rows={3} bind:value={cSelectOptions} />
      </Field>
    {/if}

    <div class="flex flex-col gap-1">
      <span class="flex items-center gap-1 text-xs font-medium text-fg">
        Libraries <Help text={HELP.cf_libraries} label="custom field libraries" />
      </span>
      {@render libraryChips(cLibraryIds, (id) => (cLibraryIds = toggle(cLibraryIds, id)))}
    </div>

    <div class="flex flex-wrap items-center gap-4">
      <Checkbox label="facetable" bind:checked={cFacetable} />
      <span class="inline-flex items-center gap-2">
        <Checkbox label="sortable" bind:checked={cSortable} />
        <Help text={HELP.cf_facet_sort} label="facet/sort" />
      </span>
      <span class="inline-flex items-center gap-2">
        <Checkbox label="required" bind:checked={cRequired} />
        <Help text={HELP.cf_required} label="required" />
      </span>
    </div>
    {#if cFacetable || cSortable}
      <p class="-mt-1 text-xs text-warn">
        Faceting/sorting takes effect after an index rebuild (P4-T6).
      </p>
    {/if}

    <Button type="submit" variant="primary" class="self-start" disabled={creating}>
      {creating ? "Adding…" : "Add custom field"}
    </Button>
    {#if createError}
      <Alert tone="danger" onDismiss={() => (createError = "")}>{createError}</Alert>
    {/if}
  </form>
</section>

<Modal
  open={!!editing}
  onClose={() => (editing = null)}
  title={`Edit custom field ${editing?.name ?? ""}`}
  dirty={() => editDirty}
>
  {#if editing}
    <p class="text-xs text-muted">
      Name (<code>{editing.name}</code>) and type (<code>{editing.data_type}</code>) are
      immutable — create a new field to change them.
    </p>

    <div class="mt-4 flex flex-col gap-3">
      <Field label="Label">
        <Input bind:value={eLabel} />
      </Field>

      {#if editing.data_type === "select"}
        <Field label="Options (one per line)">
          <Textarea rows={3} bind:value={eSelectOptions} />
        </Field>
      {/if}

      <div class="flex flex-col gap-1">
        <span class="flex items-center gap-1 text-xs font-medium text-fg">
          Libraries <Help text={HELP.cf_libraries} label="custom field libraries" />
        </span>
        {@render libraryChips(eLibraryIds, (id) => (eLibraryIds = toggle(eLibraryIds, id)))}
      </div>

      <div class="flex flex-wrap items-center gap-4">
        <Checkbox label="facetable" bind:checked={eFacetable} />
        <Checkbox label="sortable" bind:checked={eSortable} />
        <Checkbox label="required" bind:checked={eRequired} />
      </div>
      {#if eFacetable || eSortable}
        <p class="-mt-1 text-xs text-warn">
          Faceting/sorting takes effect after an index rebuild (P4-T6).
        </p>
      {/if}
      {#if editError}
        <Alert tone="danger" onDismiss={() => (editError = "")}>{editError}</Alert>
      {/if}
    </div>
  {/if}
  {#snippet footer({ close })}
    <Button onclick={close}>Cancel</Button>
    <Button variant="primary" disabled={saving} onclick={saveEdit}>{saving ? "Saving…" : "Save"}</Button>
  {/snippet}
</Modal>
