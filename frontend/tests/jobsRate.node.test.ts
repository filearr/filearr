// jobsRate.ts — rolling completion rate and backlog ETA for the Jobs page
// (B47). The `done` counters are counts of succeeded rows in
// procrastinate_jobs, which purge_job_history trims daily, so a counter that
// goes DOWN is a purge, not a stall: the window restarts. Samples come from
// poll ticks, which pause while the tab is hidden, so a sample after a long
// gap restarts the window too instead of averaging across the gap.

import assert from "node:assert/strict";
import { test } from "node:test";

import { pushSample, queueEta, rateOf, type RateSample } from "../src/lib/jobsRate.ts";

const OPTS = { window: 6, maxGapMs: 12_000 };

function feed(points: [number, number][]): RateSample[] {
  let s: RateSample[] = [];
  for (const [t, done] of points) s = pushSample(s, t, done, OPTS);
  return s;
}

test("rateOf: null with fewer than two samples", () => {
  assert.equal(rateOf([]), null);
  assert.equal(rateOf(feed([[0, 10]])), null);
});

test("rateOf: per-second delta over the window", () => {
  assert.equal(rateOf(feed([[0, 0], [4000, 40], [8000, 80]])), 10);
});

test("rateOf: a flat counter is 0 (stalled)", () => {
  assert.equal(rateOf(feed([[0, 5], [4000, 5]])), 0);
});

test("pushSample: keeps at most `window` samples (oldest dropped)", () => {
  const s = feed([[0, 0], [1000, 1], [2000, 2], [3000, 3], [4000, 4], [5000, 5], [6000, 6], [7000, 7]]);
  assert.equal(s.length, 6);
  assert.equal(s[0].t, 2000);
});

test("pushSample: a decreasing counter (history purge) restarts the window", () => {
  const s = feed([[0, 1000], [4000, 1040], [8000, 20]]);
  assert.deepEqual(s, [{ t: 8000, done: 20 }]);
  assert.equal(rateOf(s), null, "unknown, not stalled");
  const s2 = pushSample(s, 12_000, 60, OPTS);
  assert.equal(rateOf(s2), 10);
});

test("pushSample: a gap longer than maxGapMs restarts the window", () => {
  const s = feed([[0, 0], [4000, 40], [600_000, 5000]]);
  assert.deepEqual(s, [{ t: 600_000, done: 5000 }]);
  assert.equal(rateOf(s), null);
});

test("pushSample: a gap of exactly maxGapMs is still continuous", () => {
  assert.equal(rateOf(feed([[0, 0], [12_000, 120]])), 10);
});

test("pushSample: does not mutate its input", () => {
  const s: RateSample[] = [{ t: 0, done: 0 }];
  pushSample(s, 4000, 4, OPTS);
  assert.deepEqual(s, [{ t: 0, done: 0 }]);
});

test("pushSample: a non-advancing clock replaces nothing and yields no rate", () => {
  const s = feed([[1000, 0], [1000, 5]]);
  assert.equal(rateOf(s), null);
});

test("queueEta: 0 when nothing is queued, — when unknown, stalled at 0, else a duration", () => {
  assert.equal(queueEta(0, null), "0");
  assert.equal(queueEta(-1, 5), "0");
  assert.equal(queueEta(10, null), "—");
  assert.equal(queueEta(10, 0), "stalled");
  assert.equal(queueEta(600, 10), "1m 0s");
  assert.equal(queueEta(36_000, 2), "5h 0m");
});
