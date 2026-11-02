<script lang="ts">
  import { fmtBytes } from "./format";
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import { focusPanel } from "./rowPanel";
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
    enqueueCustomReportExport,
    errorStatus,
    EXPORT_FORMATS,
    type ExportFormat,
    type ReportDefinition,
    type ColumnRegistry,
    type ReportValidationError,
    type CustomRunPage,
    errorMessage,
  } from "./api";
  import { Alert, Button, Checkbox, DataTable, Field, Input, Popover, StateBlock, confirm } from "./ui";

  // ------------------------------------------------------------------ //
  // P11 custom (saved-query) reports. A report is a querydsl string +   //
  // a column projection; the backend validates by PARSING + TRANSLATING //
  // the query on save, and /run streams the same CSV path as canned.    //
  // ------------------------------------------------------------------ //

  // B21: the host refreshes its Exports panel when a background export lands.
  let { onExportQueued }: { onExportQueued?: () => void } = $props();

  let defs = $state<ReportDefinition[]>([]);
  let registry = $state<ColumnRegistry | null>(null);
  // Errors by origin: `listError` is the definitions load (in place of the
  // list), `saveError` sits under Save, `rowError` holds a Copy/Download/
  // Delete failure under its report's row, `runError` replaces the result.
  let listError = $state("");
  let loaded = $state(false);
  let saveError = $state("");
  let rowError = $state<Record<string, string>>({});
  let rowNote = $state<Record<string, string>>({});
  let runError = $state("");
  // B41: one action per report at a time (double-clicks on Copy/Run/Delete/
  // Download would otherwise fire twice).
  let busy = $state<Record<string, boolean>>({});

  // form state
  let showForm = $state(false);
  let editingId = $state<string | null>(null);
  let fName = $state("");
  let fQuery = $state("");
  let fSelected = $state<Set<string>>(new Set(["rel_path", "library", "size"]));
  let fExtra = $state(""); // comma-separated free meta./cf. columns
  // B41: the old "Default format" select is gone: nothing read it (downloads
  // pick a format from the menu) and the API defaults it, so creates omit it
  // and edits leave the stored value alone.
  let vErrors = $state<ReportValidationError[]>([]);
  // B41: false until a validation of the CURRENT query + columns says ok; any
  // edit resets it, and `validating` covers the debounce + round trip.
  let vOk = $state(false);
  let validating = $state(false);
  let saving = $state(false);
  const editorId = "custom-report-editor";

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
  // Last-request-wins: an older validate answering late never lands.
  const validator = latest((signal, body: { query: string; columns: string[] }) =>
    validateCustomReport(body, signal),
  );
  function scheduleValidate() {
    clearTimeout(validateTimer);
    validator.cancel();
    vOk = false;
    validating = true;
    validateTimer = setTimeout(runValidate, 300);
  }
  onDestroy(() => {
    clearTimeout(validateTimer);
    validator.cancel();
  });

  // FIX-12 (Item B): a "Query syntax" help chip appends its example to the report
  // query and re-validates, teaching the filter DSL by example.
  function insertDsl(frag: string) {
    fQuery = fQuery.trim() ? `${fQuery.trim()} ${frag}` : frag;
    scheduleValidate();
  }
  async function runValidate() {
    try {
      const res = await validator.run({ query: fQuery, columns: formColumns() });
      if (res === undefined) return;
      vOk = res.ok;
      vErrors = res.errors;
      validating = false;
    } catch (e) {
      vOk = false;
      vErrors = [{ error: "request_error", message: errorMessage(e) }];
      validating = false;
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
    vErrors = [];
    saveError = "";
    showForm = true;
    // An empty query (all items) is valid; checking it now enables Create.
    scheduleValidate();
    void focusPanel(editorId);
  }
  function openEdit(d: ReportDefinition) {
    editingId = d.id;
    fName = d.name;
    fQuery = d.query;
    const known = new Set(registryColumns());
    fSelected = new Set(d.columns.filter((c) => known.has(c)));
    fExtra = d.columns.filter((c) => !known.has(c)).join(", ");
    vErrors = [];
    saveError = "";
    showForm = true;
    scheduleValidate();
    void focusPanel(editorId);
  }
  function cancelForm() {
    showForm = false;
    editingId = null;
    clearTimeout(validateTimer);
    validator.cancel();
    validating = false;
  }

  /** Run `fn` for report `id` unless an action for it is already in flight. */
  async function guarded(id: string, fn: () => Promise<void>) {
    if (busy[id]) return;
    busy[id] = true;
    try {
      await fn();
    } finally {
      busy[id] = false;
    }
  }

  // B41: Run and Download go through /run, which needs the write scope (the
  // list only needs read): say so instead of a bare permission sentence.
  function actionError(e: unknown, verb: string): string {
    if (errorStatus(e) === 403) {
      return `You don't have permission to ${verb}. Running, downloading or exporting a custom report needs write access; a read-only key or role can list reports but not run them.`;
    }
    return errorMessage(e, { verb });
  }

  // Round-trip to the visual builder: parse this report's DSL back into rows.
  function copyReport(d: ReportDefinition) {
    return guarded(d.id, async () => {
      rowError[d.id] = "";
      try {
        const copy = await copyCustomReport(d.id);
        defs = await listCustomReports();
        openEdit(copy); // land straight in the edit form: copy-and-edit in one click
      } catch (e) {
        rowError[d.id] = errorMessage(e, { verb: "copy this report" });
      }
    });
  }

  function editInBuilder(d: ReportDefinition) {
    setBuilderPrefill({ query: d.query });
    location.hash = "#/filter-builder";
  }

  async function save() {
    if (saving || !vOk) return;
    saveError = "";
    saving = true;
    try {
      const body = {
        name: fName,
        query: fQuery,
        columns: formColumns(),
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
    await guarded(d.id, async () => {
      rowError[d.id] = "";
      try {
        await deleteCustomReport(d.id);
        if (runId === d.id) {
          runId = null;
          runPage = null;
        }
        if (editingId === d.id) cancelForm();
        defs = await listCustomReports();
      } catch (e) {
        rowError[d.id] = errorMessage(e, { verb: "delete this report" });
      }
    });
  }

  async function run(d: ReportDefinition, offset = 0) {
    if (running) return;
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
      runError = actionError(e, "run this report");
      runPage = null;
    } finally {
      running = false;
    }
  }

  // The streaming download in flight (one at a time across reports): Cancel
  // aborts it, and so does leaving the page.
  let downloadCtl: AbortController | null = null;
  let downloadingId = $state<string | null>(null);
  onDestroy(() => downloadCtl?.abort());

  async function download(d: ReportDefinition, format: ExportFormat) {
    downloadMenuId = null;
    if (downloadingId) return;
    rowError[d.id] = "";
    rowNote[d.id] = "";
    const ctl = new AbortController();
    downloadCtl = ctl;
    downloadingId = d.id;
    try {
      await downloadCustomReport(d.id, d.name, format, ctl.signal);
    } catch (e) {
      if (!ctl.signal.aborted) rowError[d.id] = actionError(e, "download this report");
    } finally {
      if (downloadCtl === ctl) {
        downloadCtl = null;
        downloadingId = null;
      }
    }
  }

  function cancelDownload() {
    downloadCtl?.abort();
    downloadCtl = null;
    downloadingId = null;
  }

  // B21 / Ruling 11: custom reports get the background path too — the sync
  // download is the memory-bound one for big results.
  function queueBackground(d: ReportDefinition, format: ExportFormat) {
    downloadMenuId = null;
    return guarded(d.id, async () => {
      rowError[d.id] = "";
      rowNote[d.id] = "";
      try {
        await enqueueCustomReportExport(d.id, format);
        rowNote[d.id] = `Queued a background ${format.toUpperCase()} export — it is listed in the Exports panel below.`;
        onExportQueued?.();
      } catch (e) {
        rowError[d.id] = actionError(e, "export this report");
      }
    });
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
      // openCreate scrolls the editor into view and focuses it: the list sits
      // above it on a long page, so the hand-off would otherwise land unseen.
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
    <div
      id={editorId}
      tabindex="-1"
      aria-label={editingId ? "Edit custom report" : "New custom report"}
      class="mb-5 rounded-panel border border-border-strong p-4 focus:outline-none">
      <div class="grid gap-3 sm:grid-cols-2">
        <Field label="Name" required>
          <Input bind:value={fName} placeholder="e.g. Low-res videos" />
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

      {#if validating}
        <p class="mt-3 text-xs text-muted">Checking the query…</p>
      {:else if vErrors.length}
        <!-- Live validation as you type: shown, not announced on every keystroke. -->
        <Alert tone="warn" class="mt-3" live={false}>
          <ul class="space-y-1 text-xs">
            {#each vErrors as e, i (i)}
              <li>{errText(e)}</li>
            {/each}
          </ul>
        </Alert>
      {:else if vOk}
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
            <Button size="sm" onclick={() => run(d)} disabled={running || busy[d.id]}>Run</Button>
            {#if downloadingId === d.id}
              <span class="text-xs text-muted">Preparing…</span>
              <Button size="sm" onclick={cancelDownload}>Cancel download</Button>
            {/if}
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
                  class="block w-full rounded-control px-3 py-1.5 text-left text-sm hover:bg-surface-3 disabled:opacity-50"
                  disabled={downloadingId !== null}
                  onclick={() => download(d, f)}>{f.toUpperCase()}</button>
              {/each}
              <div class="my-1 border-t border-border"></div>
              {#each EXPORT_FORMATS as f (f)}
                <button
                  type="button"
                  class="block w-full rounded-control px-3 py-1.5 text-left text-xs text-muted hover:bg-surface-3 hover:text-fg"
                  onclick={() => queueBackground(d, f)}>Background {f.toUpperCase()}</button>
              {/each}
            </Popover>
            <Button
              size="sm"
              title="Clone this report and open the clone for editing — the original is untouched"
              disabled={busy[d.id]}
              onclick={() => copyReport(d)}>Copy</Button>
            <Button size="sm" onclick={() => editInBuilder(d)}>Edit in builder</Button>
            <Button size="sm" disabled={busy[d.id]} onclick={() => openEdit(d)}>Edit</Button>
            <Button size="sm" variant="danger" disabled={busy[d.id]} onclick={() => remove(d)}>Delete</Button>
          </div>
          {#if rowNote[d.id]}
            <Alert tone="ok" class="mt-2" onDismiss={() => (rowNote[d.id] = "")}>{rowNote[d.id]}</Alert>
          {/if}
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
