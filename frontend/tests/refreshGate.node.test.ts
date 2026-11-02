// refreshGate.ts — one refresh at a time. A plain call while one is in flight
// joins it (the poll tick must not stack requests); a forced call queues
// exactly ONE trailing run, so the refresh after an action always reflects it
// (B26: the post-action refresh used to be skipped by `if (loading) return`).

import assert from "node:assert/strict";
import { test } from "node:test";

import { refreshGate } from "../src/lib/refreshGate.ts";

function deferred() {
  let resolve!: () => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<void>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("refreshGate: idle call runs fn once", async () => {
  let n = 0;
  const run = refreshGate(async () => {
    n++;
  });
  await run();
  assert.equal(n, 1);
});

test("refreshGate: a plain call while in flight joins it (no second run)", async () => {
  const d = deferred();
  let n = 0;
  const run = refreshGate(() => {
    n++;
    return d.promise;
  });
  const a = run();
  const b = run();
  d.resolve();
  await Promise.all([a, b]);
  assert.equal(n, 1);
});

test("refreshGate: a forced call while in flight queues one trailing run after it", async () => {
  const gates = [deferred(), deferred()];
  let n = 0;
  const order: string[] = [];
  const run = refreshGate(() => {
    const i = n++;
    order.push(`start${i}`);
    return gates[i].promise.then(() => {
      order.push(`end${i}`);
    });
  });
  const first = run();
  const forced = run({ force: true });
  await Promise.resolve();
  assert.equal(n, 1, "trailing run waits for the in-flight one");
  gates[0].resolve();
  await first;
  await new Promise((r) => setTimeout(r, 0));
  assert.equal(n, 2);
  let forcedDone = false;
  void forced.then(() => (forcedDone = true));
  await Promise.resolve();
  assert.equal(forcedDone, false, "forced call resolves only after the trailing run");
  gates[1].resolve();
  await forced;
  assert.deepEqual(order, ["start0", "end0", "start1", "end1"]);
});

test("refreshGate: many forced calls while in flight queue exactly one trailing run", async () => {
  const gates = [deferred(), deferred(), deferred()];
  let n = 0;
  const run = refreshGate(() => gates[n++].promise);
  const first = run();
  const f = [run({ force: true }), run({ force: true }), run({ force: true })];
  gates[0].resolve();
  await first;
  await new Promise((r) => setTimeout(r, 0));
  gates[1].resolve();
  await Promise.all(f);
  assert.equal(n, 2);
});

test("refreshGate: a plain call during the trailing run joins it", async () => {
  const gates = [deferred(), deferred()];
  let n = 0;
  const run = refreshGate(() => gates[n++].promise);
  void run();
  const forced = run({ force: true });
  gates[0].resolve();
  await new Promise((r) => setTimeout(r, 0));
  const plain = run();
  gates[1].resolve();
  await Promise.all([forced, plain]);
  assert.equal(n, 2);
});

test("refreshGate: a failed run still lets the trailing run start; errors reach their callers", async () => {
  const gates = [deferred(), deferred(), deferred()];
  let n = 0;
  const run = refreshGate(() => gates[n++].promise);
  const first = run();
  const forced = run({ force: true });
  gates[0].reject(new Error("boom"));
  await assert.rejects(first, /boom/);
  await new Promise((r) => setTimeout(r, 0));
  gates[1].resolve();
  await forced;
  assert.equal(n, 2);
  // Idle again: the next plain call starts a fresh run.
  const third = run();
  gates[2].resolve();
  await third;
  assert.equal(n, 3);
});
