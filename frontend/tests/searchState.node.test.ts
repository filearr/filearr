// searchState.ts: hash / saved-search params → SearchPage UI state, with every
// value validated before it can reach /search (A36), plus the chips for range
// filters that arrived by URL and have no slider position yet (A7).

process.env.TZ = "UTC";

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  SORTS,
  UI_ONLY_PARAMS,
  applyParamsPure,
  clearRangePair,
  pendingRangeChips,
} from "../src/lib/searchState.ts";

const NO_BOUNDS = { size: null, mtime: null };

test("applyParamsPure: empty params → blank state, not searchable, nothing invalid", () => {
  const r = applyParamsPure({}, NO_BOUNDS);
  assert.equal(r.searchable, false);
  assert.deepEqual(r.invalid, []);
  assert.deepEqual(r.state, {
    q: "",
    extension: "",
    selectedCategories: [],
    selectedGroups: [],
    selectedLibraries: [],
    selectedPrincipals: [],
    worldReadable: "",
    exposure: "",
    shareMismatch: false,
    selectedTags: [],
    includeSidecars: false,
    searchIn: "all",
    sortMode: "",
    view: "list",
    geoBox: null,
    semantic: 0,
    pendingRange: {},
  });
});

test("applyParamsPure: valid values land in state; lists split on commas and trim", () => {
  const r = applyParamsPure(
    {
      q: "holiday",
      extension: "jpg",
      file_category: "image, video,,",
      file_group: "raw-photo",
      library: "l1,l2",
      principal: "alice,bob",
      world_readable: "false",
      exposure: "restricted",
      share_mismatch: "true",
      tags: "beach, 2024",
      include_sidecars: "true",
      search_in: "names",
      sort: "size:desc",
      view: "grid",
      semantic: "0.4",
    },
    NO_BOUNDS,
  );
  assert.deepEqual(r.invalid, []);
  assert.equal(r.searchable, true);
  const s = r.state;
  assert.equal(s.q, "holiday");
  assert.equal(s.extension, "jpg");
  assert.deepEqual(s.selectedCategories, ["image", "video"]);
  assert.deepEqual(s.selectedGroups, ["raw-photo"]);
  assert.deepEqual(s.selectedLibraries, ["l1", "l2"]);
  assert.deepEqual(s.selectedPrincipals, ["alice", "bob"]);
  assert.equal(s.worldReadable, "false");
  assert.equal(s.exposure, "restricted");
  assert.equal(s.shareMismatch, true);
  assert.deepEqual(s.selectedTags, ["beach", "2024"]);
  assert.equal(s.includeSidecars, true);
  assert.equal(s.searchIn, "names");
  assert.equal(s.sortMode, "size:desc");
  assert.equal(s.view, "grid");
  assert.equal(s.semantic, 0.4);
});

test("applyParamsPure: hash wins over q (exact-hash lookups ride the `hash` key)", () => {
  assert.equal(applyParamsPure({ hash: "deadbeefdeadbeef", q: "x" }, NO_BOUNDS).state.q, "deadbeefdeadbeef");
});

test("applyParamsPure: unknown enum values are dropped and named in `invalid`", () => {
  const cases: [Record<string, string>, string, unknown, string[]][] = [
    [{ q: "x", search_in: "body" }, "searchIn", "all", ["search_in"]],
    [{ q: "x", sort: "random" }, "sortMode", "", ["sort"]],
    [{ q: "x", view: "table" }, "view", "list", ["view"]],
    [{ q: "x", exposure: "everyone" }, "exposure", "", ["exposure"]],
    [{ q: "x", world_readable: "maybe" }, "worldReadable", "", ["world_readable"]],
  ];
  for (const [p, field, want, invalid] of cases) {
    const r = applyParamsPure(p, NO_BOUNDS);
    assert.equal((r.state as Record<string, unknown>)[field], want, JSON.stringify(p));
    assert.deepEqual(r.invalid, invalid, JSON.stringify(p));
  }
  // Two at once, in param order — the page shows them in one toast.
  assert.deepEqual(applyParamsPure({ sort: "x", search_in: "y", q: "z" }, NO_BOUNDS).invalid, ["sort", "search_in"]);
  // Every SORTS option is accepted.
  for (const s of SORTS) assert.deepEqual(applyParamsPure({ sort: s.value }, NO_BOUNDS).invalid, []);
});

test("applyParamsPure: semantic must be a number in [0, 1]", () => {
  const cases: [string, number, boolean][] = [
    ["0", 0, true],
    ["0.5", 0.5, true],
    ["1", 1, true],
    ["abc", 0, false],
    ["NaN", 0, false],
    ["1.5", 0, false],
    ["-0.1", 0, false],
  ];
  for (const [raw, want, ok] of cases) {
    const r = applyParamsPure({ semantic: raw }, NO_BOUNDS);
    assert.equal(r.state.semantic, want, raw);
    assert.deepEqual(r.invalid, ok ? [] : ["semantic"], raw);
  }
});

