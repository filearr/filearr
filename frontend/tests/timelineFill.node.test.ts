// timelineFill: the Timeline's x-axis must be honest (B38). The backend returns
// only non-empty UTC buckets; the page zero-fills the gaps between the first and
// last bucket so a ten-year hole never reads as consecutive months.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_FILLED_BUCKETS,
  bucketLabel,
  compactCount,
  fillTimeline,
  type Bucket,
} from "../src/lib/timelineFill.ts";

const sec = (iso: string) => Date.parse(iso) / 1000;

function month(y: number, m: number, count: number): Bucket {
  const start = Date.UTC(y, m - 1, 1);
  const end = Date.UTC(y, m, 1);
  return {
    start: new Date(start).toISOString(),
    start_epoch: start / 1000,
    end_epoch: end / 1000,
    count,
  };
}

function year(y: number, count: number): Bucket {
  const start = Date.UTC(y, 0, 1);
  return {
    start: new Date(start).toISOString(),
    start_epoch: start / 1000,
    end_epoch: Date.UTC(y + 1, 0, 1) / 1000,
    count,
  };
}

test("fillTimeline: empty input stays empty", () => {
  assert.deepEqual(fillTimeline([], "month"), { buckets: [], sparse: false });
});

test("fillTimeline: a single bucket is returned as-is", () => {
  const b = month(2026, 3, 5);
  assert.deepEqual(fillTimeline([b], "month").buckets, [b]);
});

test("fillTimeline: month gaps are zero-filled with exact UTC boundaries", () => {
  const r = fillTimeline([month(2025, 11, 4), month(2026, 2, 9)], "month");
  assert.equal(r.sparse, false);
  assert.deepEqual(
    r.buckets.map((b) => [bucketLabel(b.start_epoch, "month"), b.count]),
    [["2025-11", 4], ["2025-12", 0], ["2026-01", 0], ["2026-02", 9]],
  );
  const dec = r.buckets[1];
  assert.equal(dec.start_epoch, sec("2025-12-01T00:00:00Z"));
  assert.equal(dec.end_epoch, sec("2026-01-01T00:00:00Z"));
  // Contiguous: each bucket ends where the next starts.
  for (let i = 1; i < r.buckets.length; i++) {
    assert.equal(r.buckets[i - 1].end_epoch, r.buckets[i].start_epoch);
  }
});

test("fillTimeline: real buckets are kept verbatim, in order, even if unsorted", () => {
  const a = month(2026, 1, 1);
  const c = month(2026, 3, 3);
  const r = fillTimeline([c, a], "month");
  assert.equal(r.buckets[0], a);
  assert.equal(r.buckets[2], c);
  assert.equal(r.buckets[1].count, 0);
});

test("fillTimeline: year gaps are zero-filled", () => {
  const r = fillTimeline([year(2010, 2), year(2014, 7)], "year");
  assert.deepEqual(
    r.buckets.map((b) => [bucketLabel(b.start_epoch, "year"), b.count]),
    [["2010", 2], ["2011", 0], ["2012", 0], ["2013", 0], ["2014", 7]],
  );
  assert.equal(r.buckets[1].end_epoch, sec("2012-01-01T00:00:00Z"));
});

test("fillTimeline: a span wider than the cap stays sparse and says so", () => {
  // 1601 (a FILETIME-zero mtime) to 2026 in months is ~5,100 buckets.
  const r = fillTimeline([month(1601, 1, 1), month(2026, 1, 1)], "month");
  assert.ok(MAX_FILLED_BUCKETS < 5100);
  assert.equal(r.sparse, true);
  assert.equal(r.buckets.length, 2);
});

test("bucketLabel: labels are the UTC period, not the viewer's local one", () => {
  // 2026-03-01T00:00Z is still February in UTC-5; the label must say March.
  assert.equal(bucketLabel(sec("2026-03-01T00:00:00Z"), "month"), "2026-03");
  assert.equal(bucketLabel(sec("2026-01-01T00:00:00Z"), "year"), "2026");
});

test("compactCount: short counts verbatim, large counts compact", () => {
  assert.equal(compactCount(0, "en-US"), "0");
  assert.equal(compactCount(999, "en-US"), "999");
  assert.equal(compactCount(12_345, "en-US"), "12.3K");
  assert.equal(compactCount(1_090_000, "en-US"), "1.09M");
  assert.equal(compactCount(123_456, "en-US"), "123K");
  // The column is 32px: never more than 5 characters.
  for (const n of [1, 10_000, 99_999, 123_456, 9_999_999, 1_234_567_890]) {
    assert.ok(compactCount(n, "en-US").length <= 5, String(n));
  }
});
