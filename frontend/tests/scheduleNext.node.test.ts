// scheduleNext.ts — nextRuns(cron, n, now) for the 5-field UTC crons the
// backend stores. The expected runs below were produced by the backend's own
// `filearr.schedule.next_occurrence` (cronsim 2.7), chained n times:
//
//   cd backend && uv run python -c '... next_occurrence(cron, prev) ...'
//
// so they pin cronsim semantics: lists, ranges, steps, names, `L`/`LW` in
// day-of-month, `5L`/`MON#2` in day-of-week, cron 7 == Sunday, and the
// Debian rule that day-of-month and day-of-week are OR-ed unless either field
// STARTS with `*` (then AND-ed — so `*/2` + `MON` means "even-ish days that are
// Mondays"). Anything cronsim rejects yields no runs.

import assert from "node:assert/strict";
import { test } from "node:test";

import { cronProblem, nextRuns } from "../src/lib/scheduleNext.ts";

const BACKEND: { cron: string; now: string; runs: string[] }[] = [
  { cron: "0 3 * * *", now: "2026-11-02T10:15:30Z", runs: ["2026-11-03T03:00:00Z", "2026-11-04T03:00:00Z", "2026-11-05T03:00:00Z"] },
  { cron: "*/15 * * * *", now: "2026-11-02T10:15:30Z", runs: ["2026-11-02T10:30:00Z", "2026-11-02T10:45:00Z", "2026-11-02T11:00:00Z"] },
  { cron: "30 4 * * 1-5", now: "2026-11-06T05:00:00Z", runs: ["2026-11-09T04:30:00Z", "2026-11-10T04:30:00Z", "2026-11-11T04:30:00Z"] },
  { cron: "0 0 31 * *", now: "2026-01-31T00:00:00Z", runs: ["2026-03-31T00:00:00Z", "2026-05-31T00:00:00Z", "2026-07-31T00:00:00Z"] },
  { cron: "0 0 29 2 *", now: "2026-11-02T00:00:00Z", runs: ["2028-02-29T00:00:00Z", "2032-02-29T00:00:00Z", "2036-02-29T00:00:00Z"] },
  { cron: "0 12 1 * MON", now: "2026-11-02T12:00:00Z", runs: ["2026-11-09T12:00:00Z", "2026-11-16T12:00:00Z", "2026-11-23T12:00:00Z"] },
  { cron: "0 12 */2 * MON", now: "2026-11-01T00:00:00Z", runs: ["2026-11-09T12:00:00Z", "2026-11-23T12:00:00Z", "2026-12-07T12:00:00Z"] },
  { cron: "0 0 * * 5L", now: "2026-11-02T00:00:00Z", runs: ["2026-11-27T00:00:00Z", "2026-12-25T00:00:00Z", "2027-01-29T00:00:00Z"] },
  { cron: "0 0 * * MON#2", now: "2026-11-02T00:00:00Z", runs: ["2026-11-09T00:00:00Z", "2026-12-14T00:00:00Z", "2027-01-11T00:00:00Z"] },
  { cron: "0 0 L * *", now: "2026-01-15T00:00:00Z", runs: ["2026-01-31T00:00:00Z", "2026-02-28T00:00:00Z", "2026-03-31T00:00:00Z"] },
  { cron: "0 0 LW * *", now: "2026-01-15T00:00:00Z", runs: ["2026-01-30T00:00:00Z", "2026-02-27T00:00:00Z", "2026-03-31T00:00:00Z"] },
  { cron: "0 9 * jan,jul sun", now: "2026-11-02T00:00:00Z", runs: ["2027-01-03T09:00:00Z", "2027-01-10T09:00:00Z", "2027-01-17T09:00:00Z"] },
  { cron: "5-10/2 * * * *", now: "2026-11-02T10:09:00Z", runs: ["2026-11-02T11:05:00Z", "2026-11-02T11:07:00Z", "2026-11-02T11:09:00Z"] },
  { cron: "0 0 * * 7", now: "2026-11-02T00:00:00Z", runs: ["2026-11-08T00:00:00Z", "2026-11-15T00:00:00Z", "2026-11-22T00:00:00Z"] },
  { cron: "0 0 1-31/10 * *", now: "2026-11-02T00:00:00Z", runs: ["2026-11-11T00:00:00Z", "2026-11-21T00:00:00Z", "2026-12-01T00:00:00Z"] },
  { cron: "0 23 31 * *", now: "2026-11-02T00:00:00Z", runs: ["2026-12-31T23:00:00Z", "2027-01-31T23:00:00Z", "2027-03-31T23:00:00Z"] },
  { cron: "59 23 31 12 *", now: "2026-12-31T23:59:00Z", runs: ["2027-12-31T23:59:00Z", "2028-12-31T23:59:00Z", "2029-12-31T23:59:00Z"] },
  { cron: "0 0 * * SUN-TUE", now: "2026-11-02T00:00:00Z", runs: ["2026-11-03T00:00:00Z", "2026-11-08T00:00:00Z", "2026-11-09T00:00:00Z"] },
  { cron: "0 6 * * 1/2", now: "2026-11-02T07:00:00Z", runs: ["2026-11-04T06:00:00Z", "2026-11-06T06:00:00Z", "2026-11-08T06:00:00Z"] },
  { cron: "61 * * * *", now: "2026-11-02T00:00:00Z", runs: [] },
  { cron: "0 0 30 2 *", now: "2026-11-02T00:00:00Z", runs: [] },
  { cron: "0 0 * *", now: "2026-11-02T00:00:00Z", runs: [] },
  { cron: "", now: "2026-11-02T00:00:00Z", runs: [] },
];

