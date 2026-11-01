// createHashSync(): the own-write token that tells SearchPage's hashchange
// listener "this is the hash I just wrote" from "the user navigated" (A5).
// Replays are written out step by step so each line reads as browser history.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createHashSync } from "../src/lib/searchHash.ts";

const A = "#/search?q=alpha";
const B = "#/search?q=beta";
const C = "#/search?q=gamma";

test("hash sync: external navigation A → B → Back(A) → Forward(B) applies every step", () => {
  const sync = createHashSync();
  assert.equal(sync.incoming(A), "apply");
  assert.equal(sync.incoming(B), "apply");
  assert.equal(sync.incoming(A), "apply", "Back");
  assert.equal(sync.incoming(B), "apply", "Forward");
});

test("hash sync: own pushes A, B, then Back(A) and Forward(B) both apply (A5)", () => {
  const sync = createHashSync();
  assert.deepEqual(sync.reflect("#/search", A, "push"), { write: "push" });
  assert.equal(sync.incoming(A), "ignore", "our own write of A");
  assert.deepEqual(sync.reflect(A, B, "push"), { write: "push" });
  assert.equal(sync.incoming(B), "ignore", "our own write of B");
  // Back: the page applies A, re-runs, and reflects the hash it is already on.
  assert.equal(sync.incoming(A), "apply", "Back to A");
  assert.deepEqual(sync.reflect(A, A, "push"), { write: "none" });
  // Forward to the last own-written hash must apply (the old lastWrittenHash bug).
  assert.equal(sync.incoming(B), "apply", "Forward to B");
  assert.deepEqual(sync.reflect(B, B, "push"), { write: "none" });
});

test("hash sync: own write then matching hashchange is ignored once, a second identical one applies", () => {
  const sync = createHashSync();
  assert.deepEqual(sync.reflect(A, B, "push"), { write: "push" });
  assert.equal(sync.incoming(B), "ignore");
  assert.equal(sync.incoming(B), "apply");
});

test("hash sync: a different hash than the pending own write applies and clears the token", () => {
  const sync = createHashSync();
  sync.reflect(A, B, "push");
  assert.equal(sync.incoming(C), "apply");
  assert.equal(sync.incoming(B), "apply", "token was cleared by C");
});

test("hash sync: replace writes record the token too; the next reflect or incoming clears it", () => {
  const sync = createHashSync();
  // replaceState fires no hashchange, so the token lingers until the next event.
  assert.deepEqual(sync.reflect(A, B, "replace"), { write: "replace" });
  assert.equal(sync.incoming(C), "apply", "Back to some other entry");
  assert.equal(sync.incoming(B), "apply", "Forward to the replaced entry applies");

  const s2 = createHashSync();
  s2.reflect(A, B, "replace");
  assert.deepEqual(s2.reflect(B, C, "push"), { write: "push" });
  assert.equal(s2.incoming(C), "ignore", "token now names C");
  assert.equal(s2.incoming(B), "apply");
});

test("hash sync: reflecting the hash already in the URL writes nothing and clears a stale token", () => {
  const sync = createHashSync();
  sync.reflect(A, B, "replace");
  assert.deepEqual(sync.reflect(B, B, "push"), { write: "none" });
  assert.deepEqual(sync.reflect(B, B, "replace"), { write: "none" });
  assert.equal(sync.incoming(B), "apply");
});
