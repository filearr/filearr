// Search result paging integrity (A12), DOM-free so it is Node-tested.
//
// - mergeHits: append a page by id, dropping rows already on screen. The
//   backend cursor is an offset, so a document added or removed between pages
//   shifts rows and repeats one; a repeated id breaks Svelte's keyed {#each}.
// - createPageSession: loadMore pages with the params of the LAST fresh
//   search, never the live form (a keystroke inside the debounce or a slider
//   mid-drag would otherwise page the old offset of a different query).
// - boundsProbe*: a deep link carrying a range filter has no slider bounds yet;
//   one extra request without the range keys reads the unfiltered facet_stats
//   so the sliders show the whole range (controller ruling, P6 Task 3).

import { RANGE_PAIRS, pairPending, type Bounds, type RangePair } from "./searchState.ts";

/** `existing` followed by the rows of `incoming` whose id is not already
 *  present (first occurrence wins, order kept). `dropped` counts the rows
 *  left out, including repeats inside `incoming` itself. Inputs are not
 *  mutated. */
export function mergeHits<T>(
  existing: T[],
  incoming: T[],
  idOf: (t: T) => string,
): { hits: T[]; dropped: number } {
  if (!incoming.length) return { hits: existing, dropped: 0 };
  const seen = new Set(existing.map(idOf));
  const hits = existing.slice();
  let dropped = 0;
  for (const t of incoming) {
    const id = idOf(t);
    if (seen.has(id)) {
      dropped++;
      continue;
    }
    seen.add(id);
    hits.push(t);
  }
  return { hits, dropped };
}

export type PageSession = {
  /** Snapshot the params a fresh search was sent with (copied). */
  start(params: Record<string, string>): void;
  /** The params for the page at `cursor`: the snapshot plus the cursor, as a
   *  fresh object; null without a snapshot or a cursor. */
  pageParams(cursor: string | null): Record<string, string> | null;
  /** Forget the snapshot (blank slate). */
  clear(): void;
};

export function createPageSession(): PageSession {
  let snap: Record<string, string> | null = null;
  return {
    start(params) {
      snap = { ...params };
    },
    pageParams(cursor) {
      return snap && cursor ? { ...snap, cursor } : null;
    },
    clear() {
      snap = null;
    },
  };
}

/** The smallest `limit` /search accepts (`limit: int = Query(le=200)`, no
 *  lower bound): Meilisearch returns facet_stats with no hits. */
export const BOUNDS_PROBE_LIMIT = 0;

const RANGE_KEYS = [...RANGE_PAIRS.size, ...RANGE_PAIRS.date];

/** The range pairs still carried by URL / saved search (`pending`) whose
 *  slider has no bounds yet: those take their bounds from the probe. */
export function boundsProbePairs(
  pending: Record<string, string>,
  bounds: { size: Bounds | null; mtime: Bounds | null },
): RangePair[] {
  const out: RangePair[] = [];
  if (!bounds.size && pairPending(pending, "size")) out.push("size");
  if (!bounds.mtime && pairPending(pending, "date")) out.push("date");
  return out;
}

/** The probe request: `params` without the four range keys or a cursor, at
 *  the smallest limit. */
export function boundsProbeParams(params: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [k, v] of Object.entries(params)) {
    if (RANGE_KEYS.includes(k) || k === "cursor" || k === "limit") continue;
    out[k] = v;
  }
  out.limit = String(BOUNDS_PROBE_LIMIT);
  return out;
}
