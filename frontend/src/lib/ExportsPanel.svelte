<script lang="ts">
  import { fmtBytes, fmtDateTime, fmtUtc } from "./format";
  import { onMount, onDestroy } from "svelte";
  import { usePolling, type Poller } from "./polling";
  import { latest } from "./latest";
  import {
    listExports,
    listCustomReports,
    downloadExport,
    type ReportExport,
    type ReportMeta,
    errorMessage,
  } from "./api";
  import { Alert, Badge, Button, DataTable, toneForStatus, type Tone } from "./ui";

  // P11-T5/T11 — background export jobs: a status list + download. Populated by
  // scheduled deliveries and by "Background …" from a canned or custom report.
  // B21: the host calls `refresh()` right after queueing one (bind:this), and
  // the list polls fast while anything is queued/running, slowly when idle so
  // scheduled and other sessions' exports still show up.
  let { reports = [] }: { reports?: ReportMeta[] } = $props();

  let exports = $state<ReportExport[]>([]);
  // Custom report names by id, for the Report column (B53); refetched only
  // when an export names a report this panel has not seen.
  let customNames = $state<Record<string, string>>({});
  // Errors by origin: the list load (DataTable error on first load, an Alert
  // above the kept rows on a later refresh) and a row's download.
  let error = $state("");
  let loaded = $state(false);
  let downloadError = $state<{ label: string; message: string } | null>(null);
  let loading = $state(false);
  let poller: Poller | undefined;
  let lastRefresh = 0;

  const FAST_MS = 4000;
  const IDLE_MS = 60_000;

  const anyRunning = $derived(
    exports.some((e) => e.status === "queued" || e.status === "running"),
  );
  const cannedTitles = $derived(new Map(reports.map((r) => [r.id, r.title])));

  // Last-request-wins: a refresh right after a queue must not be overwritten by
  // an older poll that answers later.
  const loader = latest((signal) => listExports(signal));

  /** Reload the list now. Exported for the host page (bind:this). */
  export async function refresh() {
    loading = true;
    lastRefresh = Date.now();
    try {
      const r = await loader.run();
      if (r === undefined) return;
      exports = r;
      error = "";
      loaded = true;
      loading = false;
      void loadCustomNames(r);
    } catch (e) {
      error = errorMessage(e);
      loading = false;
    }
  }

  // Every definition id already looked up, found or not: an export of a
  // deleted custom report must not re-fetch the whole list on every refresh.
  const lookedUp = new Set<string>();
  async function loadCustomNames(list: ReportExport[]) {
    const ids = list.map((e) => e.report_definition_id).filter((id): id is string => !!id);
    if (!ids.some((id) => !lookedUp.has(id))) return;
    try {
      const defs = await listCustomReports();
      customNames = Object.fromEntries(defs.map((d) => [d.id, d.name]));
      // Missing ids (deleted reports) count as looked up too; a failed fetch
      // marks nothing, so the next refresh retries.
      for (const id of ids) lookedUp.add(id);
      for (const d of defs) lookedUp.add(d.id);
    } catch {
      /* names are a nicety: the row falls back to "Custom report" */
    }
  }

  async function download(ex: ReportExport) {
    downloadError = null;
    try {
      await downloadExport(ex);
    } catch (e) {
      downloadError = {
        label: reportLabel(ex),
        message: errorMessage(e, { verb: "download this export" }),
      };
    }
  }

  function reportLabel(ex: ReportExport): string {
    if (ex.canned_report_key) return cannedTitles.get(ex.canned_report_key) ?? ex.canned_report_key;
    const name = ex.report_definition_id ? customNames[ex.report_definition_id] : undefined;
    return name ?? "Custom report";
  }

  // "complete" is this endpoint's success word (the kit vocabulary has
  // "completed"); everything else goes through the shared status map.
  function statusTone(status: string): Tone {
    // A queued export is waiting its turn, not a problem: neutral, not warn.
    if (status === "queued") return "neutral";
    return status === "complete" ? "ok" : toneForStatus(status);
  }

  onMount(() => {
    refresh();
    // One fast ticker; the gate is read fresh on every tick (and on return to
    // the tab): every tick while something runs, else once a minute.
    poller = usePolling(
      () => {
        if (anyRunning || Date.now() - lastRefresh >= IDLE_MS) return refresh();
      },
      FAST_MS,
      { refreshOnReturn: true },
    );
  });
  onDestroy(() => {
    poller?.stop();
    loader.cancel();
  });
</script>

{#snippet reportCell(ex: ReportExport)}
  {reportLabel(ex)}
  {#if !ex.canned_report_key}
    <span class="ml-1 text-xs text-muted">(custom)</span>
  {/if}
  {#if ex.triggered_by === "schedule"}
    <span class="ml-1 text-xs text-muted">(scheduled)</span>
  {/if}
{/snippet}
{#snippet formatCell(ex: ReportExport)}<span class="uppercase">{ex.format}</span>{/snippet}
{#snippet statusCell(ex: ReportExport)}
  <Badge tone={statusTone(ex.status)}>{ex.status}</Badge>
  {#if ex.delivery_status}
    <span class="ml-1 text-xs text-muted">· {ex.delivery_status}</span>
  {/if}
  {#if ex.status === "failed" && ex.error}
    <!-- Was a hover-only ⓘ; the message is shown so touch and keyboard users get it too. -->
    <span class="mt-0.5 block max-w-[24rem] break-words text-xs text-danger">{ex.error}</span>
  {/if}
{/snippet}
{#snippet rowsCell(ex: ReportExport)}<span class="tabular-nums">{ex.row_count ?? "—"}</span>{/snippet}
{#snippet sizeCell(ex: ReportExport)}<span class="tabular-nums">{fmtBytes(ex.file_size_bytes, { zero: "—" })}</span>{/snippet}
{#snippet createdCell(ex: ReportExport)}
  <span class="text-xs text-muted" title={fmtUtc(ex.created_at)}>{fmtDateTime(ex.created_at)}</span>
{/snippet}
{#snippet actionsCell(ex: ReportExport)}
  <Button size="sm" onclick={() => download(ex)} disabled={!ex.downloadable}>
    {ex.purged_at ? "Expired" : "Download"}
  </Button>
{/snippet}

<section class="mt-8">
  <div class="mb-3 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Exports</h2>
    <Button size="sm" onclick={refresh} disabled={loading}>Refresh</Button>
  </div>

  {#if loaded && error}
    <!-- A failed refresh keeps the rows already shown; the first load's
         failure replaces the table instead (DataTable error + Retry). -->
    <Alert tone="danger" class="mb-3" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  <DataTable
    caption="Background exports"
    captionHidden
    columns={[
      { key: "report", label: "Report", cell: reportCell },
      { key: "format", label: "Format", cell: formatCell },
      { key: "status", label: "Status", cell: statusCell },
      { key: "row_count", label: "Rows", align: "right", cell: rowsCell },
      { key: "file_size_bytes", label: "Size", align: "right", cell: sizeCell },
      { key: "created_at", label: "Created", cell: createdCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={exports}
    rowKey={(ex) => ex.id}
    loading={!loaded}
    error={loaded ? null : error || null}
    onRetry={refresh}
    emptyMessage="No background exports yet. Queue one from a report, or set up a schedule."
  />
  {#if downloadError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (downloadError = null)}>
      {downloadError.label}: {downloadError.message}
    </Alert>
  {/if}
</section>
