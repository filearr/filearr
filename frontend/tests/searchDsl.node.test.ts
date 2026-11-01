// dslToParams(): the Search page lifts the translatable query-DSL operators into
// the flat /search params the backend has (ruling 1, finding A2). Everything the
// backend cannot express is reported in `unsupported` and removed from `q`.
// Local-date cases pin the zone (Tokyo, UTC+9, no DST) so "local midnight" is
// visibly not UTC midnight.

process.env.TZ = "Asia/Tokyo";

import assert from "node:assert/strict";
import { test } from "node:test";

import { dslToParams, mergeDslParams } from "../src/lib/searchDsl.ts";
import { DSL_SECTIONS, dslSectionsFor } from "../src/lib/dslHelp.ts";

const NOW = Date.UTC(2026, 10, 1, 12, 0, 0, 750); // fractional second: floored
const NOW_S = Math.floor(NOW / 1000);
const DAY = 86_400;
const K = 1024;
const M = 1024 ** 2;
const G = 1024 ** 3;
const T = 1024 ** 4;
const midnight = (y: number, m: number, d: number): number => new Date(y, m - 1, d).getTime() / 1000;

type Row = [string, { q: string; params: Record<string, string>; unsupported: string[] }];

const rows: Row[] = [
  ["", { q: "", params: {}, unsupported: [] }],
  ["   \t ", { q: "", params: {}, unsupported: [] }],
  ["kind:video", { q: "", params: { file_category: "video" }, unsupported: [] }],
  ["kind:video kind:audio", { q: "", params: { file_category: "video,audio" }, unsupported: [] }],
  ["KIND:Video", { q: "", params: { file_category: "video" }, unsupported: [] }],
  ["ext:pdf", { q: "", params: { extension: "pdf" }, unsupported: [] }],
  ["ext:.PDF", { q: "", params: { extension: "pdf" }, unsupported: [] }],
  ["ext:mp4;mkv", { q: "", params: { extension: "mp4" }, unsupported: ["ext:mkv"] }],
  ["ext:pdf ext:doc", { q: "", params: { extension: "pdf" }, unsupported: ["ext:doc"] }],
  ["size:>1G", { q: "", params: { size_gte: String(G + 1) }, unsupported: [] }],
  ["size:>=1G", { q: "", params: { size_gte: String(G) }, unsupported: [] }],
  ["size:<500K", { q: "", params: { size_lte: String(500 * K - 1) }, unsupported: [] }],
  ["size:<=500K", { q: "", params: { size_lte: String(500 * K) }, unsupported: [] }],
  ["size:=10M", { q: "", params: { size_gte: String(10 * M), size_lte: String(10 * M) }, unsupported: [] }],
  ["size:10M", { q: "", params: { size_gte: String(10 * M), size_lte: String(10 * M) }, unsupported: [] }],
  ["size:100M..4G", { q: "", params: { size_gte: String(100 * M), size_lte: String(4 * G) }, unsupported: [] }],
  ["size:>2T", { q: "", params: { size_gte: String(2 * T + 1) }, unsupported: [] }],
  ["size:>4096", { q: "", params: { size_gte: "4097" }, unsupported: [] }],
  ["size:>1m", { q: "", params: { size_gte: String(M + 1) }, unsupported: [] }],
  ["size:>1M size:<1G", { q: "", params: { size_gte: String(M + 1), size_lte: String(G - 1) }, unsupported: [] }],
  ["size:abc", { q: "", params: {}, unsupported: ["size:abc"] }],
  ["size:1.5G", { q: "", params: {}, unsupported: ["size:1.5G"] }],
  ["size:<0", { q: "", params: {}, unsupported: ["size:<0"] }],
  ["size:>1G..2G", { q: "", params: {}, unsupported: ["size:>1G..2G"] }],
  ["modified:>7d", { q: "", params: { mtime_gte: String(NOW_S - 7 * DAY) }, unsupported: [] }],
  ["modified:<30d", { q: "", params: { mtime_lte: String(NOW_S - 30 * DAY) }, unsupported: [] }],
  ["modified:>12h", { q: "", params: { mtime_gte: String(NOW_S - 12 * 3600) }, unsupported: [] }],
  ["modified:>2w", { q: "", params: { mtime_gte: String(NOW_S - 14 * DAY) }, unsupported: [] }],
  ["modified:>=2025-01-01", { q: "", params: { mtime_gte: String(midnight(2025, 1, 1)) }, unsupported: [] }],
  ["modified:>2025-01-01", { q: "", params: { mtime_gte: String(midnight(2025, 1, 2)) }, unsupported: [] }],
  ["modified:<2025-01-01", { q: "", params: { mtime_lte: String(midnight(2025, 1, 1) - 1) }, unsupported: [] }],
  ["modified:<=2025-01-01", { q: "", params: { mtime_lte: String(midnight(2025, 1, 2) - 1) }, unsupported: [] }],
  [
    "modified:2025-01-01",
    { q: "", params: { mtime_gte: String(midnight(2025, 1, 1)), mtime_lte: String(midnight(2025, 1, 2) - 1) }, unsupported: [] },
  ],
  [
    "modified:2024-01-01..2024-12-31",
    { q: "", params: { mtime_gte: String(midnight(2024, 1, 1)), mtime_lte: String(midnight(2025, 1, 1) - 1) }, unsupported: [] },
  ],
  ["modified:2025-02-31", { q: "", params: {}, unsupported: ["modified:2025-02-31"] }],
  ["modified:7d", { q: "", params: {}, unsupported: ["modified:7d"] }],
  ["modified:30d..7d", { q: "", params: {}, unsupported: ["modified:30d..7d"] }],
  ["modified:soon", { q: "", params: {}, unsupported: ["modified:soon"] }],
  ["tag:archived tag:x", { q: "", params: { tags: "archived,x" }, unsupported: [] }],
  ["tag:Archived", { q: "", params: { tags: "Archived" }, unsupported: [] }],
  ['tag:"to do"', { q: "", params: { tags: "to do" }, unsupported: [] }],
  ["tag:a,b", { q: "", params: {}, unsupported: ["tag:a,b"] }],
  ['-word "a phrase" kind:video report', { q: '-word "a phrase" report', params: { file_category: "video" }, unsupported: [] }],
  ["meta.height:>=1080", { q: "", params: {}, unsupported: ["meta.height:>=1080"] }],
  ["cf.rating:>=4", { q: "", params: {}, unsupported: ["cf.rating:>=4"] }],
  ["path:*/x/*", { q: "", params: {}, unsupported: ["path:*/x/*"] }],
  ['path:"*/Season 01/*"', { q: "", params: {}, unsupported: ['path:"*/Season 01/*"'] }],
  ["created:>7d", { q: "", params: {}, unsupported: ["created:>7d"] }],
  ["group:raw-photo", { q: "", params: { file_group: "raw-photo" }, unsupported: [] }],
  ["group:Raw-Photo group:audio-lossless group:raw-photo", { q: "", params: { file_group: "raw-photo,audio-lossless" }, unsupported: [] }],
  ["-group:pdf", { q: "", params: {}, unsupported: ["-group:pdf"] }],
  ["group:a_b", { q: "", params: { file_group: "a_b" }, unsupported: [] }],
  ["group:a,b", { q: "", params: {}, unsupported: ["group:a,b"] }],
  ["kind:video,audio", { q: "", params: {}, unsupported: ["kind:video,audio"] }],
  ["hash:e3b0c442", { q: "", params: {}, unsupported: ["hash:e3b0c442"] }],
  ["-tag:draft", { q: "", params: {}, unsupported: ["-tag:draft"] }],
  ["!ext:tmp", { q: "", params: {}, unsupported: ["!ext:tmp"] }],
  ["~fuzzy", { q: "", params: {}, unsupported: ["~fuzzy"] }],
  ["kind:", { q: "", params: {}, unsupported: ["kind:"] }],
  [
    "invoice meta.height:>=1080 kind:video  -draft",
    { q: "invoice -draft", params: { file_category: "video" }, unsupported: ["meta.height:>=1080"] },
  ],
  // A colon inside quotes is free text, as is an unknown key (URLs, times).
  ['"kind:video"', { q: '"kind:video"', params: {}, unsupported: [] }],
  ['"note: kind:video"', { q: '"note: kind:video"', params: {}, unsupported: [] }],
  ['"kind":video', { q: '"kind":video', params: {}, unsupported: [] }],
  ["http://example 10:30", { q: "http://example 10:30", params: {}, unsupported: [] }],
  ["-word !bang", { q: "-word !bang", params: {}, unsupported: [] }],
  ["~", { q: "~", params: {}, unsupported: [] }],
];

