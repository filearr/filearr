// format.ts: the one place bytes, dates, durations and relative times are
// turned into display strings (A32). Table-driven; local-time cases pin the
// zone so "local" and "UTC" visibly differ (Tokyo is UTC+9, no DST).

process.env.TZ = "Asia/Tokyo";

import assert from "node:assert/strict";
import { test } from "node:test";

import { fmtBytes, fmtDate, fmtDateTime, fmtDuration, fmtUtc, relTime } from "../src/lib/format.ts";

test("fmtBytes: base 1024, integer bytes, one decimal above, exact multiples bare", () => {
  const cases: [number, string][] = [
    [1, "1 B"],
    [512, "512 B"],
    [1023, "1023 B"],
    [1024, "1 KB"],
    [1536, "1.5 KB"],
    [2 * 1024 ** 2, "2 MB"],
    [133_000_000, "126.8 MB"],
    [5 * 1024 ** 3, "5 GB"],
    [2 ** 40, "1 TB"],
    [3 * 2 ** 50, "3 PB"],
    [2 ** 60, "1024 PB"],
    // Rounding up to the next unit moves the unit instead of printing "1024.0 KB".
    [1024 ** 2 - 1, "1 MB"],
  ];
  for (const [n, want] of cases) assert.equal(fmtBytes(n), want, `fmtBytes(${n})`);
});

test("fmtBytes: zero is '0 B' unless the caller names its own zero", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(0, { zero: "0" }), "0");
  assert.equal(fmtBytes(0, { zero: "—" }), "—");
});

test("fmtBytes: absent, non-finite and negative are a dash, never '0 B'", () => {
  for (const v of [Number.NaN, null, undefined, -1, Number.POSITIVE_INFINITY]) {
    assert.equal(fmtBytes(v), "—", `fmtBytes(${v})`);
  }
  assert.equal(fmtBytes(null, { zero: "0" }), "—");
});

test("fmtDate: local YYYY-MM-DD from epoch seconds or milliseconds", () => {
  // 2024-01-15T20:00:00Z is already the 16th in Tokyo.
  const ms = Date.UTC(2024, 0, 15, 20, 0, 0);
  assert.equal(fmtDate(ms / 1000), "2024-01-16");
  assert.equal(fmtDate(ms), "2024-01-16");
  assert.equal(fmtDate(new Date(ms)), "2024-01-16");
  assert.equal(fmtDate(0), "1970-01-01");
});

test("fmtDate: ISO strings with an offset, date-only ISO, and EXIF", () => {
  assert.equal(fmtDate("2024-03-10T23:30:00-05:00"), "2024-03-11"); // 13:30 next day in Tokyo
  assert.equal(fmtDate("2024-03-10T10:00:00Z"), "2024-03-10");
  assert.equal(fmtDate("2024-03-10"), "2024-03-10"); // a calendar date stays that date
  assert.equal(fmtDate("2019:07:04 18:22:05"), "2019-07-04"); // EXIF wall time, local
  assert.equal(fmtDate("2019:07:04"), "2019-07-04");
});

test("fmtDate: garbage and absent values are a dash", () => {
  for (const v of ["", "not a date", "0000:00:00 00:00:00", "2019:13:40 00:00:00", null, undefined, Number.NaN, new Date("x")]) {
    assert.equal(fmtDate(v), "—", `fmtDate(${String(v)})`);
  }
});

test("fmtDateTime: local YYYY-MM-DD HH:mm", () => {
  assert.equal(fmtDateTime(Date.UTC(2024, 0, 15, 20, 5, 59)), "2024-01-16 05:05");
  assert.equal(fmtDateTime("2019:07:04 18:22:05"), "2019-07-04 18:22");
  assert.equal(fmtDateTime("nope"), "—");
});

test("fmtUtc: YYYY-MM-DDTHH:mm:ssZ for title tooltips", () => {
  assert.equal(fmtUtc(Date.UTC(2024, 0, 15, 20, 5, 59, 123)), "2024-01-15T20:05:59Z");
  assert.equal(fmtUtc(1_705_349_159), "2024-01-15T20:05:59Z");
  assert.equal(fmtUtc("2024-03-10T23:30:00-05:00"), "2024-03-11T04:30:00Z");
  assert.equal(fmtUtc(null), "—");
});

test("fmtDuration: s, m s, h m — no days, no zero padding", () => {
  const cases: [number, string][] = [
    [0, "0s"],
    [0.4, "0s"],
    [59, "59s"],
    [59.6, "1m 0s"],
    [65, "1m 5s"],
    [3600, "1h 0m"],
    [3720, "1h 2m"],
    [90000, "25h 0m"],
  ];
  for (const [n, want] of cases) assert.equal(fmtDuration(n), want, `fmtDuration(${n})`);
});

test("fmtDuration: negative, NaN and absent are a dash", () => {
  for (const v of [-1, Number.NaN, null, undefined, Number.POSITIVE_INFINITY]) {
    assert.equal(fmtDuration(v), "—", `fmtDuration(${v})`);
  }
});

test("relTime: boundaries against a fixed now", () => {
  const now = Date.UTC(2024, 5, 15, 12, 0, 0);
  const ago = (s: number) => now - s * 1000;
  const cases: [number, string][] = [
    [0, "just now"],
    [44, "just now"],
    [45, "1 min ago"],
    [89, "1 min ago"],
    [90, "2 min ago"],
    [59 * 60, "59 min ago"],
    [59.5 * 60, "1 h ago"],
    [2 * 3600, "2 h ago"],
    [23 * 3600, "23 h ago"],
    [23.5 * 3600, "yesterday"],
    [35 * 3600, "yesterday"],
    [36 * 3600, "2 days ago"],
    [29 * 86400, "29 days ago"],
  ];
  for (const [s, want] of cases) assert.equal(relTime(ago(s), now), want, `relTime(-${s}s)`);
});

test("relTime: a month or more falls back to the local date", () => {
  const now = Date.UTC(2024, 5, 15, 12, 0, 0);
  const then = now - 30 * 86400 * 1000;
  assert.equal(relTime(then, now), fmtDate(then));
  assert.equal(relTime(then, now), "2024-05-16");
});

test("relTime: future instants read forwards", () => {
  const now = Date.UTC(2024, 5, 15, 12, 0, 0);
  const ahead = (s: number) => now + s * 1000;
  assert.equal(relTime(ahead(10), now), "just now");
  assert.equal(relTime(ahead(5 * 60), now), "in 5 min");
  assert.equal(relTime(ahead(3 * 3600), now), "in 3 h");
  assert.equal(relTime(ahead(30 * 3600), now), "tomorrow");
  assert.equal(relTime(ahead(4 * 86400), now), "in 4 days");
});

test("relTime: accepts ISO strings and epoch seconds; garbage is a dash", () => {
  const now = Date.UTC(2024, 5, 15, 12, 0, 0);
  assert.equal(relTime("2024-06-15T11:55:00Z", now), "5 min ago");
  assert.equal(relTime(now / 1000 - 7200, now), "2 h ago");
  assert.equal(relTime("garbage", now), "—");
  assert.equal(relTime(null, now), "—");
});
