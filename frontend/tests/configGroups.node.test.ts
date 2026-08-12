// Pure-logic tests for configuration-group layering (P13, 2026-08-11).
//
// These replace agentPolicyGroups.node.test.ts, which pinned the whole-document
// policy-scope walk that this change deleted. What is worth pinning now is the
// same class of thing: rules the console MIRRORS from the backend, where drift
// makes the UI confidently wrong rather than merely unhelpful.
//
//   * merge order (ascending priority, later wins) and its (name, id) tie-break
//     — get it backwards and a preview says the wrong group is in charge;
//   * per-key override and section isolation — the settings/policy split is two
//     validators on the backend and must not bleed in the client mirror;
//   * tier validation — the console disables "publish with rollout" on these,
//     so a rule the server has and the client does not turns a clear inline
//     message into a raw 422 (and vice versa: a rule the client invents alone
//     blocks a publish the server would have accepted);
//   * provenance formatting — the source badge is the only thing that makes a
//     surprising effective value traceable.
//
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler / DOM.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_ROLLOUT_TIERS,
  describeRollout,
  formatProvenance,
  mergeDocuments,
  provenanceFor,
  shadowedBy,
  sortLayers,
  tierEtaMinutes,
  validateTiers,
  type ConfigLayer,
} from "../src/lib/configGroups.ts";

const layer = (
  id: string,
  name: string,
  priority: number,
  policy: Record<string, unknown> = {},
  settings: Record<string, unknown> = {},
): ConfigLayer => ({ id, name, priority, policy, settings });

// --------------------------------------------------------------------------- //
// Merge order                                                                   //
// --------------------------------------------------------------------------- //
test("layers apply in ascending priority and the LAST one wins a contested key", () => {
  const merged = mergeDocuments([
    layer("c", "filers", 200, { watch_mode: true }),
    layer("a", "Global", 0, { watch_mode: false, auto_update: true }),
  ]);
  assert.equal(merged.policy.watch_mode, true, "priority 200 must beat priority 0");
  assert.equal(merged.policy.auto_update, true, "an uncontested key still applies");
  assert.deepEqual(
    merged.order.map((l) => l.name),
    ["Global", "filers"],
    "the reported order is the APPLY order, lowest priority first",
  );
});

test("input order never matters — only priority does", () => {
  const high = layer("z", "high", 500, { poll_interval_seconds: 900 });
  const low = layer("y", "low", 10, { poll_interval_seconds: 60 });
  assert.equal(mergeDocuments([high, low]).policy.poll_interval_seconds, 900);
  assert.equal(mergeDocuments([low, high]).policy.poll_interval_seconds, 900);
});

test("equal priorities tie-break by name, then by id", () => {
  // Ties are a NORMAL state (no unique constraint on priority), so the answer
  // has to be deterministic and identical to the backend's (priority, name, id).
  const merged = mergeDocuments([
    layer("id-b", "bravo", 100, { scan_on_start: false }),
    layer("id-a", "alpha", 100, { scan_on_start: true }),
  ]);
  assert.equal(merged.policy.scan_on_start, false, "bravo sorts after alpha, so it wins");

  const sameName = sortLayers([
    { id: "id-2", name: "same", priority: 100 },
    { id: "id-1", name: "same", priority: 100 },
  ]);
  assert.deepEqual(
    sameName.map((l) => l.id),
    ["id-1", "id-2"],
    "identical names fall through to the id",
  );
});

test("per-key override: a later layer overrides ONLY the keys it sets", () => {
  const merged = mergeDocuments([
    layer("a", "Global", 0, {
      watch_mode: false,
      extract_enabled: true,
      poll_interval_seconds: 60,
    }),
    layer("b", "filers", 100, { watch_mode: true }),
  ]);
  assert.deepEqual(merged.policy, {
    watch_mode: true,
    extract_enabled: true,
    poll_interval_seconds: 60,
  });
});

test("null and undefined mean 'inherit', not 'override with nothing'", () => {
  // The tri-state form emits absent keys, but a hand-authored raw document (or
  // a typed settings object with explicit nulls) can carry them. Treating a
  // null as an override would blank a lower layer's real value.
  const merged = mergeDocuments([
    layer("a", "Global", 0, { watch_mode: true }),
    layer("b", "later", 100, { watch_mode: null, extract_ocr: undefined }),
  ]);
  assert.equal(merged.policy.watch_mode, true);
  assert.ok(!("extract_ocr" in merged.policy));
  assert.equal(merged.provenance["policy.watch_mode"].group_name, "Global");
});

