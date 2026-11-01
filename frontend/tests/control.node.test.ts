// Shared class string for the kit's form controls (Input, Select, Textarea).
// control.ts is DOM-free so it runs on Node's built-in test runner.

import assert from "node:assert/strict";
import { test } from "node:test";

import { controlClass } from "../src/lib/ui/control.ts";

const BASE = "w-full rounded-control border bg-surface text-fg placeholder:text-muted disabled:opacity-50";

function classes(s: string): string[] {
  return s.split(/\s+/).filter(Boolean);
}

test("controlClass always carries the shared base utilities", () => {
  for (const size of ["sm", "md", "lg"] as const) {
    for (const invalid of [false, true]) {
      const got = classes(controlClass(size, invalid));
      for (const c of classes(BASE)) assert.ok(got.includes(c), `${size}/${invalid} missing ${c}`);
    }
  }
});

test("controlClass leaves focus to the global :focus-visible outline", () => {
  for (const c of classes(controlClass("md", false))) {
    assert.ok(!c.startsWith("focus-visible:"), `unexpected ${c}`);
  }
});

test("controlClass md size uses px-3 py-1.5 text-sm", () => {
  const got = classes(controlClass("md", false));
  for (const c of ["px-3", "py-1.5", "text-sm"]) assert.ok(got.includes(c), c);
  for (const c of ["px-2", "py-1", "text-xs"]) assert.ok(!got.includes(c), c);
});

test("controlClass sm size uses px-2 py-1 text-xs", () => {
  const got = classes(controlClass("sm", false));
  for (const c of ["px-2", "py-1", "text-xs"]) assert.ok(got.includes(c), c);
  for (const c of ["px-3", "py-1.5", "text-sm"]) assert.ok(!got.includes(c), c);
});

test("controlClass lg size uses min-h-11 px-4 text-base", () => {
  const got = classes(controlClass("lg", false));
  for (const c of ["min-h-11", "px-4", "text-base"]) assert.ok(got.includes(c), c);
  for (const c of ["px-3", "py-1.5", "text-sm", "px-2", "py-1", "text-xs"]) assert.ok(!got.includes(c), c);
});

test("controlClass swaps border-border-strong for border-danger when invalid", () => {
  const ok = classes(controlClass("md", false));
  assert.ok(ok.includes("border-border-strong"));
  assert.ok(!ok.includes("border-danger"));

  const bad = classes(controlClass("md", true));
  assert.ok(bad.includes("border-danger"));
  assert.ok(!bad.includes("border-border-strong"));
});
