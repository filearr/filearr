// Hash / saved-search params → SearchPage UI state (the applyParams side).
// Pure and DOM-free so Node tests import it directly.
//
// Every value is validated before it can reach /search (A36): an unknown enum
// from an old saved search or a hand-edited URL is DROPPED (the control shows
// its default) and its key is reported in `invalid`, instead of being sent to
// the server for a raw 422 or leaving a <select> blank. Range params have no
// slider position until the facet stats land, so they ride `pendingRange` and
// get their own chips (A7).
//
// This module never sees the query DSL: q is stored as typed and translated per
// request by buildParams() (searchDsl.ts), so nothing here folds DSL params in.

import { fmtBytes, fmtDate } from "./format.ts";
import { boxError, parseBox, type GeoBox } from "./map/geo.ts";

export type Bounds = { min: number; max: number };
export type SearchIn = "all" | "content" | "names";
export type View = "list" | "grid" | "map";
export type WorldReadable = "" | "true" | "false";
export type Exposure = "" | "anonymous" | "authenticated" | "restricted";
export type RangePair = "size" | "date";

/** Explicit sort options (P3-T7 deep-linkable). "" = relevance; "newest" uses
 *  the FIX-3 clamped mtime_sort so bogus future mtimes cannot float to the top. */
export const SORTS: readonly { value: string; label: string }[] = [
  { value: "", label: "Relevance" },
  { value: "newest", label: "Newest" },
  { value: "size:desc", label: "Largest" },
  { value: "title:asc", label: "Name (A–Z)" },
];

/** Hash-only display prefs that are not /search params. POST/PATCH
 *  /saved-searches 422s any key outside the /search vocabulary (A4). */
export const UI_ONLY_PARAMS = ["view"] as const;

/** The two range pairs and the flat params each one owns. */
export const RANGE_PAIRS: Record<RangePair, readonly [string, string]> = {
  size: ["size_gte", "size_lte"],
  date: ["mtime_gte", "mtime_lte"],
};
const RANGE_KEYS = [...RANGE_PAIRS.size, ...RANGE_PAIRS.date];
const GEO_KEYS = ["geo_top_lat", "geo_right_lng", "geo_bottom_lat", "geo_left_lng"];

// Display-only params: restoring them never starts a search on its own
// (roadmap §20 blank slate; live report 2026-08-08).
const DISPLAY_ONLY = new Set(["view", "sort", "semantic"]);

const ENUMS: Record<string, readonly string[]> = {
  search_in: ["all", "content", "names"],
  sort: SORTS.map((s) => s.value),
  view: ["list", "grid", "map"],
  exposure: ["anonymous", "authenticated", "restricted"],
  world_readable: ["true", "false"],
};

// Free-text and list params: any value is acceptable.
const FREE = new Set([
  "q",
  "hash",
  "extension",
  "file_category",
  "file_group",
  "library",
  "principal",
  "tags",
  "share_mismatch",
  "include_sidecars",
]);

/** The page state applyParamsPure() sets. Slider positions are present only
 *  when the matching bounds are known. */
export type SearchState = {
  q: string;
  extension: string;
  selectedCategories: string[];
  selectedGroups: string[];
  selectedLibraries: string[];
  selectedPrincipals: string[];
  worldReadable: WorldReadable;
  exposure: Exposure;
  shareMismatch: boolean;
  selectedTags: string[];
  includeSidecars: boolean;
  searchIn: SearchIn;
  sortMode: string;
  view: View;
  geoBox: GeoBox | null;
  semantic: number;
  pendingRange: Record<string, string>;
  sizeLo: number;
  sizeHi: number;
  mtimeLo: number;
  mtimeHi: number;
};
type SliderKey = "sizeLo" | "sizeHi" | "mtimeLo" | "mtimeHi";
export type AppliedState = Omit<SearchState, SliderKey> & Partial<Pick<SearchState, SliderKey>>;

const list = (v: string | undefined): string[] =>
  v ? v.split(",").map((t) => t.trim()).filter(Boolean) : [];

const numeric = (v: string): boolean => v.trim() !== "" && Number.isFinite(Number(v));

/** Validate a flat param bundle and turn it into page state.
 *  - `invalid`: keys dropped (unknown key, enum value outside its set,
 *    non-numeric range or semantic, partial / inverted geo box), in param order.
 *  - `searchable`: some kept, non-display param has a value — false means the
 *    page returns to the blank slate instead of searching (A6). */