test("sections are isolated: a settings key never leaks into policy", () => {
  const merged = mergeDocuments([
    layer("a", "Global", 0, { log_level: "policy-side" }, { log_level: "info" }),
    layer("b", "filers", 100, {}, { log_level: "debug" }),
  ]);
  assert.equal(merged.settings.log_level, "debug");
  assert.equal(
    merged.policy.log_level,
    "policy-side",
    "the settings layer must not touch the same-named policy key",
  );
  assert.equal(merged.provenance["settings.log_level"].group_name, "filers");
  assert.equal(merged.provenance["policy.log_level"].group_name, "Global");
});

test("nested objects REPLACE wholesale — the merge is shallow per section", () => {
  // Documented backend behaviour and the operator mental model for `inventory`:
  // "this group's collector list", not "unioned with something below it".
  const merged = mergeDocuments([
    layer("a", "Global", 0, {}, { inventory: { enabled: true, collectors: ["stat", "owner"] } }),
    layer("b", "filers", 100, {}, { inventory: { enabled: false } }),
  ]);
  assert.deepEqual(merged.settings.inventory, { enabled: false });
});

test("provenance records the WINNING layer for every merged key", () => {
  const merged = mergeDocuments([
    layer("a", "Global", 0, { watch_mode: false, auto_update: false }),
    layer("b", "filers", 100, { watch_mode: true }),
  ]);
  assert.deepEqual(merged.provenance["policy.watch_mode"], {
    group_id: "b",
    group_name: "filers",
  });
  assert.deepEqual(merged.provenance["policy.auto_update"], {
    group_id: "a",
    group_name: "Global",
  });
});

test("an empty layer set merges to an empty document rather than throwing", () => {
  const merged = mergeDocuments([]);
  assert.deepEqual(merged.policy, {});
  assert.deepEqual(merged.settings, {});
  assert.deepEqual(merged.order, []);
});

test("shadowedBy names every losing setter of a key, winner excluded", () => {
  const layers = [
    layer("a", "Global", 0, { watch_mode: false }),
    layer("b", "desktops", 50, { watch_mode: false }),
    layer("c", "filers", 100, { watch_mode: true }),
    layer("d", "unrelated", 150, { auto_update: true }),
  ];
  assert.deepEqual(
    shadowedBy(layers, "policy", "watch_mode").map((l) => l.name),
    ["Global", "desktops"],
  );
  assert.deepEqual(shadowedBy(layers, "policy", "auto_update"), []);
});

// --------------------------------------------------------------------------- //
// Tier validation — mirrors filearr.agent_config.validate_tiers                 //
// --------------------------------------------------------------------------- //
test("a single 100% tier is the minimal valid rollout", () => {
  assert.equal(validateTiers([{ percent: 100, delay_minutes: 0 }]), null);
});

test("a realistic ascending schedule validates", () => {
  assert.equal(
    validateTiers([
      { percent: 10, delay_minutes: 0 },
      { percent: 50, delay_minutes: 60 },
      { percent: 100, delay_minutes: 120 },
    ]),
    null,
  );
});

test("an empty tier list is rejected", () => {
  assert.match(validateTiers([]) ?? "", /at least one tier/i);
});

test("more than five tiers is rejected", () => {
  const six = [10, 20, 40, 60, 80, 100].map((percent) => ({ percent, delay_minutes: 5 }));
  assert.equal(six.length, MAX_ROLLOUT_TIERS + 1);
  assert.match(validateTiers(six) ?? "", /at most 5/);
  // …and exactly five is fine, so the bound is not off by one.
  assert.equal(validateTiers(six.slice(1)), null);
});

test("percents must strictly ascend — equal is not 'ascending'", () => {
  assert.match(
    validateTiers([
      { percent: 50, delay_minutes: 0 },
      { percent: 50, delay_minutes: 10 },
      { percent: 100, delay_minutes: 10 },
    ]) ?? "",
    /Tier 2.*greater than/s,
  );
  assert.match(
    validateTiers([
      { percent: 60, delay_minutes: 0 },
      { percent: 30, delay_minutes: 10 },
      { percent: 100, delay_minutes: 10 },
    ]) ?? "",
    /Tier 2/,
  );
});

test("percent must be a whole number inside 1..100", () => {
  assert.match(validateTiers([{ percent: 0, delay_minutes: 0 }]) ?? "", /between 1 and 100/);
  assert.match(validateTiers([{ percent: 101, delay_minutes: 0 }]) ?? "", /between 1 and 100/);
  assert.match(validateTiers([{ percent: 10.5, delay_minutes: 0 }]) ?? "", /whole number/);
});

