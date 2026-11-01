// numOrNull / intOrNull: <input type="number"> bind:value yields a number, or
// null when emptied; seeded state may still be a string. Both normalise to
// number | null.

import assert from "node:assert/strict";
import { test } from "node:test";

import { intOrNull, numOrNull } from "../src/lib/numInput.ts";

test("numOrNull: finite numbers pass through", () => {
  assert.equal(numOrNull(0), 0);
  assert.equal(numOrNull(42), 42);
  assert.equal(numOrNull(-1.5), -1.5);
});

test("numOrNull: empty-ish values are null", () => {
  assert.equal(numOrNull(null), null);
  assert.equal(numOrNull(undefined), null);
  assert.equal(numOrNull(""), null);
  assert.equal(numOrNull("   "), null);
});

test("numOrNull: numeric strings parse (trimmed, dot decimal)", () => {
  assert.equal(numOrNull("12"), 12);
  assert.equal(numOrNull(" 2.5 "), 2.5);
  assert.equal(numOrNull("-3"), -3);
  assert.equal(numOrNull("0"), 0);
});

test("numOrNull: forms a number input also accepts (exponent, bare dot, sign)", () => {
  assert.equal(numOrNull("1e3"), 1000);
  assert.equal(numOrNull("2.5E-1"), 0.25);
  assert.equal(numOrNull(".5"), 0.5);
  assert.equal(numOrNull("5."), 5);
  assert.equal(numOrNull("+5"), 5);
});

test("numOrNull: overflow to Infinity is null", () => {
  assert.equal(numOrNull("1e400"), null);
  assert.equal(numOrNull("-1e400"), null);
});

test("numOrNull: non-numeric and non-finite are null", () => {
  assert.equal(numOrNull(NaN), null);
  assert.equal(numOrNull(Infinity), null);
  assert.equal(numOrNull(-Infinity), null);
  assert.equal(numOrNull("abc"), null);
  assert.equal(numOrNull("12abc"), null);
  assert.equal(numOrNull("1,5"), null);
  assert.equal(numOrNull("Infinity"), null);
  assert.equal(numOrNull(true), null);
  assert.equal(numOrNull({}), null);
  assert.equal(numOrNull([]), null);
});

test("intOrNull: integers pass, fractions are null", () => {
  assert.equal(intOrNull(5), 5);
  assert.equal(intOrNull("7"), 7);
  assert.equal(intOrNull(0), 0);
  assert.equal(intOrNull(2.5), null);
  assert.equal(intOrNull("2.5"), null);
});

test("intOrNull: empty and junk are null", () => {
  assert.equal(intOrNull(null), null);
  assert.equal(intOrNull(""), null);
  assert.equal(intOrNull("x"), null);
  assert.equal(intOrNull(NaN), null);
});

test("intOrNull: min/max bounds are inclusive; out of range is null", () => {
  assert.equal(intOrNull(1, { min: 1 }), 1);
  assert.equal(intOrNull(0, { min: 1 }), null);
  assert.equal(intOrNull(10, { max: 10 }), 10);
  assert.equal(intOrNull(11, { max: 10 }), null);
  assert.equal(intOrNull(5, { min: 1, max: 10 }), 5);
  assert.equal(intOrNull(-1, { min: 0, max: 10 }), null);
});
