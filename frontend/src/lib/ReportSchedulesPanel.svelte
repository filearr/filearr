<script lang="ts">
  import { onMount } from "svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import { nextRuns } from "./scheduleNext";
  import { describeSchedule, parseCron } from "./schedule";
  import { fmtDateTime } from "./format";
  import { editedScheduleParams, reportScheduleParams } from "./reportQuery";
  import { focusPanel, focusRowButton } from "./rowPanel";
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
    type Library,
    errorMessage,
  } from "./api";
  import { Alert, Button, DataTable, Field, Input, Select, confirm, type SortState } from "./ui";

  // P11-T9 — schedule manager: list/create/edit/toggle/delete scheduled report
  // deliveries. The cron is authored with the shared friendly ScheduleField
  // (Off/Hourly/Daily/…/Advanced), the source is a canned OR custom report, and
  // delivery rides a Phase-8 alert channel.
  //
  // B30: `current` is the report open on the Reports page with its on-screen
  // filter; a schedule of that report stores the same params (library,
  // threshold), so the scheduled file matches what the page shows. Edit
  // changes name, params, format, cron and channel (backend PATCH).
  type Current = { meta: ReportMeta; libraryId: string; thresholdDays: number | null };
  let {
    current = null,
    libraries = [],
  }: { current?: Current | null; libraries?: Library[] } = $props();

  let schedules = $state<ReportSchedule[]>([]);
  let canned = $state<ReportMeta[]>([]);
  let custom = $state<ReportDefinition[]>([]);
  let channels = $state<AlertChannel[]>([]);
  // `error` is the schedule list load; `lookupNote` says a side list (canned
  // or custom reports, channels) failed — B53: each list loads on its own, so
  // one failing call never blanks the schedules. `createError` sits under
  // Create; `rowError` holds a toggle/delete failure per schedule.
  let error = $state("");
  let lookupNote = $state("");
  let loading = $state(true);
  let loaded = $state(false);
  let createError = $state("");
  let rowError = $state<Record<string, string>>({});
  let busy = $state<Record<string, boolean>>({});
  let creating = $state(false);
  let sort = $state<SortState>(null);

  // create form
  let name = $state("");
  let source = $state(""); // "canned:<id>" | "custom:<id>"
  let format = $state<ExportFormat>("csv");
  let cron = $state<string | null>("0 6 * * *");
  // ScheduleField's field error (weekly with no day, a day-shifting monthly
  // time, …): the field emits a null cron for those and says why under it.
  let scheduleError = $state<string | null>(null);
  let channelId = $state("");

  // edit form (one schedule at a time, below the table)
  let editingId = $state<string | null>(null);
  let eName = $state("");
  let eFormat = $state<ExportFormat>("csv");
  let eCron = $state<string | null>(null);
  let eScheduleError = $state<string | null>(null);
  let eChannel = $state("");
  let eLibrary = $state("");
  let eThreshold = $state<number | null>(null);
  let saving = $state(false);
  let editError = $state("");

  const editing = $derived(schedules.find((s) => s.id === editingId) ?? null);
  const editMeta = $derived(editing?.canned_report_key ? cannedMeta(editing.canned_report_key) : undefined);
  const editPanelId = "report-schedule-editor";

  const currentSource = $derived(current ? `canned:${current.meta.id}` : "");
  const carriesCurrent = $derived(!!current && source === currentSource);

  function cannedMeta(key: string): ReportMeta | undefined {
    return canned.find((r) => r.id === key);
  }

  async function refreshSchedules() {
    try {
      schedules = await listReportSchedules();
      error = "";
      loaded = true;
    } catch (e) {
      error = errorMessage(e, { verb: "view report schedules" });
    } finally {
      loading = false;
    }
  }

  async function loadLookups() {
    const [c, u, ch] = await Promise.allSettled([listReports(), listCustomReports(), listAlertChannels()]);
    const failed: string[] = [];
    if (c.status === "fulfilled") canned = c.value;
    else failed.push("reports");
    if (u.status === "fulfilled") custom = u.value;
    else failed.push("custom reports");
    if (ch.status === "fulfilled") channels = ch.value;
    else failed.push("alert channels");
    lookupNote = failed.length ? `Couldn't load ${failed.join(", ")}; those names and choices are missing.` : "";
  }

  function libraryName(id: unknown): string {
    if (!id) return "all libraries";
    return libraries.find((l) => l.id === id)?.name ?? "one library";
  }

  function sourceLabel(s: ReportSchedule): string {
    if (s.canned_report_key) return cannedMeta(s.canned_report_key)?.title ?? s.canned_report_key;
    const c = custom.find((x) => x.id === s.report_definition_id);
    return c ? c.name : "Custom report";
  }

  /** The stored params a person cares about, as one short line. */
  function paramsLabel(p: Record<string, unknown>): string {
    const bits: string[] = [];
    if (p.library_id) bits.push(libraryName(p.library_id));
    if (p.threshold_days != null) bits.push(`${p.threshold_days} days`);
    return bits.join(" · ");
  }

  function channelLabel(id: string | null): string {
    if (!id) return "no channel";
    const c = channels.find((x) => x.id === id);
    return c ? c.name : "unknown channel";
  }

  function scheduleText(c: string): string {
    return describeSchedule(parseCron(c));
  }

  function paramsFor(src: string): Record<string, unknown> {
    const [kind, id] = src.split(":");
    if (kind !== "canned") return {};
    if (current && current.meta.id === id) return reportScheduleParams(current.meta, current);
    const meta = cannedMeta(id);
    return meta ? reportScheduleParams(meta, {}) : {};
  }

  async function create() {
    if (creating) return;
    if (scheduleError) {
      createError = "The schedule needs attention (see the message under it).";
      return;
    }
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
        params: paramsFor(source),
        format,
        cron,
        channel_id: channelId || null,
        enabled: true,
      });
      name = "";
      source = "";
      channelId = "";
      await refreshSchedules();
    } catch (e) {
      createError = errorMessage(e, { verb: "manage report schedules" });
    } finally {
      creating = false;
    }
  }

  function openEdit(s: ReportSchedule) {
    editingId = s.id;
    eName = s.name;
    eFormat = s.format;
    eCron = s.cron;
    eScheduleError = null;
    eChannel = s.channel_id ?? "";
    eLibrary = typeof s.params.library_id === "string" ? s.params.library_id : "";
    // Seeded from the stored value only; blank = the report's default (shown
    // as the placeholder), never pinned on save unless the operator types one.
    eThreshold = typeof s.params.threshold_days === "number" ? s.params.threshold_days : null;
    editError = "";
    void focusPanel(editPanelId);
  }

  function closeEdit() {
    const id = editingId;
    editingId = null;
    if (id) void focusRowButton("data-edit-for", id);
  }

  async function saveEdit() {
    const s = editing;
    if (!s || saving) return;
    if (eScheduleError) {
      editError = "The schedule needs attention (see the message under it).";
      return;
    }
    if (!eName.trim() || !eCron) {
      editError = "Name and a schedule are required.";
      return;
    }
    const patch: Parameters<typeof updateReportSchedule>[1] = {
      name: eName.trim(),
      format: eFormat,
      cron: eCron,
      channel_id: eChannel || null,
    };
    if (editMeta) {
      // Stored params with only this form's fields applied: the stored `limit`
      // and any other key survive the edit.
      const p = editedScheduleParams(s.params, editMeta, { libraryId: eLibrary, thresholdDays: eThreshold });
      if (!p.ok) {
        editError = p.error;
        return;
      }
      patch.params = p.params;
    }
    saving = true;
    editError = "";
    try {
      await updateReportSchedule(s.id, patch);
      await refreshSchedules();
      closeEdit();
    } catch (e) {
      editError = errorMessage(e, { verb: "manage report schedules" });
    } finally {
      saving = false;
    }
  }

  async function toggle(s: ReportSchedule) {
    if (busy[s.id]) return;
    busy[s.id] = true;
    rowError[s.id] = "";
    try {
      await updateReportSchedule(s.id, { enabled: !s.enabled });
      await refreshSchedules();
    } catch (e) {
      rowError[s.id] = errorMessage(e, { verb: "manage report schedules" });
    } finally {
      busy[s.id] = false;
    }
  }

  async function remove(s: ReportSchedule) {
    if (busy[s.id]) return;
    const ok = await confirm({
      title: "Delete schedule?",
      message: `Delete schedule "${s.name}"?`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    busy[s.id] = true;
    rowError[s.id] = "";
    try {
      await deleteReportSchedule(s.id);
      if (editingId === s.id) editingId = null;
      await refreshSchedules();
    } catch (e) {
      rowError[s.id] = errorMessage(e, { verb: "manage report schedules" });
    } finally {
      busy[s.id] = false;
    }
  }

  // Sort keys for the computed columns read the same labels the cells show.
  function sortValue(s: ReportSchedule, key: string): unknown {
    if (key === "report") return sourceLabel(s);
    if (key === "channel") return channelLabel(s.channel_id);
    return (s as unknown as Record<string, unknown>)[key];
  }

  onMount(() => {
    void refreshSchedules();
    void loadLookups();
  });
</script>

{#snippet reportCell(s: ReportSchedule)}
  {@const extra = paramsLabel(s.params)}
  {sourceLabel(s)}
  {#if extra}<span class="block text-xs text-muted">{extra}</span>{/if}
{/snippet}
{#snippet formatCell(s: ReportSchedule)}<span class="uppercase">{s.format}</span>{/snippet}
{#snippet cronCell(s: ReportSchedule)}
  {@const next = s.enabled ? nextRuns(s.cron, 1, new Date())[0] : undefined}
  <span title="Stored cron (UTC): {s.cron}">{scheduleText(s.cron)}</span>
  {#if next}<span class="block text-xs text-muted">next {fmtDateTime(next)}</span>{/if}
{/snippet}
{#snippet enabledCell(s: ReportSchedule)}
  <Button
    size="sm"
    onclick={() => toggle(s)}
    disabled={busy[s.id]}
    title={s.enabled ? "Click to disable" : "Click to enable"}>
    {s.enabled ? "On" : "Off"}
  </Button>
{/snippet}
{#snippet actionsCell(s: ReportSchedule)}
  <div class="flex justify-end gap-2">
    <Button
      size="sm"
      onclick={() => (editingId === s.id ? closeEdit() : openEdit(s))}
      aria-expanded={editingId === s.id}
      aria-controls={editingId === s.id ? editPanelId : undefined}
      data-edit-for={s.id}
      aria-label="Edit {s.name}">Edit</Button>
    <Button size="sm" variant="danger" onclick={() => remove(s)} disabled={busy[s.id]} aria-label="Delete {s.name}">Delete</Button>
  </div>
{/snippet}

<section class="mt-8">
  <h2 class="mb-3 text-lg font-semibold">Scheduled reports</h2>

  {#if error && loaded}
    <!-- A failed refresh after a change keeps the last list on screen. -->
    <Alert tone="danger" class="mb-3" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}
  {#if lookupNote}
    <Alert tone="warn" class="mb-3" live={false} onDismiss={() => (lookupNote = "")}>{lookupNote}</Alert>
  {/if}

  <!-- existing schedules -->
  <DataTable
    class="mb-4"
    caption="Scheduled reports"
    captionHidden
    columns={[
      { key: "name", label: "Name", sortable: true },
      { key: "report", label: "Report", sortable: true, cell: reportCell },
      { key: "format", label: "Format", sortable: true, cell: formatCell },
      { key: "cron", label: "Schedule", cell: cronCell },
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
    onRetry={refreshSchedules}
    emptyMessage="No scheduled reports yet. Create one below to deliver a report on a cron schedule."
  />
  {#each schedules as s (s.id)}
    {#if rowError[s.id]}
      <Alert tone="danger" class="mb-2" onDismiss={() => (rowError[s.id] = "")}>{s.name}: {rowError[s.id]}</Alert>
    {/if}
  {/each}

  {#if editing}
    <div
      id={editPanelId}
      tabindex="-1"
      aria-labelledby="{editPanelId}-heading"
      class="mb-4 space-y-3 rounded-panel border border-border-strong p-4 focus:outline-none">
      <h3 id="{editPanelId}-heading" class="text-sm font-semibold">Edit schedule: {editing.name}</h3>
      <p class="text-xs text-muted">Report: {sourceLabel(editing)}</p>
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <Field label="Name" required>
          <Input bind:value={eName} />
        </Field>
        <Field label="Format">
          <Select bind:value={eFormat}>
            {#each EXPORT_FORMATS as f (f)}
              <option value={f}>{f.toUpperCase()}</option>
            {/each}
          </Select>
        </Field>
        <Field label="Deliver to channel">
          <Select bind:value={eChannel}>
            <option value="">No channel (generate only)</option>
            {#each channels as c (c.id)}
              <option value={c.id}>{c.name} ({c.type})</option>
            {/each}
          </Select>
        </Field>
        {#if editMeta?.supports_library}
          <Field label="Library">
            <Select bind:value={eLibrary}>
              <option value="">All libraries</option>
              {#each libraries as l (l.id)}
                <option value={l.id}>{l.name}</option>
              {/each}
              {#if eLibrary && !libraries.some((l) => l.id === eLibrary)}
                <option value={eLibrary}>Current library (not listed)</option>
              {/if}
            </Select>
          </Field>
        {/if}
        {#if editMeta?.supports_threshold}
          <Field label={editMeta.threshold_label || "Threshold (days)"}>
            <Input type="number" min="1" max="36500" step="1" bind:value={eThreshold}
              placeholder="{editMeta.default_threshold_days} (default)" />
          </Field>
        {/if}
      </div>
      <div class="text-sm">
        <span class="text-xs font-medium text-fg">Schedule</span>
        {#key editing.id}
          <ScheduleField value={eCron} onChange={(c) => (eCron = c)} onValidity={(e) => (eScheduleError = e)} />
        {/key}
      </div>
      <div class="flex items-center gap-2">
        <Button variant="primary" onclick={saveEdit} disabled={saving}>
          {saving ? "Saving…" : "Save changes"}
        </Button>
        <Button onclick={closeEdit}>Cancel</Button>
      </div>
      {#if editError}
        <Alert tone="danger" onDismiss={() => (editError = "")}>{editError}</Alert>
      {/if}
    </div>
  {/if}

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
        {#if current && carriesCurrent}
          {@const p = paramsLabel(reportScheduleParams(current.meta, current))}
          <p class="mt-1 text-xs text-muted">
            Uses the filter shown above{p ? `: ${p}` : " (report defaults)"}.
          </p>
        {:else if current}
          <Button size="sm" variant="ghost" class="mt-1" onclick={() => (source = currentSource)}>
            Use “{current.meta.title}” with its current filter
          </Button>
        {/if}
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
      <ScheduleField value={cron} onChange={(c) => (cron = c)} onValidity={(e) => (scheduleError = e)} />
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