for (const [input, want] of rows) {
  test(`dslToParams(${JSON.stringify(input)})`, () => {
    assert.deepEqual(dslToParams(input, NOW), want);
  });
}

test("dslToParams: whitespace collapses between kept tokens, never inside quotes", () => {
  assert.deepEqual(dslToParams('  a   "b   c"\tkind:video   d ', NOW), {
    q: 'a "b   c" d',
    params: { file_category: "video" },
    unsupported: [],
  });
});

test("dslToParams: an unterminated quote runs to the end and stays free text", () => {
  assert.deepEqual(dslToParams('report "kind:video ext:pdf', NOW), {
    q: 'report "kind:video ext:pdf',
    params: {},
    unsupported: [],
  });
});

test("dslToParams: duplicate list values collapse; 'now' defaults to the clock", () => {
  assert.deepEqual(dslToParams("kind:video kind:Video tag:x tag:x", NOW).params, {
    file_category: "video",
    tags: "x",
  });
  const before = Math.floor(Date.now() / 1000);
  const got = Number(dslToParams("modified:>1d").params.mtime_gte);
  const after = Math.floor(Date.now() / 1000);
  assert.ok(got >= before - DAY && got <= after - DAY);
});

// ---- mergeDslParams: buildParams() precedence ------------------------------

