// libraryScanState: the Libraries page's scan row state, DOM-free.
// mergeRunning/applyFrame fold the filtered running list and SSE frames into
// per-scan live state (B9, B34); rowScan picks the row's run; failedReason
// follows Ruling 6 (B11); slowScan compares full runs only (B10); isQueued
// and planStreams keep the optimistic "queued" flag and the stream set right
// (B43: a backing-off stream is still "open", an ended one is never reopened).

import assert from "node:assert/strict";
import { test } from "node:test";

import type { LastScan, ScanRun } from "../src/lib/api";
import {
  applyFrame,
  failedReason,
  fmtCount,
  fmtLiveStats,
  isQueued,
  lastScanCounts,
  mergeRunning,
  planStreams,
  rowScan,
  scanRunReason,
  scopedSummary,
  slowScan,
  statNum,
  statStr,
} from "../src/lib/libraryScanState.ts";

const run = (over: Partial<ScanRun> = {}): ScanRun => ({
  id: "s1",
  library_id: "L1",
  started_at: "2026-11-02T10:00:00Z",
  finished_at: null,
  status: "running",
  stats: {},
  rel_path: null,
  ...over,
});

const last = (over: Partial<LastScan> = {}): LastScan => ({
  id: "s0",
  rel_path: null,
  error: null,
  reconcile_note: null,
  started_at: "2026-11-01T10:00:00Z",
  finished_at: "2026-11-01T10:05:00Z",
  status: "finished",
  ...over,
});

const n = (x: number) => x.toLocaleString();

test("statNum/statStr read typed values out of untyped stats", () => {
  const st: Record<string, unknown> = { seen: 5, bad: "7", nan: Number.NaN, error: "boom", list: ["a"] };
  assert.equal(statNum(st, "seen"), 5);
  assert.equal(statNum(st, "bad"), undefined);
  assert.equal(statNum(st, "nan"), undefined);
  assert.equal(statNum(st, "missing"), undefined);
  assert.equal(statNum(null, "seen"), undefined);
  assert.equal(statStr(st, "error"), "boom");
  assert.equal(statStr(st, "list"), undefined);
});

test("mergeRunning keeps only active runs and preserves SSE-only rate/elapsed", () => {
  const prev = {
    s1: { status: "running", seen: 1, rate: 4.5, elapsed: 10 },
    gone: { status: "running", seen: 9 },
  };
  const out = mergeRunning(prev, [
    run({ id: "s1", stats: { seen: 50, new: 2, pruned_paths: [".git"], error: "x" } }),
    run({ id: "s2", status: "stopping", stats: { seen: 3 } }),
  ]);
  assert.deepEqual(Object.keys(out).sort(), ["s1", "s2"], "an ended run drops out");
  assert.deepEqual(out.s1, { status: "running", seen: 50, new: 2, rate: 4.5, elapsed: 10 });
  assert.deepEqual(out.s2, { status: "stopping", seen: 3 });
});

test("mergeRunning ignores non-active statuses in the input", () => {
  const out = mergeRunning({}, [run({ status: "finished" })]);
  assert.deepEqual(out, {});
});

test("applyFrame folds a typed snapshot; junk fields and non-objects are ignored", () => {
  const base = { s1: { status: "running", seen: 1 } };
  const out = applyFrame(base, "s1", {
    scan_id: "s1", status: "running", seen: 20, rate: 3.2, elapsed: 7, excluded: 4,
    bytes_seen: 100, pruned_paths: ["x"], new: "nope",
  });
  assert.deepEqual(out.s1, { status: "running", seen: 20, rate: 3.2, elapsed: 7, excluded: 4, bytes_seen: 100 });
  assert.notEqual(out, base, "reassigned for reactivity");
  assert.equal(applyFrame(base, "s1", null), base);
  assert.equal(applyFrame(base, "s1", "x"), base);
  // A frame without a status keeps the previous one.
  assert.equal(applyFrame(base, "s1", { seen: 2 }).s1.status, "running");
});

test("rowScan prefers the library's full run over a newer scoped one", () => {
  const scoped = run({ id: "sc", rel_path: "Movies/new", started_at: "2026-11-02T11:00:00Z" });
  const full = run({ id: "full", started_at: "2026-11-02T09:00:00Z" });
  const other = run({ id: "o", library_id: "L2" });
  assert.equal(rowScan([other, scoped, full], "L1")?.id, "full");
  assert.equal(rowScan([other, scoped], "L1")?.id, "sc", "else the newest scoped");
  assert.equal(rowScan([other], "L1"), undefined);
});

