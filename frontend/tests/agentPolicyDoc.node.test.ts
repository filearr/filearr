// Pure-logic tests for the agent-policy form <-> document serializer.
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler / DOM.
//
// The two invariants worth a test are both DATA-LOSS classes:
//   * a forward-compat key the console has never heard of must survive an edit
//     (the backend stores the submitted document VERBATIM, and PolicyModel is
//     extra="allow" precisely so newer agents can read newer keys);
//   * "inherit" must serialize as an ABSENT key, never `null` — policy
//     resolution is most-specific-wins with no key merging, so an absent key
//     means "agent default" while a null is a different (often invalid) value.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  POLICY_FIELDS,
  blankPolicyForm,
  buildPolicyDoc,
  cronShapeError,
  describeScope,
  formFromDoc,
  ignoredPolicySettings,
  passthroughFromDoc,
  unknownPolicyKeys,
  unparsedPolicyKeys,
  validatePolicyForm,
  type AgentPolicyDoc,
} from "../src/lib/agentPolicyDoc.ts";

// --------------------------------------------------------------------------- //
// Unknown-key round-trip                                                        //
// --------------------------------------------------------------------------- //
test("forward-compat keys survive a load -> edit -> save round-trip", () => {
  const stored: AgentPolicyDoc = {
    watch_mode: true,
    poll_interval_seconds: 120,
    // Keys this console does not model, from a newer agent build:
    future_knob: { nested: [1, 2, 3] },
    another_future_key: "keep me",
    // Reserved: not editable, but must not be dropped either.
    read_only: true,
  };

  const form = formFromDoc(stored);
  const passthrough = passthroughFromDoc(stored, form);
  form.watch_mode = { set: true, value: "false" }; // an ordinary edit

  const saved = buildPolicyDoc(form, passthrough);
  assert.deepEqual(saved.future_knob, { nested: [1, 2, 3] });
  assert.equal(saved.another_future_key, "keep me");
  assert.equal(saved.read_only, true);
  assert.equal(saved.watch_mode, false);
  assert.equal(saved.poll_interval_seconds, 120);
});

test("unknownPolicyKeys lists only genuinely unmodelled keys", () => {
  const doc: AgentPolicyDoc = {
    watch_mode: true,
    read_only: true,
    taxonomy_version: 7,
    group: {},
    zeta_future: 1,
    alpha_future: 2,
  };
  // Reserved keys (read_only / taxonomy_version / group) are modelled, so they
  // must NOT be reported as unknown; the rest are, sorted.
  assert.deepEqual(unknownPolicyKeys(doc), ["alpha_future", "zeta_future"]);
  assert.deepEqual(unknownPolicyKeys(null), []);
});

test("a stored value the form cannot parse is preserved, not silently dropped", () => {
  // `watch_mode` stored as a string (hand-written JSON / a type change): the
  // form leaves it on Inherit and flags it, but the value survives the save.
  const stored = {
    watch_mode: "yes",
    exclude_globs: "not-a-list",
  } as unknown as AgentPolicyDoc;
  const form = formFromDoc(stored);
  assert.equal(form.watch_mode.set, false);
  assert.equal(form.exclude_globs.set, false);
  assert.deepEqual(unparsedPolicyKeys(stored, form).sort(), [
    "exclude_globs",
    "watch_mode",
  ]);

  const saved = buildPolicyDoc(form, passthroughFromDoc(stored, form));
  assert.equal(saved.watch_mode, "yes");
  assert.equal(saved.exclude_globs, "not-a-list");
});

// --------------------------------------------------------------------------- //
// "Inherit" == absent key                                                       //
// --------------------------------------------------------------------------- //
test("inherit fields are omitted, never nulled", () => {
  const form = blankPolicyForm();
  form.auto_update = { set: true, value: "false" };
  const doc = buildPolicyDoc(form);

  assert.deepEqual(doc, { auto_update: false });
  // Explicitly: no key at all, not a null value.
  assert.equal("watch_mode" in doc, false);
  assert.equal("presets" in doc, false);
  assert.equal(Object.values(doc).includes(null as never), false);
});

test("toggling a previously-set field back to Inherit removes the key", () => {
  const stored: AgentPolicyDoc = { watch_mode: true, auto_update: false };
  const form = formFromDoc(stored);
  const passthrough = passthroughFromDoc(stored, form);
  form.watch_mode = { set: false, value: "true" };

  const saved = buildPolicyDoc(form, passthrough);
  assert.equal("watch_mode" in saved, false);
  assert.equal(saved.auto_update, false);
});