test("the last tier must be exactly 100", () => {
  const err = validateTiers([
    { percent: 10, delay_minutes: 0 },
    { percent: 90, delay_minutes: 30 },
  ]);
  assert.match(err ?? "", /last tier must be 100/);
  // The message has to explain the alternative, not just refuse: "hold a subset
  // permanently" is a narrower GROUP, never a stalled rollout.
  assert.match(err ?? "", /narrower group/);
});

test("delay must be a whole number of minutes, zero or more", () => {
  assert.match(
    validateTiers([{ percent: 100, delay_minutes: -1 }]) ?? "",
    /0 or more/,
  );
  assert.match(
    validateTiers([{ percent: 100, delay_minutes: 1.5 }]) ?? "",
    /whole number of minutes/,
  );
  assert.equal(validateTiers([{ percent: 100, delay_minutes: 0 }]), null);
});

test("tier ETA is a RUNNING SUM — each delay counts from the previous tier", () => {
  const tiers = [
    { percent: 10, delay_minutes: 0 },
    { percent: 50, delay_minutes: 60 },
    { percent: 100, delay_minutes: 30 },
  ];
  assert.equal(tierEtaMinutes(tiers, 0), 0);
  assert.equal(tierEtaMinutes(tiers, 1), 60);
  assert.equal(tierEtaMinutes(tiers, 2), 90);
});

// --------------------------------------------------------------------------- //
// Status text                                                                   //
// --------------------------------------------------------------------------- //
test("describeRollout reads as 'scheduled' before the first tier activates", () => {
  const tiers = [
    { percent: 10, delay_minutes: 0 },
    { percent: 100, delay_minutes: 60 },
  ];
  // current_tier is -1 until the engine fires tier 0 — never render that as
  // "tier 0", which reads as though something already shipped.
  assert.match(describeRollout({ status: "scheduled", current_tier: -1, tiers }), /scheduled/);
  assert.match(describeRollout({ status: "scheduled", current_tier: -1, tiers }), /2 tiers/);
});

test("describeRollout counts tiers from 1 and reports coverage", () => {
  const tiers = [
    { percent: 10, delay_minutes: 0 },
    { percent: 50, delay_minutes: 60 },
    { percent: 100, delay_minutes: 60 },
  ];
  assert.equal(
    describeRollout({ status: "running", current_tier: 1, tiers, covered_percent: 50 }),
    "tier 2 of 3 · 50% covered",
  );
});

test("finished rollouts state their outcome rather than a tier", () => {
  const tiers = [{ percent: 100, delay_minutes: 0 }];
  assert.match(describeRollout({ status: "completed", current_tier: 0, tiers }), /completed/);
  assert.match(describeRollout({ status: "cancelled", current_tier: 0, tiers }), /cancelled/);
});

// --------------------------------------------------------------------------- //
// Provenance formatting                                                         //
// --------------------------------------------------------------------------- //
test("formatProvenance renders '<group> v<version>'", () => {
  assert.equal(formatProvenance({ group_name: "filers", version: 7 }), "filers v7");
});

test("a key no group sets reads as the built-in default, not as blank", () => {
  assert.equal(formatProvenance(undefined), "built-in default");
});

test("a value delivered by a rollout tier says so", () => {
  // Two agents in one group can legitimately sit on different versions
  // mid-rollout; without the marker the console looks like it is reporting
  // stale data.
  assert.equal(
    formatProvenance({ group_name: "filers", version: 8 }, { viaRollout: true }),
    "filers v8 · via rollout",
  );
});

test("provenanceFor keys on '<section>.<key>' and resolves via_rollout", () => {
  const provenance = {
    "policy.watch_mode": { group_id: "g1", group_name: "filers", version: 3 },
    "settings.log_level": { group_id: "g2", group_name: "Global", version: 1 },
  };
  const groups = [
    { id: "g1", via_rollout: true },
    { id: "g2", via_rollout: false },
  ];
  assert.equal(
    provenanceFor(provenance, groups, "policy", "watch_mode"),
    "filers v3 · via rollout",
  );
  assert.equal(provenanceFor(provenance, groups, "settings", "log_level"), "Global v1");
  // Section isolation again, from the reader's side: the same key name in the
  // other section must not borrow this one's badge.
  assert.equal(
    provenanceFor(provenance, groups, "settings", "watch_mode"),
    "built-in default",
  );
  assert.equal(provenanceFor(provenance, groups, "policy", "nothing_sets_me"), "built-in default");
});
