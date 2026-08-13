// IN-T4 — the rules behind bulk metadata editing, tested where they live.
//
// The one that matters most: `tags` REPLACES the whole list on the wire, so
// "add a tag to 300 files" is 300 individually-computed lists. A bug in that
// arithmetic does not throw — it silently DELETES tags. Same for the absent-vs-
// null distinction (untouched vs cleared) and for chunking at the server's
// 500-key cap. All of it is pure, so all of it is pinned here rather than
// discovered in production.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BATCH_CHUNK,
  addTags,
  applicableFields,
  buildPatches,
  chunk,
  coerceCustomValue,
  computeTagPatch,
  describeResult,
  fieldApplies,
  mergeSummaries,
  opsAreEmpty,
  rangeIndices,
  removeTags,
  summarizeResults,
  EMPTY_OPS,
  type BulkOps,
  type BulkTarget,
} from "../src/lib/bulkEdit.ts";
import type { CustomField, CustomFieldType } from "../src/lib/api.ts";

// ---- fixtures --------------------------------------------------------------

function def(
  name: string,
  data_type: CustomFieldType = "string",
  over: Partial<CustomField> = {},
): CustomField {
  return {
    id: `id-${name}`,
    name,
    label: name,
    data_type,
    select_options: null,
    applies_to: [],
    library_ids: [],
    facetable: false,
    sortable: false,
    required: false,
    created_at: "2026-08-13T00:00:00Z",
    ...over,
  };
}

function target(id: string, tags: string[], cat = "video", lib = "L1"): BulkTarget {
  return { id, tags, file_category: cat, library_id: lib };
}

function ops(over: Partial<BulkOps> = {}): BulkOps {
  return { ...EMPTY_OPS, ...over };
}

// ---- chunking --------------------------------------------------------------

test("chunk: exact-multiple, remainder, empty and single-chunk cases", () => {
  assert.equal(BATCH_CHUNK, 500);
  assert.deepEqual(chunk([], 500), []);
  assert.deepEqual(chunk([1, 2, 3], 500), [[1, 2, 3]]);
  assert.deepEqual(chunk([1, 2, 3, 4, 5], 2), [[1, 2], [3, 4], [5]]);

  const ids = Array.from({ length: 1200 }, (_, i) => `i${i}`);
  const groups = chunk(ids);
  assert.equal(groups.length, 3);
  assert.deepEqual(groups.map((g) => g.length), [500, 500, 200]);
  // Nothing lost, nothing duplicated, order preserved.
  assert.deepEqual(groups.flat(), ids);

  const exact = chunk(Array.from({ length: 1000 }, (_, i) => i));
  assert.equal(exact.length, 2);
  assert.deepEqual(exact.map((g) => g.length), [500, 500]);

  assert.throws(() => chunk([1], 0), /chunk size/);
});

// ---- tag arithmetic --------------------------------------------------------

test("addTags: union, existing spelling wins, order preserved", () => {
  assert.deepEqual(addTags(["hdr", "remux"], ["4k"]), ["hdr", "remux", "4k"]);
  // Already present (case-insensitively) — the item's own spelling is kept.
  assert.deepEqual(addTags(["HDR"], ["hdr"]), ["HDR"]);
  // Duplicates within the add list collapse.
  assert.deepEqual(addTags([], ["x", "X", " x "]), ["x"]);
  // Whitespace-only additions are ignored.
  assert.deepEqual(addTags(["a"], ["  ", ""]), ["a"]);
});

test("removeTags: case-insensitive, leaves everything else untouched", () => {
  assert.deepEqual(removeTags(["HDR", "remux", "4k"], ["hdr"]), ["remux", "4k"]);
  assert.deepEqual(removeTags(["a", "b"], ["z"]), ["a", "b"]);
  assert.deepEqual(removeTags([], ["z"]), []);
});

test("computeTagPatch: returns null for a no-op so the item is left out entirely", () => {
  assert.equal(computeTagPatch(["a"], [], []), null); // nothing asked for
  assert.equal(computeTagPatch(["a"], ["a"], []), null); // already has it
  assert.equal(computeTagPatch(["a"], [], ["z"]), null); // does not have it
  assert.deepEqual(computeTagPatch(["a"], ["b"], []), ["a", "b"]);
  assert.deepEqual(computeTagPatch(["a", "b"], [], ["a"]), ["b"]);
});

test("computeTagPatch: remove runs BEFORE add, so remove+add re-spells a tag", () => {
  assert.deepEqual(computeTagPatch(["HDR"], ["hdr"], ["HDR"]), ["hdr"]);
});

