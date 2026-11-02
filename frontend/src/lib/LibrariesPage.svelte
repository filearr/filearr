<script lang="ts">
  import { fmtBytes, fmtDateTime, fmtUtc, relTime } from "./format";
  import { onDestroy, onMount } from "svelte";
  import { usePolling, type Poller } from "./polling";
  import { latest } from "./latest";
  import { connectSse } from "./sse";
  import {
    applyFrame, failedReason, fmtCount, fmtScanRun, isActive, isQueued as queuedState,
    lastScanCounts, mergeRunning, planStreams, rowScan, scanRunReason, scopedSummary, slowScan,
    type Live,
  } from "./libraryScanState";
  import { toast } from "./ui/toast.svelte";
  import { describeSchedule, parseCron } from "./schedule";
  import { DELETE_SUCCESS_NOTE, deleteBlockedReason, deleteImpactDetails } from "./libraryDelete";
  import {
    cancelScan, clearFailedJobs, forceClearScan, stopScan, failedJobs, libraryErrors,
    libraryHashStatus, listLibraries, listPresets, listScans, listShareMap,
    deleteImpact, deleteLibrary, errorStatus, rehashLibrary,
    retryExtracts, retryAllExtracts, scanEventsUrl, mintScanEventsToken, scanLibrary, targetedScan, getTaxonomy,
    stats as fetchStats,
    libraryStats,
    type FailedJob, type FailingItem, type Library, type LibraryDeleteImpact, type LibraryHashStatus, type LibraryStatsResponse,
    type PresetsResponse, type ScanRun, type ShareMapEntry, type TaxonomyNode,
    errorMessage,
  } from "./api";
  import LibraryEditModal from "./LibraryEditModal.svelte";
  import CustomFieldsPanel from "./CustomFieldsPanel.svelte";
  import LibraryDiagnoseModal from "./LibraryDiagnoseModal.svelte";
  import PresetsPanel from "./PresetsPanel.svelte";
  import { gotoBrowse } from "./routes";
  import { focusPanel, focusRowButton } from "./rowPanel";
  import { isAdminPrincipal, type AuthPrincipal } from "./api";
  import { Alert, Badge, Button, Checkbox, DataTable, Field, Input, Modal, StateBlock, confirm, toneForStatus, type Tone } from "./ui";

  // Libraries tab (split out of the Admin page 2026-08-19): everything about
  // the catalog's libraries — the list/scan controls, agent libraries, custom
  // fields, scan presets, recent scans and failed jobs. Users / roles / keys /
  // sessions / audit live on the Admin tab. `me` is the session principal
  // (null when auth is disabled); it gates the admin-only Presets panel.
  let { me = null }: { me?: AuthPrincipal | null } = $props();
  const isAdmin = $derived(isAdminPrincipal(me));

  let libraries = $state<Library[]>([]);
  // First-load flag: the libraries query joins scan/stat data and can take a
  // few seconds on a large catalog — until it lands the table must say
  // "loading", never "no libraries yet" (which reads as data loss).
  let loaded = $state(false);
  // Agent-owned libraries are REPLICATED in — central never scans them, so
  // they get their own section (agent identity + replication freshness)
  // instead of rendering in the scan table with controls that only 422 and a
  // forever-empty "Last scan" column.
  const centralLibraries = $derived(libraries.filter((l) => !l.source_agent_id));
  const agentLibraries = $derived(libraries.filter((l) => !!l.source_agent_id));

  // Hash staleness (per library): items whose stored hashes were computed by an
  // older algorithm (provenance scheme mismatch). Non-zero counts surface a
  // prompt offering the LIGHT re-hash — hashes only, no metadata extraction /
  // thumbnails / embeddings — as the cheap alternative to a full rescan.
  let hashStatus = $state<Record<string, LibraryHashStatus>>({});
  let rehashing = $state<Record<string, boolean>>({});
  const staleHashRows = $derived(Object.values(hashStatus).filter((r) => r.stale > 0));
  const staleHashTotal = $derived(staleHashRows.reduce((n, r) => n + r.stale, 0));

  async function runRehash(id: string) {
    if (rehashing[id]) return;
    rehashing[id] = true;
    rowError = "";
    try {
      await rehashLibrary(id);
      if (hashStatus[id]) hashStatus[id] = { ...hashStatus[id], rehash_pending: true };
    } catch (e) {
      rowError = errorMessage(e, { verb: "re-hash this library" });
    } finally {
      rehashing[id] = false;
    }
  }

  // Catalog footprint (file count + bytes per library, catalog totals). One
  // grouped aggregate over `items` — loaded once on mount + on demand via the
  // refresh button, NOT on the 30s safety poll (a million-row sum every 30s is
  // real DB load for a number that changes only when scans/purges run).
  let libStats = $state<LibraryStatsResponse | null>(null);
  let libStatsBusy = $state(false);
  const statsByLib = $derived(
    new Map((libStats?.libraries ?? []).map((r) => [r.library_id, r])),
  );

  async function loadLibStats() {
    if (libStatsBusy) return;
    libStatsBusy = true;
    try {
      libStats = await libraryStats();
    } catch {
      libStats = null; // transient failure — columns show "—"
    } finally {
      libStatsBusy = false;
    }
  }

  function statCell(libId: string, kind: "count" | "bytes"): string {
    const row = statsByLib.get(libId);
    if (!row) return "—";
    return kind === "count" ? row.file_count.toLocaleString() : fmtBytes(row.total_bytes);
  }

  function statTitle(libId: string): string {
    const row = statsByLib.get(libId);
    if (!row) return "";
    const parts = [
      `${row.file_count.toLocaleString()} active files, ${fmtBytes(row.total_bytes)}`,
    ];
    if (row.sidecar_count) parts.push(`${row.sidecar_count.toLocaleString()} linked sidecars`);
    if (row.missing_count) parts.push(`${row.missing_count.toLocaleString()} missing (tombstoned)`);
    if (row.trashed_count) parts.push(`${row.trashed_count.toLocaleString()} trashed (recycle bin)`);
    return parts.join(" · ");
  }

  // The part of statTitle beyond the count/size the cell already shows, as
  // visible text under the count (a title tooltip alone never reaches touch or
  // keyboard users).
  function statExtras(libId: string): string {
    const row = statsByLib.get(libId);
    if (!row) return "";
    const parts: string[] = [];
    if (row.sidecar_count) parts.push(`${row.sidecar_count.toLocaleString()} sidecars`);
    if (row.missing_count) parts.push(`${row.missing_count.toLocaleString()} missing`);
    if (row.trashed_count) parts.push(`${row.trashed_count.toLocaleString()} trashed`);
    return parts.join(" · ");
  }

  // The active (running/stopping) runs, from GET /scans?status=running,stopping
  // so a running full scan never falls out of a recent-runs window (B9).
  let running = $state<ScanRun[]>([]);
  // Errors by origin: the snapshot load (StateBlock/Alert at the table), row
  // actions (scan, re-hash, retry: an Alert under the table), the two bulk
  // actions and delete (each by its own control), and per-library failing-item
  // loads (inside that library's panel).
  let loadError = $state("");
  let rowError = $state("");
  let retryAllError = $state("");
  let clearFailedError = $state("");
  // Delete refusals/failures per library, beside that row's actions (B17).
  let deleteError = $state<Record<string, string>>({});
  let failingError = $state<Record<string, string>>({});
  // /stats (error counts + degraded map) and the failed-jobs page load on
  // mount + the refresh button; their failures are shown, not swallowed (B53).
  let statsError = $state("");
  let failedJobsError = $state("");
  // Stop / Cancel / Force-clear failures, per library, beside its actions (B12).
  let scanActionError = $state<Record<string, string>>({});
  let scanActing = $state<Record<string, boolean>>({});
  // The SSE `error` frame's detail per scan id (Ruling 6: preferred reason).
  let sseDetail = $state<Record<string, string>>({});
  let busy = $state<Record<string, boolean>>({});

  // T11 error surfacing: live per-library extraction-error counts (from /stats),
  // an expandable per-library failing-items list, and a failed-jobs table.
  let errorCounts = $state<Record<string, number>>({});
  // /stats bounds each of its aggregates and reports the ones it had to give up
  // on (2026-08-11). Without this the extraction-error column would read as a
  // confident "0 errors" when the truth is "we could not count them" — the two
  // demand opposite reactions from an operator.
  let statsDegraded = $state<Record<string, string>>({});
  let retrying = $state<Record<string, boolean>>({});
  let expanded = $state<Record<string, boolean>>({});
  let failing = $state<Record<string, FailingItem[]>>({});
  // B35: the per-library list pages by `offset`; total = the library's count.
  const FAILING_PAGE = 50;
  let failingTotal = $state<Record<string, number>>({});
  let failingMore = $state<Record<string, boolean>>({});
  const failingSeq: Record<string, number> = {};
  // FIX-8: paginated failed-jobs table (used to grow unbounded on screen).
  const FAILED_PAGE = 25;
  let failedJobsList = $state<FailedJob[]>([]);
  let failedTotal = $state(0);
  let failedOffset = $state(0);
  let clearingFailed = $state(false);

  // P2-T5 catalogue (read-only), passed to the edit modal's indexing sections.
  let presetsMeta = $state<PresetsResponse>({ presets: [] });

  // UI-T1 modal target, and the library whose delete request is in flight.
  let editing = $state<Library | null>(null);
  let deletingId = $state<string | null>(null);

  // W9 targeted-scan dialog: rescan a specific file/folder path under a library
  // (automation-style — the path need not appear in the catalog yet, but must
  // exist on disk; a 404 is surfaced inline). recursive is ignored for a file.
  let targeting = $state<Library | null>(null);
  let tgtPath = $state("");
  let tgtRecursive = $state(true);
  let tgtBusy = $state(false);
  let tgtMsg = $state<{ ok: boolean; text: string } | null>(null);

  function openTargeted(lib: Library) {
    targeting = lib;
    tgtPath = "";
    tgtRecursive = true;
    tgtMsg = null;
  }

  async function submitTargeted(e: Event) {
    e.preventDefault();
    if (!targeting || tgtBusy) return;
    tgtBusy = true;
    tgtMsg = null;
    const target = tgtPath.trim();
    try {
      const r = await targetedScan(targeting.id, { path: target, recursive: tgtRecursive });
      tgtMsg = {
        ok: true,
        text: r.coalesced
          ? `Already scanning "${target || "whole library"}" — joined it.`
          : `Queued scan of "${target || "whole library"}"${r.is_file ? " (file)" : ""}.`,
      };
      // Same deferred-job race as runScan: only mark the row queued when a NEW
      // run was created. A coalesced request joined a run that is already live,
      // and the row should keep showing that run's progress.
      if (!r.coalesced) {
        queued = { ...queued, [targeting.id]: Date.now() };
        void pollUntilStarted(targeting.id);
      }
      await refresh();
    } catch (err) {
      tgtMsg = { ok: false, text: errorMessage(err, { verb: "scan these paths" }) };
    } finally {
      tgtBusy = false;
    }
  }

  // Add library: the SAME modal as Edit, opened with library=null, so the page
  // carries one button instead of a half-copy of the edit form (and every option
  // Edit has, Add has — the inline form had drifted to a subset).
  let adding = $state(false);
  // Library diagnosis dialog (2026-08-16): "why is this library red?".
  let diagnosing = $state<Library | null>(null);
  // OPS-T7: deploy mount map — the add dialog previews the auto share_prefix a
  // new library root would inherit (a placeholder/hint, not a stored value).
  let shareMap = $state<ShareMapEntry[]>([]);
  // W8: the file taxonomy tree (category -> group -> extensions) drives both the
  // add-library gating selector and (passed down) the edit modal's. Fetched once
  // on mount; an empty tree (service offline) degrades to "all types".
  let taxonomyTree = $state<TaxonomyNode[]>([]);

  // Live scan progress, keyed by scan id: the running list's persisted stats
  // plus SSE frames (rate/elapsed only arrive over SSE). libraryScanState.ts.
  let live = $state<Record<string, Live>>({});

  // One stream owner per streamed scan id (sse.ts). The entry stays while the
  // helper backs off after a drop, so the poll can't open a second stream and
  // bypass the backoff (B43). `ended`: ids whose stream already delivered its
  // terminal frame — never reopened while the running list is still stale.
  const streams = new Map<string, AbortController>();
  const ended = new Set<string>();

  // B15: the 30 s poll reads only /libraries and the running set. Heavy
  // aggregates (/stats, hash-status, catalog footprint, failed jobs) load on
  // mount and via the refresh button; the share map once on mount.
  // Last-request-wins: a newer refresh supersedes an older one.
  const snapshot = latest((signal) =>
    Promise.all([
      listLibraries(signal),
      listScans({ status: "running,stopping", limit: 500 }, signal),
    ]),
  );

  // Set in onDestroy: every continuation that resumes after an await checks it,
  // so nothing (in particular no EventSource) is opened after unmount.
  let destroyed = false;

  async function refresh() {
    if (destroyed) return;
    try {
      const r = await snapshot.run();
      if (r === undefined || destroyed) return;
      loadError = "";
      const [libs, act] = r;
      libraries = libs;
      loaded = true;
      running = act;
      // UI-T8: fold the running rows into `live` on EVERY refresh so counters
      // advance even when every SSE stream is dead; ended runs drop out.
      live = mergeRunning(live, act);
      syncStreams();
    } catch (e) {
      loadError = errorMessage(e);
    }
  }

  const statsLoader = latest((signal) => fetchStats(signal));
  async function loadStats() {
    try {
      const st = await statsLoader.run();
      if (st === undefined) return;
      statsError = "";
      errorCounts = (st.extract_errors as Record<string, number>) ?? {};
      statsDegraded = (st.degraded as Record<string, string>) ?? {};
    } catch (e) {
      statsError = errorMessage(e);
    }
  }

  const hashLoader = latest((signal) => libraryHashStatus(signal));
  async function loadHashStatus() {
    try {
      const hs = await hashLoader.run();
      if (hs) hashStatus = Object.fromEntries(hs.map((r) => [r.library_id, r]));
    } catch {
      /* non-fatal: the stale-hash prompt just doesn't show */
    }
  }

  const failedLoader = latest((signal, offset: number) => failedJobs(FAILED_PAGE, offset, signal));
  async function loadFailedJobs() {
    try {
      const jobs = await failedLoader.run(failedOffset);
      if (jobs === undefined) return;
      failedJobsError = "";
      failedJobsList = jobs.items;
      failedTotal = jobs.total;
      // Clamp the offset if rows aged out / were cleared under us.
      if (failedOffset > 0 && failedOffset >= failedTotal) {
        failedOffset = Math.max(0, (Math.ceil(failedTotal / FAILED_PAGE) - 1) * FAILED_PAGE);
        void loadFailedJobs();
      }
    } catch (e) {
      failedJobsError = errorMessage(e, { verb: "load failed jobs" });
    }
  }

  // The refresh button: every heavy aggregate in parallel (B36: nothing is
  // serialized behind the per-library error lists).
  let aggregatesBusy = $state(false);
  async function refreshAggregates() {
    if (aggregatesBusy) return;
    aggregatesBusy = true;
    try {
      await Promise.all([
        loadLibStats(),
        loadStats(),
        loadHashStatus(),
        loadFailedJobs(),
        ...Object.keys(expanded).filter((id) => expanded[id]).map((id) => loadFailing(id)),
      ]);
    } finally {
      aggregatesBusy = false;
    }
  }

  /** Open streams for active runs; abort streams whose run ended. */
  function syncStreams() {
    if (destroyed) return;
    const activeIds = running.filter((s) => isActive(s.status)).map((s) => s.id);
    const activeSet = new Set(activeIds);
    for (const id of [...ended]) if (!activeSet.has(id)) ended.delete(id);
    const plan = planStreams(activeIds, streams.keys(), ended);
    for (const id of plan.close) closeStream(id);
    for (const id of plan.open) openStream(id);
  }

  function closeStream(id: string) {
    streams.get(id)?.abort();
    streams.delete(id);
  }

  function openStream(id: string) {
    if (destroyed) return;
    const ctl = new AbortController();
    streams.set(id, ctl);
    connectSse({
      // A fresh single-use token per (re)connect; a failed mint tries tokenless.
      mint: async () => (await mintScanEventsToken(id)).token,
      url: (token) => scanEventsUrl(id, token),
      onFrame: (d) => {
        live = applyFrame(live, id, d);
      },
      onTerminal: (t) => {
        if (streams.get(id) === ctl) streams.delete(id);
        ended.add(id);
        // A failed scan's `error` frame carries the reason (B11, Ruling 6).
        if (t.kind === "error" && t.detail) sseDetail = { ...sseDetail, [id]: t.detail };
        if (t.kind === "done") live = applyFrame(live, id, t.data);
        // One authoritative refresh so the row shows the persisted last scan.
        void refresh();
        if (historyOpen) void loadHistory();
      },
      signal: ctl.signal,
    });
  }

  // Recent scans: fetched on demand (section opened / its Refresh), not polled.
  const HISTORY_LIMIT = 20;
  let historyOpen = $state(false);
  let history = $state<ScanRun[] | null>(null);
  let historyError = $state("");
  let historyBusy = $state(false);
  const historyLoader = latest((signal) => listScans({ limit: HISTORY_LIMIT }, signal));
  async function loadHistory() {
    historyBusy = true;
    try {
      const r = await historyLoader.run();
      if (r === undefined) return;
      historyError = "";
      history = r;
    } catch (e) {
      historyError = errorMessage(e, { verb: "load recent scans" });
    } finally {
      historyBusy = false;
    }
  }

  function toggleHistory() {
    historyOpen = !historyOpen;
    if (historyOpen) void loadHistory();
  }

  // First page (`more` false) or the next page appended (B35). A per-library
  // sequence drops a response a newer load superseded.
  async function loadFailing(libId: string, more = false) {
    const seq = (failingSeq[libId] = (failingSeq[libId] ?? 0) + 1);
    const offset = more ? (failing[libId]?.length ?? 0) : 0;
    failingMore = { ...failingMore, [libId]: more };
    try {
      const res = await libraryErrors(libId, FAILING_PAGE, offset);
      if (seq !== failingSeq[libId]) return;
      failing = { ...failing, [libId]: more ? [...(failing[libId] ?? []), ...res.items] : res.items };
      failingTotal = { ...failingTotal, [libId]: res.count };
      errorCounts = { ...errorCounts, [libId]: res.count };
      if (failingError[libId]) {
        const { [libId]: _cleared, ...rest } = failingError;
        failingError = rest;
      }
    } catch (e) {
      if (seq !== failingSeq[libId]) return;
      failingError = { ...failingError, [libId]: errorMessage(e, { verb: "load failing items" }) };
    } finally {
      if (seq === failingSeq[libId]) failingMore = { ...failingMore, [libId]: false };
    }
  }

  // The failing-items and last-scan panels render below the tables; opening
  // one focuses it, its Hide button returns focus to the row (rowPanel.ts).
  async function toggleErrors(libId: string) {
    const open = !expanded[libId];
    expanded = { ...expanded, [libId]: open };
    if (open) void focusPanel(`failing-${libId}`);
    if (open && !failing[libId]) await loadFailing(libId);
  }

  function hideErrors(libId: string) {
    expanded = { ...expanded, [libId]: false };
    void focusRowButton("data-failing-for", libId);
  }

  // FIX-8: failed-jobs pager + manual clear.
  async function failedPage(delta: number) {
    const next = failedOffset + delta * FAILED_PAGE;
    failedOffset = Math.max(0, Math.min(next, Math.max(0, failedTotal - 1)));
    await loadFailedJobs();
  }

  async function clearFailed() {
    if (clearingFailed) return;
    const ok = await confirm({
      title: "Clear failed job history?",
      message: `Clear all ${fmtCount(failedTotal)} failed job record(s)?`,
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
      failedOffset = 0;
      await loadFailedJobs();
    } catch (e) {
      clearFailedError = errorMessage(e, { verb: "clear failed items" });
    } finally {
      clearingFailed = false;
    }
  }

  let retryingAll = $state(false);
  const errorTotal = $derived(Object.values(errorCounts).reduce((a, b) => a + b, 0));
  // B36: the counts could not be read (degraded aggregate or a failed /stats):
  // "Retry all failed" must stay reachable exactly then.
  const errorsUnknown = $derived(!!statsDegraded.extract_errors || !!statsError);

  async function retryAllFailed() {
    if (retryingAll) return;
    const ok = await confirm({
      title: "Retry all failed items?",
      message: errorTotal > 0
        ? `Re-extract all ${fmtCount(errorTotal)} failed item(s) across every library?`
        : "Re-extract every failed item across every library? (The current count could not be read.)",
      details: [
        "Errors are cleared and the items re-queued",
        "Anything still broken will re-record its error",
      ],
      confirmLabel: "Retry all",
    });
    if (!ok) return;
    retryingAll = true;
    retryAllError = "";
    try {
      const r = await retryAllExtracts();
      toast(`Requeued ${fmtCount(r.retried)} item${r.retried === 1 ? "" : "s"} for extraction.`, { tone: "ok" });
      failing = {};
      failingTotal = {};
      await Promise.all([
        loadStats(),
        ...Object.keys(expanded).filter((id) => expanded[id]).map((id) => loadFailing(id)),
      ]);
    } catch (e) {
      retryAllError = errorMessage(e, { verb: "retry failed items" });
    } finally {
      retryingAll = false;
    }
  }

  async function retryFailed(libId: string) {
    retrying[libId] = true;
    rowError = "";
    try {
      const r = await retryExtracts(libId);
      const name = libraries.find((l) => l.id === libId)?.name ?? "this library";
      toast(`Requeued ${fmtCount(r.retried)} item${r.retried === 1 ? "" : "s"} in ${name}.`, { tone: "ok" });
      await loadFailing(libId);
    } catch (e) {
      rowError = errorMessage(e, { verb: "retry failed items" });
    } finally {
      retrying[libId] = false;
    }
  }

  // Stop / Cancel / Force-clear (B12): the abort asks first; any failure (403
  // read-only key, 404, 409 "not running" / "still active; use stop") shows
  // the server's reason beside that row's actions.
  async function scanAction(lib: Library, scan: ScanRun, kind: "stop" | "cancel" | "force") {
    if (scanActing[lib.id]) return;
    if (kind === "cancel") {
      const ok = await confirm({
        title: "Cancel this scan?",
        message: `Abort the running scan of ${lib.name} immediately?`,
        details: [
          "The in-flight batch is discarded",
          "Stop (keep progress) keeps everything scanned so far instead",
        ],
        confirmLabel: "Cancel scan",
        cancelLabel: "Keep scanning",
        tone: "danger",
      });
      if (!ok) return;
    }
    scanActing = { ...scanActing, [lib.id]: true };
    const { [lib.id]: _prev, ...rest } = scanActionError;
    scanActionError = rest;
    try {
      if (kind === "stop") await stopScan(scan.id);
      else if (kind === "cancel") await cancelScan(scan.id);
      else await forceClearScan(scan.id);
    } catch (e) {
      const verb = kind === "stop" ? "stop this scan" : kind === "cancel" ? "cancel this scan" : "force-clear this scan";
      scanActionError = { ...scanActionError, [lib.id]: errorMessage(e, { verb }) };
    } finally {
      scanActing = { ...scanActing, [lib.id]: false };
    }
    void refresh();
  }

  // Optimistic scan feedback. POST /libraries/{id}/scan only DEFERS a job — the
  // ScanRun row is created later, by the worker — so refresh() immediately after
  // the click usually still returns the PREVIOUS scan and the button looks like
  // it did nothing (the safety poll is 30s away). Record the request time on
  // click and let the row report "queued" until its own run appears.
  let queued = $state<Record<string, number>>({});

  // True from the moment a scan is requested until its ScanRun row exists
  // (libraryScanState.isQueued: an active run, or any run that started after
  // the request, clears it).
  function isQueued(libId: string): boolean {
    const lib = libraries.find((l) => l.id === libId);
    if (!lib) return false;
    return queuedState(queued[libId], rowScan(running, libId), lib);
  }

  // The worker normally picks the job up within a second or two. Poll a short
  // decaying burst so the row flips from "queued" to live progress promptly
  // rather than sitting there until the 30s safety poll. Fire-and-forget: the
  // button is already re-enabled, so this never blocks another click.
  async function pollUntilStarted(libId: string) {
    for (const delay of [800, 1500, 3000, 5000, 8000]) {
      if (destroyed || !isQueued(libId)) return;
      await new Promise((r) => setTimeout(r, delay));
      if (destroyed) return;
      await refresh();
    }
  }

  async function runScan(lib: Library) {
    busy[lib.id] = true;
    rowError = "";
    queued = { ...queued, [lib.id]: Date.now() }; // reassign, don't mutate
    try {
      const res = await scanLibrary(lib.id);
      if (res.already_running) {
        // Nothing new was enqueued, so there is no "queued" job to wait for (B8).
        const { [lib.id]: _dup, ...rest } = queued;
        queued = rest;
        toast(`A scan is already running for ${lib.name}.`, { tone: "info" });
        return;
      }
      await refresh();
    } catch (e) {
      rowError = errorMessage(e, { verb: "scan this library" });
      // The enqueue itself failed, so nothing is pending — drop the marker
      // rather than leaving a "queued" row that will never start.
      const { [lib.id]: _failed, ...rest } = queued;
      queued = rest;
      return;
    } finally {
      busy[lib.id] = false;
    }
    void pollUntilStarted(lib.id);
  }

  // After a successful add, close the dialog and reconcile.
  function afterAdd() {
    adding = false;
    void refresh();
    void refreshAggregates();
  }

  // Which libraries have their last-scan details panel open (below the table:
  // the exclusion breakdown and the "slower than usual" verdict). A native
  // `title=` tooltip only appears after a hover delay and ignores clicks, so the
  // badge looked broken; the breakdown is now click-to-open (title kept as a
  // bonus for hover users).
  let excludedOpen = $state<Record<string, boolean>>({});
  // Two controls open the panel (the slow badge, the excluded count), each with
  // its own data attribute; Hide returns focus to whichever opened it.
  type DetailsOpener = "data-scan-details-for" | "data-excluded-for";
  let excludedOpener = $state<Record<string, DetailsOpener>>({});

  function toggleExcluded(libId: string, opener: DetailsOpener) {
    excludedOpen = { ...excludedOpen, [libId]: !excludedOpen[libId] };
    if (excludedOpen[libId]) {
      excludedOpener = { ...excludedOpener, [libId]: opener };
      void focusPanel(`scan-details-${libId}`);
    }
  }

  function hideExcluded(libId: string) {
    excludedOpen = { ...excludedOpen, [libId]: false };
    void focusRowButton(excludedOpener[libId] ?? "data-excluded-for", libId);
  }

  // Failing-item kinds: chip tone and the explanation shown once per panel.
  const KIND_TONE: Record<string, Tone> = { dependency: "danger", guard: "info", corrupt: "neutral" };
  const kindTone = (k: string): Tone => KIND_TONE[k] ?? "warn";
  function kindTip(k: string): string {
    return k === "dependency"
      ? "Deployment bug: a python module is missing from the server image — fix the deployment, not the file"
      : k === "guard"
        ? "An intentional resource ceiling rejected this file (tunable via FILEARR_* limits)"
        : k === "corrupt"
          ? "The file itself is malformed/truncated — the parser rejected its bytes"
          : "I/O or unexpected failure (also shown for errors recorded before classification existed)";
  }
  const kindsOf = (items: FailingItem[]) => [...new Set(items.map((f) => f.kind))];

  // Structured breakdown for the click-to-open panel — one cause per line, so it
  // stays readable. excludedDetail() below flattens the same facts for `title`.
  function excludedLines(ls: NonNullable<Library["last_scan"]>): string[] {
    const out: string[] = [];
    if (ls.excluded_gate)
      out.push(`${ls.excluded_gate.toLocaleString()} rejected by this library's category/group selection.`);
    if (ls.excluded_filtered)
      out.push(`${ls.excluded_filtered.toLocaleString()} rejected by exclusion presets/globs (system files, hidden dotfiles, …).`);
    if (ls.pruned_counted && ls.pruned_files)
      out.push(`${ls.pruned_files.toLocaleString()} inside ${ls.pruned_dirs ?? 0} pruned folder(s). seen + excluded + pruned now accounts for every file on disk.`);
    else if (ls.pruned_dirs)
      out.push(`${ls.pruned_dirs} folder(s) were pruned and their contents were NOT counted, so this total is a lower bound — expect your OS to report more files. Turn on “Count files in pruned folders” in this library's settings to make it reconcile exactly.`);
    if (ls.pruned_paths?.length)
      out.push(`Pruned: ${ls.pruned_paths.slice(0, 8).join(", ")}${ls.pruned_paths.length > 8 ? ", …" : ""}`);
    if (ls.permission_denied)
      out.push(`${ls.permission_denied} folder(s) could not be read at all — contents not counted (same caveat).`);
    return out;
  }

  // Why the scan skipped files. Rendered as its own badge (not folded into
  // lastScanCounts) because it is the number that explains a gap between an OS
  // folder count and the library total — `seen` alone never could.
  function excludedDetail(ls: NonNullable<Library["last_scan"]>): string {
    const why: string[] = [];
    if (ls.excluded_gate)
      why.push(`${fmtCount(ls.excluded_gate)} rejected by this library's category/group selection`);
    if (ls.excluded_filtered)
      why.push(`${fmtCount(ls.excluded_filtered)} rejected by exclusion presets/globs (system files, hidden dotfiles, …)`);
    let s = why.length
      ? `${fmtCount(ls.excluded ?? 0)} file(s) enumerated but not ingested — ${why.join("; ")}.`
      : `${fmtCount(ls.excluded ?? 0)} file(s) enumerated but not ingested.`;

    // THE DISCREPANCY NOTE. Pruned/unreadable directories are skipped WITHOUT
    // being enumerated, so files inside them are counted nowhere. If the numbers
    // do not add up against the OS, this is nearly always why — so point the
    // operator straight at it, name the culprits, and name the fix.
    if (ls.pruned_counted && ls.pruned_files) {
      s += ` Plus ${ls.pruned_files.toLocaleString()} file(s) inside ${ls.pruned_dirs ?? 0} pruned director(ies), so seen + excluded + pruned = every file on disk.`;
    } else if (ls.pruned_dirs) {
      s += ` HEADS UP: ${ls.pruned_dirs} director(ies) were pruned entirely and their contents were NOT enumerated, so this count is a LOWER BOUND — expect the OS folder count to be higher.`;
      s += ` Enable "Count files in pruned folders" in this library's settings to make the numbers reconcile exactly.`;
    }
    if (ls.pruned_paths?.length)
      s += ` Pruned: ${ls.pruned_paths.slice(0, 8).join(", ")}${ls.pruned_paths.length > 8 ? ", …" : ""}.`;
    if (ls.permission_denied)
      s += ` ${ls.permission_denied} director(ies) could not be read at all (same lower-bound caveat).`;
    return s;
  }

  // After a successful edit, close the modal and reconcile everything.
  function afterEdit() {
    editing = null;
    void refresh();
  }

  // UI-T2: the product's ONE intentional hard-delete, behind a typed-name
  // confirm. B16/B17 (Ruling 7): the confirm lists what GET delete-impact says
  // the cascade removes; a queued/running scan job means the server would 409,
  // so delete is not offered (reason beside the action). An impact fetch
  // failure still allows delete, with the counts flagged as unavailable.
  // DELETE /libraries/{id}?confirm=<name>: 404 = already gone (the outcome
  // asked for), 409 = a scan is queued/running (server detail shown), 422 =
  // confirm mismatch (the typed-name gate makes it unlikely; surfaced anyway).
  function setDeleteError(id: string, msg: string) {
    const { [id]: _prev, ...rest } = deleteError;
    deleteError = msg ? { ...rest, [id]: msg } : rest;
  }

  async function removeLibrary(lib: Library) {
    if (deletingId) return;
    deletingId = lib.id;
    setDeleteError(lib.id, "");
    let impact: LibraryDeleteImpact | null = null;
    try {
      impact = await deleteImpact(lib.id);
    } catch (e) {
      if (errorStatus(e) === 404) {
        deletingId = null;
        await refresh();
        return;
      }
      impact = null;
    }
    const blocked = deleteBlockedReason(impact);
    if (blocked) {
      setDeleteError(lib.id, blocked);
      deletingId = null;
      return;
    }
    // The dialog is open while deletingId is held: no second delete starts.
    const ok = await confirm({
      title: "Delete library?",
      message: `This permanently removes ${lib.name} and everything listed below. This is a hard delete.`,
      details: deleteImpactDetails(impact),
      confirmLabel: "Delete library",
      tone: "danger",
      typedName: lib.name,
    });
    if (!ok) {
      deletingId = null;
      return;
    }
    try {
      await deleteLibrary(lib.id, lib.name);
      toast(`Deleted ${lib.name}. ${DELETE_SUCCESS_NOTE}`, { tone: "ok" });
      await refresh();
      void refreshAggregates();
    } catch (e) {
      const status = errorStatus(e);
      if (status === 404) await refresh();
      else if (status === 422) setDeleteError(lib.id, "Confirmation name did not match.");
      else setDeleteError(lib.id, errorMessage(e, { verb: "delete this library" }));
    } finally {
      deletingId = null;
    }
  }

  let safety: Poller | undefined;
  onMount(() => {
    void refresh();
    void refreshAggregates();
    listShareMap()
      .then((m) => (shareMap = m))
      .catch(() => (shareMap = []));
    listPresets()
      .then((m) => (presetsMeta = m))
      .catch((e) => (loadError = errorMessage(e)));
    // W8: taxonomy for the type-gating selectors. Non-fatal — an empty tree just
    // means the selector shows "all types" and libraries can still be created.
    getTaxonomy()
      .then((t) => (taxonomyTree = t.tree))
      .catch(() => (taxonomyTree = []));
    // Light safety-net refresh (/libraries + running set); paused while hidden.
    safety = usePolling(refresh, 30000);
  });
  onDestroy(() => {
    destroyed = true;
    safety?.stop();
    for (const l of [snapshot, statsLoader, hashLoader, failedLoader, historyLoader]) l.cancel();
    for (const id of [...streams.keys()]) closeStream(id);
  });
</script>

<!-- Cells shared by the central and agent tables. Cells render twice (table +
     card list), so none carries an id, a bind:this or focus management; the
     expandable panels render below the tables, keyed by library. -->
<!-- Name, then the operator's description (two lines at most; the full text
     is in the title). The table keeps to its frame: every free-text line here
     wraps (wrap-anywhere / break-all) instead of widening the column. -->
{#snippet nameCell(lib: Library)}
  <span class="font-medium wrap-anywhere">{lib.name}</span>
  {#if !lib.enabled}<Badge class="ml-1">off</Badge>{/if}
  {#if lib.description}
    <span class="mt-0.5 line-clamp-2 whitespace-pre-line wrap-anywhere text-xs text-muted" title={lib.description}>{lib.description}</span>
  {/if}
{/snippet}

<!-- Item count, the sidecar/extra note, then the size: one column, so the
     table has room for the rest. -->
{#snippet itemsCell(lib: Library)}
  <span class="whitespace-nowrap tabular-nums text-xs text-muted" title={statTitle(lib.id)}>{statCell(lib.id, "count")}</span>
  {#each statExtras(lib.id) ? statExtras(lib.id).split(" · ") : [] as extra}
    <span class="block whitespace-nowrap text-2xs text-muted">{extra}</span>
  {/each}
  <span class="block whitespace-nowrap tabular-nums text-2xs text-muted" title={statTitle(lib.id)}>{statCell(lib.id, "bytes")}</span>
{/snippet}

{#snippet errorsCell(lib: Library)}
  {@const ec = errorCounts[lib.id] ?? 0}
  <!-- nowrap: the count badge + retry button previously stacked, which alone
       made the whole row two lines tall. -->
  <span class="inline-flex items-center gap-1 whitespace-nowrap">
    <button
      type="button"
      class="rounded-full"
      aria-expanded={!!expanded[lib.id]}
      aria-controls={expanded[lib.id] ? `failing-${lib.id}` : undefined}
      data-failing-for={lib.id}
      aria-label="{ec} extraction error{ec === 1 ? '' : 's'}: {expanded[lib.id] ? 'hide' : 'show'} failing items"
      title={ec > 0 ? "Show failing items" : "No extraction errors"}
      onclick={() => toggleErrors(lib.id)}>
      <Badge tone={ec > 0 ? "danger" : "neutral"}>{ec}</Badge>
    </button>
    {#if ec > 0}
      <Button
        size="sm"
        variant="ghost"
        title="Clear errors and re-extract the failing items"
        disabled={retrying[lib.id]}
        onclick={() => retryFailed(lib.id)}>
        {retrying[lib.id] ? "…" : "retry"}
      </Button>
    {/if}
  </span>
{/snippet}

{#snippet commonActions(lib: Library)}
  <Button size="sm" onclick={() => gotoBrowse(lib.id, "")}>Browse</Button>
  {#if !lib.source_agent_id}
    <Button size="sm" title="Rescan a specific file or folder under this library (no full scan)" onclick={() => openTargeted(lib)}>Targeted…</Button>
  {/if}
  <Button size="sm" onclick={() => (editing = lib)}>Edit</Button>
  <Button
    size="sm"
    title="Why is this library failing? Probe the path, read the last scans, errors, jobs and logs"
    onclick={() => (diagnosing = lib)}>Diagnose</Button>
  <Button size="sm" variant="danger" disabled={deletingId !== null} onclick={() => removeLibrary(lib)}>
    {deletingId === lib.id ? "Deleting…" : "Delete"}
  </Button>
{/snippet}

{#snippet deleteErrorAlert(lib: Library)}
  {#if deleteError[lib.id]}
    <Alert tone="danger" class="mt-1 text-left" onDismiss={() => setDeleteError(lib.id, "")}>{deleteError[lib.id]}</Alert>
  {/if}
{/snippet}

<!-- Central table cells -->
{#snippet libraryCell(lib: Library)}
  <div class="min-w-[9rem]">
    {@render nameCell(lib)}
    <span class="mt-0.5 block font-mono text-2xs text-muted break-all">{lib.root_path}</span>
  </div>
{/snippet}

<!-- Scan settings in one column: schedule (cron verbatim on one line, its
     meaning in the title) and watch, then file types and hash policy only
     when they differ from the defaults ("all types", "auto"). -->
{#snippet settingsCell(lib: Library)}
  <span class="flex flex-wrap items-center gap-x-2 gap-y-0.5 text-xs text-muted">
    {#if lib.scan_cron}
      <span class="whitespace-nowrap font-mono" title={describeSchedule(parseCron(lib.scan_cron))}>{lib.scan_cron}</span>
    {:else}
      <span class="whitespace-nowrap" title="No automatic scans (manual only).">manual</span>
    {/if}
    {#if lib.watch_mode}<span class="whitespace-nowrap" title="Watch mode: changes are picked up as they happen">· watch</span>{/if}
  </span>
  {#if lib.enabled_categories?.length || lib.enabled_groups?.length}
    <span class="mt-1 flex flex-wrap gap-1">
      {#each lib.enabled_categories ?? [] as c}
        <span title="category"><Badge tone="accent"><span class="sr-only">category </span>{c}</Badge></span>
      {/each}
      {#each lib.enabled_groups ?? [] as g}
        <span title="group"><Badge><span class="sr-only">group </span>{g}</Badge></span>
      {/each}
    </span>
  {/if}
  {#if lib.hash_policy !== "auto"}
    <span class="mt-0.5 block whitespace-nowrap text-2xs text-muted">hash: {lib.hash_policy === "quick_only" ? "quick only" : lib.hash_policy}</span>
  {/if}
  {#if hashStatus[lib.id]?.stale}
    {@const hsr = hashStatus[lib.id]}
    <span class="mt-1 flex items-center gap-1 whitespace-nowrap">
      <span title="Items whose stored hashes were computed by an older algorithm">
        <Badge tone="warn">{hsr.stale.toLocaleString()} stale</Badge>
      </span>
      <Button
        size="sm"
        variant="ghost"
        title="Recompute this library's file hashes only — no metadata extraction, thumbnails or embeddings"
        disabled={hsr.rehash_pending || rehashing[lib.id]}
        onclick={() => runRehash(lib.id)}>
        {hsr.rehash_pending ? "updating…" : "update hashes"}
      </Button>
    </span>
  {/if}
{/snippet}


{#snippet lastScanCell(lib: Library)}
  {@const scan = rowScan(running, lib.id)}
  {@const reason = scan || isQueued(lib.id) ? null : failedReason(lib.last_scan, sseDetail)}
  {@const sc = lib.last_scoped_scan}
  <!-- Wraps (status, age, counts, badges flow onto further lines) so the
       column never forces the table wider than its frame; the breakdown panel
       below the table carries the long text. -->
  <span class="inline-flex min-w-[10rem] flex-wrap items-center gap-1 text-xs text-muted">
    <!-- FIX-10: a running/stopping scan is streamed live; otherwise the
         authoritative last scan comes from lib.last_scan (scan_runs DISTINCT
         ON), which survives redeploys. "never" only when null. -->
    {#if isQueued(lib.id)}
      <!-- Optimistic: the job is deferred but the worker has not created its
           ScanRun yet. Without this the click produced no visible change for
           up to 30s. -->
      <Badge tone="accent" class="gap-1">
        <span class="inline-block h-1.5 w-1.5 animate-ping rounded-full bg-current" aria-hidden="true"></span>
        queued
      </Badge>
      <span>waiting for a worker…</span>
    {:else if scan}
      {fmtScanRun(scan, live)}
      {#if scopedSummary(scan)}<Badge>{scopedSummary(scan)}</Badge>{/if}
    {:else if lib.last_scan}
      {@const ls = lib.last_scan}
      {@const slow = slowScan(ls)}
      {@const when = ls.finished_at ?? ls.started_at}
      <Badge tone={toneForStatus(ls.status)}>{ls.status}</Badge>
      <span title={fmtUtc(when)}>{relTime(when)}</span>
      {#if lastScanCounts(ls)}<span>— {lastScanCounts(ls)}</span>{/if}
      <!-- The slow verdict and the exclusion breakdown open the same details
           panel below the table (their full text no longer lives in a title). -->
      {#if slow}
        <button
          type="button"
          class="rounded-full"
          title={slow}
          aria-expanded={!!excludedOpen[lib.id]}
          aria-controls={excludedOpen[lib.id] ? `scan-details-${lib.id}` : undefined}
          data-scan-details-for={lib.id}
          onclick={() => toggleExcluded(lib.id, "data-scan-details-for")}>
          <Badge tone="warn">slower than usual</Badge>
        </button>
      {/if}
      <!-- Render when anything was skipped, INCLUDING the pruned-only case
           (excluded 0 but whole trees dropped) — that case is exactly the one
           that silently loses tens of thousands of files. The trailing "+"
           marks a lower-bound count. -->
      {#if ls.excluded || ls.pruned_dirs}
        {@const lowerBound = !!ls.pruned_dirs && !ls.pruned_counted}
        <button
          type="button"
          class="cursor-pointer text-warn underline decoration-dotted underline-offset-2 hover:decoration-solid"
          title={excludedDetail(ls)}
          aria-expanded={!!excludedOpen[lib.id]}
          aria-controls={excludedOpen[lib.id] ? `scan-details-${lib.id}` : undefined}
          data-excluded-for={lib.id}
          onclick={() => toggleExcluded(lib.id, "data-excluded-for")}
        >excluded {(ls.excluded ?? 0).toLocaleString()}{lowerBound
            ? "+"
            : ls.pruned_files
              ? ` + ${ls.pruned_files.toLocaleString()} pruned`
              : ""}
          <span class="text-[9px]" aria-hidden="true">{excludedOpen[lib.id] ? "▲" : "▼"}</span>
        </button>
      {/if}
    {:else}
      never scanned
    {/if}
  </span>
  <!-- Ruling 6: a failed full scan says why on the row (SSE detail, else the
       recorded error / reconcile note, else "no reason recorded"). -->
  {#if reason}
    <span class="mt-0.5 block max-w-[28rem] whitespace-normal wrap-anywhere text-xs text-danger">{reason}</span>
  {/if}
  <!-- B10: the newest scoped (hot-folder / targeted) run, compactly; the main
       line above is always the newest FULL run. -->
  {#if sc && sc.id !== scan?.id}
    {@const scWhen = sc.finished_at ?? sc.started_at}
    {@const scReason = failedReason(sc, sseDetail)}
    <span class="mt-0.5 block max-w-[28rem] whitespace-normal wrap-anywhere text-2xs text-muted">
      {scopedSummary(sc)} · <span title={fmtUtc(scWhen)}>{relTime(scWhen)}</span>{#if sc.status !== "finished"}{" · "}{sc.status}{/if}{#if scReason}<span class="text-danger">{" — "}{scReason}</span>{/if}
    </span>
  {/if}
{/snippet}

{#snippet centralActionsCell(lib: Library)}
  {@const scan = rowScan(running, lib.id)}
  {@const isRunning = scan?.status === "running"}
  {@const stopping = scan?.status === "stopping"}
  <!-- Capped width: the buttons wrap onto a second row instead of widening
       the table past its frame. -->
  <div class="ml-auto flex max-w-[16rem] flex-wrap items-center justify-end gap-1">
    {#if isRunning && scan}
      <Button
        size="sm"
        disabled={scanActing[lib.id]}
        title="Finish the current batch and keep everything scanned so far, then end the scan. Skips deletion detection (unvisited files are NOT marked missing). The next scan picks up where this left off."
        onclick={() => scanAction(lib, scan, "stop")}>
        Stop (keep progress)
      </Button>
      <Button
        size="sm"
        variant="danger"
        disabled={scanActing[lib.id]}
        title="Abort the scan immediately, discarding the in-flight batch. Use Stop to keep what was already scanned."
        onclick={() => scanAction(lib, scan, "cancel")}>
        Cancel (abort)
      </Button>
    {:else if stopping && scan}
      <span title="Stop requested — finishing the current batch and wrap-up, then the scan ends as stopped.">
        <Badge tone="warn">stopping…</Badge>
      </span>
      <!-- FIX-15: escape hatch for a scan wedged in 'stopping' (worker died /
           stop never observed). Drives it terminal; the server refuses (409)
           if a live worker is still draining it. -->
      <Button
        size="sm"
        variant="danger"
        disabled={scanActing[lib.id]}
        title="Force this stuck scan to a terminal state. Use only if it has been 'stopping' for a while with no progress (its worker likely died). Refused if a live worker is still processing it."
        onclick={() => scanAction(lib, scan, "force")}>
        Force clear
      </Button>
    {/if}
    {@render commonActions(lib)}
    <Button size="sm" variant="primary" disabled={busy[lib.id] || isRunning || stopping} onclick={() => runScan(lib)}>
      {#if busy[lib.id]}
        Starting…
      {:else if isQueued(lib.id)}
        Queued
      {:else}
        Scan
      {/if}
    </Button>
  </div>
  {#if scanActionError[lib.id]}
    <Alert
      tone="danger"
      class="mt-1 text-left"
      onDismiss={() => {
        const { [lib.id]: _gone, ...rest } = scanActionError;
        scanActionError = rest;
      }}>{scanActionError[lib.id]}</Alert>
  {/if}
  {@render deleteErrorAlert(lib)}
{/snippet}

<!-- Agent table cells -->
{#snippet agentCell(lib: Library)}{lib.agent_name ?? "unknown"}{/snippet}
{#snippet agentStatusCell(lib: Library)}
  <Badge tone={toneForStatus(lib.agent_status)}>{lib.agent_status ?? "unknown"}</Badge>
{/snippet}
{#snippet lastSyncCell(lib: Library)}
  <span class="text-muted" title={lib.agent_last_seen_at ? fmtUtc(lib.agent_last_seen_at) : "the agent has never replicated"}>
    {lib.agent_last_seen_at ? relTime(lib.agent_last_seen_at) : "never"}
  </span>
{/snippet}
{#snippet lastReconcileCell(lib: Library)}
  <span class="text-muted" title={lib.agent_last_reconcile_at ? fmtUtc(lib.agent_last_reconcile_at) : "no full-manifest reconcile yet"}>
    {lib.agent_last_reconcile_at ? relTime(lib.agent_last_reconcile_at) : "never"}
  </span>
{/snippet}
{#snippet agentActionsCell(lib: Library)}
  <div class="flex flex-wrap justify-end gap-1">{@render commonActions(lib)}</div>
  {@render deleteErrorAlert(lib)}
{/snippet}

<!-- Failed-jobs cells -->
{#snippet jobQueueCell(j: FailedJob)}<span class="text-muted">{j.queue}</span>{/snippet}
{#snippet jobTaskCell(j: FailedJob)}<span class="font-mono text-xs">{j.task}</span>{/snippet}
{#snippet jobAttemptsCell(j: FailedJob)}<span class="text-muted">{j.attempts ?? "—"}</span>{/snippet}
{#snippet jobAttemptedCell(j: FailedJob)}
  <span class="text-muted" title={j.attempted_at ? fmtUtc(j.attempted_at) : undefined}>{fmtDateTime(j.attempted_at)}</span>
{/snippet}
{#snippet jobErrorCell(j: FailedJob)}
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

<!-- Expanded per-library failing-item list (below its table). -->
{#snippet failingPanel(lib: Library)}
  {@const failingLoaded = failing[lib.id] !== undefined}
  {@const items = failing[lib.id] ?? []}
  <section
    id="failing-{lib.id}"
    tabindex="-1"
    aria-label="Failing items — {lib.name}"
    class="mt-2 rounded-control border border-border bg-surface-2 px-3 py-2 text-xs"
  >
    <div class="flex items-center gap-2">
      <h3 class="text-sm font-semibold">Failing items — {lib.name}</h3>
      <span class="grow"></span>
      <Button size="sm" variant="ghost" onclick={() => hideErrors(lib.id)}>Hide</Button>
    </div>
    {#if failingError[lib.id] && !failingLoaded}
      <StateBlock state="error" compact message={failingError[lib.id]} onRetry={() => loadFailing(lib.id)} />
    {:else if !failingLoaded}
      <StateBlock state="loading" compact />
    {:else if items.length === 0}
      <StateBlock state="empty" compact message="No failing items — nothing to retry." />
    {:else}
      {@const total = failingTotal[lib.id] ?? items.length}
      <dl class="mt-1 flex flex-col gap-0.5 text-muted">
        {#each kindsOf(items) as k (k)}
          <div class="flex items-baseline gap-2">
            <dt><Badge tone={kindTone(k)} class="font-semibold uppercase tracking-wide">{k}</Badge></dt>
            <dd>{kindTip(k)}</dd>
          </div>
        {/each}
      </dl>
      <ul class="mt-2 space-y-1">
        {#each items as f (f.id)}
          <li class="flex items-baseline gap-2">
            <Badge tone={kindTone(f.kind)} class="shrink-0 font-semibold uppercase tracking-wide">{f.kind}</Badge>
            <span class="font-mono">{f.rel_path}</span>
            <span class="text-danger">— {f.error}</span>
          </li>
        {/each}
      </ul>
      <!-- B35: the badge shows the full count; the list pages by offset. -->
      <div class="mt-2 flex flex-wrap items-center gap-3 text-muted">
        <span>Showing {fmtCount(items.length)} of {fmtCount(total)}</span>
        {#if items.length < total}
          <Button size="sm" disabled={failingMore[lib.id]} onclick={() => loadFailing(lib.id, true)}>
            {failingMore[lib.id] ? "Loading…" : "Load more"}
          </Button>
        {/if}
      </div>
      {#if failingError[lib.id]}
        <Alert tone="danger" class="mt-2" onDismiss={() => {
          const { [lib.id]: _gone, ...rest } = failingError;
          failingError = rest;
        }}>{failingError[lib.id]}</Alert>
      {/if}
    {/if}
  </section>
{/snippet}

<div class="mt-4">
  <div class="flex flex-wrap items-center gap-3">
    <h2 class="text-lg font-semibold">Libraries</h2>
    {#if libStats}
      <span
        class="text-sm text-muted"
        title="Active files across every library (central + agent). Linked sidecars are included; missing/trashed tombstones are not.">
        {libStats.total_files.toLocaleString()} files · {fmtBytes(libStats.total_bytes)}
      </span>
    {/if}
    <!-- B15: the heavy aggregates are not on the 30 s poll; this reloads
         them all (catalog footprint, extraction-error counts, hash status,
         failed jobs, open failing-item lists). -->
    <Button
      size="sm"
      variant="ghost"
      aria-label="Refresh counts, sizes, errors and hash status"
      title="Refresh file counts and sizes, extraction-error counts, hash status and failed jobs (aggregate queries over the whole catalog — not polled)"
      onclick={refreshAggregates}
      disabled={aggregatesBusy}>
      <span aria-hidden="true">{aggregatesBusy ? "…" : "↻"}</span>
    </Button>
    <div class="grow"></div>
    <!-- W8: the taxonomy editor lives on its own route, reachable from here. -->
    <Button size="sm" href="#/taxonomy">Manage file taxonomy</Button>
  </div>

  <!--
    BK-T1: the key-fingerprint guard's verdict, folded into the SAME `degraded`
    map /stats already publishes. A wrong FILEARR_SECRET_KEY after a restore is
    the one failure in this stack that reports success everywhere and destroys
    data anyway — every encrypted alert-channel secret becomes unreadable, and
    the operator finds out weeks later from an alert that never arrived. So this
    banner is deliberately NOT dismissible: the condition persists until someone
    fixes the key or re-enters the secrets.
  -->
  {#each ["secret_key", "ca_root"] as fpKey (fpKey)}
    {#if statsDegraded[fpKey]}
      <Alert
        tone="danger"
        class="mt-3"
        title={fpKey === "secret_key" ? "Secret key mismatch" : "Agent CA root mismatch"}>
        <p class="mt-0.5 text-xs">{statsDegraded[fpKey]}</p>
        <p class="mt-1 text-xs">
          Fingerprints are on the <a class="underline" href="#/about">About page</a>.
        </p>
      </Alert>
    {/if}
  {/each}
  {#if errorsUnknown}
    <!-- B36: when the counts can't be read, "Retry all failed" stays here. -->
    <Alert tone="warn" class="mt-2">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span>
          Extraction-error counts unavailable ({statsDegraded.extract_errors || statsError}) — the
          Errors column below reads 0 because it could not be read, not because
          there are none.
        </span>
        <span class="flex gap-2">
          {#if statsError}<Button size="sm" onclick={loadStats}>Reload counts</Button>{/if}
          {#if errorTotal === 0}
            <Button size="sm" disabled={retryingAll} onclick={retryAllFailed}>
              {retryingAll ? "requeuing…" : "Retry all failed"}
            </Button>
          {/if}
        </span>
      </div>
    </Alert>
  {/if}
  {#if staleHashRows.length}
    <Alert tone="warn" class="mt-3" title="Stored hashes need updating">
      <p class="mt-0.5 text-xs">
        {staleHashTotal.toLocaleString()} item{staleHashTotal === 1 ? "" : "s"} across
        {staleHashRows.length} librar{staleHashRows.length === 1 ? "y" : "ies"}
        ({staleHashRows.map((r) => r.name).join(", ")}) still carry hashes computed by an
        older algorithm. Use <span class="font-medium">update hashes</span> in the Settings
        column below — it re-reads the files to refresh their hashes only (no metadata
        extraction, thumbnails or embeddings), far lighter than a full rescan. A full
        rescan also converges them, at full extract cost.
      </p>
    </Alert>
  {/if}

  {#if errorTotal > 0}
    <Alert tone="danger" class="mt-3">
      <div class="flex flex-wrap items-center justify-between gap-3">
        <div>
          <span class="font-semibold">{fmtCount(errorTotal)} item{errorTotal === 1 ? "" : "s"} failed extraction.</span>
          <span class="text-xs"> Expand a library's Errors count for details, or requeue everything at once.</span>
        </div>
        <Button size="sm" disabled={retryingAll} onclick={retryAllFailed}>
          {retryingAll ? "requeuing…" : "Retry all failed"}
        </Button>
      </div>
    </Alert>
  {/if}
  {#if retryAllError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (retryAllError = "")}>{retryAllError}</Alert>
  {/if}

  {#if loaded && loadError}
    <!-- A failed refresh keeps the rows already shown; the first load's
         failure replaces the table instead (DataTable error + Retry). -->
    <Alert tone="danger" class="mt-3" onDismiss={() => (loadError = "")}>{loadError}</Alert>
  {/if}

  <DataTable
    class="mt-3"
    caption="Libraries"
    captionHidden
    stackBelow="lg"
    columns={[
      { key: "name", label: "Library", cell: libraryCell },
      { key: "items", label: "Items", align: "right", cell: itemsCell },
      { key: "settings", label: "Settings", cell: settingsCell },
      { key: "errors", label: "Errors", cell: errorsCell },
      { key: "last_scan", label: "Last scan", cell: lastScanCell },
      // A width hint so the buttons sit two or three to a row rather than
      // one per line (the cell caps itself at 16rem and wraps beyond that).
      { key: "actions", label: "Actions", align: "right", width: "14rem", cell: centralActionsCell },
    ]}
    rows={centralLibraries}
    rowKey={(l) => l.id}
    loading={!loaded}
    error={loaded ? null : loadError || null}
    onRetry={refresh}
    emptyMessage="No libraries yet — add one below."
  />
  {#if rowError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (rowError = "")}>{rowError}</Alert>
  {/if}

  <!-- Last-scan details (slow verdict + exclusion breakdown), one panel per
       library that has it open. -->
  {#each centralLibraries as lib (lib.id)}
    {#if excludedOpen[lib.id] && lib.last_scan}
      {@const ls = lib.last_scan}
      {@const slow = slowScan(ls)}
      <section
        id="scan-details-{lib.id}"
        tabindex="-1"
        aria-label="Last scan details — {lib.name}"
        class="mt-2 rounded-control border border-warn/40 bg-warn-bg px-3 py-2 text-xs"
      >
        <div class="flex items-center gap-2">
          <h3 class="text-sm font-semibold">Last scan details — {lib.name}</h3>
          <span class="grow"></span>
          <Button size="sm" variant="ghost" onclick={() => hideExcluded(lib.id)}>Hide</Button>
        </div>
        <ul class="mt-1 max-w-3xl list-disc space-y-1 pl-5 leading-snug">
          {#if slow}<li>{slow}</li>{/if}
          {#if ls.excluded || ls.pruned_dirs}
            {#each excludedLines(ls) as line}
              <li>{line}</li>
            {/each}
          {/if}
        </ul>
      </section>
    {/if}
  {/each}
  {#each centralLibraries as lib (lib.id)}
    {#if expanded[lib.id]}{@render failingPanel(lib)}{/if}
  {/each}

  {#if agentLibraries.length > 0}
    <h2 class="mt-8 text-lg font-semibold">Agent libraries</h2>
    <p class="mt-1 text-xs text-muted">
      Replicated in by remote agents — central never scans these, so freshness is
      the owning agent's replication heartbeat (last sync) and full-manifest
      reconcile watermark, not a scan date. Manage the fleet on the
      <a class="underline" href="#/agents">Agents</a> page.
    </p>
    <DataTable
      class="mt-3"
      caption="Agent libraries"
      captionHidden
      columns={[
        { key: "name", label: "Name", cell: nameCell },
        { key: "items", label: "Items", align: "right", cell: itemsCell },
        { key: "agent_name", label: "Agent", cell: agentCell },
        { key: "agent_status", label: "Agent status", cell: agentStatusCell },
        { key: "agent_last_seen_at", label: "Last sync", cell: lastSyncCell },
        { key: "agent_last_reconcile_at", label: "Last reconcile", cell: lastReconcileCell },
        { key: "errors", label: "Errors", cell: errorsCell },
        { key: "actions", label: "Actions", align: "right", cell: agentActionsCell },
      ]}
      rows={agentLibraries}
      rowKey={(l) => l.id}
    />
    {#each agentLibraries as lib (lib.id)}
      {#if expanded[lib.id]}{@render failingPanel(lib)}{/if}
    {/each}
  {/if}

  <div class="mt-6">
    <Button variant="primary" onclick={() => (adding = true)}>Add library…</Button>
  </div>

  <CustomFieldsPanel {libraries} />
  {#if isAdmin}<PresetsPanel />{/if}

  <!-- Recent scans: on demand (open / Refresh), never polled (B15). -->
  <div class="mt-8 flex flex-wrap items-center gap-3">
    <h2 class="text-lg font-semibold">Recent scans</h2>
    <Button size="sm" variant="ghost" aria-expanded={historyOpen} aria-controls="recent-scans" onclick={toggleHistory}>
      {historyOpen ? "Hide" : "Show"}
    </Button>
    {#if historyOpen}
      <Button size="sm" disabled={historyBusy} onclick={loadHistory}>{historyBusy ? "Refreshing…" : "Refresh"}</Button>
    {/if}
  </div>
  {#if historyOpen}
    <div id="recent-scans">
      {#if historyError}
        <StateBlock state="error" compact message={historyError} onRetry={loadHistory} />
      {:else if history === null}
        <StateBlock state="loading" compact />
      {:else if history.length === 0}
        <StateBlock state="empty" compact message="No scans yet — start one with a library's Scan button." />
      {:else}
        <ul class="mt-2 divide-y divide-border text-sm">
          {#each history as s (s.id)}
            {@const lib = libraries.find((l) => l.id === s.library_id)}
            {@const why = scanRunReason(s, sseDetail)}
            <li class="flex flex-wrap items-center gap-3 py-2">
              <Badge tone={toneForStatus(s.status)}>{s.status}</Badge>
              <span class="font-medium">{lib?.name ?? s.library_id}</span>
              {#if scopedSummary(s)}<span class="font-mono text-xs text-muted">{scopedSummary(s)}</span>{/if}
              <span class="text-muted" title={fmtUtc(s.started_at)}>{fmtDateTime(s.started_at)}</span>
              <span class="grow"></span>
              <span class="text-xs text-muted">{fmtScanRun(s, live)}</span>
              {#if why}<span class="basis-full text-xs text-danger">{why}</span>{/if}
            </li>
          {/each}
        </ul>
        <p class="mt-1 text-xs text-muted">The newest {HISTORY_LIMIT} runs across all libraries.</p>
      {/if}
    </div>
  {/if}

  {#if failedJobsError}
    <Alert tone="danger" class="mt-8" onDismiss={() => (failedJobsError = "")}>
      <div class="flex flex-wrap items-center justify-between gap-3">
        <span>{failedJobsError}</span>
        <Button size="sm" onclick={loadFailedJobs}>Retry</Button>
      </div>
    </Alert>
  {/if}
  {#if failedTotal > 0}
    <div class="mt-8 flex items-center gap-3">
      <h2 class="text-lg font-semibold">Failed jobs</h2>
      <span class="text-xs text-muted">{fmtCount(failedTotal)} total</span>
      <div class="grow"></div>
      <Button
        size="sm"
        variant="danger"
        onclick={clearFailed}
        disabled={clearingFailed}
        title="Delete every failed job history row now. Does not affect queued or running work; failed rows also age out automatically on retention.">
        {clearingFailed ? "Clearing…" : "Clear failed history"}</Button>
    </div>
    {#if clearFailedError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (clearFailedError = "")}>{clearFailedError}</Alert>
    {/if}
    <DataTable
      class="mt-2"
      caption="Failed jobs"
      captionHidden
      columns={[
        { key: "queue", label: "Queue", cell: jobQueueCell },
        { key: "task", label: "Task", cell: jobTaskCell },
        { key: "attempts", label: "Attempts", cell: jobAttemptsCell },
        { key: "attempted_at", label: "Last attempt", cell: jobAttemptedCell },
        { key: "error", label: "Error", cell: jobErrorCell },
      ]}
      rows={failedJobsList}
      rowKey={(j) => j.id}
    />
    <div class="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
      <span>
        {fmtCount(failedOffset + 1)}–{fmtCount(Math.min(failedOffset + FAILED_PAGE, failedTotal))} of {fmtCount(failedTotal)}
      </span>
      <Button size="sm" onclick={() => failedPage(-1)} disabled={failedOffset === 0}>Prev</Button>
      <Button size="sm" onclick={() => failedPage(1)} disabled={failedOffset + FAILED_PAGE >= failedTotal}>Next</Button>
      <span class="grow"></span>
      <span>Expand an error for its traceback. Failures from before the error-capture middleware show "—" (worker logs only).</span>
    </div>
  {/if}
</div>

{#if editing}
  <LibraryEditModal
    library={editing}
    {presetsMeta}
    {taxonomyTree}
    onSaved={afterEdit}
    onClose={() => (editing = null)}
  />
{/if}

{#if diagnosing}
  <LibraryDiagnoseModal library={diagnosing} onClose={() => (diagnosing = null)} />
{/if}

{#if adding}
  <LibraryEditModal
    library={null}
    {presetsMeta}
    {taxonomyTree}
    {shareMap}
    onSaved={afterAdd}
    onClose={() => (adding = false)}
  />
{/if}

<!-- W9 targeted-scan dialog. Stays open after a submit so the result shows. -->
<Modal
  open={!!targeting}
  onClose={() => (targeting = null)}
  title="Targeted scan — {targeting?.name ?? ''}"
  description="Rescan one file or folder under this library. The path need not be in the catalog yet, but must exist on disk (e.g. a freshly created folder)."
  size="md"
>
  {#snippet children({ close })}
    <form class="flex flex-col gap-3" onsubmit={submitTargeted}>
      <Field label="Path under the library root (blank = whole library)">
        <Input class="font-mono" placeholder="e.g. Movies/Incoming or Movies/Incoming/x.mkv" bind:value={tgtPath} />
      </Field>
      <Checkbox label="Include subfolders" description="Ignored when the path is a file" bind:checked={tgtRecursive} />
      {#if tgtMsg}
        {#if tgtMsg.ok}
          <Alert tone="ok">{tgtMsg.text}</Alert>
        {:else}
          <Alert tone="danger" onDismiss={() => (tgtMsg = null)}>{tgtMsg.text}</Alert>
        {/if}
      {/if}
      <div class="flex justify-end gap-2">
        <Button onclick={close}>Close</Button>
        <Button type="submit" variant="primary" disabled={tgtBusy}>
          {tgtBusy ? "Queueing…" : "Scan"}
        </Button>
      </div>
    </form>
  {/snippet}
</Modal>