export function applyParamsPure(
  p: Record<string, string>,
  bounds: { size: Bounds | null; mtime: Bounds | null },
): { state: AppliedState; searchable: boolean; invalid: string[] } {
  const bad = new Set<string>();
  for (const [k, v] of Object.entries(p)) {
    if (v === "" || v == null) continue;
    if (k in ENUMS) {
      if (!ENUMS[k].includes(v)) bad.add(k);
    } else if (k === "semantic") {
      const n = Number(v);
      if (!numeric(v) || n < 0 || n > 1) bad.add(k);
    } else if (RANGE_KEYS.includes(k)) {
      if (!numeric(v)) bad.add(k);
    } else if (!FREE.has(k) && !GEO_KEYS.includes(k)) {
      bad.add(k);
    }
  }
  // Geo: all four edges and a box the API accepts, or none of it.
  const geoPresent = GEO_KEYS.filter((k) => p[k] != null && p[k] !== "");
  let geoBox: GeoBox | null = null;
  if (geoPresent.length) {
    const box = parseBox(p);
    if (box && !boxError(box)) geoBox = box;
    else for (const k of geoPresent) bad.add(k);
  }
  const ok = (k: string): string => (p[k] != null && !bad.has(k) ? p[k] : "");

  const pendingRange: Record<string, string> = {};
  for (const k of RANGE_KEYS) if (ok(k)) pendingRange[k] = ok(k);

  const state: AppliedState = {
    q: p.hash ?? p.q ?? "",
    extension: p.extension ?? "",
    selectedCategories: list(p.file_category),
    selectedGroups: list(p.file_group),
    selectedLibraries: list(p.library),
    selectedPrincipals: list(p.principal),
    worldReadable: ok("world_readable") as WorldReadable,
    exposure: ok("exposure") as Exposure,
    shareMismatch: p.share_mismatch === "true",
    selectedTags: list(p.tags),
    includeSidecars: p.include_sidecars === "true",
    searchIn: (ok("search_in") || "all") as SearchIn,
    sortMode: ok("sort"),
    view: (ok("view") || "list") as View,
    geoBox,
    semantic: ok("semantic") ? Number(p.semantic) : 0,
    pendingRange,
  };
  // Deactivate the sliders so pendingRange drives the filter until one moves.
  if (bounds.size) {
    state.sizeLo = bounds.size.min;
    state.sizeHi = bounds.size.max;
  }
  if (bounds.mtime) {
    state.mtimeLo = bounds.mtime.min;
    state.mtimeHi = bounds.mtime.max;
  }

  const searchable = Object.entries(p).some(
    ([k, v]) => v !== "" && v != null && !bad.has(k) && !DISPLAY_ONLY.has(k),
  );
  return { state, searchable, invalid: Object.keys(p).filter((k) => bad.has(k)) };
}

export type PendingChip = { key: string; pair: RangePair; label: string };

/** One chip per range pair carried by URL / saved search: `size ≥ 1 MB`,
 *  `size ≤ 4 GB`, `size 1 MB–4 GB`, `date ≥ 2024-01-01`. Blank values are
 *  ignored. The page adds the clearer (clearRangePair + re-query). */
export function pendingRangeChips(pending: Record<string, string>): PendingChip[] {
  const out: PendingChip[] = [];
  const fmt: Record<RangePair, (n: number) => string> = { size: (n) => fmtBytes(n), date: fmtDate };
  for (const pair of ["size", "date"] as const) {
    const [gk, lk] = RANGE_PAIRS[pair];
    const lo = pending[gk];
    const hi = pending[lk];
    if (!lo && !hi) continue;
    const f = fmt[pair];
    const range = lo && hi ? `${f(Number(lo))}–${f(Number(hi))}` : lo ? `≥ ${f(Number(lo))}` : `≤ ${f(Number(hi))}`;
    out.push({ key: `pending:${pair}`, pair, label: `${pair} ${range}` });
  }
  return out;
}

/** `pending` without one pair's two keys (the other pair is kept). */
export function clearRangePair(pending: Record<string, string>, pair: RangePair): Record<string, string> {
  const out = { ...pending };
  for (const k of RANGE_PAIRS[pair]) delete out[k];
  return out;
}

/** True while that pair still carries a URL / saved-search value. */
export function pairPending(pending: Record<string, string>, pair: RangePair): boolean {
  return RANGE_PAIRS[pair].some((k) => !!pending[k]);
}