test("failedReason (Ruling 6): SSE detail, else error, else reconcile_note, else a fallback", () => {
  assert.equal(failedReason(last({ status: "finished" }), {}), null, "only failed runs have a reason");
  assert.equal(failedReason(null, {}), null);
  const f = last({ id: "s9", status: "failed", error: "db error", reconcile_note: "orphan" });
  assert.equal(failedReason(f, { s9: "live detail" }), "live detail");
  assert.equal(failedReason(f, { other: "x" }), "db error");
  assert.equal(failedReason({ ...f, error: null }, {}), "orphan");
  assert.equal(failedReason({ ...f, error: null, reconcile_note: null }, {}), "failed (no reason recorded)");
  assert.equal(failedReason({ ...f, error: "" , reconcile_note: null }, {}), "failed (no reason recorded)");
});

test("scanRunReason reads the same order out of a ScanRun's untyped stats", () => {
  const f = run({ id: "r1", status: "failed", stats: { error: "crash", reconcile_note: "orphan" } });
  assert.equal(scanRunReason(run(), {}), null);
  assert.equal(scanRunReason(f, { r1: "sse" }), "sse");
  assert.equal(scanRunReason(f, {}), "crash");
  assert.equal(scanRunReason({ ...f, stats: { error: 5, reconcile_note: "orphan" } }, {}), "orphan");
  assert.equal(scanRunReason({ ...f, stats: {} }, {}), "failed (no reason recorded)");
});

test("slowScan: full runs only, >= 3 runs of history, < 60% of the median", () => {
  const ls = last({ files_per_s: 10, median_files_per_s: 50, throughput_runs: 5 });
  assert.match(slowScan(ls) ?? "", /10\.0 files\/s vs a 50\.0 files\/s median over the last 5 full scans/);
  assert.equal(slowScan({ ...ls, rel_path: "Sub" }), null, "a scoped run is never compared");
  assert.equal(slowScan({ ...ls, throughput_runs: 2 }), null);
  assert.equal(slowScan({ ...ls, files_per_s: 30 }), null);
  assert.equal(slowScan({ ...ls, median_files_per_s: 0 }), null);
  assert.equal(slowScan({ ...ls, files_per_s: null }), null);
});

test("counts are locale-formatted (B34)", () => {
  assert.equal(fmtCount(1234567), n(1234567));
  assert.equal(
    lastScanCounts(last({ seen: 12345, new: 0, changed: null, missing: 3 })),
    `seen ${n(12345)}, new 0, missing 3`,
  );
  assert.equal(
    fmtLiveStats("running", { status: "running", seen: 5000, new: 1, rate: 12.345 }, null),
    `running… — seen ${n(5000)}, new 1, 12.3/s`,
  );
  assert.equal(fmtLiveStats("stopping", { status: "stopping" }, null), "stopping…");
  assert.equal(
    fmtLiveStats("finished", { status: "finished", seen: 2 }, "2026-11-02T10:00:00Z"),
    "finished — seen 2",
  );
});

test("scopedSummary names the subtree compactly", () => {
  assert.equal(scopedSummary(null), null);
  assert.equal(scopedSummary(last({ rel_path: "Movies/Incoming" })), "targeted: Movies/Incoming");
  assert.equal(scopedSummary(last({ rel_path: "" })), "targeted: (library root)");
});

test("isQueued: queued until a run for the library exists or one is already active", () => {
  const at = Date.parse("2026-11-02T10:00:00Z");
  const lib = { last_scan: last({ started_at: "2026-11-01T10:00:00Z" }), last_scoped_scan: null };
  assert.equal(isQueued(undefined, undefined, lib), false, "never requested");
  assert.equal(isQueued(at, undefined, lib), true, "only an older run exists");
  assert.equal(isQueued(at, undefined, { last_scan: null, last_scoped_scan: null }), true);
  assert.equal(isQueued(at, run({ started_at: "2026-11-01T09:00:00Z" }), lib), false, "coalesced into an active run");
  // Our run already started AND finished between polls.
  assert.equal(
    isQueued(at, undefined, { ...lib, last_scan: last({ started_at: "2026-11-02T10:00:01Z" }) }),
    false,
  );
  // A scoped run that started after the request also counts (targeted scans).
  assert.equal(
    isQueued(at, undefined, { ...lib, last_scoped_scan: last({ rel_path: "x", started_at: "2026-11-02T09:59:59Z" }) }),
    false,
    "2 s of clock-skew slack",
  );
});

test("planStreams: open new active runs, close ended ones, keep backing-off ones, never reopen ended", () => {
  const p = planStreams(["a", "b", "c"], ["a", "x"], new Set(["c"]));
  assert.deepEqual(p.open, ["b"], "a is open (or backing off), c already ended");
  assert.deepEqual(p.close, ["x"]);
  assert.deepEqual(planStreams([], [], new Set()), { open: [], close: [] });
});
