// deleteImpactDetails: GET /libraries/{id}/delete-impact → the typed-name
// confirm's consequence bullets (B16, Ruling 7). deleteBlockedReason: a queued
// or running scan job means the DELETE would 409 (B17), so it is not offered.

import assert from "node:assert/strict";
import { test } from "node:test";

import type { LibraryDeleteImpact } from "../src/lib/api";
import {
  DELETE_SUCCESS_NOTE,
  deleteBlockedReason,
  deleteImpactDetails,
} from "../src/lib/libraryDelete.ts";

const zero: LibraryDeleteImpact = {
  library_id: "L1", name: "Movies",
  items: 0, scan_runs: 0, alert_rules: 0, alert_events: 0,
  path_grants: 0, scan_paths: 0, pending_jobs: 0, scan_job_pending: false,
};
const imp = (o: Partial<LibraryDeleteImpact>): LibraryDeleteImpact => ({ ...zero, ...o });
const NO_UNDO = "This cannot be undone";

test("plural counts with locale grouping, in a fixed order", () => {
  const d = deleteImpactDetails(
    imp({ items: 1_204_331, scan_runs: 4, alert_rules: 3, alert_events: 12, path_grants: 2, scan_paths: 1 }),
    "en-US",
  );
  assert.deepEqual(d, [
    "1,204,331 indexed items (their search results disappear)",
    "4 scan runs",
    "3 alert rules and their 12 delivery events",
    "2 permission grants",
    "1 hot folder",
    NO_UNDO,
  ]);
});

test("singulars", () => {
  const d = deleteImpactDetails(
    imp({ items: 1, scan_runs: 1, alert_rules: 1, alert_events: 1, path_grants: 1, scan_paths: 1, pending_jobs: 1 }),
    "en-US",
  );
  assert.deepEqual(d, [
    "1 indexed item (its search result disappears)",
    "1 scan run",
    "1 alert rule and its 1 delivery event",
    "1 permission grant",
    "1 hot folder",
    "1 pending job for this library will fail or be dropped",
    NO_UNDO,
  ]);
});

test("zero counts are omitted; alert rules without events say so plainly", () => {
  const d = deleteImpactDetails(imp({ alert_rules: 2, scan_paths: 3 }), "en-US");
  assert.deepEqual(d, ["2 alert rules", "3 hot folders", NO_UNDO]);
});

test("pending jobs line is plural and only when > 0", () => {
  assert.ok(deleteImpactDetails(imp({ pending_jobs: 5 }), "en-US")
    .includes("5 pending jobs for this library will fail or be dropped"));
  assert.ok(!deleteImpactDetails(imp({ items: 3 }), "en-US").some((l) => /pending/.test(l)));
});

test("an empty library says nothing else is removed", () => {
  assert.deepEqual(deleteImpactDetails(zero, "en-US"), [
    "Only the library definition: it has no indexed items, scan runs, alert rules, grants or hot folders",
    NO_UNDO,
  ]);
});

test("impact unavailable: delete stays possible, counts flagged as unknown", () => {
  assert.deepEqual(deleteImpactDetails(null, "en-US"), [
    "Counts unavailable: could not load what this delete removes",
    "All indexed items, scan history, library alert rules, grants and hot folders are removed",
    NO_UNDO,
  ]);
});

test("locale numbers follow the given locale", () => {
  assert.equal(deleteImpactDetails(imp({ items: 1234 }), "de-DE")[0],
    "1.234 indexed items (their search results disappear)");
});

test("blocked while a scan job is queued or running; otherwise null", () => {
  assert.equal(deleteBlockedReason(null), null);
  assert.equal(deleteBlockedReason(imp({ items: 9 })), null);
  assert.equal(
    deleteBlockedReason(imp({ scan_job_pending: true })),
    "Movies can't be deleted while a scan is queued or running. Stop or cancel the scan first, then delete.",
  );
});

test("success note mentions background index cleanup", () => {
  assert.match(DELETE_SUCCESS_NOTE, /search index/i);
  assert.match(DELETE_SUCCESS_NOTE, /background/i);
});
