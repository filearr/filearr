<script lang="ts">
  import { fmtBytes } from "./format";
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import CustomReportsPage from "./CustomReportsPage.svelte";
  import ExportsPanel from "./ExportsPanel.svelte";
  import ReportSchedulesPanel from "./ReportSchedulesPanel.svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import FolderTreemap from "./FolderTreemap.svelte";
  import { encodeSearchHash } from "./searchparams";
  import { gotoBrowse } from "./routes";
  import {
    listReports,
    runReport,
    downloadReport,
    enqueueReportExport,
    listLibraries,
    EXPORT_FORMATS,
    type ExportFormat,
    type ReportMeta,
    type ReportPage,
    type ReportRunOpts,
    type Library,
    errorMessage,
  } from "./api";
  import { Alert, Button, Checkbox, DataTable, Field, Input, Popover, Select, StateBlock } from "./ui";

  // ------------------------------------------------------------------ //
  // P11 reporting v1 + polish — pick a canned report, run it (paginated  //
  // table), optionally filter by library, and export the full result as  //
  // CSV / NDJSON / XML. Rows are interactive per the report's row_link:   //
  // per-item rows open the ItemDetail modal (like a search hit); an       //
  // aggregate extension/hash row deep-links into a pre-filtered search.   //
  // ------------------------------------------------------------------ //

  let reports = $state<ReportMeta[]>([]);
  let libs = $state<Library[]>([]);
  let selected = $state<ReportMeta | null>(null);
  let library = $state("");
  let page = $state<ReportPage | null>(null);
  let offset = $state(0);
  // `listError`: the report registry load (in place of the list); `error`: a
  // run (in place of the result table); `dlError`: a download/queue failure,
  // under the Download menu.
  let listError = $state("");
  let error = $state("");
  let dlError = $state("");
  let loading = $state(false);
  let downloading = $state(false);
  let downloadOpen = $state(false);
  let showAllColumns = $state(false);
  let detailId = $state<string | null>(null); // open ItemDetail id
  let queued = $state(""); // background-export queued notice

  // IN-T3: `largest_folders` gains a treemap MODE. Deliberately a mode of this
  // page, not a separate page or a forked component tree — the report selector,
  // library filter and export dropdown all still apply, and the canned report
  // (and every export of it) is completely unchanged. The treemap drives its own
  // hierarchical endpoint because the flat top-N report cannot express nesting.
  const TREEMAP_REPORT = "largest_folders";
  let viewMode = $state<"table" | "treemap">("table");
  const treemapAvailable = $derived(selected?.id === TREEMAP_REPORT);
  const showTreemap = $derived(treemapAvailable && viewMode === "treemap");

  // IN-T2: one generic numeric parameter, rendered ONLY for reports whose meta
  // declares `supports_threshold`. No per-report special-casing here — a future
  // report that declares it gets the input for free.
  let thresholdDays = $state(0);
  const thresholdParam = $derived(
    selected?.supports_threshold && thresholdDays > 0 ? thresholdDays : undefined,
  );

  const pageSize = $derived(selected ? selected.default_limit : 100);

  // Bytes-ish columns get human formatting; everything else prints as text.
  const BYTE_COLUMNS = new Set(["size", "total_bytes", "wasted_bytes"]);
  // Wide path-context columns: present in every export, hidden in the table by
  // default behind a toggle (keep the table readable — library + rel_path).
  const WIDE_COLUMNS = new Set(["path", "native_path", "share_url"]);

  const visibleColumns = $derived(
    page
      ? page.columns.filter((c) => showAllColumns || !WIDE_COLUMNS.has(c))
      : [],
  );
  const hasWideColumns = $derived(
    !!page && page.columns.some((c) => WIDE_COLUMNS.has(c)),
  );

  function cell(row: Record<string, unknown>, col: string): string {
    const v = row[col];
    if (v == null) return "";
    if (BYTE_COLUMNS.has(col) && typeof v === "number") return fmtBytes(v, { zero: "0" });
    return String(v);
  }

  // The interactive affordance for a row, driven by the report's row_link.
  function rowActionLabel(): string | null {
    if (!selected) return null;
    switch (selected.row_link) {
      case "item":
        return "Open";
      case "search_ext":
        return "Find files";
      case "search_hash":
        return "Find copies";
      case "browse":
        return "Browse";
      default:
        return null;
    }
  }

  function rowActionDisabled(row: Record<string, unknown>): boolean {
    if (!selected) return true;
    if (selected.row_link === "item") return !row.item_id;
    if (selected.row_link === "search_ext") return !row.extension;
    if (selected.row_link === "search_hash")
      return !(row.content_hash || row.quick_hash);
    if (selected.row_link === "browse") return !(row.library_id && row.folder);
    return true;
  }

  function activateRow(row: Record<string, unknown>) {
    if (!selected) return;
    if (selected.row_link === "item") {
      if (row.item_id) detailId = String(row.item_id);
    } else if (selected.row_link === "search_ext") {
      const ext = String(row.extension ?? "");
      if (ext) location.hash = encodeSearchHash({ extension: ext });
    } else if (selected.row_link === "search_hash") {
      const h = String(row.content_hash ?? row.quick_hash ?? "");
      if (h) location.hash = encodeSearchHash({ hash: h });
    } else if (selected.row_link === "browse") {
      if (row.library_id && row.folder)
        gotoBrowse(String(row.library_id), String(row.folder));
    }
  }

  async function select(r: ReportMeta) {
    selected = r;
    offset = 0;
    showAllColumns = false;
    // Every report starts in the table: the treemap is an opt-in lens, and
    // landing in it would hide the columns the report actually documents.
    viewMode = "table";
    // Seed the threshold from the report's own declared default (730 days for
    // `stale_files`) so the first run matches what the description promises.
    thresholdDays = r.default_threshold_days ?? 0;
    if (!r.supports_library) library = "";
    // Another report's rows (and columns) must not linger dimmed while this
    // one runs; paging the same report keeps its rows on screen.
    page = null;
    // The previous report's download failure / queued notice belongs to it.
    dlError = "";
    queued = "";
    await load();
  }

  // Last-request-wins: switching report or paging aborts the older run, and a
  // superseded run leaves `loading` to the newer one.
  const reportLoader = latest((signal, id: string, opts: ReportRunOpts) =>
    runReport(id, opts, signal),
  );
  onDestroy(() => reportLoader.cancel());

  async function load() {
    if (!selected) return;
    error = "";
    loading = true;
    try {
      const r = await reportLoader.run(selected.id, {
        limit: pageSize,
        offset,
        libraryId: selected.supports_library ? library || undefined : undefined,
        thresholdDays: thresholdParam,
      });
      if (r === undefined) return;
      page = r;
      loading = false;
    } catch (e) {
      error = errorMessage(e);
      page = null;
      loading = false;
    }
  }

  async function nextPage() {
    if (!page?.has_more) return;
    offset += pageSize;
    await load();
  }
  async function prevPage() {
    if (offset <= 0) return;
    offset = Math.max(0, offset - pageSize);
    await load();
  }

  async function download(format: ExportFormat) {
    if (!selected) return;
    downloadOpen = false;
    dlError = "";
    downloading = true;
    try {
      await downloadReport(selected.id, format, {
        limit: pageSize,
        libraryId: selected.supports_library ? library || undefined : undefined,
        thresholdDays: thresholdParam,
      });
    } catch (e) {
      dlError = errorMessage(e);
    } finally {
      downloading = false;
    }
  }

  async function queueBackground(format: ExportFormat) {
    if (!selected) return;
    downloadOpen = false;
    queued = "";
    dlError = "";
    try {
      await enqueueReportExport(selected.id, format, {
        libraryId: selected.supports_library ? library || undefined : undefined,
        thresholdDays: thresholdParam,
      });
      queued = `Queued a background ${format.toUpperCase()} export — see the Exports panel below.`;
    } catch (e) {
      dlError = errorMessage(e);
    }
  }

  async function loadRegistry() {
    listError = "";
    try {
      [reports, libs] = await Promise.all([listReports(), listLibraries()]);
    } catch (e) {
      listError = errorMessage(e);
    }
  }
  onMount(loadRegistry);

  // DataTable columns for the current page: one per visible report column,
  // plus the row_link action when the report has one.
  const tableColumns = $derived([
    ...visibleColumns.map((c) => ({
      key: c,
      label: c,
      align: (BYTE_COLUMNS.has(c) ? "right" : "left") as "left" | "right",
    })),
    ...(rowActionLabel() ? [{ key: "__action", label: "Actions", align: "right" as const, cell: actionCell }] : []),
  ]);
  // Report rows carry no id; a row's index in the page is stable while shown.
  const rowIndex = (row: Record<string, unknown>) => page?.rows.indexOf(row) ?? -1;