test("an empty list field set explicitly emits [] (distinct from inherit)", () => {
  const form = blankPolicyForm();
  form.exclude_globs = { set: true, value: "  \n \n" };
  const doc = buildPolicyDoc(form);
  assert.deepEqual(doc.exclude_globs, []);

  form.exclude_globs = { set: false, value: "" };
  assert.equal("exclude_globs" in buildPolicyDoc(form), false);
});

// --------------------------------------------------------------------------- //
// Parsing / serializing individual kinds                                        //
// --------------------------------------------------------------------------- //
test("formFromDoc / buildPolicyDoc round-trip every field kind", () => {
  const stored: AgentPolicyDoc = {
    presets: ["dev-artifacts", "system-junk"],
    include_globs: ["**/*.mkv", "**/*.mp4"],
    content_hash_max_bytes: 0,
    watch_mode: false,
    scan_cron: "0 3 * * *",
    scan_interval_seconds: 900,
    scan_on_start: true,
    poll_interval_seconds: 60,
    upload_rate_bytes_per_sec: 0,
    path_scope: ["media/**"],
  };
  const form = formFromDoc(stored);
  assert.equal(form.presets.value, "dev-artifacts, system-junk");
  assert.equal(form.include_globs.value, "**/*.mkv\n**/*.mp4");
  assert.equal(form.content_hash_max_bytes.value, "0");
  assert.equal(form.watch_mode.value, "false");

  assert.deepEqual(buildPolicyDoc(form, passthroughFromDoc(stored, form)), stored);
});

// --------------------------------------------------------------------------- //
// Content-extraction keys (2026-08-09 agent extraction parity)                  //
// --------------------------------------------------------------------------- //
test("the four extraction keys round-trip and respect inherit-vs-set", () => {
  const stored: AgentPolicyDoc = {
    extract_enabled: true,
    extract_body_text: true,
    extract_ocr: false,
    extract_max_bytes: 33554432,
  };
  const form = formFromDoc(stored);
  const passthrough = passthroughFromDoc(stored, form); // as the editor does: at load
  assert.equal(form.extract_enabled.value, "true");
  assert.equal(form.extract_ocr.value, "false"); // an explicit false, not inherit
  assert.equal(form.extract_ocr.set, true);
  assert.equal(form.extract_max_bytes.value, "33554432");
  assert.deepEqual(buildPolicyDoc(form, passthrough), stored);

  // Inherit means the key is ABSENT — an agent-default OFF, not an explicit false.
  form.extract_ocr = { set: false, value: "false" };
  const saved = buildPolicyDoc(form, passthrough);
  assert.equal("extract_ocr" in saved, false);
  assert.equal(saved.extract_enabled, true);
});

test("extraction keys are grouped in their own editor section, with bounds", () => {
  const keys = POLICY_FIELDS.filter((f) => f.section === "extraction").map((f) => f.key);
  assert.deepEqual(keys.sort(), [
    "extract_body_text",
    "extract_enabled",
    "extract_max_bytes",
    "extract_ocr",
  ]);
  const form = blankPolicyForm();
  form.extract_max_bytes = { set: true, value: "-1" };
  assert.match(validatePolicyForm(form).extract_max_bytes, /≥ 0/);
  form.extract_max_bytes = { set: true, value: "0" }; // 0 = extract nothing, valid
  assert.equal(validatePolicyForm(form).extract_max_bytes, undefined);
});

// --------------------------------------------------------------------------- //
// Capability advertisement vs. effective policy                                 //
// --------------------------------------------------------------------------- //
test("ignoredPolicySettings stays silent when capabilities are unknown", () => {
  const policy = { extract_enabled: true, extract_ocr: true };
  assert.deepEqual(ignoredPolicySettings(policy, null), []);
  assert.deepEqual(ignoredPolicySettings(null, { extract: true }), []);
});

test("an agent with no extraction pass ignores every extraction key that does something", () => {
  const out = ignoredPolicySettings(
    {
      extract_enabled: true,
      extract_body_text: true,
      extract_ocr: false, // an explicit off asks nothing of the agent
      extract_max_bytes: 1024,
    },
    { container: true }, // an older advertisement: no `extract` key at all
  );
  assert.deepEqual(
    out.map((i) => i.key).sort(),
    ["extract_body_text", "extract_enabled", "extract_max_bytes"],
  );
  assert.match(out[0].reason, /no extraction pass/);
});

