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
  // Open an editing session first so the replace below stays a replace.
  assert.deepEqual(sync.reflect("#/search", A, "replace"), { write: "push" });
  assert.equal(sync.incoming(A), "ignore");
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

// ---- editing sessions (final-review item 1) --------------------------------
// A browser-history model: `entries` + `index`, driven by the reducer's writes.
function history(start: string) {
  const entries = [start];
  let index = 0;
  return {
    get url() { return entries[index]; },
    get length() { return entries.length; },
    write(w: string, hash: string) {
      if (w === "push") { entries.splice(index + 1); entries.push(hash); index++; }
      else if (w === "replace") entries[index] = hash;
    },
    back() { index--; return entries[index]; },
  };
}

test("hash sync: push A → replace B → replace C occupy one new entry; Back lands on A", () => {
  const sync = createHashSync();
  const h = history("#/search");
  h.write(sync.reflect(h.url, A, "push").write, A);
  assert.equal(sync.incoming(A), "ignore");
  // First keystroke after the commit: upgraded to a push, so A survives.
  const w1 = sync.reflect(h.url, B, "replace").write;
  assert.equal(w1, "push");
  h.write(w1, B);
  assert.equal(sync.incoming(B), "ignore");
  // Further typing in the same session replaces.
  const w2 = sync.reflect(h.url, C, "replace").write;
  assert.equal(w2, "replace");
  h.write(w2, C);
  assert.equal(h.length, 3, "#/search, A, and one entry for B/C");
  assert.equal(h.back(), A, "Back lands on the committed A");
  assert.equal(sync.incoming(A), "apply");
});

test("hash sync: push A → incoming X (apply) → replace B makes B a new entry", () => {
  const sync = createHashSync();
  sync.reflect("#/search", A, "push");
  assert.equal(sync.incoming(A), "ignore");
  sync.reflect(A, B, "replace"); // editing session on top of A
  assert.equal(sync.incoming(C), "apply", "Back/Forward or a pasted link");
  // The page re-encodes what it applied: a canonical write never pushes.
  assert.deepEqual(sync.reflect(C, C, "canonical"), { write: "none" });
  assert.deepEqual(sync.reflect(C, B, "replace"), { write: "push" }, "a new editing session");
  assert.deepEqual(sync.reflect(B, A, "replace"), { write: "replace" });
});

test("hash sync: any push (even a no-op Enter) ends the editing session", () => {
  const sync = createHashSync();
  assert.deepEqual(sync.reflect("#/search", A, "replace"), { write: "push" });
  assert.deepEqual(sync.reflect(A, B, "replace"), { write: "replace" });
  assert.deepEqual(sync.reflect(B, B, "push"), { write: "none" }, "Enter on an already-reflected query");
  assert.deepEqual(sync.reflect(B, C, "replace"), { write: "push" }, "next edit gets its own entry");
});

test("hash sync: a canonical write after an applied hash replaces and never traps Back", () => {
  const sync = createHashSync();
  const h = history("#/search?b=2&a=1"); // a non-canonical deep link
  const w = sync.reflect(h.url, "#/search?a=1&b=2", "canonical").write;
  assert.equal(w, "replace", "re-encoding what was applied replaces in place");
  h.write(w, "#/search?a=1&b=2");
  assert.equal(h.length, 1);
  // Still a fresh session: the first keystroke pushes.
  assert.deepEqual(sync.reflect(h.url, A, "replace"), { write: "push" });
});

test("hash sync: a no-op replace does not open a session", () => {
  const sync = createHashSync();
  assert.deepEqual(sync.reflect(A, A, "replace"), { write: "none" });
  assert.deepEqual(sync.reflect(A, B, "replace"), { write: "push" });
});
