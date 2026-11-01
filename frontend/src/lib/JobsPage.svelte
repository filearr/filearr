<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { usePolling, type Poller } from "./polling";
  import { latest } from "./latest";
  import {
    clearFailedJobs,
    downloadBackup,
    failedJobs,
    jobsSummary,
    listBackups,
    listMaintenance,
    reapStalledJobs,
    runBackup,
    runMaintenance,
    setJobPriority,
    setMaintenanceMode,
    updateMaintenance,
    type BackupBundle,
    type FailedJob,
    type JobsSummary,
    type MaintenanceTask,
    type ReapResult,
    type RunningJob,
    type ScanRunning,
    type AgentReplicationActivity,
    errorMessage,
  } from "./api";
  import FeaturesPanel from "./FeaturesPanel.svelte";
  import LogsPanel from "./LogsPanel.svelte";
  import UpdatesPanel from "./UpdatesPanel.svelte";
  import { help } from "./help";
  import { focusPanel, focusRowButton } from "./rowPanel";
  import { Alert, Badge, Button, DataTable, Field, Input, StateBlock, confirm, promptText, type Tone } from "./ui";

  // UI-T10 — live job-system dashboard. Polls ONE composite endpoint
  // (`/system/jobs/summary`) every 4s, but ONLY while this tab is actually
  // visible (document.visibilityState) so a backgrounded tab never hammers the
  // API. All strings that originate from the filesystem (rel_path, task names)
  // render as text — Svelte auto-escapes, so a crafted path cannot inject markup.

  const POLL_MS = 4000;
  // Fixed display order for the queue cards (matches the worker's queue names).
  // `thumbs` sits after `index` (the thumbnail-creation monitor). Any OTHER queue
  // the backend reports (embed / alerts / exports / a future queue) is appended
  // dynamically by `displayQueues` so no queue silently disappears from the page.
  const QUEUES = ["scan", "extract", "index", "thumbs", "maintenance"] as const;

  let summary = $state<JobsSummary | null>(null);

  // Union of the fixed order + any extra queue present in the summary (alphabetical),
  // so embed/alerts/exports and future queues always render a card.
  let displayQueues = $derived.by<string[]>(() => {
    const fixed = QUEUES as readonly string[];
    const present = summary?.queues ? Object.keys(summary.queues) : [];
    const extras = present.filter((q) => !fixed.includes(q)).sort();
    return [...fixed, ...extras];
  });
  let error = $state("");
  let loading = $state(false);
  let lastUpdated = $state<number | null>(null);

  // FIX-8 — paginated failed-jobs list + manual clear. The failures table used
  // to render the fixed 10 rows the summary embeds (`failed_recent`); it now
  // pages the real `/system/failed-jobs` endpoint so it can never grow unbounded
  // on screen and an operator can clear the accumulated history.
  const FAILED_PAGE = 25;
  let failures = $state<FailedJob[]>([]);
  let failuresTotal = $state(0);
  let failuresOffset = $state(0);
  let clearingFailed = $state(false);
  // Action errors by origin, each rendered as an Alert under its control.
  let clearFailedError = $state("");
  let failuresError = $state("");

  // FIX-6 — stalled-job reaper controls. `reaping` guards the button; `reapMsg`
  // shows the last run's counts for a few seconds.
  let reaping = $state(false);
  let reapMsg = $state("");
  let reapError = $state("");

  // UI-T14 — per-queue priority stepper. `prioInput` holds the pending edit per
  // queue (seeded from the server default the first time a queue is seen);
  // `prioBusy`/`prioMsg` drive the per-queue apply button + its result note.
  let prioInput = $state<Record<string, number>>({});
  let prioBusy = $state<Record<string, boolean>>({});
  let prioMsg = $state<Record<string, string>>({});
  let prioErr = $state<Record<string, string>>({});

  // Effective input value for a queue: the user's pending edit, else the current
  // server default, else 0.
  function prioValue(q: string): number {
    if (prioInput[q] !== undefined) return prioInput[q];
    return summary?.priorities?.[q] ?? 0;
  }

  function clampPrio(n: number): number {
    if (!isFinite(n)) return 0;
    return Math.max(-100, Math.min(100, Math.round(n)));
  }

  async function applyPriority(q: string) {
    if (prioBusy[q]) return;
    const priority = clampPrio(prioValue(q));
    prioInput[q] = priority;
    prioBusy[q] = true;
    prioMsg[q] = "";
    prioErr[q] = "";
    try {
      const r = await setJobPriority(q, priority);
      prioMsg[q] =
        r.updated === 0 ? "no pending jobs" : `updated ${r.updated} pending`;
      await refresh();
    } catch (e) {
      prioErr[q] = `failed: ${errorMessage(e)}`;
    } finally {
      prioBusy[q] = false;
    }
  }

  // Local clock so "running for" counters tick between polls (1s cadence).
  let now = $state(Date.now());

  // Rolling throughput samples per counter KEY (generalised from the original
  // extract-only tracker). Each poll appends {t, done} for a key; the rate is a
  // delta over the oldest/newest sample in the window. "—" (null) until we have
  // >=2 samples (the contract). Used for both the extract backlog and the thumbs
  // queue's completed count.
  type Sample = { t: number; done: number };
  const rateSamples: Record<string, Sample[]> = {};
  let rates = $state<Record<string, number | null>>({});

  // Cumulative-counter deltas for the I/O + network tiles: keep the previous
  // snapshot + timestamp; the displayed rate is (Δbytes / Δt) between consecutive
  // polls. First poll shows "—"; a negative delta (counter reset / device change)
  // skips that sample but still re-baselines so the next delta is correct.
  let prevIo = $state<{ read: number; write: number; t: number } | null>(null);
  let ioRate = $state<{ read: number; write: number } | null>(null);
  let prevNet = $state<{ rx: number; tx: number; t: number } | null>(null);
  let netRate = $state<{ rx: number; tx: number } | null>(null);

  // Compact relative time for the agent-replication tile.
  function relTime(iso: string): string {
    const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return `${secs}s ago`;
    const m = Math.round(secs / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.round(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.round(h / 24)}d ago`;
  }

  // FIX-11 — human GB for the low-space banner.
  function fmtGB(bytes: number): string {
    return `${(bytes / 1024 ** 3).toFixed(1)} GB`;
  }

  // Auto-scaling binary size (B/KiB/MiB/GiB/TiB) for the disk indicator + the
  // thumbnail-cache size, so a small cache reads "820 MiB" not "0.0 GB".
  function fmtBytes(bytes: number): string {
    if (!isFinite(bytes) || bytes <= 0) return "0 B";
    const units = ["B", "KiB", "MiB", "GiB", "TiB"];
    let v = bytes;
    let i = 0;
    while (v >= 1024 && i < units.length - 1) {
      v /= 1024;
      i++;
    }
    return `${v.toFixed(i === 0 || v >= 100 ? 0 : 1)} ${units[i]}`;
  }

  // Friendly names for the disk watch roles. The backend labels rows by ROLE
  // ("tmp" = the app container's temp dir, which sits on the app's own disk);
  // spelled out here so "tmp" doesn't read as "only a scratch mount" when it is
  // in fact the free space of the filesystem the app itself runs on. Merged
  // rows arrive comma-joined ("tmp, postgres" = roles sharing one filesystem);
  // those use the SHORT names so a shared-disk row stays one readable line —
  // the parenthetical detail lives in the row tooltip's member list.
  const DISK_ROLE_NAMES: Record<string, string> = {
    thumbnails: "thumbnails",
    tmp: "temp (app disk)",
    postgres: "database (postgres)",
  };
  const DISK_ROLE_SHORT: Record<string, string> = {
    thumbnails: "thumbnails",
    tmp: "temp",
    postgres: "database",
  };
  function diskLabel(label: string): string {
    const parts = label.split(", ");
    if (parts.length > 1)
      return parts.map((p) => DISK_ROLE_SHORT[p] ?? p).join(" + ");
    return DISK_ROLE_NAMES[label] ?? label;
  }

  // Maintenance panel — the registry of periodic housekeeping tasks with
  // schedule overrides + run-now (rides refresh() on a slower 30s throttle:
  // schedules change rarely and the list is a heavier join than the summary).
  const MAINT_POLL_MS = 30_000;
  let maint = $state<MaintenanceTask[]>([]);
  let maintFetched = 0;
  let maintBusy = $state<Record<string, boolean>>({});
  let maintMsg = $state<Record<string, string>>({});
  // A task's failed Run now / schedule save; persists until dismissed or retried.
  let maintErr = $state<Record<string, string>>({});
  let editKey = $state<string | null>(null);
  let editCron = $state("");

  const MAINT_CATS: [MaintenanceTask["category"], string][] = [
    ["cleanup", "Cleanup"],
    ["integrity", "Data & search integrity"],
    ["monitors", "Monitors"],
    ["system", "System ticks"],
    ["ondemand", "On-demand"],
  ];

  async function refreshMaint(force = false) {
    if (!force && Date.now() - maintFetched < MAINT_POLL_MS) return;
    try {
      maint = (await listMaintenance()).tasks;
      maintFetched = Date.now();
    } catch {
      // best-effort panel; the summary error banner covers hard API loss
    }
  }

  // --- BK-T3 in-app backup ------------------------------------------------
  // Loaded lazily alongside the maintenance registry. The list is admin-only,
  // so a read-scoped operator simply sees an empty panel rather than an error.
  let backups = $state<BackupBundle[]>([]);
  let backupNote = $state("");
  let backupDir = $state("");
  let backupKeep = $state(0);
  let backupBusy = $state(false);
  let backupMsg = $state("");
  let backupError = $state("");
  let downloadError = $state<{ name: string; message: string } | null>(null);

  async function refreshBackups() {
    try {
      const r = await listBackups();
      backups = r.bundles;
      backupNote = r.incomplete_note;
      backupDir = r.dir;
      backupKeep = r.keep;
    } catch {
      // best-effort panel (403 for a non-admin, 404 on an older backend)
    }
  }

  async function backupNow() {
    backupBusy = true;
    backupMsg = "";
    backupError = "";
    try {
      const r = await runBackup();
      backupMsg = r.job_id != null ? `queued (job ${r.job_id})` : "queued";
      // The dump runs on the worker; the bundle appears on a later poll. Give
      // it one nudge rather than pretending it is instant.
      setTimeout(() => void refreshBackups(), 4000);
      await refreshMaint(true);
    } catch (e) {
      backupError = errorMessage(e, { verb: "run a backup" });
    } finally {
      backupBusy = false;
    }
  }

  async function backupDownload(b: BackupBundle) {
    downloadError = null;
    try {
      await downloadBackup(b.name);
    } catch (e) {
      downloadError = { name: b.name, message: errorMessage(e, { verb: "download backups" }) };
    }
  }

  function flashMaint(key: string, msg: string) {
    maintMsg[key] = msg;
    setTimeout(() => {
      if (maintMsg[key] === msg) maintMsg[key] = "";
    }, 6000);
  }

  async function maintRun(t: MaintenanceTask) {
    maintBusy[t.key] = true;
    maintErr[t.key] = "";
    try {
      const r = await runMaintenance(t.key);
      flashMaint(t.key, r.job_id != null ? `queued (job ${r.job_id})` : "queued");
      await refreshMaint(true);
    } catch (e) {
      maintErr[t.key] = errorMessage(e, { verb: "run maintenance tasks" });
    } finally {
      maintBusy[t.key] = false;
    }
  }

  // The cron editor renders below the table (rowPanel.ts): opening focuses it,
  // a save that closes it returns focus to the row's Edit button.
  const uid = $props.id();
  const cronPanelId = (key: string) => `${uid}-cron-${key}`;

  function maintEditToggle(t: MaintenanceTask) {
    if (editKey === t.key) {
      editKey = null;
      return;
    }
    editKey = t.key;
    editCron = t.cron ?? "";
    void focusPanel(cronPanelId(t.key));
  }

  async function maintSave(
    t: MaintenanceTask,
    body: { cron?: string | null; enabled?: boolean },
  ) {
    maintBusy[t.key] = true;
    maintErr[t.key] = "";
    try {
      const updated = await updateMaintenance(t.key, body);
      maint = maint.map((x) => (x.key === t.key ? updated : x));
      if ("cron" in body) {
        editKey = null;
        void focusRowButton("data-cron-for", t.key);
      }
      flashMaint(t.key, "saved — applies within a minute");
    } catch (e) {
      maintErr[t.key] = errorMessage(e, { verb: "change maintenance settings" });
    } finally {
      maintBusy[t.key] = false;
    }
  }

  function lastRunLabel(s: string): string {
    return (
      { succeeded: "ok", failed: "failed", doing: "running", todo: "queued" }[s] ?? s
    );
  }

  // Procrastinate's raw states; "todo" stays neutral as it always rendered.
  function lastRunTone(s: string): Tone {
    if (s === "succeeded") return "ok";
    if (s === "failed") return "danger";
    if (s === "doing") return "info";
    return "neutral";
  }

  let poller: Poller | undefined;
  let ticker: Poller | undefined;

  async function refresh() {
    if (loading) return;
    loading = true;
    try {
      error = "";
      const s = await jobsSummary();
      summary = s;
      lastUpdated = Date.now();
      recordRate("extract", s.extract.done);
      recordRate("thumbs", s.thumbs?.queue?.succeeded ?? 0);
      computeIoNet(s);
      await refreshFailures();
      await refreshMaint();
      // Rides the same throttle as the maintenance registry (both are cheap
      // and neither needs the 4s summary cadence).
      if (Date.now() - maintFetched < 1000) await refreshBackups();
    } catch (e) {
      error = errorMessage(e);
    } finally {
      loading = false;
    }
  }

  // FIX-8 — pull the current failures page. Clamps the offset back into range if
  // rows aged out (retention purge) or were cleared under us between polls.
  // Last-request-wins, so a pager click racing the 4s poll cannot land an
  // older page over a newer one.
  const failuresLoader = latest(async (signal, offset: number) => {
    let page = await failedJobs(FAILED_PAGE, offset, signal);
    if (offset > 0 && offset >= page.total) {
      offset = Math.max(0, (Math.ceil(page.total / FAILED_PAGE) - 1) * FAILED_PAGE);
      page = await failedJobs(FAILED_PAGE, offset, signal);
    }
    return { page, offset };
  });

  async function refreshFailures() {
    const r = await failuresLoader.run(failuresOffset);
    if (r === undefined) return;
    failures = r.page.items;
    failuresTotal = r.page.total;
    failuresOffset = r.offset;
  }

  async function failuresPage(delta: number) {
    failuresOffset = Math.max(0, failuresOffset + delta * FAILED_PAGE);
    failuresError = "";
    try {
      await refreshFailures();
    } catch (e) {
      failuresError = errorMessage(e);
    }
  }

  async function clearFailed() {
    if (clearingFailed) return;
    const ok = await confirm({
      title: "Clear failed job history?",
      message: `Clear all ${failuresTotal} failed job record(s)?`,
      details: [
        "This only removes the history rows",
        "It does not affect queued or running work",
      ],
      confirmLabel: "Clear history",
      tone: "danger",
    });
    if (!ok) return;
    clearingFailed = true;
    clearFailedError = "";
    try {
      await clearFailedJobs();
      failuresOffset = 0;
      await refresh();
    } catch (e) {
      clearFailedError = `Clear failed: ${errorMessage(e, { verb: "clear failed jobs" })}`;
    } finally {
      clearingFailed = false;
    }
  }

  // Global maintenance MODE (2026-08-09): toggle + banner state. (Distinct
  // from `maintBusy` above, which is the per-task busy map of the maintenance
  // SCHEDULES panel.)
  let modeBusy = $state(false);
  let modeError = $state("");
  async function toggleMaintenance() {
    if (modeBusy) return;
    const active = summary?.maintenance?.active ?? false;
    let reason: string | undefined;
    if (!active) {
      // The note is optional free text (no parse rule): blank means none.
      const input = await promptText({
        title: "Enter maintenance mode?",
        message:
          "Suspends scan/maintenance/report scheduling and tells agents to pause replication (they keep scanning locally). Safety reapers and alerts keep running.",
        label: "Optional note shown while active",
        placeholder: "leave blank for none",
        confirmLabel: "Enter maintenance mode",
        required: false,
      });
      if (input === null) return; // cancelled
      reason = input || undefined;
    }
    modeBusy = true;
    modeError = "";
    try {
      await setMaintenanceMode(!active, reason);
      await refresh();
    } catch (e) {
      modeError = `Maintenance mode: ${errorMessage(e, { verb: "change maintenance mode" })}`;
    } finally {
      modeBusy = false;
    }
  }

  async function reap() {
    if (reaping) return;
    reaping = true;
    reapMsg = "";
    reapError = "";
    try {
      const r: ReapResult = await reapStalledJobs();
      reapMsg =
        r.reaped === 0 && r.pruned_workers === 0
          ? "Nothing stalled"
          : `Reaped ${r.reaped} (retried ${r.retried}, failed ${r.failed}), pruned ${r.pruned_workers} worker${r.pruned_workers === 1 ? "" : "s"}`;
      await refresh();
    } catch (e) {
      reapError = `Reap failed: ${errorMessage(e)}`;
    } finally {
      reaping = false;
    }
  }

  // Generalised rolling-rate recorder: append {t, done} for `key` and recompute
  // its per-second rate over the sample window (null with <2 samples; 0 when the
  // counter is flat/receding so a stalled queue reads "stalled").
  function recordRate(key: string, done: number) {
    const t = Date.now();
    const arr = rateSamples[key] ?? (rateSamples[key] = []);
    const prev = arr[arr.length - 1];
    arr.push({ t, done });
    if (arr.length > 6) arr.shift();
    if (arr.length >= 2 && prev) {
      const first = arr[0];
      const last = arr[arr.length - 1];
      const dt = (last.t - first.t) / 1000;
      const dd = last.done - first.done;
      rates[key] = dt > 0 && dd > 0 ? dd / dt : dd <= 0 ? 0 : null;
    } else {
      rates[key] = null;
    }
  }

  // ETA string for a queue backlog: depth / rolling-rate. "—" with <2 samples or
  // an unknown rate; "stalled" when the rate is 0; "0" when nothing is queued.
  function queueEta(key: string, depth: number): string {
    if (depth <= 0) return "0";
    const r = rates[key];
    if (r == null) return "—";
    if (r <= 0) return "stalled";
    return fmtDuration(depth / r);
  }

  // Rolling completion rate for a key, expressed per MINUTE (null with <2 samples).
  function ratePerMin(key: string): number | null {
    const r = rates[key];
    return r == null ? null : r * 60;
  }

  // Files/min for a running scan: seen ÷ elapsed since started_at. Deliberately
  // the same cumulative-average definition the SSE endpoint's `rate` uses, so
  // the Jobs page and the Admin live view never disagree. Depends on `now`
  // (1s ticker) so it keeps moving between 4s polls. Null when the backend sent
  // no started_at or under a second has passed (the divisor would explode).
  function scanFilesPerMin(s: ScanRunning): number | null {
    if (!s.started_at) return null;
    const started = Date.parse(s.started_at);
    if (Number.isNaN(started)) return null;
    const elapsedMin = (now - started) / 60000;
    if (elapsedMin <= 1 / 60) return null;
    return (s.stats.seen ?? 0) / elapsedMin;
  }

  // Compute I/O + network B/s from the cumulative counters between two polls.
  // Skips a sample on a negative delta (counter reset) but re-baselines so the
  // NEXT interval is correct; nulls the rate when the counters go away.
  function computeIoNet(s: JobsSummary) {
    const t = Date.now();
    const io = s.resources?.io ?? null;
    if (io) {
      if (prevIo) {
        const dt = (t - prevIo.t) / 1000;
        const dr = io.read_bytes - prevIo.read;
        const dw = io.write_bytes - prevIo.write;
        if (dt > 0 && dr >= 0 && dw >= 0) ioRate = { read: dr / dt, write: dw / dt };
      }
      prevIo = { read: io.read_bytes, write: io.write_bytes, t };
    } else {
      prevIo = null;
      ioRate = null;
    }
    const net = s.resources?.net ?? null;
    if (net) {
      if (prevNet) {
        const dt = (t - prevNet.t) / 1000;
        const dr = net.rx_bytes - prevNet.rx;
        const dtx = net.tx_bytes - prevNet.tx;
        if (dt > 0 && dr >= 0 && dtx >= 0) netRate = { rx: dr / dt, tx: dtx / dt };
      }
      prevNet = { rx: net.rx_bytes, tx: net.tx_bytes, t };
    } else {
      prevNet = null;
      netRate = null;
    }
  }

  // Auto-scaled byte-rate ("12.4 MiB/s").
  function fmtRate(bytesPerSec: number): string {
    return `${fmtBytes(bytesPerSec)}/s`;
  }

  // Local wall-clock HH:MM for an upcoming ISO instant.
  function hhmm(iso: string): string {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    return new Date(t).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  // Relative "in Xd Yh" / "in Xh Ym" / "in Xm" for an upcoming instant (uses the
  // reactive `now` clock so it ticks between polls).
  function relFuture(iso: string): string {
    const t = Date.parse(iso);
    if (isNaN(t)) return "";
    let secs = Math.max(0, (t - now) / 1000);
    const d = Math.floor(secs / 86400);
    secs -= d * 86400;
    const h = Math.floor(secs / 3600);
    secs -= h * 3600;
    const m = Math.floor(secs / 60);
    if (d > 0) return `in ${d}d ${h}h`;
    if (h > 0) return `in ${h}h ${m}m`;
    return `in ${m}m`;
  }

  function fmtDuration(secs: number): string {
    if (!isFinite(secs) || secs < 0) return "—";
    const s = Math.round(secs);
    if (s < 60) return `${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ${s % 60}s`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ${m % 60}m`;
    const d = Math.floor(h / 24);
    return `${d}d ${h % 24}h`;
  }

  // FIX-12 — the "attempts" column used to show procrastinate's raw per-job
  // counter. That number is honest but easily MISREAD: for extract it also counts
  // the staged-pipeline gate *reschedules* (a wait while a scan walks the same
  // library — no work done, never a failure), and legacy rows also carry pre-cap
  // stalled-job-reaper requeues. So a single file could read "attempted 137 times"
  // without ever having failed. We now pair attempts with the task's genuine-
  // failure retry budget (`retry_cap`) and split any excess out as reschedules/
  // requeues, so the number reads honestly (e.g. "2/2  +135 waiting").
  type AttemptsView = { main: string; extra: number; extraLabel: string; tooltip: string };
  function attemptsView(
    task: string,
    attempts: number | null,
    cap: number | null,
  ): AttemptsView {
    const a = attempts ?? 0;
    if (cap === null || cap === undefined) {
      // No retry configured (scan / periodic maintenance): a single failure is
      // terminal, so the raw count is already meaningful.
      return {
        main: String(a),
        extra: 0,
        extraLabel: "",
        tooltip: a === 0 ? "First run (no prior attempts)" : `${a} prior attempt${a === 1 ? "" : "s"}`,
      };
    }
    const tries = Math.min(a, cap);
    const extra = Math.max(0, a - cap);
    // Extract's excess are staged-pipeline reschedules (waits for a running scan);
    // any other task's excess (legacy pre-cap rows) are reaper requeues.
    const isExtract = task === "extract_item" || task.endsWith(".extract_item");
    const extraLabel = extra > 0 ? (isExtract ? `+${extra} waiting` : `+${extra} re-queued`) : "";
    const tooltip =
      extra > 0
        ? isExtract
          ? `${tries} of ${cap} genuine-failure retries used. The other ${extra} are staged-pipeline reschedules — the file was re-queued while a scan was running, not retried after a failure.`
          : `${tries} of ${cap} genuine-failure retries used. The other ${extra} are stalled-job-reaper re-queues (the worker died mid-job), not failures.`
        : `${tries} of ${cap} genuine-failure retries used (0 = first run).`;
    return { main: `${tries}/${cap}`, extra, extraLabel, tooltip };
  }

  // Live "running for" seconds — prefers the server-reported started_at so it
  // ticks accurately; falls back to the static seconds_running snapshot.
  function runningSecs(startedAt: string | null, snapshot: number | null): number | null {
    if (startedAt) {
      const t = Date.parse(startedAt);
      if (!isNaN(t)) return Math.max((now - t) / 1000, 0);
    }
    return snapshot;
  }

  function jobTarget(job: RunningJob): string {
    if (job.rel_path) return job.rel_path;
    const a = job.args ?? {};
    if (typeof a.rel_path === "string" && a.rel_path) return a.rel_path;
    if (typeof job.library_name === "string" && job.library_name) return `library: ${job.library_name}`;
    if (typeof a.library_id === "string") return `library ${a.library_id.slice(0, 8)}`;
    if (typeof a.item_id === "string") return `item ${a.item_id.slice(0, 8)}`;
    return "—";
  }

  function isThumbTask(task: string): boolean {
    return task === "thumb_item" || task.endsWith(".thumb_item");
  }

  // Job target with a trailing file-size suffix for THUMBNAIL tasks only (size
  // predicts thumbnail-generation duration — the user's rationale). Other tasks
  // render the bare target.
  function jobTargetDisplay(job: RunningJob): string {
    const base = jobTarget(job);
    if (isThumbTask(job.task) && typeof job.size === "number" && job.size > 0) {
      return `${base} (${fmtBytes(job.size)})`;
    }
    return base;
  }

  function qCount(q: string, status: string): number {
    return summary?.queues?.[q]?.[status] ?? 0;
  }

  function qStalled(q: string): number {
    return summary?.stalled?.by_queue?.[q] ?? 0;
  }

  // Inline scan progress for a running scan_library job: match the job's
  // library_id + rel_path to a running ScanRun and surface its live stats.
  function scanProgressFor(
    job: RunningJob,
  ): ScanRunning | null {
    if (job.task !== "scan_library" || !summary) return null;
    const a = job.args ?? {};
    const lid = typeof a.library_id === "string" ? a.library_id : null;
    if (!lid) return null;
    const rel = typeof a.rel_path === "string" ? a.rel_path : null;
    return (
      summary.scans_running.find(
        (r) => r.library_id === lid && (r.rel_path ?? null) === rel,
      ) ?? null
    );
  }

  onMount(() => {
    refresh();
    // Poll only while the tab is the visible foreground document; the helper
    // also refreshes immediately when the tab is brought back.
    poller = usePolling(refresh, POLL_MS);
    // Local 1s tick for the running-for counters (cheap, no network). Paused
    // while hidden too; refresh-on-return snaps the counters to the real time.
    ticker = usePolling(() => (now = Date.now()), 1000);
  });

  onDestroy(() => {
    poller?.stop();
    ticker?.stop();
    failuresLoader.cancel();
  });
</script>

<!-- Agent replication cells -->
{#snippet replNameCell(a: AgentReplicationActivity)}<span class="font-medium">{a.name}</span>{/snippet}
{#snippet replSeqCell(a: AgentReplicationActivity)}<span class="tabular-nums text-muted">{a.seq_no.toLocaleString()}</span>{/snippet}
{#snippet replBatchCell(a: AgentReplicationActivity)}
  <span class="text-muted" title={new Date(a.last_seen_at).toLocaleString()}>{relTime(a.last_seen_at)}</span>
{/snippet}
{#snippet replReconcileCell(a: AgentReplicationActivity)}
  <span class="text-muted">{a.last_reconcile_at ? relTime(a.last_reconcile_at) : "never"}</span>
{/snippet}

<!-- Running-scan cells -->
{#snippet scanLibCell(s: ScanRunning)}<span class="font-medium">{s.library_name}</span>{/snippet}
{#snippet scanScopeCell(s: ScanRunning)}<span class="font-mono text-xs text-muted">{s.rel_path ?? "(whole library)"}</span>{/snippet}
{#snippet scanSeenCell(s: ScanRunning)}<span class="tabular-nums text-muted">{s.stats.seen ?? 0}</span>{/snippet}
{#snippet scanNewCell(s: ScanRunning)}<span class="tabular-nums text-muted">{s.stats.new ?? 0}</span>{/snippet}
{#snippet scanChangedCell(s: ScanRunning)}<span class="tabular-nums text-muted">{s.stats.changed ?? 0}</span>{/snippet}
{#snippet scanExcludedCell(s: ScanRunning)}<span class="tabular-nums text-muted">{s.stats.excluded ?? 0}</span>{/snippet}
{#snippet scanSizeCell(s: ScanRunning)}<span class="tabular-nums text-muted">{fmtBytes(s.stats.bytes_seen ?? 0)}</span>{/snippet}
{#snippet scanRateCell(s: ScanRunning)}
  {@const fpm = scanFilesPerMin(s)}
  <span class="tabular-nums text-muted">{fpm === null ? "—" : Math.round(fpm).toLocaleString()}</span>
{/snippet}

<!-- Running-job cells. The task cell carries the stalled chip (the row tint
     it replaced is not available in DataTable) and, for a scan, its live
     progress line. -->
{#snippet runTaskCell(job: RunningJob)}
  {@const prog = scanProgressFor(job)}
  <span class="flex flex-wrap items-center gap-1.5 font-mono text-xs">
    {job.task}
    {#if job.stalled}
      <span
        title={job.worker_id === null
          ? "No live worker owns this job (worker restarted) — the reaper will requeue it"
          : !job.worker_alive
            ? "This job's worker stopped sending heartbeats — the reaper will requeue it"
            : "Running far longer than expected — the reaper will requeue it"}><Badge tone="warn">stalled</Badge></span>
    {/if}
  </span>
  {#if prog}
    {@const pfpm = scanFilesPerMin(prog)}
    <span class="mt-0.5 block text-2xs text-muted">
      seen {prog.stats.seen ?? 0} · new {prog.stats.new ?? 0} · changed {prog.stats.changed ?? 0}{#if prog.stats.excluded} · excluded {prog.stats.excluded}{/if}
      {#if pfpm !== null}· {Math.round(pfpm).toLocaleString()}/min{/if}
      {#if prog.stats.bytes_seen}· {fmtBytes(prog.stats.bytes_seen)}{/if}
    </span>
  {/if}
{/snippet}
{#snippet runQueueCell(job: RunningJob)}<span class="text-muted">{job.queue}</span>{/snippet}
{#snippet runTargetCell(job: RunningJob)}
  <span class="block max-w-[28rem] truncate font-mono text-xs text-muted" title={jobTargetDisplay(job)}>{jobTargetDisplay(job)}</span>
{/snippet}
{#snippet attempts(task: string, n: number | null, cap: number | null)}
  {@const av = attemptsView(task, n, cap)}
  <span class="tabular-nums text-muted" title={av.tooltip}>
    {av.main}{#if av.extra > 0}<span class="ml-1 text-2xs font-normal">{av.extraLabel}</span>{/if}
  </span>
{/snippet}
{#snippet runAttemptsCell(job: RunningJob)}{@render attempts(job.task, job.attempts, job.retry_cap)}{/snippet}
{#snippet runForCell(job: RunningJob)}
  {@const secs = runningSecs(job.started_at, job.seconds_running)}
  <span class="tabular-nums text-muted">{secs === null ? "—" : fmtDuration(secs)}</span>
{/snippet}

<!-- Failed-job cells -->
{#snippet failTaskCell(j: FailedJob)}<span class="font-mono text-xs">{j.task}</span>{/snippet}
{#snippet failQueueCell(j: FailedJob)}<span class="text-muted">{j.queue}</span>{/snippet}
{#snippet failAttemptsCell(j: FailedJob)}{@render attempts(j.task, j.attempts, j.retry_cap)}{/snippet}
{#snippet failAttemptedCell(j: FailedJob)}
  <span class="text-muted">{j.attempted_at ? new Date(j.attempted_at).toLocaleString() : "—"}</span>
{/snippet}
{#snippet failErrorCell(j: FailedJob)}
  {#if j.traceback}
    <!-- The traceback used to live in a hover title; a disclosure reaches
         touch and keyboard users too. -->
    <details class="max-w-[24rem]">
      <summary class="cursor-pointer truncate font-mono text-xs text-danger">{j.error ?? "—"}</summary>
      <pre class="mt-1 max-h-64 overflow-auto whitespace-pre-wrap rounded-control bg-surface-3 p-2 font-mono text-[11px]">{j.traceback}</pre>
    </details>
  {:else}
    <span class="block max-w-[24rem] truncate font-mono text-xs text-danger" title={j.error ?? undefined}>{j.error ?? "—"}</span>
  {/if}
{/snippet}

<!-- Scheduled-maintenance cells -->
{#snippet maintTaskCell(t: MaintenanceTask)}
  <span class="cursor-help font-medium underline decoration-dotted underline-offset-2" title={t.description}>{t.title}</span>
  {#if t.gate}
    <!-- The task's optional feature is off, so a run returns immediately.
         The queue keeps no task result, so this pre-flight gate state is the
         only explanation available for the 0-second run. -->
    <span class="ml-1" title={t.gate.reason}><Badge tone="warn">will no-op</Badge></span>
  {/if}
  {#if maintMsg[t.key]}
    <span class="ml-2 text-2xs text-muted">{maintMsg[t.key]}</span>
  {/if}
{/snippet}
{#snippet maintScheduleCell(t: MaintenanceTask)}
  {#if t.cron}
    <code class="rounded bg-surface-3 px-1.5 py-0.5 text-xs tabular-nums">{t.cron}</code>
    {#if t.overridden}
      <span class="ml-1" title="Custom schedule — the registry default is {t.default_cron}"><Badge tone="info">custom</Badge></span>
    {:else if !t.editable}
      <span class="ml-1 text-2xs text-muted" title="This cadence is part of the task's semantics and cannot be changed.">fixed</span>
    {/if}
  {:else}
    <span class="text-xs text-muted">on demand</span>
  {/if}
  <!-- Carries the dimmed-row cue the table used to give a disabled task. -->
  {#if !t.enabled}<Badge class="ml-1">disabled</Badge>{/if}
{/snippet}
{#snippet maintNextCell(t: MaintenanceTask)}
  {#if t.next_run_at && t.enabled}
    <span class="tabular-nums text-muted" title={new Date(t.next_run_at).toLocaleString()}>{hhmm(t.next_run_at)} · {relFuture(t.next_run_at)}</span>
  {:else}
    <span class="text-muted">—</span>
  {/if}
{/snippet}
{#snippet maintLastCell(t: MaintenanceTask)}
  {#if t.last_run}
    <Badge tone={lastRunTone(t.last_run.status)}>{lastRunLabel(t.last_run.status)}</Badge>
    {#if t.last_run.at}
      <span class="ml-1 tabular-nums text-muted" title={new Date(t.last_run.at).toLocaleString()}>{relTime(t.last_run.at)}</span>
    {/if}
    {#if t.last_run.status === "doing" && t.last_run.started_at}
      <span class="ml-1 tabular-nums text-muted" title="Elapsed so far (refreshes with the page poll)">· {fmtDuration((Date.now() - new Date(t.last_run.started_at).getTime()) / 1000)}</span>
    {:else if t.last_run.duration_seconds != null}
      <span class="ml-1 tabular-nums text-muted" title="Wall time of the last completed run{t.last_run.started_at ? ` (started ${new Date(t.last_run.started_at).toLocaleString()})` : ''}">· took {fmtDuration(t.last_run.duration_seconds)}</span>
    {/if}
  {:else}
    <span class="text-xs text-muted" title="No run within the job-history retention window (succeeded runs are kept ~48h).">no recent run</span>
  {/if}
{/snippet}
{#snippet maintActionsCell(t: MaintenanceTask)}
  <div class="flex flex-wrap justify-end gap-1">
    {#if t.runnable}
      <Button
        size="sm"
        onclick={() => maintRun(t)}
        disabled={maintBusy[t.key]}
        title="Queue this task to run immediately (admin).">
        {maintBusy[t.key] ? "…" : "Run now"}</Button>
    {/if}
    {#if t.editable}
      <Button
        size="sm"
        aria-expanded={editKey === t.key}
        aria-controls={editKey === t.key ? cronPanelId(t.key) : undefined}
        data-cron-for={t.key}
        onclick={() => maintEditToggle(t)}
        disabled={maintBusy[t.key]}
        title="Change this task's cron schedule or disable it.">
        {editKey === t.key ? "Close" : "Edit"}</Button>
    {/if}
  </div>
{/snippet}

<!-- Backup cells -->
{#snippet bundleNameCell(b: BackupBundle)}<span class="font-mono text-xs">{b.name}</span>{/snippet}
{#snippet bundleTakenCell(b: BackupBundle)}<span class="text-xs text-muted">{b.created_at ? relTime(b.created_at) : "—"}</span>{/snippet}
{#snippet bundleItemsCell(b: BackupBundle)}<span class="text-xs">{b.item_count ?? "—"}</span>{/snippet}
{#snippet bundleSizeCell(b: BackupBundle)}<span class="text-xs">{fmtBytes(b.bytes)}</span>{/snippet}
{#snippet bundleCoversCell(b: BackupBundle)}
  <!-- Never render a partial bundle as simply "ok": the missing pieces are
       named on the row an operator is about to trust. -->
  <Badge tone={b.complete ? "ok" : "warn"}>{b.complete ? "full" : "database only"}</Badge>
  {#if !b.complete}
    <span class="ml-1 text-xs text-muted">not included: {b.missing.join(", ")}</span>
  {/if}
{/snippet}
{#snippet bundleActionsCell(b: BackupBundle)}
  <Button
    size="sm"
    onclick={() => backupDownload(b)}
    title="Download this bundle's Postgres dump. Keep it OFF this box — a backup on the volume it protects is not a backup.">
    Download</Button>
{/snippet}

<div class="mt-4">
  <div class="flex flex-wrap items-center gap-3">
    <h2 class="text-lg font-semibold">Jobs</h2>
    <span class="flex items-center gap-1.5 text-xs text-muted">
      {#if loading}
        <span class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-border-strong border-t-accent"
          aria-hidden="true"></span>
      {/if}
      {#if lastUpdated}updated {new Date(lastUpdated).toLocaleTimeString()}{#if loading} · refreshing…{/if}{:else}loading…{/if}
    </span>
    {#if summary && summary.stalled.total > 0}
      <span title="Jobs stranded in 'doing' by a dead/restarted worker — the reaper will requeue or fail them">
        <Badge tone="warn">{summary.stalled.total} stalled</Badge>
      </span>
    {/if}
    <div class="grow"></div>
    {#if reapMsg}<span class="text-xs text-muted">{reapMsg}</span>{/if}
    {#if summary && !summary.maintenance?.active}
      <Button
        size="sm"
        onclick={toggleMaintenance}
        disabled={modeBusy}
        title="Suspend all regular background processing (scan scheduling, nightly maintenance, report exports) and tell agents to pause replication — for long-running operator work like database maintenance. Safety reapers and alerts keep running.">
        {modeBusy ? "Switching…" : "Enter maintenance mode"}</Button>
    {/if}
    <Button
      size="sm"
      onclick={reap}
      disabled={reaping}
      title="Requeue or fail jobs orphaned by a dead worker (also runs automatically every 5 minutes)">
      {reaping ? "Reaping…" : "Reap now"}</Button>
    <Button size="sm" onclick={refresh} disabled={loading}>Refresh</Button>
  </div>
  {#if reapError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (reapError = "")}>{reapError}</Alert>
  {/if}
  {#if modeError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (modeError = "")}>{modeError}</Alert>
  {/if}

  {#if !summary}
    <!-- First load: the summary query fans out over several tables and can
         take a few seconds on a large catalog, so say so instead of showing
         a header over nothing. A first-load failure takes its place. -->
    {#if error}
      <StateBlock state="error" message={error} onRetry={refresh} />
    {:else}
      <StateBlock
        state="loading"
        message="Loading job status — counting queues, scans and failures; this can take several seconds on a large catalog." />
    {/if}
  {:else}
    {#if error}
      <!-- A failed poll keeps the last summary on screen. -->
      <Alert tone="danger" class="mt-2" onDismiss={() => (error = "")}>{error}</Alert>
    {/if}

    <!-- Global maintenance-mode banner (2026-08-09): shown while the operator
         switch is active. Data piggybacks the jobs-summary poll. -->
    {#if summary.maintenance?.active}
      <Alert tone="info" class="mt-3">
        <div class="flex flex-wrap items-center gap-3">
          <div class="min-w-0 flex-1">
            <span class="font-semibold">Maintenance mode is active</span>
            {#if summary.maintenance.reason}
              <span class="text-current/80">— {summary.maintenance.reason}</span>
            {/if}
            {#if summary.maintenance.started_at}
              <span class="text-xs text-current/70">
                (since {new Date(summary.maintenance.started_at).toLocaleString()})</span>
            {/if}
            <p class="mt-0.5 text-xs">
              Scan scheduling, nightly maintenance tasks and report exports are
              suspended; manual scans are refused; agents pause replication (their
              local scans continue and their backlog drains on exit). Stalled-job
              reaping and alert delivery keep running.
            </p>
          </div>
          <Button size="sm" class="shrink-0" onclick={toggleMaintenance} disabled={modeBusy}>
            {modeBusy ? "Switching…" : "Exit maintenance mode"}</Button>
        </div>
      </Alert>
    {/if}

    <!-- FIX-11 low-space banner: danger at critical, warn at warn, hidden when ok.
         Data piggybacks the existing jobs-summary poll (summary.disk). -->
    {#if summary.disk && summary.disk.status !== "ok"}
      {@const crit = summary.disk.status === "critical"}
      <Alert tone={crit ? "danger" : "warn"} class="mt-3" title={crit ? "Critically low disk space" : "Low disk space"}>
        <p class="mt-0.5 text-xs">
          {#if crit}
            Thumbnail generation is paused (writes are refused) to protect the
            database. Free space, then reclaim via the daily/emergency thumbnail GC.
          {:else}
            Disk is running low — free space before it reaches the critical floor.
          {/if}
        </p>
        <ul class="mt-1 space-y-0.5 text-xs">
          {#each summary.disk.low as d (d.path)}
            <li>
              <span class="font-medium">{d.label}</span>
              <span class="text-current/70">({d.path})</span>:
              {fmtGB(d.free)} free of {fmtGB(d.total)}
              ({d.pct_free.toFixed(1)}%) — {d.reason}
            </li>
          {/each}
        </ul>
        {#if summary.disk.alerting && (!summary.disk.alerting.enabled || summary.disk.alerting.channels === 0)}
          <p class="mt-1.5 text-xs font-medium"
            title="The disk monitor records hourly-deduped warn/critical/recovery events, but delivery needs the 'System: low disk space' rule ENABLED with at least one channel (webhook/SMTP) attached — it ships disabled so nothing sends without your say-so. Set it up under the Alerts tab.">
            ⚠ No alert will be sent for this:
            {#if !summary.disk.alerting.enabled}the "System: low disk space" alert rule is disabled{:else}the rule has no channels attached{/if}
            — configure it in the Alerts tab.
          </p>
        {/if}
      </Alert>
    {/if}
    <!-- Resource monitors: always-on disk / CPU / I/O / network / DB tiles. All
         ride the existing jobs-summary poll (no new request). Each tile hides
         itself when its data is unavailable (older backend without `paths`, a host
         with no load average / no /proc — non-Linux dev backend — or a failed DB
         probe). -->
    {#if (summary?.disk?.paths?.length ?? 0) > 0 || summary?.resources?.cpu?.percent != null || summary?.resources?.io || summary?.resources?.net || summary?.resources?.db}
      <!-- auto-fit: every resource card shares ONE row when the viewport is wide
           enough (min 240px per card) and reflows to fewer columns as it narrows —
           no breakpoint tuning, the DB card no longer wraps alone on wide screens. -->
      <div class="mt-4 grid gap-3 grid-cols-[repeat(auto-fit,minmax(240px,1fr))]">
        {#if summary?.disk?.paths && summary.disk.paths.length > 0}
          <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium">Disk space</span>
              <span
                class="text-xs text-slate-600 dark:text-slate-400"
                title="Free headroom on each filesystem the app monitors: the thumbnail cache, the app container's temp dir (i.e. the disk the app itself runs on), and — when the pgdata volume is mounted (compose default) — the filesystem holding the Postgres database. Roles on the same filesystem merge into one row. Rows turn amber/red as they approach the low-space floors that pause thumbnail writes.">
                monitored paths
              </span>
            </div>
            <div class="mt-2 space-y-2">
              {#each summary.disk.paths as d (d.path)}
                {@const used = Math.max(0, d.total - d.free)}
                {@const usedPct = d.total > 0 ? Math.min(100, (used / d.total) * 100) : 0}
                {@const warn = d.status === "warn"}
                {@const crit = d.status === "critical"}
                <div
                  class="rounded-lg px-2 py-1 {crit ? 'bg-danger-bg' : warn ? 'bg-warn-bg' : ''}">
                  <div class="flex items-baseline justify-between gap-2 text-xs">
                    <span
                      class="truncate font-medium"
                      title={d.members && d.members.length > 1
                        ? d.members.map((m) => `${diskLabel(m.label)}: ${m.path}`).join("\n")
                        : d.path}>{diskLabel(d.label)}</span>
                    <span
                      class="tabular-nums text-slate-500"
                      title="{fmtBytes(used)} used of {fmtBytes(d.total)} — {fmtBytes(d.free)} ({d.pct_free.toFixed(0)}%) free">
                      {fmtBytes(d.free)} free · {usedPct.toFixed(0)}% used
                    </span>
                  </div>
                  <div
                    class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-3"
                    title="{fmtBytes(used)} used of {fmtBytes(d.total)}">
                    <div
                      class="h-full rounded-full {crit ? 'bg-danger' : warn ? 'bg-warn' : 'bg-ok'}"
                      style="width: {usedPct}%"></div>
                  </div>
                </div>
              {/each}
            </div>
          </div>
        {/if}

        {#if summary?.resources?.cpu?.percent != null}
          {@const cpu = summary.resources.cpu}
          {@const pct = cpu.percent ?? 0}
          {@const barPct = Math.min(100, Math.max(0, pct))}
          {@const hot = pct >= 95}
          {@const warm = pct >= 80}
          <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium">CPU</span>
              <span
                class="tabular-nums text-xs text-slate-500"
                title="Utilization sampled across ALL cores between polls (busy time / total time from /proc/stat) — 100% means every core fully busy.">
                {pct}%
              </span>
            </div>
            <div class="mt-2 h-1.5 w-full overflow-hidden rounded-full bg-surface-3">
              <div
                class="h-full rounded-full {hot ? 'bg-danger' : warm ? 'bg-warn' : 'bg-ok'}"
                style="width: {barPct}%"></div>
            </div>
            <div
              class="mt-1.5 tabular-nums text-xs text-slate-500"
              title="Unix run-queue load averages over 1 / 5 / 15 minutes (saturation: can exceed the core count when work is queued waiting for CPU) and the core count.">
              load {cpu.load1?.toFixed(2) ?? "—"} / {cpu.load5?.toFixed(2) ?? "—"} / {cpu.load15?.toFixed(2) ?? "—"}
              {#if cpu.cores != null}· {cpu.cores} core{cpu.cores === 1 ? "" : "s"}{/if}
            </div>
          </div>
        {/if}

        <!-- Combined I/O + network tile: each side alone left too much horizontal
             whitespace (two short rows per card), so disk throughput and network
             throughput share one card as side-by-side columns. B/s computed
             client-side between polls from the cumulative /proc counters; "—" on
             the first poll; a side hides itself when its counters are absent. -->
        {#if summary?.resources?.io || summary?.resources?.net}
          <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium">I/O &amp; network</span>
              <span
                class="text-xs text-slate-600 dark:text-slate-400"
                title="Left: whole-disk read/write throughput across physical block devices (cumulative /proc/diskstats deltas between polls; partitions and loop/dm excluded). Right: receive/transmit summed across all interfaces except loopback (/proc/net/dev deltas).">
                throughput
              </span>
            </div>
            <div class="mt-2 grid grid-cols-2 gap-3">
              {#if summary?.resources?.io}
                <div>
                  <div class="text-xs text-slate-600 dark:text-slate-400">disk</div>
                  {#if ioRate}
                    <div class="tabular-nums text-sm text-slate-600 dark:text-slate-300">
                      {fmtRate(ioRate.read)} <span class="text-xs text-slate-600 dark:text-slate-400">read</span>
                    </div>
                    <div class="mt-0.5 tabular-nums text-sm text-slate-600 dark:text-slate-300">
                      {fmtRate(ioRate.write)} <span class="text-xs text-slate-600 dark:text-slate-400">write</span>
                    </div>
                  {:else}
                    <div class="text-sm text-slate-600 dark:text-slate-400">—</div>
                  {/if}
                </div>
              {/if}
              {#if summary?.resources?.net}
                <div>
                  <div class="text-xs text-slate-600 dark:text-slate-400">network</div>
                  {#if netRate}
                    <div class="tabular-nums text-sm text-slate-600 dark:text-slate-300">
                      {fmtRate(netRate.rx)} <span class="text-xs text-slate-600 dark:text-slate-400">rx</span>
                    </div>
                    <div class="mt-0.5 tabular-nums text-sm text-slate-600 dark:text-slate-300">
                      {fmtRate(netRate.tx)} <span class="text-xs text-slate-600 dark:text-slate-400">tx</span>
                    </div>
                  {:else}
                    <div class="text-sm text-slate-600 dark:text-slate-400">—</div>
                  {/if}
                </div>
              {/if}
            </div>
          </div>
        {/if}

        <!-- Postgres health tile. Amber highlights are INFORMATIONAL thresholds
             (documented in the tooltip), not alerts. -->
        {#if summary?.resources?.db}
          {@const db = summary.resources.db}
          {@const idleHot = db.longest_idle_in_tx_s > 60}
          {@const queryHot = db.longest_query_s > 300}
          {@const cacheHot = db.cache_hit_ratio != null && db.cache_hit_ratio < 0.9}
          <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
            <div class="flex items-center justify-between gap-2">
              <span class="font-medium">Database</span>
              <span class="tabular-nums text-xs text-slate-500">
                {#if db.db_size_bytes}
                  <span title="Whole-database size on disk (pg_database_size): every table, index, and TOAST segment. Grows with the catalog — a steadily growing value is normal; plan disk headroom from it.">{fmtBytes(db.db_size_bytes)}</span>
                  ·
                {/if}
                <span title="Live backends from pg_stat_activity for this database (active / idle-in-transaction breakdown).">{db.backends} conn</span>
              </span>
            </div>
            <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
              <span class="text-slate-500">active <b class="tabular-nums text-slate-700 dark:text-slate-200">{db.active}</b></span>
              <span class="{idleHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
                title="Backends idle inside an open transaction, and the age of the longest such session. Amber above 60s (informational — a long idle-in-transaction can hold locks / bloat).">
                idle-in-tx <b class="tabular-nums">{db.idle_in_tx}</b>
                {#if db.longest_idle_in_tx_s > 0}<span class="text-slate-600 dark:text-slate-400">({fmtDuration(db.longest_idle_in_tx_s)})</span>{/if}
              </span>
              {#if db.waiting > 0}
                <span class="text-slate-500">waiting <b class="tabular-nums">{db.waiting}</b></span>
              {/if}
            </div>
            <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs">
              <span class="{queryHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
                title="Age of the longest currently-running query. Amber above 5m (informational).">
                longest query <b class="tabular-nums">{db.longest_query_s > 0 ? fmtDuration(db.longest_query_s) : "—"}</b>
              </span>
              <span class="{cacheHot ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
                title="Shared-buffer cache hit ratio (blks_hit / (hit + read)). Amber below 90% (informational — a cold cache or an undersized shared_buffers).">
                cache {db.cache_hit_ratio != null ? `${(db.cache_hit_ratio * 100).toFixed(1)}%` : "—"}
              </span>
            </div>
            <div class="mt-1 flex flex-wrap gap-x-3 gap-y-1 text-xs text-slate-500">
              <span>deadlocks <b class="tabular-nums {db.deadlocks > 0 ? 'text-amber-600 dark:text-amber-400' : ''}">{db.deadlocks}</b></span>
              <span title="Temporary files spilled to disk since the stats were last reset (a sign of work_mem pressure).">temp files <b class="tabular-nums">{db.temp_files}</b></span>
              <span title="Total procrastinate 'todo' backlog across all queues.">queue backlog <b class="tabular-nums">{db.queue_backlog}</b></span>
            </div>
            {#if db.largest_tables?.length}
              <div class="mt-1 text-xs text-slate-500"
                title="Largest tables by TOTAL on-disk size (table + its indexes + TOAST). 'items' dominating is normal — it IS the catalog; a runaway procrastinate/app_logs/doc_chunks table here is the early signal of a retention problem.">
                largest:
                {#each db.largest_tables as t, i (t.name)}
                  {i > 0 ? ", " : ""}<span class="whitespace-nowrap">{t.name} <b class="tabular-nums">{fmtBytes(t.bytes)}</b></span>
                {/each}
              </div>
            {/if}
          </div>
        {/if}
      </div>
    {/if}
    <!-- Queue summary cards -->
    <div class="mt-4 grid grid-cols-2 gap-3 lg:grid-cols-4">
      {#each displayQueues as q (q)}
        {@const failed = qCount(q, "failed")}
        {@const stalled = qStalled(q)}
        <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center justify-between gap-2">
            <span class="font-medium capitalize">{q}</span>
            <span class="flex items-center gap-1">
              {#if stalled > 0}
                <span title="stalled doing jobs the reaper will act on"><Badge tone="warn">{stalled} stalled</Badge></span>
              {/if}
              {#if failed > 0}
                <Badge tone="danger">{failed} failed</Badge>
              {/if}
            </span>
          </div>
          {#if q === "extract"}
            <!-- Backlog depth is the headline signal for the extract queue. -->
            <div class="mt-2 flex items-baseline gap-2">
              <span class="text-3xl font-bold tabular-nums text-accent-text">
                {summary?.extract.depth ?? 0}
              </span>
              <span class="text-xs text-slate-500">queued</span>
            </div>
            <div class="mt-1 text-xs text-slate-500">
              ETA {queueEta("extract", summary?.extract.depth ?? 0)}
              {#if rates["extract"] != null && rates["extract"] > 0}
                <span class="text-slate-600 dark:text-slate-400">· {rates["extract"].toFixed(1)}/s</span>
              {/if}
            </div>
            {#if summary?.staged_pipeline && summary.scans_running.length > 0}
              <!-- UI-T14: staged mode holds extraction until the running scan ends -->
              <div class="mt-1 text-xs font-medium text-amber-600 dark:text-amber-400"
                title="Staged pipeline is on: extraction is deferred until the running scan finishes walking, so the scan and extract workers don't hit disk/network at once.">
                staged — waiting for scan
              </div>
            {/if}
          {/if}
          {#if q === "thumbs" && summary?.thumbs}
            <!-- Thumbnail-creation monitor: pending headline + whole-cache totals. -->
            <div class="mt-2 flex items-baseline gap-2">
              <span class="text-3xl font-bold tabular-nums text-accent-text">
                {qCount(q, "todo")}
              </span>
              <span class="text-xs text-slate-500">pending</span>
            </div>
            <div class="mt-1 text-xs text-slate-500"
              title="Average completion rate of the thumbnail queue over the last few polls and the resulting ETA to clear the current backlog. '—' until at least two samples are seen.">
              {#if ratePerMin("thumbs") != null}
                ~{Math.round(ratePerMin("thumbs") ?? 0)}/min · ETA {queueEta("thumbs", qCount(q, "todo"))}
              {:else}
                —
              {/if}
            </div>
            <div class="mt-1 text-xs text-slate-500"
              title="Thumbnails generated into the cache (rows in the thumbnail manifest) and the total bytes they occupy on disk. Failed generations show as the queue's failed count.">
              generated <b class="tabular-nums text-slate-700 dark:text-slate-200">{summary.thumbs.generated}</b>
              · {fmtBytes(summary.thumbs.bytes)}
            </div>
            {#if summary.thumbs.budget_bytes}
              {@const budgetPct = (summary.thumbs.bytes / summary.thumbs.budget_bytes) * 100}
              {@const overB = summary.thumbs.over_budget}
              <div class="mt-1 text-xs {overB ? 'text-amber-600 dark:text-amber-400' : 'text-slate-500'}"
                title="How full the ADVISORY thumbnail storage budget is (cache size ÷ budget). Passing 100% stops nothing and deletes nothing — the disk-space floors drive emergency eviction independently; to shrink the cache itself, run 'Thumbnail cache GC' in Scheduled maintenance below or disable thumbnailing for libraries that don't need it (anything removed regenerates on demand). Change the budget with FILEARR_THUMBNAIL_BUDGET_GB (0 disables): on a Proxmox deploy add e.g. FILEARR_THUMBNAIL_BUDGET_GB=50 to ~/.config/filearr/env.overrides on the host (applied every deploy), or edit /opt/filearr/.env in the CT and run 'docker compose up -d'. On Docker/Unraid set the variable on the app and worker containers.">
                budget <b class="tabular-nums">{budgetPct.toFixed(0)}%</b> of {fmtBytes(summary.thumbs.budget_bytes)}{overB ? " — over" : ""}
              </div>
              <div class="mt-1 h-1.5 w-full overflow-hidden rounded-full bg-surface-3">
                <div
                  class="h-full rounded-full {budgetPct > 100 ? 'bg-danger' : budgetPct > 80 ? 'bg-warn' : 'bg-ok'}"
                  style="width: {Math.min(100, budgetPct)}%"></div>
              </div>
            {/if}
          {/if}
          {#if q === "scan" && summary?.scan_throughput}
            <!-- Aggregate walk throughput across recent FINISHED scans. Weighted
                 (total files ÷ total walk seconds), so one tiny scoped rescan
                 cannot skew it the way averaging per-run rates would. -->
            {@const tp = summary.scan_throughput}
            <div class="mt-2 flex items-baseline gap-2">
              <span class="text-3xl font-bold tabular-nums text-accent-text">
                {tp.runs > 0 ? Math.round(tp.files_per_min).toLocaleString() : "—"}
              </span>
              <span class="text-xs text-slate-500">files/min avg</span>
            </div>
            <div class="mt-1 text-xs text-slate-500"
              title="Weighted average across finished scans in the window: total files walked ÷ total walk seconds. NOT the mean of each run's rate — that would let a 3-file rescan outweigh a 500k-file full scan. Only the directory walk counts; extraction runs out-of-band on the extract queue.">
              {#if tp.runs > 0}
                {tp.runs} run{tp.runs === 1 ? "" : "s"} · last {tp.window_days}d
              {:else}
                no finished scans in the last {tp.window_days}d
              {/if}
            </div>
            {#if tp.runs > 0}
              <div class="mt-1 text-xs text-slate-500"
                title="Total files walked and their total on-disk size across those runs, with the resulting throughput.">
                walked <b class="tabular-nums text-slate-700 dark:text-slate-200">{tp.files.toLocaleString()}</b>
                · {fmtBytes(tp.bytes)}{#if tp.bytes_per_s > 0} · {fmtRate(tp.bytes_per_s)}{/if}
              </div>
            {/if}
          {/if}
          <div class="mt-2 flex flex-wrap gap-x-3 gap-y-1 text-xs">
            <span class="text-slate-500">todo <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "todo")}</b></span>
            <span class="text-slate-500">doing <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "doing")}</b></span>
            {#if stalled > 0}
              <span class="text-amber-600 dark:text-amber-400">stalled <b class="tabular-nums">{stalled}</b></span>
            {/if}
            <span class="text-slate-500">done <b class="tabular-nums text-slate-700 dark:text-slate-200">{qCount(q, "succeeded")}</b></span>
            <span class={failed > 0 ? "text-danger" : "text-muted"}>failed <b class="tabular-nums">{failed}</b></span>
          </div>

          <!-- UI-T14: per-queue priority stepper (admin). Shows the current default
               and applies a new priority to this queue's PENDING (todo) jobs;
               running jobs keep the priority they were fetched with. -->
          <div class="mt-3 border-t border-border pt-2">
            <div class="flex items-start gap-2">
              <Field label="priority" hint="default {summary?.priorities?.[q] ?? 0}">
                <div class="flex items-center gap-1">
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label="Lower {q} priority"
                    title="lower priority (runs later)"
                    onclick={() => (prioInput[q] = clampPrio(prioValue(q) - 1))}
                    disabled={prioBusy[q]}>−</Button>
                  <div class="w-16">
                    <Input
                      size="sm"
                      class="text-center tabular-nums"
                      type="number"
                      min="-100"
                      max="100"
                      value={prioValue(q)}
                      oninput={(e) => (prioInput[q] = clampPrio(+(e.currentTarget as HTMLInputElement).value))}
                      disabled={prioBusy[q]} />
                  </div>
                  <Button
                    size="sm"
                    variant="ghost"
                    aria-label="Raise {q} priority"
                    title="higher priority (runs sooner)"
                    onclick={() => (prioInput[q] = clampPrio(prioValue(q) + 1))}
                    disabled={prioBusy[q]}>+</Button>
                </div>
              </Field>
              <Button
                size="sm"
                class="ml-auto mt-4"
                onclick={() => applyPriority(q)}
                disabled={prioBusy[q]}
                title="Apply this priority to the queue's pending (todo) jobs — running jobs are unaffected.">
                {prioBusy[q] ? "…" : `apply to ${qCount(q, "todo")}`}</Button>
            </div>
            {#if prioMsg[q]}<div class="mt-1 text-2xs text-muted">{prioMsg[q]}</div>{/if}
            {#if prioErr[q]}
              <Alert tone="danger" class="mt-1" onDismiss={() => (prioErr[q] = "")}>{prioErr[q]}</Alert>
            {/if}
          </div>

          <!-- Upcoming scheduled work for this queue (≤3 soonest). Rides the same
               poll (summary.upcoming). Omitted entirely when the queue has none. -->
          {#if summary?.upcoming?.[q]?.length}
            <div class="mt-3 border-t border-slate-100 pt-2 dark:border-slate-800">
              <div class="text-[10px] font-medium uppercase tracking-wide text-slate-600 dark:text-slate-400">Upcoming</div>
              <ul class="mt-1 space-y-0.5">
                {#each summary.upcoming[q] as u (u.label + u.at)}
                  <li
                    class="flex items-baseline justify-between gap-2 text-[11px] text-slate-500"
                    title="{u.task} — {u.label} at {new Date(u.at).toLocaleString()}">
                    <span class="truncate">{u.label}</span>
                    <span class="shrink-0 tabular-nums text-slate-600 dark:text-slate-400">{hhmm(u.at)} · {relFuture(u.at)}</span>
                  </li>
                {/each}
              </ul>
            </div>
          {/if}
        </div>
      {/each}
    </div>
    <p class="mt-2 text-xs text-slate-500">{help("job_priority")}</p>
    <!-- Meili sync strip -->
    {#if summary.meili}
      {@const m = summary.meili}
      <div class="mt-4 flex flex-wrap items-center gap-3 rounded-xl border border-border px-4 py-2 text-sm">
        <span class="font-medium">Search index</span>
        {#if !m.healthy}
          <Badge tone="danger">unreachable</Badge>
        {:else if m.in_sync}
          <Badge tone="ok">in sync</Badge>
        {:else}
          <Badge tone="warn">drift {m.drift ?? "?"}</Badge>
        {/if}
        <span class="text-muted">docs <b class="tabular-nums text-fg">{m.document_count ?? "—"}</b></span>
        <span class="text-muted">postgres active <b class="tabular-nums text-fg">{m.postgres_active}</b></span>
        {#if m.is_indexing}
          <span class="flex items-center gap-1 text-xs text-muted">
            <span class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-border-strong border-t-accent" aria-hidden="true"></span>
            indexing
          </span>
        {/if}
        {#if m.failed_tasks}
          <span title="Meilisearch tasks that failed for this index in the last 24 hours (document writes are fire-and-forget; this is the only place they surface)">
            <Badge tone="danger">{m.failed_tasks.toLocaleString()} failed Meili task{m.failed_tasks === 1 ? "" : "s"} (24h)</Badge>
          </span>
        {:else if m.failed_tasks_total}
          <span title="No Meilisearch task failed in the last 24 hours; this is old history Meili still keeps. Maintenance → 'Clear failed search-index tasks' removes the records (the index itself is untouched).">
            <Badge>{m.failed_tasks_total.toLocaleString()} old failed Meili task{m.failed_tasks_total === 1 ? "" : "s"} (history)</Badge>
          </span>
        {/if}
        {#if m.last_failed_task && m.failed_tasks}
          <div class="basis-full text-xs text-danger">
            <span class="font-medium">Latest Meili failure</span>
            ({m.last_failed_task.type ?? "task"}{m.last_failed_task.finished_at ? `, ${new Date(m.last_failed_task.finished_at).toLocaleString()}` : ""}):
            <code class="break-all">{m.last_failed_task.code ?? ""}</code>
            {m.last_failed_task.message}
          </div>
        {/if}
      </div>
    {/if}

    <!-- Agent replication (not queue jobs: independent per-agent apply lanes) -->
    {#if summary.agent_replication?.length > 0}
      <h3 class="mt-6 text-base font-semibold">Agent replication</h3>
      <p class="mt-1 text-xs text-muted">
        Inventory streaming in from remote agents. Replication is not a queue
        job — each agent applies its own seq-ordered batches through the API,
        independently and without blocking other agents or the worker queues —
        so it appears here rather than under running jobs. Applied seq is the
        agent's contiguous replication watermark — the highest change sequence
        central has durably applied.
      </p>
      <DataTable
        class="mt-2"
        caption="Agent replication"
        captionHidden
        columns={[
          { key: "name", label: "Agent", cell: replNameCell },
          { key: "seq_no", label: "Applied seq", align: "right", cell: replSeqCell },
          { key: "last_seen_at", label: "Last batch", cell: replBatchCell },
          { key: "last_reconcile_at", label: "Last reconcile", cell: replReconcileCell },
        ]}
        rows={summary.agent_replication}
        rowKey={(a) => a.id}
      />
    {/if}

    <!-- Running scans -->
    {#if summary.scans_running.length > 0}
      <h3 class="mt-6 text-base font-semibold">Running scans</h3>
      <DataTable
        class="mt-2"
        caption="Running scans"
        captionHidden
        columns={[
          { key: "library_name", label: "Library", cell: scanLibCell },
          { key: "rel_path", label: "Scope", cell: scanScopeCell },
          { key: "seen", label: "Seen", cell: scanSeenCell },
          { key: "new", label: "New", cell: scanNewCell },
          { key: "changed", label: "Changed", cell: scanChangedCell },
          { key: "excluded", label: "Excluded", align: "right", cell: scanExcludedCell },
          { key: "bytes_seen", label: "Size", align: "right", cell: scanSizeCell },
          { key: "files_per_min", label: "Files/min", align: "right", cell: scanRateCell },
        ]}
        rows={summary.scans_running}
        rowKey={(s) => s.id}
      />
      <p class="mt-2 text-xs text-muted">
        Excluded counts files the library's category/group selection or the
        exclusion presets rejected (seen + excluded = files enumerated). Size is
        the total on-disk size walked so far. Files/min is the average since the
        scan started (seen ÷ elapsed), not an instantaneous rate; seen advances in
        batches of 250, so short scans read as a staircase.
      </p>
    {/if}

    <!-- Running now -->
    <h3 class="mt-6 text-base font-semibold">Running now</h3>
    <DataTable
      class="mt-2"
      caption="Running jobs"
      captionHidden
      columns={[
        { key: "task", label: "Task", cell: runTaskCell },
        { key: "queue", label: "Queue", cell: runQueueCell },
        { key: "target", label: "Target", cell: runTargetCell },
        { key: "attempts", label: "Attempts", align: "right", cell: runAttemptsCell },
        { key: "running_for", label: "Running for", align: "right", cell: runForCell },
      ]}
      rows={summary.running}
      rowKey={(j) => j.id}
      emptyMessage="No jobs are executing right now. Queued work shows here as soon as a worker picks it up."
    />
    {#if summary.running.length > 0}
      <p class="mt-2 text-xs text-muted">
        Attempts shows tries used against each task's genuine-failure retry budget
        (attempts/cap). Extract re-queues itself while a scan is running — those waits
        are counted separately as “waiting”, not as failed tries, so a file is never
        retried past its cap after a real failure.
      </p>
    {/if}

    <!-- Failed jobs (FIX-8: paginated + clearable) -->
    <div class="mt-6 flex items-center gap-3">
      <h3 class="text-base font-semibold">Failed jobs</h3>
      {#if failuresTotal > 0}
        <span class="text-xs text-muted">{failuresTotal} total</span>
      {/if}
      <div class="grow"></div>
      {#if failuresTotal > 0}
        <Button
          size="sm"
          variant="danger"
          onclick={clearFailed}
          disabled={clearingFailed}
          title="Delete every failed job history row now. Does not affect queued or running work; failed rows also age out automatically on retention.">
          {clearingFailed ? "Clearing…" : "Clear failed history"}</Button>
      {/if}
    </div>
    {#if clearFailedError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (clearFailedError = "")}>{clearFailedError}</Alert>
    {/if}
    <DataTable
      class="mt-2"
      caption="Failed jobs"
      captionHidden
      columns={[
        { key: "task", label: "Task", cell: failTaskCell },
        { key: "queue", label: "Queue", cell: failQueueCell },
        { key: "attempts", label: "Attempts", cell: failAttemptsCell },
        { key: "attempted_at", label: "Last attempt", cell: failAttemptedCell },
        { key: "error", label: "Error", cell: failErrorCell },
      ]}
      rows={failures}
      rowKey={(j) => j.id}
      emptyMessage="No failed jobs. A job that exhausts its retries shows here with its error."
    />
    {#if failures.length > 0}
      <div class="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
        <span>
          {failuresOffset + 1}–{Math.min(failuresOffset + FAILED_PAGE, failuresTotal)} of {failuresTotal}
        </span>
        <Button size="sm" onclick={() => failuresPage(-1)} disabled={failuresOffset === 0}>Prev</Button>
        <Button
          size="sm"
          onclick={() => failuresPage(1)}
          disabled={failuresOffset + FAILED_PAGE >= failuresTotal}>Next</Button>
        <span class="grow"></span>
        <span>Error text isn't stored in the queue DB (a known Procrastinate limitation) — check the worker logs for tracebacks.</span>
      </div>
      {#if failuresError}
        <Alert tone="danger" class="mt-2" onDismiss={() => (failuresError = "")}>{failuresError}</Alert>
      {/if}
      <p class="mt-2 text-xs text-muted">
        Attempts counts tries used against the task's genuine-failure retry budget
        (attempts/cap); a large legacy number can include reschedules/requeues, not
        repeated failures. Error is the sanitized failure message recorded per
        attempt; expand it for the traceback. Pre-middleware failures show — (worker
        logs only).
      </p>
    {/if}
  {/if}
  <p class="mt-2 text-xs text-muted">{help("job_history_retention")}</p>

  <!-- Scheduled maintenance: the worker's housekeeping registry, one table
       per category. Hover a task name for what it does; editable rows accept
       a cron override / disable (applied by the next scheduler tick, no
       restart) in an editor below their table; Run now defers the task
       immediately. -->
  {#if maint.length > 0}
    <h3 class="mt-6 text-base font-semibold">Scheduled maintenance</h3>
    <p class="mt-1 text-xs text-muted">
      Housekeeping the worker runs on a schedule. Hover a task for what it does.
      Cleanup and integrity schedules are editable (cron, five fields, UTC) and
      apply within a minute — no restart. Monitors and system ticks are fixed by
      design.
    </p>
    {#each MAINT_CATS as [cat, catLabel] (cat)}
      {@const rows = maint.filter((t) => t.category === cat)}
      {#if rows.length > 0}
        <h4 class="mt-4 text-2xs font-medium uppercase tracking-wide text-muted">{catLabel}</h4>
        <DataTable
          class="mt-1"
          caption="{catLabel} maintenance tasks"
          captionHidden
          columns={[
            { key: "title", label: "Task", cell: maintTaskCell },
            { key: "cron", label: "Schedule", cell: maintScheduleCell },
            { key: "next_run_at", label: "Next run", cell: maintNextCell },
            { key: "last_run", label: "Last run", cell: maintLastCell },
            { key: "actions", label: "Actions", align: "right", cell: maintActionsCell },
          ]}
          {rows}
          rowKey={(t) => t.key}
        />
        {#each rows as t (t.key)}
          {#if editKey === t.key}
            <section
              id={cronPanelId(t.key)}
              tabindex="-1"
              aria-label="Edit schedule — {t.title}"
              class="mt-2 rounded-control border border-border bg-surface-2 px-3 py-2">
              <div class="flex flex-wrap items-end gap-2">
                <Field
                  label="cron (UTC) — {t.title}"
                  hint="five fields: min hour day month weekday — e.g. “0 4 * * *” = daily 04:00 UTC">
                  <Input size="sm" class="font-mono" bind:value={editCron} placeholder={t.default_cron ?? ""} />
                </Field>
                <Button
                  size="sm"
                  onclick={() => maintSave(t, { cron: editCron.trim() || null })}
                  disabled={maintBusy[t.key]}>Save</Button>
                {#if t.overridden}
                  <Button
                    size="sm"
                    onclick={() => maintSave(t, { cron: null })}
                    disabled={maintBusy[t.key]}
                    title="Drop the override and return to the default schedule ({t.default_cron}).">Reset to default</Button>
                {/if}
                <Button
                  size="sm"
                  onclick={() => maintSave(t, { enabled: !t.enabled })}
                  disabled={maintBusy[t.key]}
                  title={t.enabled ? "Stop scheduling this task (it can still be run manually)." : "Resume the schedule."}>
                  {t.enabled ? "Disable schedule" : "Enable schedule"}</Button>
              </div>
            </section>
          {/if}
          {#if maintErr[t.key]}
            <Alert tone="danger" class="mt-2" onDismiss={() => (maintErr[t.key] = "")}>{t.title}: {maintErr[t.key]}</Alert>
          {/if}
        {/each}
      {/if}
    {/each}
  {/if}

  <!--
    BK-T3 backups. The honest caveat is rendered ABOVE the button, not in a
    tooltip and not only in the manual: an operator who believes this button
    produces a disaster-recovery backup will discover otherwise at the worst
    possible moment. `incomplete_note` comes from the backend
    (filearr.backup.INCOMPLETE_NOTE) so the wording here can never drift from
    the wording in the manifest inside the bundle.
  -->
  <h3 class="mt-6 text-base font-semibold">Backups</h3>
  <!-- Standing caveat, not news: no live role (it was role="note"). -->
  <Alert tone="warn" class="mt-2" live={false}>
    <p class="text-xs">
      <span class="font-semibold">This is not, on its own, a disaster-recovery backup.</span>
      {backupNote ||
        "A backup taken inside the container cannot include the host .env (which holds FILEARR_SECRET_KEY) or the step-ca volume. Restoring this dump under a different secret key succeeds while leaving every encrypted alert-channel secret permanently undecryptable. Copy .env and the step-ca volume separately, or run scripts/backup.sh on the host, which does both."}
    </p>
  </Alert>
  <div class="mt-2 flex flex-wrap items-center gap-3">
    <Button
      size="sm"
      onclick={backupNow}
      disabled={backupBusy}
      title="Queue a pg_dump + manifest bundle into the config volume (admin). Runs on the worker; the bundle appears below when it finishes.">
      {backupBusy ? "…" : "Back up now"}</Button>
    {#if backupDir}
      <span class="text-xs text-muted">
        {backupDir} · keeping the newest {backupKeep}
      </span>
    {/if}
    {#if backupMsg}<span class="text-xs text-muted">{backupMsg}</span>{/if}
  </div>
  {#if backupError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (backupError = "")}>{backupError}</Alert>
  {/if}
  <DataTable
    class="mt-2"
    caption="Backup bundles"
    captionHidden
    columns={[
      { key: "name", label: "Bundle", cell: bundleNameCell },
      { key: "created_at", label: "Taken", cell: bundleTakenCell },
      { key: "item_count", label: "Items", align: "right", cell: bundleItemsCell },
      { key: "bytes", label: "Size", align: "right", cell: bundleSizeCell },
      { key: "complete", label: "Covers", cell: bundleCoversCell },
      { key: "actions", label: "Actions", align: "right", cell: bundleActionsCell },
    ]}
    rows={backups}
    rowKey={(b) => b.name}
    emptyMessage="No bundles yet. Back up now to write one; bundles live on the config volume, so download them or take them off the box another way. See the manual for the full restore procedure and for what else must be backed up."
  />
  {#if downloadError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (downloadError = null)}>
      {downloadError.name}: {downloadError.message}
    </Alert>
  {/if}

  <div class="mt-6">
    <FeaturesPanel />
  </div>

  <div class="mt-6">
    <UpdatesPanel />
  </div>

  <div class="mt-6">
    <LogsPanel />
  </div>

</div>
