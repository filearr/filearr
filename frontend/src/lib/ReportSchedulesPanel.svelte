<script lang="ts">
  import { onMount } from "svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import {
    listReportSchedules,
    createReportSchedule,
    updateReportSchedule,
    deleteReportSchedule,
    listReports,
    listCustomReports,
    listAlertChannels,
    EXPORT_FORMATS,
    type ReportSchedule,
    type ReportMeta,
    type ReportDefinition,
    type AlertChannel,
    type ExportFormat,
    errorMessage,
  } from "./api";
  import { Alert, Button, DataTable, Field, Input, Select, confirm, type SortState } from "./ui";

  // P11-T9 — schedule manager: list/create/toggle/delete scheduled report
  // deliveries. The cron is authored with the shared friendly ScheduleField
  // (Off/Hourly/Daily/…/Advanced), the source is a canned OR custom report, and
  // delivery rides a Phase-8 alert channel.
  let schedules = $state<ReportSchedule[]>([]);
  let canned = $state<ReportMeta[]>([]);
  let custom = $state<ReportDefinition[]>([]);
  let channels = $state<AlertChannel[]>([]);
  // `error` is the list load; `createError` sits under Create; `rowError`
  // holds a toggle/delete failure per schedule, shown under the table.
  let error = $state("");
  let loading = $state(true);
  let loaded = $state(false);
  let createError = $state("");
  let rowError = $state<Record<string, string>>({});
  let creating = $state(false);
  let sort = $state<SortState>(null);

  // create form
  let name = $state("");
  let source = $state(""); // "canned:<id>" | "custom:<id>"
  let format = $state<ExportFormat>("csv");
  let cron = $state<string | null>("0 6 * * *");
  let channelId = $state("");

  async function refresh() {
    try {
      [schedules, canned, custom, channels] = await Promise.all([
        listReportSchedules(),
        listReports(),
        listCustomReports(),
        listAlertChannels().catch(() => [] as AlertChannel[]),
      ]);
      error = "";
      loaded = true;
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
    }
  }

  function sourceLabel(s: ReportSchedule): string {
    if (s.canned_report_key) return s.canned_report_key;
    const c = custom.find((x) => x.id === s.report_definition_id);
    return c ? c.name : "custom report";
  }

  function channelLabel(id: string | null): string {
    if (!id) return "no channel";
    const c = channels.find((x) => x.id === id);
    return c ? c.name : "unknown channel";
  }

  async function create() {
    if (!name.trim() || !source || !cron) {
      createError = "Name, report, and a schedule are required.";
      return;
    }
    creating = true;
    createError = "";
    try {
      const [kind, id] = source.split(":");
      await createReportSchedule({
        name: name.trim(),
        canned_report_key: kind === "canned" ? id : null,
        report_definition_id: kind === "custom" ? id : null,
        format,
        cron,
        channel_id: channelId || null,
        enabled: true,
      });
      name = "";
      source = "";
      channelId = "";
      await refresh();
    } catch (e) {
      createError = errorMessage(e, { verb: "manage report schedules" });
    } finally {
      creating = false;
    }
  }

  async function toggle(s: ReportSchedule) {
    rowError[s.id] = "";
    try {
      await updateReportSchedule(s.id, { enabled: !s.enabled });
      await refresh();
    } catch (e) {
      rowError[s.id] = errorMessage(e, { verb: "manage report schedules" });
    }
  }

  async function remove(s: ReportSchedule) {
    const ok = await confirm({
      title: "Delete schedule?",
      message: `Delete schedule "${s.name}"?`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    rowError[s.id] = "";
    try {
      await deleteReportSchedule(s.id);
      await refresh();
    } catch (e) {
      rowError[s.id] = errorMessage(e, { verb: "manage report schedules" });
    }
  }

  // Sort keys for the computed columns read the same labels the cells show.
  function sortValue(s: ReportSchedule, key: string): unknown {
    if (key === "report") return sourceLabel(s);
    if (key === "channel") return channelLabel(s.channel_id);
    return (s as unknown as Record<string, unknown>)[key];
  }

  onMount(refresh);
</script>

{#snippet formatCell(s: ReportSchedule)}<span class="uppercase">{s.format}</span>{/snippet}
{#snippet cronCell(s: ReportSchedule)}<span class="font-mono text-xs">{s.cron}</span>{/snippet}
{#snippet enabledCell(s: ReportSchedule)}
  <Button size="sm" onclick={() => toggle(s)} title={s.enabled ? "Click to disable" : "Click to enable"}>
    {s.enabled ? "On" : "Off"}
  </Button>
{/snippet}
{#snippet actionsCell(s: ReportSchedule)}
  <Button size="sm" variant="danger" onclick={() => remove(s)} aria-label="Delete {s.name}">Delete</Button>
{/snippet}

<section class="mt-8">
  <h2 class="mb-3 text-lg font-semibold">Scheduled reports</h2>

  {#if error && loaded}
    <!-- A failed refresh after a change keeps the last list on screen. -->
    <Alert tone="danger" class="mb-3" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  <!-- existing schedules -->
  <DataTable
    class="mb-4"
    caption="Scheduled reports"
    captionHidden
    columns={[
      { key: "name", label: "Name", sortable: true },
      { key: "report", label: "Report", sortable: true },
      { key: "format", label: "Format", sortable: true, cell: formatCell },
      { key: "cron", label: "Cron (UTC)", cell: cronCell },
      { key: "channel", label: "Channel", sortable: true },
      { key: "enabled", label: "Enabled", sortable: true, cell: enabledCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={schedules}
    rowKey={(s) => s.id}
    accessor={sortValue}
    bind:sort
    {loading}
    error={loaded ? null : error || null}
    onRetry={refresh}
    emptyMessage="No scheduled reports yet. Create one below to deliver a report on a cron schedule."
  />
  {#each schedules as s (s.id)}
    {#if rowError[s.id]}
      <Alert tone="danger" class="mb-2" onDismiss={() => (rowError[s.id] = "")}>{s.name}: {rowError[s.id]}</Alert>
    {/if}
  {/each}

  <!-- create form -->
  <div class="space-y-3 rounded-panel border border-border p-4">
    <h3 class="text-sm font-semibold">New schedule</h3>
    <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
      <Field label="Name" required>
        <Input bind:value={name} placeholder="Weekly largest files" />
      </Field>
      <Field label="Report" required>
        <Select bind:value={source}>
          <option value="">Select a report…</option>
          <optgroup label="Canned">
            {#each canned as r (r.id)}
              <option value={"canned:" + r.id}>{r.title}</option>
            {/each}
          </optgroup>
          {#if custom.length > 0}
            <optgroup label="Custom">
              {#each custom as c (c.id)}
                <option value={"custom:" + c.id}>{c.name}</option>
              {/each}
            </optgroup>
          {/if}
        </Select>
      </Field>
      <Field label="Format">
        <Select bind:value={format}>
          {#each EXPORT_FORMATS as f (f)}
            <option value={f}>{f.toUpperCase()}</option>
          {/each}
        </Select>
      </Field>
      <Field label="Deliver to channel">
        <Select bind:value={channelId}>
          <option value="">No channel (generate only)</option>
          {#each channels as c (c.id)}
            <option value={c.id}>{c.name} ({c.type})</option>
          {/each}
        </Select>
      </Field>
    </div>
    <div class="text-sm">
      <span class="text-xs font-medium text-fg">Schedule</span>
      <ScheduleField value={cron} onChange={(c) => (cron = c)} />
    </div>
    <div>
      <Button variant="primary" onclick={create} disabled={creating}>
        {creating ? "Creating…" : "Create schedule"}
      </Button>
      {#if createError}
        <Alert tone="danger" class="mt-2" onDismiss={() => (createError = "")}>{createError}</Alert>
      {/if}
    </div>
  </div>
</section>