test("mergeDslParams: no DSL tokens returns the base params untouched", () => {
  const base = { q: "  holiday  photos ", extension: "jpg", tags: "a" };
  const merged = mergeDslParams(base, dslToParams(base.q, NOW));
  assert.equal(merged, base);
  assert.deepEqual(merged, { q: "  holiday  photos ", extension: "jpg", tags: "a" });
});

test("mergeDslParams: lists union (chips first, de-duplicated); DSL extension and ranges win", () => {
  const base = {
    q: "kind:video kind:audio group:pdf ext:mkv tag:x tag:y size:>1G modified:<30d report",
    file_category: "image,video",
    file_group: "raw-photo",
    extension: "jpg",
    tags: "y",
    size_gte: "5",
    size_lte: "99999999999",
    mtime_gte: "100",
    library: "lib1",
  };
  const merged = mergeDslParams(base, dslToParams(base.q, NOW));
  assert.deepEqual(merged, {
    q: "report",
    file_category: "image,video,audio",
    file_group: "raw-photo,pdf",
    extension: "mkv",
    tags: "y,x",
    size_gte: String(G + 1),
    size_lte: "99999999999",
    mtime_gte: "100",
    mtime_lte: String(NOW_S - 30 * DAY),
    library: "lib1",
  });
});

test("mergeDslParams: unsupported-only queries still strip the token from q", () => {
  const merged = mergeDslParams({ q: "cats meta.x:1", sort: "newest" }, dslToParams("cats meta.x:1", NOW));
  assert.deepEqual(merged, { q: "cats", sort: "newest" });
});

// ---- help contexts ---------------------------------------------------------

test("dslSectionsFor('reports') keeps every section and every example", () => {
  const reports = dslSectionsFor("reports");
  assert.deepEqual(
    reports.map((s) => [s.title, s.examples.map((e) => e.q)]),
    DSL_SECTIONS.map((s) => [s.title, s.examples.map((e) => e.q)]),
  );
});

test("dslSectionsFor('search') lists only what search translates", () => {
  const search = dslSectionsFor("search");
  assert.deepEqual(
    search.map((s) => [s.title, s.examples.map((e) => e.q)]),
    [
      ["Free text", ["invoice", '"annual report"', '"quarterly report"']],
      ["Negation", ["-draft"]],
      ["kind:", ["kind:video", "kind:audio"]],
      ["group:", ["group:raw-photo", "group:audio-lossless"]],
      ["ext:", ["ext:pdf"]],
      ["size:", ["size:>1G", "size:<500K", "size:100M..4G"]],
      ["modified:", ["modified:>7d", "modified:<30d", "modified:>=2025-01-01"]],
      ["tag:", ["tag:archived"]],
    ],
  );
  const modified = search.find((s) => s.title === "modified:");
  assert.match(modified?.body ?? "", /modified:2024-01-01\.\.2024-12-31/, "search help shows the date-range form");
  for (const s of search) {
    assert.ok(!/created|several with ;|or !|~|exclude a tag/i.test(`${s.title} ${s.body}`), `${s.title}: ${s.body}`);
  }
});

test("every search-context help example translates with no unsupported token", () => {
  for (const sec of dslSectionsFor("search")) {
    for (const ex of sec.examples) {
      assert.deepEqual(dslToParams(ex.q, NOW).unsupported, [], `${sec.title}: ${ex.q}`);
    }
  }
});

test("the range form the search help documents translates to both bounds", () => {
  const body = dslSectionsFor("search").find((s) => s.title === "modified:")?.body ?? "";
  const form = /modified:\S+\.\.\S+?(?=\s|$)/.exec(body)?.[0] ?? "";
  assert.equal(form, "modified:2024-01-01..2024-12-31");
  assert.deepEqual(Object.keys(dslToParams(form, NOW).params), ["mtime_gte", "mtime_lte"]);
});
