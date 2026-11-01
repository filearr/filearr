<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { usePolling, type Poller } from "./polling";
  import { latest } from "./latest";
  import { toast } from "./ui/toast.svelte";
  import {
    cancelScan, clearFailedJobs, forceClearScan, stopScan, failedJobs, libraryErrors,
    libraryHashStatus, listLibraries, listPresets, listScans, listShareMap,
    deleteLibrary, errorStatus, rehashLibrary,
    retryExtracts, retryAllExtracts, scanEventsUrl, mintScanEventsToken, scanLibrary, targetedScan, getTaxonomy,
    stats as fetchStats,
    libraryStats,
    type FailedJob, type FailingItem, type Library, type LibraryHashStatus, type LibraryStatsResponse,
    type PresetsResponse, type ScanRun, type ShareMapEntry, type TaxonomyNode,
    errorMessage,
  } from "./api";
  import LibraryEditModal from "./LibraryEditModal.svelte";
  import CustomFieldsPanel from "./CustomFieldsPanel.svelte";
  import LibraryDiagnoseModal from "./LibraryDiagnoseModal.svelte";
  import PresetsPanel from "./PresetsPanel.svelte";
  import { gotoBrowse } from "./routes";
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
      libStats = null; // older backend / transient failure — columns show "—"
    } finally {
      libStatsBusy = false;
    }
  }

  function fmtBytes(b: number): string {
    if (!isFinite(b) || b <= 0) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
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

  let scans = $state<ScanRun[]>([]);
  // Errors by origin: the snapshot load (StateBlock/Alert at the table), row
  // actions (scan, re-hash, retry: an Alert under the table), the two bulk
  // actions and delete (each by its own control), and per-library failing-item
  // loads (inside that library's panel).
  let loadError = $state("");
  let rowError = $state("");
  let retryAllError = $state("");
  let clearFailedError = $state("");
  let deleteError = $state("");
  let failingError = $state<Record<string, string>>({});
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
  // FIX-8: paginated failed-jobs table (used to grow unbounded on screen).
  const FAILED_PAGE = 25;
  let failedJobsList = $state<FailedJob[]>([]);
  let failedTotal = $state(0);
  let failedOffset = $state(0);
  let clearingFailed = $state(false);

  // P2-T5 catalogue (read-only), passed to the edit modal's indexing sections.
  let presetsMeta = $state<PresetsResponse>({ presets: [], extension_groups: [] });

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

  // Live scan progress, keyed by scan id, delivered over SSE. Merged over the
  // scan rows so the table shows batch counter + files/s ticking in real time.
  type Live = { status: string; seen?: number; new?: number; changed?: number;
    missing?: number; rate?: number; elapsed?: number;
    // Files enumerated but skipped, streamed live so a mis-scoped library is
    // obvious while the scan runs rather than only after it finishes.
    excluded?: number; bytes_seen?: number };
  let live = $state<Record<string, Live>>({});

  // One EventSource per actively-streamed scan id, plus reconnect backoff state.
  const streams = new Map<string, EventSource>();
  const backoff = new Map<string, number>();
  const retryTimers = new Map<string, ReturnType<typeof setTimeout>>();
  const RETRY_MIN = 1000;
  const RETRY_MAX = 15000;

  // Last-request-wins: a pager click or the safety poll supersedes an older
  // refresh, so a slow response cannot overwrite a newer page.
  const snapshot = latest((signal, offset: number) =>
    Promise.all([
      listLibraries(signal), listScans(signal), fetchStats(signal),
      failedJobs(FAILED_PAGE, offset, signal).catch(() => null),
      listShareMap(signal).catch(() => [] as ShareMapEntry[]),
      libraryHashStatus(signal).catch(() => null),
    ]),
  );

  async function refresh() {
    try {
      const r = await snapshot.run(failedOffset);
      if (r === undefined) return;
      loadError = "";
      const [libs, scs, st, jobs, smap, hs] = r;
      libraries = libs;
      if (hs) hashStatus = Object.fromEntries(hs.map((r) => [r.library_id, r]));
      loaded = true;
      shareMap = smap;
      scans = scs;
      errorCounts = (st.extract_errors as Record<string, number>) ?? {};
      statsDegraded = (st.degraded as Record<string, string>) ?? {};
      if (jobs) {
        failedJobsList = jobs.items;
        failedTotal = jobs.total;
        // Clamp the offset if rows aged out / were cleared under us.
        if (failedOffset > 0 && failedOffset >= failedTotal) {
          failedOffset = Math.max(0, (Math.ceil(failedTotal / FAILED_PAGE) - 1) * FAILED_PAGE);
        }
      }
      // UI-T8: fold the fresh scan rows into `live` on EVERY refresh (including
      // the 30s safety poll) so counters advance even when all SSE streams are
      // dead. This was the frozen-UI root cause — see mergeScanRows.
      mergeScanRows();
      for (const id of Object.keys(expanded)) if (expanded[id]) await loadFailing(id);
      syncStreams();
    } catch (e) {
      loadError = errorMessage(e);
    }
  }

  /**
   * UI-T8 root-cause fix. `fmtStats` renders a RUNNING scan from `live[id]` in
   * preference to the DB row, so a live SSE snapshot that stops arriving (dropped
   * stream) freezes the display — the safety poll updated `scans` but the render
   * kept reading the stale `live` entry. (A full page refresh "fixed" it only
   * because it reset the empty `live` map, falling back to the fresh row.)
   *
   * Fix: whenever we (re)fetch scan rows we ALWAYS merge them into `live`, so the
   * poll alone advances the counters with SSE dead. SSE-only derived fields
   * (rate/elapsed) are preserved from any prior snapshot; an ended scan drops out
   * of `live` so its final persisted stats render straight from the row.
   */
  function mergeScanRows() {
    const merged: Record<string, Live> = { ...live };
    for (const s of scans) {
      if (s.status === "running" || s.status === "stopping") {
        const prev = merged[s.id];
        merged[s.id] = {
          ...(s.stats ?? {}),
          status: s.status,
          rate: prev?.rate,
          elapsed: prev?.elapsed,
        };
      } else if (merged[s.id]) {
        delete merged[s.id];
      }
    }
    live = merged;
  }

  /** Open SSE streams for running scans; drop streams for scans that ended. */
  function syncStreams() {
    const running = new Set(
      scans
        .filter((s) => s.status === "running" || s.status === "stopping")
        .map((s) => s.id),
    );
    for (const id of running) if (!streams.has(id)) openStream(id);
    for (const id of [...streams.keys()]) if (!running.has(id)) closeStream(id);
  }

  function closeStream(id: string) {
    streams.get(id)?.close();
    streams.delete(id);
    const t = retryTimers.get(id);
    if (t) clearTimeout(t);
    retryTimers.delete(id);
    backoff.delete(id);
  }

  function applySnapshot(id: string, d: Live) {
    // Reassign (not mutate) so Svelte's $state reactivity re-renders the row.
    live = { ...live, [id]: d };
  }

  // Guards concurrent opens for the same scan while a token mint is in flight
  // (syncStreams may fire again from the safety poll before the mint returns).
  const opening = new Set<string>();

  async function openStream(id: string) {
    if (opening.has(id)) return;
    opening.add(id);
    // A prior stream/backoff timer may exist after a drop — clear before reopen.
    streams.get(id)?.close();
    // EventSource can't set headers, so mint a single-use scoped stream token
    // and attach it as ?stream_token= (the real API key never rides the URL).
    // Tokens are consumed on first use — every (re)connect mints a fresh one.
    // If the mint fails (auth off / transient), try tokenless: with auth
    // disabled the stream needs none, and with auth on the 401 lands in the
    // error handler's normal backoff path.
    let token = "";
    try {
      token = (await mintScanEventsToken(id)).token;
    } catch {
      /* tokenless attempt below */
    }
    opening.delete(id);
    const es = new EventSource(scanEventsUrl(id, token));
    streams.set(id, es);
    // Guards the post-close `error` the browser fires after a clean server end,
    // so we don't spuriously reconnect a scan that already finished.
    let terminated = false;

    const onData = (ev: MessageEvent) => {
      backoff.delete(id); // healthy message → reset backoff
      try {
        applySnapshot(id, JSON.parse(ev.data));
      } catch {
        /* ignore malformed frame */
      }
    };

    es.addEventListener("progress", onData);
    es.addEventListener("done", (ev) => {
      terminated = true;
      onData(ev as MessageEvent);
      closeStream(id);
      // Stream ended: one authoritative refresh so the row reflects the final
      // persisted stats (and any sidecar pass results) exactly.
      refresh();
    });
    es.addEventListener("error", (ev) => {
      if (terminated) return; // clean close already handled by `done`
      // A named server `error` frame (scan-not-found / failed-with-detail) carries
      // `.data`; a transient network drop does not. For a server frame, converge
      // via a single refresh rather than hammering reconnects.
      const serverFrame = Boolean((ev as MessageEvent).data);
      es.close();
      streams.delete(id);
      const stillRunning = scans.some(
        (s) => s.id === id && (s.status === "running" || s.status === "stopping"),
      );
      if (serverFrame || !stillRunning) {
        closeStream(id);
        refresh();
        return;
      }
      // Transient drop while the scan is still believed running: bounded backoff
      // reconnect. The safety poll (mergeScanRows) keeps counters live meanwhile.
      const delay = Math.min(backoff.get(id) ?? RETRY_MIN, RETRY_MAX);
      backoff.set(id, Math.min(delay * 2, RETRY_MAX));
      const t = setTimeout(() => openStream(id), delay);
      retryTimers.set(id, t);
    });
  }

  async function loadFailing(libId: string) {
    try {
      const res = await libraryErrors(libId);
      failing = { ...failing, [libId]: res.items };
      errorCounts = { ...errorCounts, [libId]: res.count };
      if (failingError[libId]) {
        const { [libId]: _cleared, ...rest } = failingError;
        failingError = rest;
      }
    } catch (e) {
      failingError = { ...failingError, [libId]: errorMessage(e) };
    }
  }

  async function toggleErrors(libId: string) {
    const open = !expanded[libId];
    expanded = { ...expanded, [libId]: open };
    if (open && !failing[libId]) await loadFailing(libId);
  }

  // FIX-8: failed-jobs pager + manual clear.
  async function failedPage(delta: number) {
    const next = failedOffset + delta * FAILED_PAGE;
    failedOffset = Math.max(0, Math.min(next, Math.max(0, failedTotal - 1)));
    await refresh();
  }

  async function clearFailed() {
    if (clearingFailed) return;
    const ok = await confirm({
      title: "Clear failed job history?",
      message: `Clear all ${failedTotal} failed job record(s)?`,
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
      await refresh();
    } catch (e) {
      clearFailedError = errorMessage(e, { verb: "clear failed items" });
    } finally {
      clearingFailed = false;
    }
  }

  let retryingAll = $state(false);
  const errorTotal = $derived(Object.values(errorCounts).reduce((a, b) => a + b, 0));

  async function retryAllFailed() {
    if (retryingAll) return;
    const ok = await confirm({
      title: "Retry all failed items?",
      message: `Re-extract all ${errorTotal.toLocaleString()} failed item(s) across every library?`,
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
      await retryAllExtracts();
      failing = {};
      await refresh();
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
      await retryExtracts(libId);
      await loadFailing(libId);
    } catch (e) {
      rowError = errorMessage(e, { verb: "retry failed items" });
    } finally {
      retrying[libId] = false;
    }
  }

  function latestScan(libId: string): ScanRun | undefined {
    return scans.find((s) => s.library_id === libId); // scans come newest-first
  }

  // Optimistic scan feedback. POST /libraries/{id}/scan only DEFERS a job — the
  // ScanRun row is created later, by the worker — so refresh() immediately after
  // the click usually still returns the PREVIOUS scan and the button looks like
  // it did nothing (the safety poll is 30s away). Record the request time on
  // click and let the row report "queued" until its own run appears.
  let queued = $state<Record<string, number>>({});

  // True from the moment a scan is requested until its ScanRun row exists. The
  // 2s slack absorbs browser/server clock skew so we still recognise our run.
  function isQueued(libId: string): boolean {
    const at = queued[libId];
    if (!at) return false;
    const s = latestScan(libId);
    if (!s) return true;
    if (Date.parse(s.started_at) >= at - 2000) return false; // our run exists
    // The request was DEDUPED into a scan that was already in flight (the API
    // coalesces rather than stacking runs). That older run is the real state —
    // show its live progress instead of a "queued" that would never clear.
    return !(s.status === "running" || s.status === "stopping");
  }

  // The worker normally picks the job up within a second or two. Poll a short
  // decaying burst so the row flips from "queued" to live progress promptly
  // rather than sitting there until the 30s safety poll. Fire-and-forget: the
  // button is already re-enabled, so this never blocks another click.
  async function pollUntilStarted(libId: string) {
    for (const delay of [800, 1500, 3000, 5000, 8000]) {
      if (!isQueued(libId)) return;
      await new Promise((r) => setTimeout(r, delay));
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
    refresh();
  }

  function fmtStats(s: ScanRun | undefined): string {
    if (!s) return "never scanned";
    const active = s.status === "running" || s.status === "stopping";
    const lv = active ? live[s.id] : undefined;
    const st: Record<string, number | undefined> = lv
      ? { seen: lv.seen, new: lv.new, changed: lv.changed, missing: lv.missing,
          excluded: lv.excluded }
      : (s.stats ?? {});
    const status = lv?.status ?? s.status;
    const inProgress = status === "running" || status === "stopping";
    const base = `${status}${s.finished_at || !inProgress ? "" : "…"}`;
    const parts = ["seen", "new", "changed", "missing", "excluded"]
      .filter((k) => st[k] !== undefined)
      .map((k) => `${k} ${st[k]}`);
    if (lv && lv.rate !== undefined) parts.push(`${lv.rate.toFixed(1)}/s`);
    return parts.length ? `${base} — ${parts.join(", ")}` : base;
  }

  // FIX-10: relative time for a persisted last-scan timestamp (compact, no deps).
  function relTime(iso: string): string {
    const secs = Math.max(0, Math.round((Date.now() - new Date(iso).getTime()) / 1000));
    if (secs < 60) return "just now";
    const mins = Math.round(secs / 60);
    if (mins < 60) return `${mins}m ago`;
    const hours = Math.round(mins / 60);
    if (hours < 24) return `${hours}h ago`;
    const days = Math.round(hours / 24);
    if (days < 30) return `${days}d ago`;
    const months = Math.round(days / 30);
    if (months < 12) return `${months}mo ago`;
    return `${Math.round(months / 12)}y ago`;
  }

  // FIX-10: status-badge tone for a persisted last-scan status. Local rather
  // than toneForStatus: "finished" is ok and stopped/cancelled a warning here.
  function lastScanTone(status: string): Tone {
    if (status === "finished") return "ok";
    if (status === "failed") return "danger";
    if (status === "stopped" || status === "cancelled") return "warn";
    return "neutral";
  }

  // §17: "slower than usual" verdict from real history — this run's walk rate
  // vs the library's rolling median over recent finished full scans. Only
  // speaks with >=3 runs behind the median (less is noise) and only when the
  // run was meaningfully slower (< 60% of the median).
  function slowScan(ls: NonNullable<Library["last_scan"]>): string | null {
    const rate = ls.files_per_s;
    const median = ls.median_files_per_s;
    const runs = ls.throughput_runs ?? 0;
    if (rate == null || median == null || runs < 3 || median <= 0) return null;
    if (rate >= median * 0.6) return null;
    return `This scan walked ${rate.toFixed(1)} files/s vs a ${median.toFixed(1)} files/s median over the last ${runs} full scans — check mount/network health or concurrent load.`;
  }

  // FIX-10: compact "seen N, new N" counts from a persisted last scan.
  function lastScanCounts(ls: NonNullable<Library["last_scan"]>): string {
    return (["seen", "new", "changed", "missing"] as const)
      .filter((k) => ls[k] != null)
      .map((k) => `${k} ${ls[k]}`)
      .join(", ");
  }

  // Which libraries have their last-scan details panel open (below the table:
  // the exclusion breakdown and the "slower than usual" verdict). A native
  // `title=` tooltip only appears after a hover delay and ignores clicks, so the
  // badge looked broken; the breakdown is now click-to-open (title kept as a
  // bonus for hover users).
  let excludedOpen = $state<Record<string, boolean>>({});

  function toggleExcluded(libId: string) {
    excludedOpen = { ...excludedOpen, [libId]: !excludedOpen[libId] };
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
      why.push(`${ls.excluded_gate} rejected by this library's category/group selection`);
    if (ls.excluded_filtered)
      why.push(`${ls.excluded_filtered} rejected by exclusion presets/globs (system files, hidden dotfiles, …)`);
    let s = why.length
      ? `${ls.excluded} file(s) enumerated but not ingested — ${why.join("; ")}.`
      : `${ls.excluded} file(s) enumerated but not ingested.`;

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
    refresh();
  }

  // UI-T2: the product's ONE intentional hard-delete, behind a typed-name
  // confirm. DELETE /libraries/{id}?confirm=<name>; the frozen contract: 404 =
  // already gone (the outcome asked for), 409 = a scan is running, 422 =
  // confirm mismatch (the typed-name gate makes it unlikely; surfaced anyway).
  async function removeLibrary(lib: Library) {
    if (deletingId) return;
    const ok = await confirm({
      title: "Delete library?",
      message:
        `This permanently removes ${lib.name}, all of its indexed items, and its scan history. ` +
        "Search results for these items disappear. This is a hard delete and cannot be undone.",
      details: [
        "All indexed items and scan history are removed",
        "Search results for these items disappear",
        "This cannot be undone",
      ],
      confirmLabel: "Delete library",
      tone: "danger",
      typedName: lib.name,
    });
    if (!ok) return;
    deletingId = lib.id;
    deleteError = "";
    try {
      await deleteLibrary(lib.id, lib.name);
      await refresh();
    } catch (e) {
      const status = errorStatus(e);
      if (status === 404) await refresh();
      else if (status === 409)
        deleteError = "A scan is currently running for this library. Cancel or wait for it to finish, then delete.";
      else if (status === 422) deleteError = "Confirmation name did not match.";
      else deleteError = errorMessage(e, { verb: "delete this library" });
    } finally {
      deletingId = null;
    }
  }

  let safety: Poller | undefined;
  onMount(() => {
    refresh();
    loadLibStats();
    listPresets()
      .then((m) => (presetsMeta = m))
      .catch((e) => (loadError = errorMessage(e)));
    // W8: taxonomy for the type-gating selectors. Non-fatal — an empty tree just
    // means the selector shows "all types" and libraries can still be created.
    getTaxonomy()
      .then((t) => (taxonomyTree = t.tree))
      .catch(() => (taxonomyTree = []));
    // Safety-net refresh; paused while the tab is hidden.
    safety = usePolling(refresh, 30000);
  });
  onDestroy(() => {
    safety?.stop();
    snapshot.cancel();
    for (const id of [...streams.keys()]) closeStream(id);
  });
</script>

<!-- Cells shared by the central and agent tables. Cells render twice (table +
     card list), so none carries an id, a bind:this or focus management; the
     expandable panels render below the tables, keyed by library. -->
{#snippet nameCell(lib: Library)}
  <span class="font-medium">{lib.name}</span>
  {#if !lib.enabled}<Badge class="ml-1">off</Badge>{/if}
{/snippet}

{#snippet itemsCell(lib: Library)}
  <span class="whitespace-nowrap tabular-nums text-xs text-muted" title={statTitle(lib.id)}>{statCell(lib.id, "count")}</span>
  {#if statExtras(lib.id)}<span class="block whitespace-nowrap text-2xs text-muted">{statExtras(lib.id)}</span>{/if}
{/snippet}

{#snippet sizeCell(lib: Library)}
  <span class="whitespace-nowrap tabular-nums text-xs text-muted" title={statTitle(lib.id)}>{statCell(lib.id, "bytes")}</span>
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

<!-- Central table cells -->
{#snippet rootCell(lib: Library)}
  <span class="block min-w-[10rem] max-w-[16rem] font-mono text-xs text-muted break-all">{lib.root_path}</span>
{/snippet}

{#snippet typesCell(lib: Library)}
  <span class="inline-flex flex-wrap gap-1">
    {#if lib.enabled_categories?.length || lib.enabled_groups?.length}
      {#each lib.enabled_categories ?? [] as c}
        <span title="category"><Badge tone="accent"><span class="sr-only">category </span>{c}</Badge></span>
      {/each}
      {#each lib.enabled_groups ?? [] as g}
        <span title="group"><Badge><span class="sr-only">group </span>{g}</Badge></span>
      {/each}
    {:else}
      <span class="text-xs text-muted">all</span>
    {/if}
  </span>
{/snippet}

{#snippet hashCell(lib: Library)}
  <span class="text-xs text-muted">{lib.hash_policy === "quick_only" ? "quick only" : lib.hash_policy}</span>
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

{#snippet cronCell(lib: Library)}<span class="font-mono text-xs text-muted">{lib.scan_cron ?? "—"}</span>{/snippet}
{#snippet watchCell(lib: Library)}<span class="text-xs text-muted">{lib.watch_mode ? "on" : "—"}</span>{/snippet}

{#snippet lastScanCell(lib: Library)}
  {@const scan = latestScan(lib.id)}
  <!-- nowrap keeps status + age + counts + the excluded badge on ONE line; the
       breakdown panel below the table carries the long text. -->
  <span class="inline-flex flex-wrap items-center gap-1 text-xs text-muted md:flex-nowrap md:whitespace-nowrap">
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
    {:else if scan && (scan.status === "running" || scan.status === "stopping")}
      {fmtStats(scan)}
    {:else if lib.last_scan}
      {@const ls = lib.last_scan}
      {@const slow = slowScan(ls)}
      <Badge tone={lastScanTone(ls.status)}>{ls.status}</Badge>
      <span title={new Date(ls.finished_at ?? ls.started_at).toLocaleString()}>{relTime(ls.finished_at ?? ls.started_at)}</span>
      {#if lastScanCounts(ls)}<span>— {lastScanCounts(ls)}</span>{/if}
      <!-- The slow verdict and the exclusion breakdown open the same details
           panel below the table (their full text no longer lives in a title). -->
      {#if slow}
        <button
          type="button"
          class="rounded-full"
          title={slow}
          aria-expanded={!!excludedOpen[lib.id]}
          onclick={() => toggleExcluded(lib.id)}>
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
          onclick={() => toggleExcluded(lib.id)}
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
{/snippet}

{#snippet centralActionsCell(lib: Library)}
  {@const scan = latestScan(lib.id)}
  {@const running = scan?.status === "running"}
  {@const stopping = scan?.status === "stopping"}
  <div class="flex flex-wrap items-center justify-end gap-1">
    {#if running && scan}
      <Button
        size="sm"
        title="Finish the current batch and keep everything scanned so far, then end the scan. Skips deletion detection (unvisited files are NOT marked missing). The next scan picks up where this left off."
        onclick={async () => { await stopScan(scan.id).catch(() => {}); refresh(); }}>
        Stop (keep progress)
      </Button>
      <Button
        size="sm"
        variant="danger"
        title="Abort the scan immediately, discarding the in-flight batch. Use Stop to keep what was already scanned."
        onclick={async () => { await cancelScan(scan.id).catch(() => {}); refresh(); }}>
        Cancel (abort)
      </Button>
    {:else if stopping}
      <span title="Stop requested — finishing the current batch and wrap-up, then the scan ends as stopped.">
        <Badge tone="warn">stopping…</Badge>
      </span>
      <!-- FIX-15: escape hatch for a scan wedged in 'stopping' (worker died /
           stop never observed). Drives it terminal; the server refuses (409)
           if a live worker is still draining it. -->
      <Button
        size="sm"
        variant="danger"
        title="Force this stuck scan to a terminal state. Use only if it has been 'stopping' for a while with no progress (its worker likely died). Refused if a live worker is still processing it."
        onclick={async () => {
          if (!scan) return;
          await forceClearScan(scan.id).catch(() => {});
          refresh();
        }}>
        Force clear
      </Button>
    {/if}
    {@render commonActions(lib)}
    <Button size="sm" variant="primary" disabled={busy[lib.id] || running || stopping} onclick={() => runScan(lib)}>
      {#if busy[lib.id]}
        Starting…
      {:else if isQueued(lib.id)}
        Queued
      {:else}
        Scan
      {/if}
    </Button>
  </div>
{/snippet}

<!-- Agent table cells -->
{#snippet agentCell(lib: Library)}{lib.agent_name ?? "unknown"}{/snippet}
{#snippet agentStatusCell(lib: Library)}
  <Badge tone={toneForStatus(lib.agent_status)}>{lib.agent_status ?? "unknown"}</Badge>
{/snippet}
{#snippet lastSyncCell(lib: Library)}
  <span class="text-muted" title={lib.agent_last_seen_at ? new Date(lib.agent_last_seen_at).toLocaleString() : "the agent has never replicated"}>
    {lib.agent_last_seen_at ? relTime(lib.agent_last_seen_at) : "never"}
  </span>
{/snippet}
{#snippet lastReconcileCell(lib: Library)}
  <span class="text-muted" title={lib.agent_last_reconcile_at ? new Date(lib.agent_last_reconcile_at).toLocaleString() : "no full-manifest reconcile yet"}>
    {lib.agent_last_reconcile_at ? relTime(lib.agent_last_reconcile_at) : "never"}
  </span>
{/snippet}
{#snippet agentActionsCell(lib: Library)}
  <div class="flex flex-wrap justify-end gap-1">{@render commonActions(lib)}</div>
{/snippet}

<!-- Failed-jobs cells -->
{#snippet jobQueueCell(j: FailedJob)}<span class="text-muted">{j.queue}</span>{/snippet}
{#snippet jobTaskCell(j: FailedJob)}<span class="font-mono text-xs">{j.task}</span>{/snippet}
{#snippet jobAttemptsCell(j: FailedJob)}<span class="text-muted">{j.attempts ?? "—"}</span>{/snippet}
{#snippet jobAttemptedCell(j: FailedJob)}
  <span class="text-muted">{j.attempted_at ? new Date(j.attempted_at).toLocaleString() : "—"}</span>
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
  {@const items = failing[lib.id] ?? []}
  <section
    id="failing-{lib.id}"
    aria-label="Failing items — {lib.name}"
    class="mt-2 rounded-control border border-border bg-surface-2 px-3 py-2 text-xs"
  >
    <div class="flex items-center gap-2">
      <h3 class="text-sm font-semibold">Failing items — {lib.name}</h3>
      <span class="grow"></span>
      <Button size="sm" variant="ghost" onclick={() => toggleErrors(lib.id)}>Hide</Button>
    </div>
    {#if failingError[lib.id]}
      <StateBlock state="error" compact message={failingError[lib.id]} onRetry={() => loadFailing(lib.id)} />
    {:else if items.length === 0}
      <StateBlock state="empty" compact message="No failing items — nothing to retry." />
    {:else}
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
    <Button
      size="sm"
      variant="ghost"
      aria-label="Recount files and sizes"
      title="Recount files and sizes (one aggregate query over the whole catalog)"
      onclick={loadLibStats}
      disabled={libStatsBusy}>
      <span aria-hidden="true">{libStatsBusy ? "…" : "↻"}</span>
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
  {#if statsDegraded.extract_errors}
    <Alert tone="warn" class="mt-2">
      Extraction-error counts unavailable ({statsDegraded.extract_errors}) — the
      Errors column below is blank because it could not be read, not because
      there are none.
    </Alert>
  {/if}
  {#if staleHashRows.length}
    <Alert tone="warn" class="mt-3" title="Stored hashes need updating">
      <p class="mt-0.5 text-xs">
        {staleHashTotal.toLocaleString()} item{staleHashTotal === 1 ? "" : "s"} across
        {staleHashRows.length} librar{staleHashRows.length === 1 ? "y" : "ies"}
        ({staleHashRows.map((r) => r.name).join(", ")}) still carry hashes computed by an
        older algorithm. Use <span class="font-medium">update hashes</span> in the Hash
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
          <span class="font-semibold">{errorTotal.toLocaleString()} item{errorTotal === 1 ? "" : "s"} failed extraction.</span>
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

  {#if deleteError}
    <Alert tone="danger" class="mt-3" onDismiss={() => (deleteError = "")}>{deleteError}</Alert>
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
    columns={[
      { key: "name", label: "Name", cell: nameCell },
      { key: "root_path", label: "Root", cell: rootCell },
      { key: "items", label: "Items", align: "right", cell: itemsCell },
      { key: "size", label: "Size", align: "right", cell: sizeCell },
      { key: "types", label: "Types", cell: typesCell },
      { key: "hash_policy", label: "Hash", cell: hashCell },
      { key: "scan_cron", label: "Cron", cell: cronCell },
      { key: "watch_mode", label: "Watch", cell: watchCell },
      { key: "errors", label: "Errors", cell: errorsCell },
      { key: "last_scan", label: "Last scan", cell: lastScanCell },
      { key: "actions", label: "Actions", align: "right", cell: centralActionsCell },
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
        aria-label="Last scan details — {lib.name}"
        class="mt-2 rounded-control border border-warn/40 bg-warn-bg px-3 py-2 text-xs"
      >
        <div class="flex items-center gap-2">
          <h3 class="text-sm font-semibold">Last scan details — {lib.name}</h3>
          <span class="grow"></span>
          <Button size="sm" variant="ghost" onclick={() => toggleExcluded(lib.id)}>Hide</Button>
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
        { key: "size", label: "Size", align: "right", cell: sizeCell },
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

  <h2 class="mt-8 text-lg font-semibold">Recent scans</h2>
  {#if scans.length}
    <ul class="mt-2 divide-y divide-border text-sm">
      {#each scans.slice(0, 10) as s (s.id)}
        {@const lib = libraries.find((l) => l.id === s.library_id)}
        <li class="flex flex-wrap items-center gap-3 py-2">
          <Badge tone={toneForStatus(s.status)}>{s.status}</Badge>
          <span class="font-medium">{lib?.name ?? s.library_id}</span>
          <span class="text-muted">{new Date(s.started_at).toLocaleString()}</span>
          <span class="grow"></span>
          <span class="text-xs text-muted">{fmtStats(s)}</span>
        </li>
      {/each}
    </ul>
  {:else if loaded}
    <StateBlock state="empty" compact message="No scans yet — start one with a library's Scan button." />
  {/if}

  {#if failedTotal > 0}
    <div class="mt-8 flex items-center gap-3">
      <h2 class="text-lg font-semibold">Failed jobs</h2>
      <span class="text-xs text-muted">{failedTotal} total</span>
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
        {failedOffset + 1}–{Math.min(failedOffset + FAILED_PAGE, failedTotal)} of {failedTotal}
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
