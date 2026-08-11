// Pure-logic tests for the console's POLICY-group surface (the agents table's
// "Policy group" column and its confirmation dialog).
//
// Background — the bug these back up: `agents.rollout_group` is the middle
// POLICY scope AND the release-canary selector, while `config_group_id` is a
// separate, orthogonal grouping that policy resolution never reads. The console
// used to expose only the second, so operators assumed policies keyed off it.
// Two pieces of logic make the first legible, and both are worth pinning:
//
//   * `resolvedPolicyScope` mirrors filearr.policy.resolve_effective_policy's
//     agent > group > global walk. If it drifts from the backend, the table
//     tells an operator a document applies when it does not — worse than saying
//     nothing. The ORDERING is the contract.
//   * `describePolicyGroupChange` is the only place the console states BOTH
//     consequences of one field. Silently dropping the canary half is exactly
//     the surprise this whole change exists to remove.
//
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler / DOM.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  describePolicyGroupChange,
  resolvedPolicyScope,
} from "../src/lib/agentPolicyDoc.ts";

const AGENT = { id: "11111111-2222-3333-4444-555555555555", rollout_group: "filers" };

// --------------------------------------------------------------------------- //
// resolvedPolicyScope — agent > group > global > none                           //
// --------------------------------------------------------------------------- //
test("the agent's own document outranks its group and global", () => {
  const scopes = [`agent:${AGENT.id}`, "group:filers", "global"];
  assert.equal(resolvedPolicyScope(scopes, AGENT), `agent:${AGENT.id}`);
});

test("the group document wins when the agent has none", () => {
  assert.equal(resolvedPolicyScope(["group:filers", "global"], AGENT), "group:filers");
});

test("a group with no document falls through to global", () => {
  assert.equal(
    resolvedPolicyScope(["group:desktops", "global"], AGENT),
    "global",
    "group:desktops belongs to a different group and must not be picked up",
  );
});

test("no document anywhere resolves to 'none', not to global", () => {
  // "none" is the backend's literal answer (an agent must never 404 the policy
  // endpoint); the console must not invent a global document that is not there.
  assert.equal(resolvedPolicyScope([], AGENT), "none");
  assert.equal(resolvedPolicyScope(["group:desktops"], AGENT), "none");
});

test("another agent's document is never mistaken for this one's", () => {
  const other = "99999999-8888-7777-6666-555555555555";
  assert.equal(resolvedPolicyScope([`agent:${other}`, "global"], AGENT), "global");
});

test("a group name containing a colon still resolves exactly", () => {
  // parse_scope splits on the FIRST colon only, so `group:eu:filers` is the
  // group literally named "eu:filers" — the console must match it byte-for-byte.
  const a = { id: AGENT.id, rollout_group: "eu:filers" };
  assert.equal(resolvedPolicyScope(["group:eu:filers", "global"], a), "group:eu:filers");
});

// --------------------------------------------------------------------------- //
// describePolicyGroupChange — both consequences, stated                         //
// --------------------------------------------------------------------------- //
const base = {
  agentName: "reception-pc",
  from: "desktops",
  to: "filers",
  canaryGroup: "canary",
  agentId: AGENT.id,
};

test("names the target document and warns that replacement is whole-document", () => {
  const msg = describePolicyGroupChange({
    ...base,
    scopes: ["group:desktops", "group:filers", "global"],
  });
  assert.match(msg, /reception-pc/);
  assert.match(msg, /group:filers policy document/);
  assert.match(msg, /instead of group:desktops/);
  assert.match(msg, /replaces the previous one WHOLE/);
});

test("says so when the target group has no document yet", () => {
  const msg = describePolicyGroupChange({
    ...base,
    scopes: ["group:desktops", "global"],
  });
  assert.match(msg, /no group:filers policy document yet/);
  assert.match(msg, /falls back to the global document/);
});

test("with no global document either, the fallback is the built-in defaults", () => {
  const msg = describePolicyGroupChange({ ...base, scopes: [] });
  assert.match(msg, /falls back to its built-in defaults/);
});

test("an agent: document outranks the move, and the dialog admits it", () => {
  // Otherwise an operator moves the machine, sees nothing change, and concludes
  // the group assignment is broken.
  const msg = describePolicyGroupChange({
    ...base,
    scopes: [`agent:${AGENT.id}`, "group:filers", "global"],
  });
  assert.match(msg, /own agent: policy document/);
  assert.match(msg, /will NOT change/);
});

test("joining the canary group is called out", () => {
  const msg = describePolicyGroupChange({
    ...base,
    to: "canary",
    scopes: ["global"],
  });
  assert.match(msg, /JOINS the release-canary group "canary"/);
  assert.match(msg, /un-promoted canary agent builds/);
});

test("leaving the canary group is called out", () => {
  const msg = describePolicyGroupChange({
    ...base,
    from: "canary",
    to: "filers",
    scopes: ["global"],
  });
  assert.match(msg, /LEAVES the release-canary group "canary"/);
});

test("a move between two non-canary groups says nothing about canary", () => {
  const msg = describePolicyGroupChange({ ...base, scopes: ["global"] });
  assert.doesNotMatch(msg, /canary/i);
});

test("always states when the change takes effect", () => {
  // The change is not pushed; it lands on the agent's next policy poll. An
  // operator who does not know that reports it as broken within ten seconds.
  const msg = describePolicyGroupChange({ ...base, scopes: ["global"] });
  assert.match(msg, /next policy poll/);
});
