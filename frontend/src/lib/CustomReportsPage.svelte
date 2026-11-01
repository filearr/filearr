<script lang="ts">
  import { fmtBytes } from "./format";
  import { onMount } from "svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import DslHelp from "./DslHelp.svelte";
  import { setBuilderPrefill, takeReportPrefill } from "./handoff";
  import {
    listCustomReports,
    getColumnRegistry,
    validateCustomReport,
    createCustomReport,
    updateCustomReport,
    deleteCustomReport,
    copyCustomReport,
    runCustomReport,
    downloadCustomReport,
    EXPORT_FORMATS,
    type ExportFormat,
    type ReportDefinition,
    type ColumnRegistry,
    type ReportValidationError,
    type CustomRunPage,
    errorMessage,
  } from "./api";
  import { Alert, Button, Checkbox, DataTable, Field, Input, Popover, Select, StateBlock, confirm } from "./ui";

  // ------------------------------------------------------------------ //
  // P11 custom (saved-query) reports. A report is a querydsl string +   //
  // a column projection; the backend validates by PARSING + TRANSLATING //
  // the query on save, and /run streams the same CSV path as canned.    //
  // ------------------------------------------------------------------ //

  let defs = $state<ReportDefinition[]>([]);
  let registry = $state<ColumnRegistry | null>(null);
  // Errors by origin: `listError` is the definitions load (in place of the
  // list), `saveError` sits under Save, `rowError` holds a Copy/Download/
  // Delete failure under its report's row, `runError` replaces the result.
  let listError = $state("");
  let loaded = $state(false);
  let saveError = $state("");
  let rowError = $state<Record<string, string>>({});
  let runError = $state("");

  // form state
  let showForm = $state(false);
  let editingId = $state<string | null>(null);
  let fName = $state("");
  let fQuery = $state("");
  let fSelected = $state<Set<string>>(new Set(["rel_path", "library", "size"]));
  let fExtra = $state(""); // comma-separated free meta./cf. columns
  let fFormat = $state("csv");
  let vErrors = $state<ReportValidationError[]>([]);
  let vOk = $state(true);
  let saving = $state(false);

  // row-open (ItemDetail) + per-report download menu
  let detailId = $state<string | null>(null);
  let downloadMenuId = $state<string | null>(null);

  // run state
  let runId = $state<string | null>(null);
  let runName = $state("");
  let runPage = $state<CustomRunPage | null>(null);
  let runOffset = $state(0);
  let running = $state(false);

  const BYTE_COLUMNS = new Set(["size", "total_bytes", "wasted_bytes"]);
  const RUN_LIMIT = 200;

  function cell(row: Record<string, unknown>, col: string): string {
    const v = row[col];
    if (v == null) return "";
    if (BYTE_COLUMNS.has(col) && typeof v === "number") return fmtBytes(v, { zero: "0" });
    return typeof v === "object" ? JSON.stringify(v) : String(v);
  }

  function formColumns(): string[] {
    const extra = fExtra
      .split(",")
      .map((s) => s.trim())
      .filter(Boolean);
    // preserve a stable order: registry order for selected, then free entries
    const ordered: string[] = [];
    for (const c of registryColumns()) if (fSelected.has(c)) ordered.push(c);
    for (const e of extra) if (!ordered.includes(e)) ordered.push(e);
    return ordered;
  }

  function registryColumns(): string[] {
    if (!registry) return [];
    return [...registry.core, ...registry.custom_fields.map((n) => `cf.${n}`)];
  }

  function toggleColumn(c: string) {
    const next = new Set(fSelected);
    if (next.has(c)) next.delete(c);
    else next.add(c);
    fSelected = next;
    scheduleValidate();
  }

  let validateTimer: ReturnType<typeof setTimeout> | undefined;
  function scheduleValidate() {
    clearTimeout(validateTimer);
    validateTimer = setTimeout(runValidate, 300);
  }

  // FIX-12 (Item B): a "Query syntax" help chip appends its example to the report
  // query and re-validates, teaching the filter DSL by example.
  function insertDsl(frag: string) {
    fQuery = fQuery.trim() ? `${fQuery.trim()} ${frag}` : frag;
    scheduleValidate();
  }
  async function runValidate() {
    try {
      const res = await validateCustomReport({
        query: fQuery,
        columns: formColumns(),
      });
      vOk = res.ok;
      vErrors = res.errors;
    } catch (e) {
      vOk = false;
      vErrors = [{ error: "request_error", message: errorMessage(e) }];
    }
  }

  // Kept: formats one validation item with its parse position, which
  // errorMessage (whole-error, msg/message only) does not render.
  function errText(e: ReportValidationError): string {
    if (e.error === "parse_error")
      return `Parse error (${e.code}) at position ${e.position}: ${e.reason}`;
    if (e.error === "translation_error")
      return e.unsupported && e.unsupported.length
        ? `${e.message}`
        : `${e.message}`;
    return e.message ?? e.error;
  }

  function openCreate() {
    editingId = null;
    fName = "";
    fQuery = "";
    fSelected = new Set(["rel_path", "library", "size"]);
    fExtra = "";
    fFormat = "csv";
    vErrors = [];
    vOk = true;
    saveError = "";
    showForm = true;
  }
  function openEdit(d: ReportDefinition) {
    editingId = d.id;
    fName = d.name;
    fQuery = d.query;
    const known = new Set(registryColumns());
    fSelected = new Set(d.columns.filter((c) => known.has(c)));
    fExtra = d.columns.filter((c) => !known.has(c)).join(", ");
    fFormat = d.format;
    vErrors = [];
    vOk = true;
    saveError = "";
    showForm = true;
    scheduleValidate();
  }
  function cancelForm() {
    showForm = false;
    editingId = null;
  }

  // Round-trip to the visual builder: parse this report's DSL back into rows.
  async function copyReport(d: ReportDefinition) {
    rowError[d.id] = "";
    try {
      const copy = await copyCustomReport(d.id);
      defs = await listCustomReports();
      openEdit(copy); // land straight in the edit form: copy-and-edit in one click
    } catch (e) {
      rowError[d.id] = errorMessage(e);
    }
  }

  function editInBuilder(d: ReportDefinition) {
    setBuilderPrefill({ query: d.query });
    location.hash = "#/filter-builder";
  }

  async function save() {
    saveError = "";
    saving = true;
    try {
      const body = {
        name: fName,
        query: fQuery,
        columns: formColumns(),
        format: fFormat,
      };
      if (editingId) await updateCustomReport(editingId, body);
      else await createCustomReport(body);
      showForm = false;
      editingId = null;
      defs = await listCustomReports();
    } catch (e) {
      saveError = errorMessage(e, { verb: "save this report" });
    } finally {
      saving = false;
    }
  }

  async function remove(d: ReportDefinition) {
    const ok = await confirm({
      title: "Delete custom report?",
      message: `Delete custom report “${d.name}”?`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    rowError[d.id] = "";
    try {
      await deleteCustomReport(d.id);
      if (runId === d.id) {
        runId = null;
        runPage = null;
      }
      defs = await listCustomReports();
    } catch (e) {
      rowError[d.id] = errorMessage(e, { verb: "delete this report" });
    }
  }

  async function run(d: ReportDefinition, offset = 0) {
    runError = "";
    running = true;
    // A different report's rows must not linger dimmed under this one's title.
    if (runId !== d.id) runPage = null;
    runId = d.id;
    runName = d.name;
    runOffset = offset;
    try {
      runPage = await runCustomReport(d.id, { limit: RUN_LIMIT, offset });
    } catch (e) {
      runError = errorMessage(e);
      runPage = null;
    } finally {
      running = false;
    }
  }

  async function download(d: ReportDefinition, format: ExportFormat) {
    downloadMenuId = null;
    rowError[d.id] = "";
    try {
      await downloadCustomReport(d.id, d.name, format);
    } catch (e) {
      rowError[d.id] = errorMessage(e);
    }
  }

  // Paging / retry re-run the report currently shown, at a given offset.
  function rerun(offset: number) {
    const d = defs.find((x) => x.id === runId);
    if (d) run(d, offset);
  }

  // Run rows carry no id; a row's index in the page is stable while shown.
  const runRowIndex = (row: Record<string, unknown>) => runPage?.rows.indexOf(row) ?? -1;
  const runColumns = $derived([
    ...(runPage?.columns ?? []).map((c) => ({
      key: c,
      label: c,
      align: (BYTE_COLUMNS.has(c) ? "right" : "left") as "left" | "right",
    })),
    { key: "__open", label: "Actions", align: "right" as const, cell: openCell },
  ]);

  async function loadDefs() {
    listError = "";
    try {
      [defs, registry] = await Promise.all([
        listCustomReports(),
        getColumnRegistry(),
      ]);
      loaded = true;
    } catch (e) {
      listError = errorMessage(e);
    }
  }

  onMount(async () => {
    await loadDefs();
    // Handoff from the filter builder ("Save as custom report"): open the create
    // form pre-filled with the compiled DSL + a sensible column projection.
    const pf = takeReportPrefill();
    if (pf) {
      openCreate();
      fQuery = pf.query;
      if (pf.name) fName = pf.name;
      if (pf.columns && pf.columns.length) {
        const known = new Set(registryColumns());
        fSelected = new Set(pf.columns.filter((c) => known.has(c)));
        fExtra = pf.columns.filter((c) => !known.has(c)).join(", ");
      }
      scheduleValidate();
    }
  });
</script>

{#snippet openCell(row: Record<string, unknown>)}
  <Button size="sm" onclick={() => row.item_id && (detailId = String(row.item_id))} disabled={!row.item_id}>Open</Button>
{/snippet}

<div class="mt-8 border-t border-border pt-6">
  <div class="mb-3 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Custom reports</h2>
    <div class="grow"></div>
    <Button variant="primary" size="sm" onclick={openCreate}>New custom report</Button>
  </div>

  <!-- create / edit form -->
  {#if showForm}
    <div class="mb-5 rounded-panel border border-border-strong p-4">
      <div class="grid gap-3 sm:grid-cols-2">
        <Field label="Name" required>
          <Input bind:value={fName} placeholder="e.g. Low-res videos" />
        </Field>
        <Field label="Default format">
          <Select bind:value={fFormat}>
            <option value="csv">CSV</option>
            <option value="json">JSON</option>
          </Select>
        </Field>
      </div>

      <Field label="Query (filter DSL)" class="mt-3">
        <Input
          class="font-mono"
          bind:value={fQuery}
          oninput={scheduleValidate}
          placeholder="kind:video meta.height:>=1080 -tag:archived" />
      </Field>
      <DslHelp onInsert={insertDsl} context="reports" />

      <fieldset class="mt-3 text-sm">
        <legend class="mb-1 text-xs font-medium text-fg">Columns</legend>
        <div class="flex flex-wrap gap-x-4 gap-y-1 font-mono">
          {#each registryColumns() as c (c)}
            <Checkbox size="sm" label={c} checked={fSelected.has(c)} onchange={() => toggleColumn(c)} />
          {/each}
        </div>
        <Field label="Extra meta./cf. columns (comma-separated)" class="mt-2">
          <Input
            size="sm"
            class="font-mono"
            bind:value={fExtra}
            oninput={scheduleValidate}
            placeholder="meta.video_codec, meta.bitrate" />
        </Field>
      </fieldset>

      {#if vErrors.length}
        <!-- Live validation as you type: shown, not announced on every keystroke. -->
        <Alert tone="warn" class="mt-3" live={false}>
          <ul class="space-y-1 text-xs">
            {#each vErrors as e, i (i)}
              <li>{errText(e)}</li>
            {/each}
          </ul>
        </Alert>
      {:else if fQuery || formColumns().length}
        <p class="mt-3 text-xs text-ok">Query is valid.</p>
      {/if}

      <div class="mt-4 flex items-center gap-2">
        <Button
          variant="primary"
          size="sm"
          onclick={save}
          disabled={saving || !vOk || !fName || !formColumns().length}>
          {saving ? "Saving…" : editingId ? "Save changes" : "Create"}
        </Button>
        <Button size="sm" onclick={cancelForm}>Cancel</Button>
      </div>
      {#if saveError}
        <Alert tone="danger" class="mt-2" onDismiss={() => (saveError = "")}>{saveError}</Alert>
      {/if}
    </div>
  {/if}

  <!-- definitions list -->
  {#if listError}
    <StateBlock state="error" message={listError} onRetry={loadDefs} />
  {:else if !loaded}
    <StateBlock state="loading" />
  {:else if defs.length === 0}
    <StateBlock
      state="empty"
      message="No custom reports yet. Use New custom report (or Save as custom report in the filter builder) to save a query." />
  {:else}
    <div class="space-y-2">
      {#each defs as d (d.id)}
        <div class="rounded-control border border-border px-3 py-2">
          <div class="flex flex-wrap items-center gap-2">
            <div class="min-w-0">
              <div class="font-medium">{d.name}</div>
              <div class="truncate font-mono text-xs text-muted">{d.query || "(all items)"}</div>
            </div>
            <div class="grow"></div>
            <Button size="sm" onclick={() => run(d)}>Run</Button>
            <Popover
              bind:open={() => downloadMenuId === d.id, (v) => (downloadMenuId = v ? d.id : null)}
              label="Download"
              ariaLabel="Download {d.name}"
              size="sm"
              align="end"
              class="w-32 p-1">
              {#each EXPORT_FORMATS as f (f)}
                <button
                  type="button"
                  class="block w-full rounded-control px-3 py-1.5 text-left text-sm hover:bg-surface-3"
                  onclick={() => download(d, f)}>{f.toUpperCase()}</button>
              {/each}
            </Popover>
            <Button
              size="sm"
              title="Clone this report and open the clone for editing — the original is untouched"
              onclick={() => copyReport(d)}>Copy</Button>
            <Button size="sm" onclick={() => editInBuilder(d)}>Edit in builder</Button>
            <Button size="sm" onclick={() => openEdit(d)}>Edit</Button>
            <Button size="sm" variant="danger" onclick={() => remove(d)}>Delete</Button>
          </div>
          {#if rowError[d.id]}
            <Alert tone="danger" class="mt-2" onDismiss={() => (rowError[d.id] = "")}>{rowError[d.id]}</Alert>
          {/if}
        </div>
      {/each}
    </div>
  {/if}

  <!-- run result -->
  {#if runId}
    <div class="mt-5">
      <h3 class="mb-2 text-base font-semibold">Result — {runName}</h3>
      <DataTable
        caption="Result — {runName}"
        captionHidden
        columns={runColumns}
        rows={runPage?.rows ?? []}
        rowKey={runRowIndex}
        accessor={(row, k) => cell(row, k)}
        loading={running}
        error={runError || null}
        onRetry={() => rerun(runOffset)}
        emptyMessage="No rows match this report's query. Edit the query to widen it." />
      {#if runPage}
        <div class="mt-3 flex items-center gap-3 text-sm">
          <Button
            size="sm"
            onclick={() => rerun(Math.max(0, runOffset - RUN_LIMIT))}
            disabled={runOffset <= 0 || running}>Previous</Button>
          <span class="text-muted">
            rows {runPage.count === 0 ? 0 : runOffset + 1}–{runOffset + runPage.count}
          </span>
          <Button
            size="sm"
            onclick={() => rerun(runOffset + RUN_LIMIT)}
            disabled={!runPage.has_more || running}>Next</Button>
        </div>
      {/if}
    </div>
  {/if}
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} onOpen={(nid) => (detailId = nid)} />
{/if}
