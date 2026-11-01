// Accent contrast helpers: WCAG relative luminance and the on-accent text colour.
// Runs on Node's built-in test runner (`npm test` from frontend/); color.ts is
// DOM-free so it loads without a browser.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  relativeLuminance,
  onAccentColor,
  normalizeAccent,
  contrastRatio,
  accentTextColor,
  mixHex,
} from "../src/lib/color.ts";

test("relativeLuminance spans 0 (black) to 1 (white)", () => {
  assert.equal(relativeLuminance("#ffffff"), 1);
  assert.equal(relativeLuminance("#000000"), 0);
});

test("onAccentColor picks white on dark/saturated accents", () => {
  assert.equal(onAccentColor("#6366f1"), "#ffffff");
  assert.equal(onAccentColor("#4f46e5"), "#ffffff");
  assert.equal(onAccentColor("#000000"), "#ffffff");
});

test("onAccentColor picks slate-900 on light accents", () => {
  assert.equal(onAccentColor("#facc15"), "#0f172a");
  assert.equal(onAccentColor("#ffffff"), "#0f172a");
});

test("normalizeAccent migrates the legacy default to the current one", () => {
  assert.equal(normalizeAccent("#6366f1"), "#4f46e5");
  assert.equal(normalizeAccent("#6366F1"), "#4f46e5"); // case-insensitive
});

test("normalizeAccent keeps a valid #rrggbb as given", () => {
  assert.equal(normalizeAccent("#ABCDEF"), "#ABCDEF");
});

test("normalizeAccent rejects anything that is not #rrggbb", () => {
  assert.equal(normalizeAccent("red"), null);
  assert.equal(normalizeAccent(null), null);
});

const HEX7 = /^#[0-9a-f]{6}$/;

test("contrastRatio is 21 for black/white, 1 for equal colours, and symmetric", () => {
  assert.equal(contrastRatio("#000000", "#ffffff"), 21);
  assert.equal(contrastRatio("#4f46e5", "#4f46e5"), 1);
  assert.equal(contrastRatio("#ffffff", "#4f46e5"), contrastRatio("#4f46e5", "#ffffff"));
});

test("mixHex blends channel-wise and returns lowercase #rrggbb", () => {
  assert.equal(mixHex("#000000", "#ffffff", 0), "#000000");
  assert.equal(mixHex("#000000", "#ffffff", 1), "#ffffff");
  assert.equal(mixHex("#000000", "#FFFFFF", 0.5), "#808080");
  assert.match(mixHex("#4F46E5", "#ffffff", 0.35), HEX7);
});

test("accentTextColor keeps an accent that already meets 4.5:1", () => {
  assert.equal(accentTextColor("#4f46e5", "#ffffff"), "#4f46e5");
});

test("accentTextColor lightens the default accent on slate-900", () => {
  const out = accentTextColor("#4f46e5", "#0f172a");
  assert.notEqual(out, "#4f46e5");
  assert.ok(relativeLuminance(out) > relativeLuminance("#4f46e5"));
  assert.ok(contrastRatio(out, "#0f172a") >= 4.5);
});

test("accentTextColor darkens a pale accent on white", () => {
  const out = accentTextColor("#facc15", "#ffffff");
  assert.ok(relativeLuminance(out) < relativeLuminance("#facc15"));
  assert.ok(contrastRatio(out, "#ffffff") >= 4.5);
});

test("accentTextColor takes the smallest step that meets the minimum", () => {
  // One 5 % step less than the answer must still fall short.
  const out = accentTextColor("#4f46e5", "#0f172a");
  let i = 0;
  while (mixHex("#4f46e5", "#ffffff", i / 20) !== out) i++;
  assert.ok(i > 0);
  assert.ok(contrastRatio(mixHex("#4f46e5", "#ffffff", (i - 1) / 20), "#0f172a") < 4.5);
});

test("accentTextColor is monotone: a stricter minimum never yields less contrast", () => {
  for (const [accent, surface] of [
    ["#4f46e5", "#0f172a"],
    ["#facc15", "#ffffff"],
    ["#22c55e", "#ffffff"],
    ["#1e3a8a", "#020617"],
  ]) {
    let prev = 0;
    for (const min of [1, 3, 4.5, 7, 12]) {
      const r = contrastRatio(accentTextColor(accent, surface, min), surface);
      assert.ok(r >= prev, `${accent} on ${surface} at ${min}: ${r} < ${prev}`);
      prev = r;
    }
  }
});

test("accentTextColor caps at the 100 % endpoint when the minimum is unreachable", () => {
  assert.equal(accentTextColor("#808080", "#ffffff", 22), "#000000");
  assert.equal(accentTextColor("#808080", "#000000", 22), "#ffffff");
});

test("accentTextColor always returns 7-char lowercase hex", () => {
  for (const accent of ["#4F46E5", "#facc15", "#000000", "#ffffff", "#ff00aa"]) {
    for (const surface of ["#ffffff", "#0f172a", "#020617"]) {
      assert.match(accentTextColor(accent.toLowerCase(), surface), HEX7);
    }
  }
});

test("border-strong tokens meet 3:1 (WCAG 1.4.11) on their surfaces", () => {
  assert.ok(contrastRatio("#7c8ba1", "#ffffff") >= 3);
  assert.ok(contrastRatio("#64748b", "#020617") >= 3);
});