const iso = (d: Date) => d.toISOString().replace(/\.000Z$/, "Z");

for (const c of BACKEND) {
  test(`nextRuns matches backend next_occurrence: "${c.cron}" after ${c.now}`, () => {
    assert.deepEqual(nextRuns(c.cron, 3, new Date(c.now)).map(iso), c.runs);
  });
}

test("nextRuns: strictly after now, with now floored to the minute", () => {
  // An occurrence at the current minute counts as in progress (next_occurrence).
  const now = new Date("2026-11-02T03:00:45Z");
  assert.deepEqual(nextRuns("0 3 * * *", 1, now).map(iso), ["2026-11-03T03:00:00Z"]);
  assert.deepEqual(nextRuns("* * * * *", 2, now).map(iso), ["2026-11-02T03:01:00Z", "2026-11-02T03:02:00Z"]);
});

test("nextRuns: n bounds the count; n <= 0 is empty", () => {
  const now = new Date("2026-11-02T00:00:00Z");
  assert.equal(nextRuns("*/5 * * * *", 7, now).length, 7);
  assert.deepEqual(nextRuns("*/5 * * * *", 0, now), []);
});

test("nextRuns: malformed fields yield no runs", () => {
  const now = new Date("2026-11-02T00:00:00Z");
  for (const bad of [
    "a * * * *",
    "* * * * * * *",
    "*/0 * * * *",
    "5-1 * * * *",
    "0 24 * * *",
    "0 0 0 * *",
    "0 0 * 13 *",
    "0 0 * * 8",
    "0 0 * * 1#6",
    "0 0 * * L",
    "1.5 * * * *",
    "-1 * * * *",
    "0 0 * * MON-",
  ]) {
    assert.deepEqual(nextRuns(bad, 3, now), [], bad);
  }
});

test("nextRuns: crossing a month and year end in UTC", () => {
  const now = new Date("2026-12-31T23:30:00Z");
  assert.deepEqual(nextRuns("0 0 1 * *", 2, now).map(iso), ["2027-01-01T00:00:00Z", "2027-02-01T00:00:00Z"]);
});

test("cronProblem: null for crons the scheduler accepts", () => {
  for (const ok of ["0 3 * * *", "*/15 * * * *", "0 0 L * *", "0 9 * * MON#2", "  30 2 * * 0  "]) {
    assert.equal(cronProblem(ok), null, ok);
  }
});

test("cronProblem: a sentence for blank, malformed or unsatisfiable crons", () => {
  for (const bad of ["", "   ", "a * * * *", "0 24 * * *", "* * * *", "* * * * * * *", "0 0 30 2 *"]) {
    const p = cronProblem(bad);
    assert.equal(typeof p, "string", bad);
    assert.ok(p && p.length > 10, bad);
  }
});

test("cronProblem: 6-field (seconds) crons are left to the server", () => {
  // cronsim accepts a sixth (seconds) field; nextRuns does not model it, so
  // the client must not refuse what the backend would store.
  assert.equal(cronProblem("0 0 3 * * *"), null);
});