</script>

{#snippet actionCell(row: Record<string, unknown>)}
  <Button size="sm" onclick={() => activateRow(row)} disabled={rowActionDisabled(row)}>
    {rowActionLabel()}
  </Button>
{/snippet}

<div class="grid grid-cols-1 gap-6 lg:grid-cols-[20rem_1fr]">
  <!-- registry list: report tiles are bespoke selectable surfaces -->
  <aside class="space-y-2">
    <h2 class="text-lg font-semibold">Reports</h2>
    {#if listError}
      <StateBlock state="error" message={listError} onRetry={loadRegistry} compact />
    {:else if reports.length === 0}
      <StateBlock state="loading" message="Loading reports…" compact />
    {/if}
    {#each reports as r (r.id)}
      <button
        type="button"
        class="w-full rounded-control border px-3 py-2 text-left text-sm transition
          {selected?.id === r.id
          ? 'border-accent bg-accent/10'
          : 'border-border hover:border-border-strong'}"
        aria-pressed={selected?.id === r.id}
        onclick={() => select(r)}>
        <div class="font-medium">{r.title}</div>
        <div class="mt-0.5 text-xs text-muted">{r.description}</div>
      </button>
    {/each}
  </aside>

  <!-- result panel -->
  <section class="min-w-0">
    {#if !selected}
      <StateBlock state="empty" message="Select a report to run it." />
    {:else}
      <div class="mb-3 flex flex-wrap items-end gap-3">
        <h3 class="text-base font-semibold">{selected.title}</h3>
        {#if selected.supports_library}
          <!-- Kit controls fill their parent: sizing wrapper. -->
          <div>
            <Select
              size="sm"
              aria-label="Filter by library"
              bind:value={library}
              onchange={() => { offset = 0; load(); }}>
              <option value="">All libraries</option>
              {#each libs as l (l.id)}
                <option value={l.id}>{l.name}</option>
              {/each}
            </Select>
          </div>
        {/if}
        {#if selected.supports_threshold}
          <!-- IN-T2 threshold. Rendered off the report's own metadata, with the
               report's own label ("Not modified in the last N days") — the page
               knows nothing about which report this is. -->
          <Field label={selected.threshold_label || "Threshold (days)"}>
            <div class="w-24">
              <Input
                type="number"
                size="sm"
                min="1"
                max="36500"
                step="1"
                bind:value={thresholdDays}
                onchange={() => { offset = 0; load(); }} />
            </div>
          </Field>
        {/if}
        {#if hasWideColumns && !showTreemap}
          <Checkbox size="sm" label="Show path columns" bind:checked={showAllColumns} />
        {/if}
        {#if treemapAvailable}
          <!-- IN-T3 table/treemap toggle. Same chip-group convention as the
               Search page's list/grid/map switch (segment chrome, not Buttons). -->
          <div
            class="flex items-center overflow-hidden rounded-control border border-border-strong"
            role="group"
            aria-label="Folder view">
            <button
              type="button"
              class="px-3 py-1 text-sm {viewMode === 'table' ? 'bg-accent text-accent-fg' : ''}"
              aria-pressed={viewMode === "table"}
              title="The canned report: a flat top-N of the largest folders across every depth"
              onclick={() => (viewMode = "table")}>Table</button>
            <button
              type="button"
              class="px-3 py-1 text-sm {viewMode === 'treemap' ? 'bg-accent text-accent-fg' : ''}"
              aria-pressed={viewMode === "treemap"}
              title="A drill-down map: one level at a time, cell area proportional to folder size"
              onclick={() => (viewMode = "treemap")}>Treemap</button>
          </div>
        {/if}
        <div class="grow"></div>
        <!-- Download menu: CSV / NDJSON / XML streaming exports, plus the same
             formats queued as background exports. -->
        <Popover bind:open={downloadOpen} ariaLabel="Download" align="end" class="w-40 p-1">
          {#snippet trigger({ props })}
            <Button size="sm" {...props} disabled={downloading}>
              {downloading ? "Preparing…" : "Download"}<span aria-hidden="true"> ▾</span>
            </Button>
          {/snippet}
          {#each EXPORT_FORMATS as f (f)}
            <button
              type="button"
              class="block w-full rounded-control px-3 py-1.5 text-left text-sm hover:bg-surface-3"
              onclick={() => download(f)}>
              {f.toUpperCase()}
            </button>
          {/each}
          <div class="my-1 border-t border-border"></div>
          {#each EXPORT_FORMATS as f (f)}
            <button
              type="button"
              class="block w-full rounded-control px-3 py-1.5 text-left text-xs text-muted hover:bg-surface-3 hover:text-fg"
              onclick={() => queueBackground(f)}>
              Background {f.toUpperCase()}
            </button>
          {/each}
        </Popover>
      </div>
      {#if dlError}
        <Alert tone="danger" class="mb-3" onDismiss={() => (dlError = "")}>{dlError}</Alert>
      {/if}
      {#if queued}
        <Alert tone="ok" class="mb-3" onDismiss={() => (queued = "")}>{queued}</Alert>
      {/if}

      {#if selected.id === "duplicate_files" || selected.id === "duplicate_files_detail"}
        <!-- The same amber caveat covers the per-COPY report (IN-T1): hash_tier
             rides every row there, and an operator about to feed the export to a
             delete script needs the sampled-vs-verified distinction in front of
             them, not only in the docs. Standing caveat: no live role. -->
        <Alert tone="warn" class="mb-3" live={false}>
          <p class="text-xs">
            A <code>quick_hash</code> tier is a <strong>sampled signal, not
            byte-verified</strong> — groups keyed on a partial (head+tail) hash may
            include files that merely share those windows. A <code>content_hash</code>
            tier is a full-hash-confirmed exact duplicate. Zero-byte files are
            excluded.
            {#if selected.id === "duplicate_files_detail"}
              <strong>keep_hint</strong> is DATA, not a decision: rank 0 is simply the
              newest copy in its group. Filearr never touches your files — export the
              rows and let your own script act.
            {/if}
          </p>
        </Alert>
      {/if}

      {#if showTreemap}
        <!-- Treemap mode replaces the table only; the report selector, library
             filter and exports above stay exactly as they were. -->
        <FolderTreemap libraryId={selected.supports_library ? library : ""} />
      {:else}
        <DataTable
          caption="{selected.title} results"
          captionHidden
          columns={tableColumns}
          rows={page?.rows ?? []}
          rowKey={rowIndex}
          accessor={(row, k) => cell(row, k)}
          {loading}
          error={error || null}
          onRetry={load}
          emptyMessage="No rows. Change the library filter or threshold, or pick another report." />

        {#if page}
          <div class="mt-3 flex items-center gap-3 text-sm">
            <Button size="sm" onclick={prevPage} disabled={offset <= 0 || loading}>Previous</Button>
            <span class="text-muted">
              rows {page.count === 0 ? 0 : offset + 1}–{offset + page.count}
            </span>
            <Button size="sm" onclick={nextPage} disabled={!page.has_more || loading}>Next</Button>
          </div>
        {/if}
      {/if}
    {/if}
  </section>
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} onOpen={(nid) => (detailId = nid)} />
{/if}

<CustomReportsPage />

<ReportSchedulesPanel />

<ExportsPanel />