test("applyParamsPure: geo box is all-or-nothing and never inverted", () => {
  const box = { geo_top_lat: "50", geo_right_lng: "10", geo_bottom_lat: "40", geo_left_lng: "0" };
  const ok = applyParamsPure(box, NO_BOUNDS);
  assert.deepEqual(ok.state.geoBox, { top: 50, right: 10, bottom: 40, left: 0 });
  assert.deepEqual(ok.invalid, []);
  assert.equal(ok.searchable, true);

  const partial = applyParamsPure({ geo_top_lat: "50", geo_bottom_lat: "40", q: "x" }, NO_BOUNDS);
  assert.equal(partial.state.geoBox, null);
  assert.deepEqual(partial.invalid, ["geo_top_lat", "geo_bottom_lat"]);

  const inverted = applyParamsPure({ ...box, geo_top_lat: "30" }, NO_BOUNDS);
  assert.equal(inverted.state.geoBox, null);
  assert.deepEqual(inverted.invalid, ["geo_top_lat", "geo_right_lng", "geo_bottom_lat", "geo_left_lng"]);
  assert.equal(inverted.searchable, false, "a dropped box filters nothing");

  const nan = applyParamsPure({ ...box, geo_left_lng: "west" }, NO_BOUNDS);
  assert.equal(nan.state.geoBox, null);
  assert.equal(nan.invalid.length, 4);
});

test("applyParamsPure: range params ride pendingRange; non-numeric ones are dropped", () => {
  const r = applyParamsPure({ size_gte: "1048576", mtime_lte: "1704067200", size_lte: "big" }, NO_BOUNDS);
  assert.deepEqual(r.state.pendingRange, { size_gte: "1048576", mtime_lte: "1704067200" });
  assert.deepEqual(r.invalid, ["size_lte"]);
  assert.equal(r.searchable, true);
});

test("applyParamsPure: unknown keys are reported, not silently kept", () => {
  const r = applyParamsPure({ q: "x", colour: "red" }, NO_BOUNDS);
  assert.deepEqual(r.invalid, ["colour"]);
});

test("applyParamsPure: display-only params are not searchable (A6 blank slate)", () => {
  const cases: [Record<string, string>, boolean][] = [
    [{}, false],
    [{ view: "grid" }, false],
    [{ sort: "newest", view: "map", semantic: "0.3" }, false],
    [{ q: "" }, false],
    [{ search_in: "bogus" }, false],
    [{ colour: "red" }, false],
    [{ q: "x" }, true],
    [{ file_category: "video" }, true],
    [{ search_in: "content" }, true],
    [{ include_sidecars: "true" }, true],
  ];
  for (const [p, want] of cases) assert.equal(applyParamsPure(p, NO_BOUNDS).searchable, want, JSON.stringify(p));
});

test("applyParamsPure: sliders are reset to the known bounds, absent without bounds", () => {
  const r = applyParamsPure({ q: "x" }, { size: { min: 1, max: 99 }, mtime: { min: 10, max: 20 } });
  assert.equal(r.state.sizeLo, 1);
  assert.equal(r.state.sizeHi, 99);
  assert.equal(r.state.mtimeLo, 10);
  assert.equal(r.state.mtimeHi, 20);
  const none = applyParamsPure({ q: "x" }, { size: null, mtime: { min: 10, max: 20 } });
  assert.equal("sizeLo" in none.state, false);
  assert.equal(none.state.mtimeLo, 10);
});

test("pendingRangeChips: one chip per pair, labels via format.ts", () => {
  const MB = 1024 ** 2;
  const GB = 1024 ** 3;
  const jan1 = String(Date.UTC(2024, 0, 1) / 1000);
  const jun30 = String(Date.UTC(2024, 5, 30) / 1000);
  assert.deepEqual(pendingRangeChips({}), []);
  assert.deepEqual(pendingRangeChips({ size_gte: String(MB) }), [
    { key: "pending:size", pair: "size", label: "size ≥ 1 MB" },
  ]);
  assert.deepEqual(pendingRangeChips({ size_lte: String(4 * GB) }), [
    { key: "pending:size", pair: "size", label: "size ≤ 4 GB" },
  ]);
  assert.deepEqual(pendingRangeChips({ size_gte: String(MB), size_lte: String(4 * GB) }), [
    { key: "pending:size", pair: "size", label: "size 1 MB–4 GB" },
  ]);
  assert.deepEqual(pendingRangeChips({ mtime_gte: jan1, size_gte: String(MB) }), [
    { key: "pending:size", pair: "size", label: "size ≥ 1 MB" },
    { key: "pending:date", pair: "date", label: "date ≥ 2024-01-01" },
  ]);
  assert.deepEqual(pendingRangeChips({ mtime_gte: jan1, mtime_lte: jun30 }), [
    { key: "pending:date", pair: "date", label: "date 2024-01-01–2024-06-30" },
  ]);
  assert.deepEqual(pendingRangeChips({ mtime_lte: jun30 }), [
    { key: "pending:date", pair: "date", label: "date ≤ 2024-06-30" },
  ]);
  // Blank values (a cleared pair) produce no chip.
  assert.deepEqual(pendingRangeChips({ size_gte: "", size_lte: "" }), []);
});

test("clearRangePair: removes only that pair's two keys", () => {
  const p = { size_gte: "1", size_lte: "2", mtime_gte: "3" };
  assert.deepEqual(clearRangePair(p, "size"), { mtime_gte: "3" });
  assert.deepEqual(clearRangePair(p, "date"), { size_gte: "1", size_lte: "2" });
  assert.deepEqual(p, { size_gte: "1", size_lte: "2", mtime_gte: "3" }, "input untouched");
});

test("UI_ONLY_PARAMS: view is the only hash-only key", () => {
  assert.deepEqual([...UI_ONLY_PARAMS], ["view"]);
});
