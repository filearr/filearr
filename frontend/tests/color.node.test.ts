// Accent contrast helpers: WCAG relative luminance and the on-accent text colour.
// Runs on Node's built-in test runner (`npm test` from frontend/); color.ts is
// DOM-free so it loads without a browser.

import assert from "node:assert/strict";
import { test } from "node:test";

import { relativeLuminance, onAccentColor } from "../src/lib/color.ts";

test("relativeLuminance spans 0 (black) to 1 (white)", () => {
  assert.equal(relativeLuminance("#ffffff"), 1);
  assert.equal(relativeLuminance("#000000"), 0);
});

test("onAccentColor picks white on dark/saturated accents", () => {
  assert.equal(onAccentColor("#6366f1"), "#ffffff");
  assert.equal(onAccentColor("#4f46e5"), "#ffffff");
  assert.equal(onAccentColor("#000000"), "#ffffff");
});

test("onAccentColor picks slate-950 on light accents", () => {
  assert.equal(onAccentColor("#facc15"), "#0f172a");
  assert.equal(onAccentColor("#ffffff"), "#0f172a");
});
