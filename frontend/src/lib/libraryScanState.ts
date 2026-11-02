// Libraries page scan-row state, DOM-free (tests/libraryScanState.node.test.ts).
//
// * The page polls `GET /scans?status=running,stopping` (B9), so the active
//   set never falls out of a recent-runs window. `mergeRunning` folds it into
//   the per-scan `live` map (dropping runs that ended) and `applyFrame` folds
//   SSE frames in, both reading `ScanRun.stats` through typed accessors (B34:
//   the JSONB also carries strings, bools and arrays).
// * `rowScan` picks the run a library row acts on (its full run first).
// * `failedReason` is Ruling 6; `slowScan` compares full runs only (B10).
// * `isQueued` / `planStreams` keep the optimistic "queued" marker and the
//   SSE stream set honest (B43).

import type { LastScan, Library, ScanRun } from "./api";

/** Live counters for an active scan (persisted stats + SSE-only rate/elapsed). */
export type Live = {
  status: string;
  seen?: number;
  new?: number;
  changed?: number;
  missing?: number;
  /** Enumerated but skipped — streamed so a mis-scoped library shows early. */
  excluded?: number;
  bytes_seen?: number;
  rate?: number;
  elapsed?: number;
};

const COUNT_KEYS = ["seen", "new", "changed", "missing", "excluded", "bytes_seen"] as const;
const SHOWN_KEYS = ["seen", "new", "changed", "missing", "excluded"] as const;

export const isActive = (status: string | null | undefined): boolean =>
  status === "running" || status === "stopping";

/** A finite number at `key`, else undefined. */
export function statNum(stats: Record<string, unknown> | null | undefined, key: string): number | undefined {
  const v = stats?.[key];
  return typeof v === "number" && Number.isFinite(v) ? v : undefined;
}

/** A string at `key`, else undefined. */
export function statStr(stats: Record<string, unknown> | null | undefined, key: string): string | undefined {
  const v = stats?.[key];
  return typeof v === "string" ? v : undefined;
}

function countsFrom(src: Record<string, unknown>, into: Live): Live {
  for (const k of COUNT_KEYS) {
    const v = statNum(src, k);
    if (v !== undefined) into[k] = v;
  }
  return into;
}

/** The active runs' persisted stats over `prev`; SSE-only rate/elapsed kept;
 *  any run not in `running` (ended) drops out so its row renders the
 *  persisted last scan. */
export function mergeRunning(prev: Record<string, Live>, running: ScanRun[]): Record<string, Live> {
  const out: Record<string, Live> = {};
  for (const s of running) {
    if (!isActive(s.status)) continue;
    const p = prev[s.id];
    const lv = countsFrom(s.stats ?? {}, { status: s.status });
    if (p?.rate !== undefined) lv.rate = p.rate;
    if (p?.elapsed !== undefined) lv.elapsed = p.elapsed;
    out[s.id] = lv;
  }
  return out;
}

/** Fold one SSE snapshot into `live[id]` (a new object, for reactivity).
 *  Non-object frames leave `live` untouched. */
export function applyFrame(live: Record<string, Live>, id: string, data: unknown): Record<string, Live> {
  if (!data || typeof data !== "object") return live;
  const d = data as Record<string, unknown>;
  const lv = countsFrom(d, { status: statStr(d, "status") ?? live[id]?.status ?? "running" });
  const rate = statNum(d, "rate");
  const elapsed = statNum(d, "elapsed");
  if (rate !== undefined) lv.rate = rate;
  if (elapsed !== undefined) lv.elapsed = elapsed;
  return { ...live, [id]: lv };
}

/** The active run a library row shows and acts on: its full run when one is
 *  active, else its newest scoped run (`running` is newest-first). */
export function rowScan(running: ScanRun[], libId: string): ScanRun | undefined {
  const mine = running.filter((s) => s.library_id === libId && isActive(s.status));
  return mine.find((s) => s.rel_path == null) ?? mine[0];
}

export const NO_REASON = "failed (no reason recorded)";

/** Ruling 6: why a failed run failed — the SSE `error` detail captured for
 *  that scan, else `error`, else `reconcile_note`, else a fallback. Null when
 *  the run did not fail. */