test("the whole point: one operation, a DIFFERENT list per item", () => {
  const targets = [
    target("a", ["hdr"]),
    target("b", ["remux", "4k"]),
    target("c", []),
    target("d", ["HDR"]), // already tagged, differently spelled -> untouched
  ];
  const patches = buildPatches(targets, ops({ tagsAdd: ["hdr"] }));
  assert.deepEqual(patches.a, undefined, "a already has it — no patch at all");
  assert.deepEqual(patches.b, { tags: ["remux", "4k", "hdr"] });
  assert.deepEqual(patches.c, { tags: ["hdr"] });
  assert.deepEqual(patches.d, undefined, "case-insensitive match — no patch");
});

// ---- applicability intersection --------------------------------------------

test("fieldApplies: empty applies_to / library_ids mean 'all'", () => {
  assert.ok(fieldApplies(def("f"), { file_category: "video", library_id: "L1" }));
  assert.ok(
    fieldApplies(def("f", "string", { applies_to: ["video"] }), {
      file_category: "video",
      library_id: "L9",
    }),
  );
  assert.ok(
    !fieldApplies(def("f", "string", { applies_to: ["audio"] }), {
      file_category: "video",
      library_id: "L1",
    }),
  );
  assert.ok(
    !fieldApplies(def("f", "string", { library_ids: ["L2"] }), {
      file_category: "video",
      library_id: "L1",
    }),
  );
});

test("applicableFields: INTERSECTION across the whole selection, not the union", () => {
  const defs = [
    def("everywhere"),
    def("videoOnly", "string", { applies_to: ["video"] }),
    def("libOne", "string", { library_ids: ["L1"] }),
  ];
  const mixedCategory = [target("a", [], "video", "L1"), target("b", [], "audio", "L1")];
  assert.deepEqual(
    applicableFields(defs, mixedCategory).map((d) => d.name),
    ["everywhere", "libOne"],
    "videoOnly must NOT be offered when an audio item is selected",
  );

  const mixedLibrary = [target("a", [], "video", "L1"), target("b", [], "video", "L2")];
  assert.deepEqual(
    applicableFields(defs, mixedLibrary).map((d) => d.name),
    ["everywhere", "videoOnly"],
  );

  const uniform = [target("a", [], "video", "L1"), target("b", [], "video", "L1")];
  assert.deepEqual(applicableFields(defs, uniform).map((d) => d.name), [
    "everywhere",
    "videoOnly",
    "libOne",
  ]);

  // Nothing selected -> nothing offered (never "everything").
  assert.deepEqual(applicableFields(defs, []), []);
});

// ---- typed coercion --------------------------------------------------------

test("coerceCustomValue: integers, floats and their rejections", () => {
  assert.deepEqual(coerceCustomValue(def("n", "integer"), "42"), { ok: true, value: 42 });
  assert.equal(coerceCustomValue(def("n", "integer"), "4.5").ok, false);
  assert.equal(coerceCustomValue(def("n", "integer"), "abc").ok, false);
  assert.equal(coerceCustomValue(def("n", "integer"), "").ok, false);
  assert.deepEqual(coerceCustomValue(def("f", "float"), "4.5"), { ok: true, value: 4.5 });
  assert.equal(coerceCustomValue(def("f", "float"), "Infinity").ok, false);
});

test("coerceCustomValue: booleans come through from either control shape", () => {
  assert.deepEqual(coerceCustomValue(def("b", "boolean"), true), { ok: true, value: true });
  assert.deepEqual(coerceCustomValue(def("b", "boolean"), "true"), { ok: true, value: true });
  assert.deepEqual(coerceCustomValue(def("b", "boolean"), ""), { ok: true, value: false });
});

test("coerceCustomValue: select membership is enforced CLIENT-side (the server does not)", () => {
  const d = def("s", "select", { select_options: ["red", "green"] });
  assert.deepEqual(coerceCustomValue(d, "green"), { ok: true, value: "green" });
  assert.equal(coerceCustomValue(d, "blue").ok, false);
  assert.equal(coerceCustomValue(d, "").ok, false);
  // A select with no options defined accepts nothing at all.
  assert.equal(coerceCustomValue(def("s2", "select"), "anything").ok, false);
});

test("coerceCustomValue: dates must be ISO days; strings/urls must be non-blank", () => {
  assert.deepEqual(coerceCustomValue(def("d", "date"), "2026-08-13"), {
    ok: true,
    value: "2026-08-13",
  });
  assert.equal(coerceCustomValue(def("d", "date"), "13/08/2026").ok, false);
  assert.equal(coerceCustomValue(def("u", "url"), "  ").ok, false);
  assert.deepEqual(coerceCustomValue(def("t", "string"), "shelf 4"), {
    ok: true,
    value: "shelf 4",
  });
  assert.equal(coerceCustomValue(def("t", "string"), "   ").ok, false);
});

// ---- patch construction ----------------------------------------------------

