// DataTable sorting: the header click cycle and the row comparator.
// sort.ts is DOM-free so it runs on Node's built-in test runner.

import assert from "node:assert/strict";
import { test } from "node:test";

import { compareValues, nextSort, sortRows, type SortState } from "../src/lib/ui/sort.ts";

test("nextSort cycles none -> asc -> desc -> none on the same key", () => {
  const a = nextSort(null, "name");
  assert.deepEqual(a, { key: "name", dir: "asc" });
  const d = nextSort(a, "name");
  assert.deepEqual(d, { key: "name", dir: "desc" });
  assert.equal(nextSort(d, "name"), null);
});

test("nextSort on a different key starts at asc", () => {
  assert.deepEqual(nextSort({ key: "name", dir: "desc" }, "size"), { key: "size", dir: "asc" });
  assert.deepEqual(nextSort({ key: "name", dir: "asc" }, "size"), { key: "size", dir: "asc" });
});

test("compareValues orders numbers numerically", () => {
  assert.ok(compareValues(2, 10) < 0);
  assert.ok(compareValues(10, 2) > 0);
  assert.equal(compareValues(3, 3), 0);
  assert.ok(compareValues(-1, 0) < 0);
});

test("compareValues orders strings naturally and case-insensitively", () => {
  assert.ok(compareValues("file2", "file10") < 0);
  assert.ok(compareValues("apple", "Banana") < 0);
  assert.equal(compareValues("abc", "ABC"), 0);
});

test("compareValues orders booleans false before true", () => {
  assert.ok(compareValues(false, true) < 0);
  assert.ok(compareValues(true, false) > 0);
  assert.equal(compareValues(true, true), 0);
});

test("compareValues orders Dates by time", () => {
  const early = new Date("2024-01-01T00:00:00Z");
  const late = new Date("2025-01-01T00:00:00Z");
  assert.ok(compareValues(early, late) < 0);
  assert.ok(compareValues(late, early) > 0);
  assert.equal(compareValues(new Date(early.getTime()), early), 0);
});

test("compareValues puts null and undefined after any value", () => {
  assert.ok(compareValues(null, 1) > 0);
  assert.ok(compareValues(1, null) < 0);
  assert.ok(compareValues(undefined, "a") > 0);
  assert.ok(compareValues("a", undefined) < 0);
  assert.equal(compareValues(null, undefined), 0);
});

type Row = { id: string; n: number | null; g: string };
const get = (r: Row, k: string) => (r as unknown as Record<string, unknown>)[k];

test("sortRows returns the input array when state is null", () => {
  const rows: Row[] = [{ id: "a", n: 2, g: "x" }];
  assert.equal(sortRows(rows, null, get), rows);
});

test("sortRows sorts ascending and descending without mutating the input", () => {
  const rows: Row[] = [
    { id: "a", n: 3, g: "x" },
    { id: "b", n: 1, g: "x" },
    { id: "c", n: 2, g: "x" },
  ];
  const asc = sortRows(rows, { key: "n", dir: "asc" }, get);
  assert.deepEqual(asc.map((r) => r.id), ["b", "c", "a"]);
  const desc = sortRows(rows, { key: "n", dir: "desc" }, get);
  assert.deepEqual(desc.map((r) => r.id), ["a", "c", "b"]);
  assert.deepEqual(rows.map((r) => r.id), ["a", "b", "c"]);
  assert.notEqual(asc, rows);
});

test("sortRows is stable in both directions", () => {
  const rows: Row[] = [
    { id: "1", n: 0, g: "b" },
    { id: "2", n: 0, g: "a" },
    { id: "3", n: 0, g: "b" },
    { id: "4", n: 0, g: "a" },
    { id: "5", n: 0, g: "b" },
  ];
  const asc = sortRows(rows, { key: "g", dir: "asc" }, get);
  assert.deepEqual(asc.map((r) => r.id), ["2", "4", "1", "3", "5"]);
  // Descending reverses the key order, not the original order of ties.
  const desc = sortRows(rows, { key: "g", dir: "desc" }, get);
  assert.deepEqual(desc.map((r) => r.id), ["1", "3", "5", "2", "4"]);
});

test("sortRows keeps null values last in both directions", () => {
  const rows: Row[] = [
    { id: "a", n: null, g: "" },
    { id: "b", n: 2, g: "" },
    { id: "c", n: null, g: "" },
    { id: "d", n: 1, g: "" },
  ];
  const asc: SortState = { key: "n", dir: "asc" };
  assert.deepEqual(sortRows(rows, asc, get).map((r) => r.id), ["d", "b", "a", "c"]);
  const desc: SortState = { key: "n", dir: "desc" };
  assert.deepEqual(sortRows(rows, desc, get).map((r) => r.id), ["b", "d", "a", "c"]);
});

test("compareValues treats NaN as missing and orders Infinity after finite numbers", () => {
  assert.ok(compareValues(NaN, 1) > 0);
  assert.ok(compareValues(1, NaN) < 0);
  assert.equal(compareValues(NaN, null), 0);
  assert.ok(compareValues(Infinity, 1e300) > 0);
  assert.ok(compareValues(-Infinity, -1e300) < 0);
  assert.equal(compareValues(Infinity, Infinity), 0);
});

test("compareValues treats an invalid Date as missing", () => {
  const bad = new Date("not a date");
  assert.ok(compareValues(bad, new Date(0)) > 0);
  assert.ok(compareValues(new Date(0), bad) < 0);
});

test("sortRows keeps NaN and invalid Dates last in both directions", () => {
  const nums = [{ v: NaN }, { v: 2 }, { v: Infinity }, { v: 1 }];
  const byV = (r: { v: unknown }) => r.v;
  assert.deepEqual(sortRows(nums, { key: "v", dir: "asc" }, byV).map((r) => r.v), [1, 2, Infinity, NaN]);
  assert.deepEqual(sortRows(nums, { key: "v", dir: "desc" }, byV).map((r) => r.v), [Infinity, 2, 1, NaN]);
  const dates = [{ v: new Date("x") }, { v: new Date(2000) }, { v: new Date(1000) }];
  const t = (rows: { v: Date }[]) => rows.map((r) => r.v.getTime());
  assert.deepEqual(t(sortRows(dates, { key: "v", dir: "asc" }, byV)), [1000, 2000, NaN]);
  assert.deepEqual(t(sortRows(dates, { key: "v", dir: "desc" }, byV)), [2000, 1000, NaN]);
});
