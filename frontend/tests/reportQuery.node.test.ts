// reportQuery: the one place a canned report's on-screen state becomes request
// parameters, for a page run, a streaming download / background export, and a
// schedule's stored params. B2: an uncapped report's export carries no `limit`.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  editedScheduleParams,
  reportQuery,
  reportScheduleParams,
  type ReportQueryMeta,
} from "../src/lib/reportQuery.ts";

const uncapped: ReportQueryMeta = {
  is_capped: false,
  default_limit: 1000,
  supports_library: true,
  supports_threshold: true,
};
const capped: ReportQueryMeta = { ...uncapped, is_capped: true, default_limit: 100 };
const plain: ReportQueryMeta = { ...uncapped, supports_library: false, supports_threshold: false };

test("export of an uncapped report sends no limit (the whole result)", () => {
  assert.deepEqual(reportQuery(uncapped, { mode: "export" }), {});
  assert.equal("limit" in reportQuery(uncapped, { mode: "export", offset: 2000 }), false);
});

test("export of a capped report sends its definitional top-N", () => {
  assert.deepEqual(reportQuery(capped, { mode: "export" }), { limit: 100 });
});

test("export never sends an offset", () => {
  assert.equal("offset" in reportQuery(capped, { mode: "export", offset: 300 }), false);
});

test("page run sends the page size and a non-negative offset", () => {
  assert.deepEqual(reportQuery(uncapped, { mode: "page" }), { limit: 1000, offset: 0 });
  assert.deepEqual(reportQuery(capped, { mode: "page", offset: 200 }), { limit: 100, offset: 200 });
  assert.deepEqual(reportQuery(capped, { mode: "page", offset: -5 }), { limit: 100, offset: 0 });
});

test("library only when the report supports it and one is picked", () => {
  assert.deepEqual(reportQuery(uncapped, { mode: "export", libraryId: "L1" }), { libraryId: "L1" });
  assert.deepEqual(reportQuery(uncapped, { mode: "export", libraryId: "" }), {});
  assert.deepEqual(reportQuery(plain, { mode: "export", libraryId: "L1" }), {});
});

test("threshold only when supported and a positive whole number of days", () => {
  assert.deepEqual(reportQuery(uncapped, { mode: "export", thresholdDays: 730 }), { thresholdDays: 730 });
  assert.deepEqual(reportQuery(uncapped, { mode: "export", thresholdDays: 30.6 }), { thresholdDays: 31 });
  for (const t of [0, -1, Number.NaN, null, undefined, 36501]) {
    assert.deepEqual(reportQuery(uncapped, { mode: "export", thresholdDays: t }), {}, String(t));
  }
  assert.deepEqual(reportQuery(plain, { mode: "export", thresholdDays: 730 }), {});
});

test("schedule params are the export params in the stored snake_case shape", () => {
  assert.deepEqual(
    reportScheduleParams(uncapped, { libraryId: "L1", thresholdDays: 90 }),
    { library_id: "L1", threshold_days: 90 },
  );
  assert.deepEqual(reportScheduleParams(capped, {}), { limit: 100 });
  assert.deepEqual(reportScheduleParams(plain, { libraryId: "L1", thresholdDays: 90 }), {});
});

test("schedule edit keeps the stored limit and unknown keys (no limit field to change)", () => {
  const stored = { limit: 250, x_extra: "keep", library_id: "L1", threshold_days: 45 };
  const out = editedScheduleParams(stored, capped, { libraryId: "L1", thresholdDays: 45 });
  assert.equal(out.ok, true);
  assert.deepEqual(out.ok && out.params, stored);
  // Not even re-added for a capped report whose stored params lack it.
  const noLimit = editedScheduleParams({}, capped, { libraryId: "", thresholdDays: null });
  assert.deepEqual(noLimit.ok && noLimit.params, {});
});

test("schedule edit: blank threshold = not pinned (server default), not the page default", () => {
  const r = editedScheduleParams({ threshold_days: 45 }, uncapped, { libraryId: "", thresholdDays: null });
  assert.deepEqual(r.ok && r.params, {});
  const set = editedScheduleParams({}, uncapped, { libraryId: "L2", thresholdDays: 30 });
  assert.deepEqual(set.ok && set.params, { library_id: "L2", threshold_days: 30 });
});

test("schedule edit: an out-of-range threshold is an error, never silently dropped", () => {
  for (const bad of [0, -1, 1.5, 36501, Number.NaN]) {
    const r = editedScheduleParams({ threshold_days: 45 }, uncapped, { libraryId: "", thresholdDays: bad });
    assert.equal(r.ok, false, String(bad));
  }
});

test("schedule edit: fields the report does not support are left as stored", () => {
  const stored = { library_id: "L1", threshold_days: 9 };
  const r = editedScheduleParams(stored, plain, { libraryId: "", thresholdDays: null });
  assert.deepEqual(r.ok && r.params, stored);
});