test("buildPatches: year set vs clear vs untouched (absent != null)", () => {
  const ts = [target("a", []), target("b", [])];
  assert.deepEqual(buildPatches(ts, ops({ yearMode: "set", year: 1999 })), {
    a: { year: 1999 },
    b: { year: 1999 },
  });
  assert.deepEqual(buildPatches(ts, ops({ yearMode: "clear" })), {
    a: { year: null },
    b: { year: null },
  });
  // "none" contributes nothing, so with no other op there is no patch at all.
  assert.deepEqual(buildPatches(ts, ops({ yearMode: "none" })), {});
});

test("buildPatches: a custom field set writes the value; clear writes an explicit null", () => {
  const ts = [target("a", [])];
  assert.deepEqual(
    buildPatches(ts, ops({ fieldName: "shelf", fieldMode: "set", fieldValue: "A4" })),
    { a: { user_metadata: { shelf: "A4" } } },
  );
  assert.deepEqual(buildPatches(ts, ops({ fieldName: "shelf", fieldMode: "clear" })), {
    a: { user_metadata: { shelf: null } },
  });
  // No field chosen -> the key never appears.
  assert.deepEqual(buildPatches(ts, ops({ fieldMode: "set", fieldValue: "x" })), {});
});

test("buildPatches: several operations combine into ONE patch per item", () => {
  const patches = buildPatches(
    [target("a", ["old"])],
    ops({
      tagsAdd: ["new"],
      tagsRemove: ["old"],
      yearMode: "set",
      year: 2001,
      fieldName: "shelf",
      fieldMode: "set",
      fieldValue: 7,
    }),
  );
  assert.deepEqual(patches, {
    a: { tags: ["new"], year: 2001, user_metadata: { shelf: 7 } },
  });
});

test("buildPatches: never sends `title` — bulk title editing is deliberately absent", () => {
  const patches = buildPatches([target("a", [])], ops({ yearMode: "clear" }));
  assert.ok(!("title" in patches.a));
});

test("opsAreEmpty: guards the Apply button against a no-op round trip", () => {
  assert.ok(opsAreEmpty(EMPTY_OPS));
  assert.ok(!opsAreEmpty(ops({ tagsAdd: ["x"] })));
  assert.ok(!opsAreEmpty(ops({ tagsRemove: ["x"] })));
  assert.ok(!opsAreEmpty(ops({ yearMode: "clear" })));
  assert.ok(!opsAreEmpty(ops({ fieldName: "shelf", fieldMode: "clear" })));
  // A mode without a field name is still nothing to do.
  assert.ok(opsAreEmpty(ops({ fieldMode: "clear" })));
});

// ---- result reading --------------------------------------------------------

test("summarizeResults: every non-ok value becomes a visible failure", () => {
  const s = summarizeResults({
    "id-1": "ok",
    "id-2": "error: You don't have permission to edit this item.",
    "id-3": { error: "validation", detail: [{ loc: ["shelf"], msg: "expected integer" }] },
    "id-4": "ok",
  });
  assert.deepEqual(s.ok, ["id-1", "id-4"]);
  assert.equal(s.failures.length, 2);
  assert.deepEqual(s.failures[0], {
    id: "id-2",
    reason: "You don't have permission to edit this item.",
  });
  assert.deepEqual(s.failures[1], { id: "id-3", reason: "shelf: expected integer" });
});

test("summarizeResults: an empty/missing results map is not a crash", () => {
  assert.deepEqual(summarizeResults({}), { ok: [], failures: [] });
  assert.deepEqual(
    summarizeResults(undefined as unknown as Record<string, unknown>),
    { ok: [], failures: [] },
  );
});

test("describeResult: unexpected shapes still render as text, never [object Object]", () => {
  assert.equal(describeResult("error: nope"), "nope");
  assert.equal(describeResult({ error: "boom" }), "boom");
  assert.equal(describeResult({ error: "validation", detail: ["a", "b"] }), "a; b");
  assert.equal(describeResult(null), "null");
  assert.ok(!describeResult({ weird: 1 }).includes("[object"));
});

test("mergeSummaries: chunked responses report as one outcome", () => {
  const merged = mergeSummaries([
    { ok: ["a"], failures: [{ id: "b", reason: "denied" }] },
    { ok: ["c"], failures: [] },
  ]);
  assert.deepEqual(merged.ok, ["a", "c"]);
  assert.deepEqual(merged.failures, [{ id: "b", reason: "denied" }]);
  assert.deepEqual(mergeSummaries([]), { ok: [], failures: [] });
});

// ---- shift-click range -----------------------------------------------------

test("rangeIndices: inclusive, and direction-independent", () => {
  assert.deepEqual(rangeIndices(2, 5), [2, 3, 4, 5]);
  assert.deepEqual(rangeIndices(5, 2), [2, 3, 4, 5]);
  assert.deepEqual(rangeIndices(3, 3), [3]);
  assert.deepEqual(rangeIndices(0, 1), [0, 1]);
});