test("extraction keys with the pass switched off are flagged once, by root cause", () => {
  const out = ignoredPolicySettings(
    { extract_ocr: true, extract_body_text: true },
    { extract: true, tools: { tesseract: true } },
  );
  assert.deepEqual(out.map((i) => i.key).sort(), ["extract_body_text", "extract_ocr"]);
  for (const i of out) assert.match(i.reason, /extract_enabled is not on/);
});

test("extract_ocr is flagged when the host has no tesseract, and only then", () => {
  const caps = {
    extract: true,
    extract_schema: 1,
    tools: { ffmpeg: true, ffprobe: true, tesseract: false, exiftool: false },
  };
  const policy = { extract_enabled: true, extract_body_text: true, extract_ocr: true };
  assert.deepEqual(ignoredPolicySettings(policy, caps), [
    { key: "extract_ocr", reason: "no tesseract on the agent host" },
  ]);

  // Same policy, a host that has tesseract: nothing is ignored.
  assert.deepEqual(
    ignoredPolicySettings(policy, { ...caps, tools: { ...caps.tools, tesseract: true } }),
    [],
  );
  // OCR not requested: the missing tool is irrelevant.
  assert.deepEqual(
    ignoredPolicySettings({ extract_enabled: true, extract_ocr: false }, caps),
    [],
  );
});

// --------------------------------------------------------------------------- //
// Client-side validation mirrors the server bounds                              //
// --------------------------------------------------------------------------- //
test("validatePolicyForm enforces the server's numeric bounds", () => {
  const form = blankPolicyForm();
  form.poll_interval_seconds = { set: true, value: "30" };
  form.reconcile_interval_seconds = { set: true, value: "299" };
  form.content_hash_max_bytes = { set: true, value: "-1" };
  form.upload_rate_bytes_per_sec = { set: true, value: "0" };
  form.offline_grace_seconds = { set: true, value: "" };

  const errors = validatePolicyForm(form);
  assert.match(errors.poll_interval_seconds, /≥ 60/);
  assert.match(errors.reconcile_interval_seconds, /≥ 300/);
  assert.match(errors.content_hash_max_bytes, /≥ 0/);
  assert.match(errors.offline_grace_seconds, /required/);
  assert.equal(errors.upload_rate_bytes_per_sec, undefined); // 0 = unlimited, valid
});

test("validatePolicyForm ignores fields left on Inherit", () => {
  const form = blankPolicyForm();
  form.poll_interval_seconds = { set: false, value: "1" }; // out of range but unset
  assert.deepEqual(validatePolicyForm(form), {});
});

test("preset names are checked against the catalogue when it is known", () => {
  const form = blankPolicyForm();
  form.presets = { set: true, value: "system-junk, not-a-preset" };
  assert.deepEqual(validatePolicyForm(form), {}); // no catalogue -> no check
  const errors = validatePolicyForm(form, { knownPresets: ["system-junk"] });
  assert.match(errors.presets, /not-a-preset/);
});

test("path_scope predicate cap", () => {
  const form = blankPolicyForm();
  form.path_scope = {
    set: true,
    value: Array.from({ length: 1001 }, (_, i) => `p${i}/**`).join("\n"),
  };
  assert.match(validatePolicyForm(form).path_scope, /max 1000/);
});

test("cronShapeError catches the obvious typos, accepts 5 fields", () => {
  assert.equal(cronShapeError("0 3 * * *"), null);
  assert.equal(cronShapeError("*/15 * * * MON-FRI"), null);
  assert.match(cronShapeError("0 3 * *") ?? "", /got 4/);
  assert.match(cronShapeError("0 3 * * * *") ?? "", /got 6/);
  assert.match(cronShapeError("   ") ?? "", /required/);
  assert.match(cronShapeError("0 3 * * @weekly") ?? "", /unexpected characters/);
});

// --------------------------------------------------------------------------- //
// Scope labels                                                                  //
// --------------------------------------------------------------------------- //
test("describeScope labels the frozen scope grammar", () => {
  assert.equal(describeScope("global"), "Global (every agent)");
  assert.equal(describeScope("group:canary"), 'Rollout group "canary"');
  assert.match(describeScope("agent:0198-abc"), /^Agent 0198-abc$/);
  assert.equal(describeScope("none"), "No policy document anywhere");
});
