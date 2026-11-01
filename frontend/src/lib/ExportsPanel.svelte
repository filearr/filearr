<script lang="ts">
  import { onMount, onDestroy } from "svelte";
  import { usePolling, type Poller } from "./polling";
  import {
    listExports,
    downloadExport,
    type ReportExport,
    errorMessage,
  } from "./api";
  import { Alert, Badge, Button, DataTable, toneForStatus, type Tone } from "./ui";

  // P11-T5/T11 — background export jobs: a status list + download. Populated by
  // scheduled deliveries and by "Queue background export" from a report. Polls
  // while any export is still queued/running so a finished job flips to
  // downloadable without a manual refresh.
  let exports = $state<ReportExport[]>([]);
  // Errors by origin: the list load (DataTable error on first load, an Alert
  // above the kept rows on a later refresh) and a row's download.
  let error = $state("");
  let loaded = $state(false);
  let downloadError = $state<{ label: string; message: string } | null>(null);
  let loading = $state(false);
  let poller: Poller | undefined;

  const anyRunning = $derived(
    exports.some((e) => e.status === "queued" || e.status === "running"),
  );

  function fmtBytes(b: number | null): string {
    if (b == null || b <= 0) return "—";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }

  async function refresh() {
    loading = true;
    try {
      exports = await listExports();
      error = "";
      loaded = true;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
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
    return ex.canned_report_key ?? "custom report";
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
    // The anyRunning gate lives inside fn so it is read fresh on every tick
    // (and on return to the tab), not captured once at mount.
    poller = usePolling(() => {
      if (anyRunning) refresh();
    }, 4000);
  });
  onDestroy(() => poller?.stop());
</script>

{#snippet reportCell(ex: ReportExport)}
  {reportLabel(ex)}
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
{#snippet sizeCell(ex: ReportExport)}<span class="tabular-nums">{fmtBytes(ex.file_size_bytes)}</span>{/snippet}
{#snippet createdCell(ex: ReportExport)}
  <span class="text-xs text-muted">{ex.created_at ? new Date(ex.created_at).toLocaleString() : ""}</span>
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