export function failedReason(ls: LastScan | null | undefined, sseDetail: Record<string, string>): string | null {
  if (!ls || ls.status !== "failed") return null;
  return sseDetail[ls.id] || ls.error || ls.reconcile_note || NO_REASON;
}

/** Ruling 6 for a ScanRun (Recent scans): SSE detail, else `stats.error`,
 *  else `stats.reconcile_note`, else the fallback; null unless failed. */
export function scanRunReason(s: ScanRun, sseDetail: Record<string, string>): string | null {
  if (s.status !== "failed") return null;
  return sseDetail[s.id] || statStr(s.stats, "error") || statStr(s.stats, "reconcile_note") || NO_REASON;
}

/** §17 "slower than usual": this FULL run's walk rate vs the library's median
 *  over recent full scans; >= 3 runs of history and < 60% of the median. A
 *  scoped run is never compared (B10). */
export function slowScan(ls: LastScan): string | null {
  if (ls.rel_path != null) return null;
  const rate = ls.files_per_s;
  const median = ls.median_files_per_s;
  const runs = ls.throughput_runs ?? 0;
  if (rate == null || median == null || runs < 3 || median <= 0) return null;
  if (rate >= median * 0.6) return null;
  return `This scan walked ${rate.toFixed(1)} files/s vs a ${median.toFixed(1)} files/s median over the last ${runs} full scans — check mount/network health or concurrent load.`;
}

export const fmtCount = (n: number): string => n.toLocaleString();

/** "seen 12,345, new 0, missing 3" from a persisted last scan. */
export function lastScanCounts(ls: LastScan): string {
  return (["seen", "new", "changed", "missing"] as const)
    .filter((k) => ls[k] != null)
    .map((k) => `${k} ${fmtCount(ls[k] as number)}`)
    .join(", ");
}

/** "running… — seen 5,000, new 1, 12.3/s" for an active (or just-ended) run. */
export function fmtLiveStats(status: string, lv: Live, finishedAt: string | null): string {
  const base = `${status}${finishedAt || !isActive(status) ? "" : "…"}`;
  const parts: string[] = SHOWN_KEYS.filter((k) => lv[k] !== undefined).map(
    (k) => `${k} ${fmtCount(lv[k] as number)}`,
  );
  if (lv.rate !== undefined) parts.push(`${lv.rate.toFixed(1)}/s`);
  return parts.length ? `${base} — ${parts.join(", ")}` : base;
}

/** Stats line for any ScanRun (active ones read `live` first). */
export function fmtScanRun(s: ScanRun, live: Record<string, Live>): string {
  const lv = isActive(s.status) ? live[s.id] : undefined;
  const view = lv ?? countsFrom(s.stats ?? {}, { status: s.status });
  return fmtLiveStats(lv?.status ?? s.status, view, s.finished_at);
}

/** "targeted: Movies/Incoming" for a scoped run; null for none. */
export function scopedSummary(ls: Pick<LastScan, "rel_path"> | null | undefined): string | null {
  if (!ls || ls.rel_path == null) return null;
  return `targeted: ${ls.rel_path || "(library root)"}`;
}

/** True from a scan request until a run for the library exists. `active` is
 *  the row's active run (a coalesced request joined it: show its progress);
 *  otherwise a full or scoped run that started at/after the request (2 s
 *  clock-skew slack) is ours, even if it already finished between polls. */
export function isQueued(
  queuedAt: number | undefined,
  active: ScanRun | undefined,
  lib: Pick<Library, "last_scan" | "last_scoped_scan">,
): boolean {
  if (!queuedAt) return false;
  if (active) return false;
  for (const ls of [lib.last_scan, lib.last_scoped_scan]) {
    if (ls && Date.parse(ls.started_at) >= queuedAt - 2000) return false;
  }
  return true;
}

/** Which streams to open and close. `open` holds every id with a live OR
 *  backing-off stream, so a poll never opens a second one (B43); `ended`
 *  holds ids whose stream already delivered a terminal frame (the running
 *  list may still be stale), never reopened. */
export function planStreams(
  activeIds: Iterable<string>,
  open: Iterable<string>,
  ended: ReadonlySet<string>,
): { open: string[]; close: string[] } {
  const active = new Set(activeIds);
  const have = new Set(open);
  return {
    open: [...active].filter((id) => !have.has(id) && !ended.has(id)),
    close: [...have].filter((id) => !active.has(id)),
  };
}
