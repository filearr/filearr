// Shared tone helpers for the UI kit: status-string → Tone mapping and the
// token-only colour utilities each tone renders with. tone.ts is DOM-free so it
// runs on Node's built-in test runner (`npm test` from frontend/).

import assert from "node:assert/strict";
import { test } from "node:test";

import { toneClasses, toneForStatus, type Tone } from "../src/lib/ui/tone.ts";

test("toneForStatus maps failure words to danger, case-insensitively", () => {
  for (const s of ["critical", "ERROR", "Failed", "failure", "fatal", "Offline", "revoked", "DISABLED"]) {
    assert.equal(toneForStatus(s), "danger", s);
  }
});

test("toneForStatus maps caution words to warn", () => {
  for (const s of ["warning", "WARN", "Degraded", "stale", "pending", "Queued", "held", "stopped", "Stopping", "cancelled", "CANCELED", "missing", "Missing"]) {
    assert.equal(toneForStatus(s), "warn", s);
  }
});

test("toneForStatus maps success words to ok", () => {
  for (const s of ["ok", "OK", "Success", "succeeded", "completed", "done", "Healthy", "online", "active", "enabled", "Verified", "Finished"]) {
    assert.equal(toneForStatus(s), "ok", s);
  }
});

test("toneForStatus maps in-flight words to info", () => {
  for (const s of ["running", "INFO", "In_Progress", "scanning", "Syncing"]) {
    assert.equal(toneForStatus(s), "info", s);
  }
});

test("toneForStatus falls back to neutral for empty, null and unknown", () => {
  assert.equal(toneForStatus(null), "neutral");
  assert.equal(toneForStatus(undefined), "neutral");
  assert.equal(toneForStatus(""), "neutral");
  assert.equal(toneForStatus("banana"), "neutral");
  assert.equal(toneForStatus("Mystery-State"), "neutral");
});

test("toneClasses returns the token utilities for each tone", () => {
  const expected: Record<Tone, string> = {
    neutral: "bg-surface-3 text-muted",
    ok: "bg-ok-bg text-ok",
    warn: "bg-warn-bg text-warn",
    danger: "bg-danger-bg text-danger",
    info: "bg-info-bg text-info",
    accent: "bg-accent/15 text-accent-text",
  };
  for (const tone of Object.keys(expected) as Tone[]) {
    assert.equal(toneClasses(tone), expected[tone], tone);
  }
});

test("toneClasses never emits raw palette colours", () => {
  for (const tone of ["neutral", "ok", "warn", "danger", "info", "accent"] as Tone[]) {
    assert.doesNotMatch(toneClasses(tone), /slate-|red-|green-|amber-|sky-|emerald-/);
  }
});
