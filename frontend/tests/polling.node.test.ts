// Visibility-aware poller: ticks only while the tab is visible, refreshes on
// return. Runs on Node's built-in test runner (`npm test` from frontend/);
// createPoller takes its clock and visibility as deps, so a hand-rolled fake
// timer and a boolean switch stand in for the browser.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createPoller, type PollerDeps } from "../src/lib/polling.ts";

// Fake clock + visibility: intervals are stored, `tick()` fires each live one
// once; `setVisible()` flips the switch and notifies subscribers like a
// `visibilitychange` event would. `advance(ms)` moves the injected `now()`
// clock, which only the return-refresh rate limit reads; `periods()` lists
// the `ms` each setInterval call was armed with.
function fakeDeps() {
  const intervals = new Map<number, () => void>();
  const listeners = new Set<() => void>();
  const periods: number[] = [];
  let nextId = 1;
  let visible = true;
  let clock = 0;
  const deps: PollerDeps = {
    setInterval: ((cb: () => void, ms: number) => {
      const id = nextId++;
      intervals.set(id, cb);
      periods.push(ms);
      return id;
    }) as unknown as typeof globalThis.setInterval,
    clearInterval: ((id: number) => {
      intervals.delete(id);
    }) as unknown as typeof globalThis.clearInterval,
    isVisible: () => visible,
    onVisibility: (cb) => {
      listeners.add(cb);
      return () => listeners.delete(cb);
    },
    now: () => clock,
  };
  return {
    deps,
    tick: () => [...intervals.values()].forEach((cb) => cb()),
    setVisible: (v: boolean) => {
      visible = v;
      [...listeners].forEach((cb) => cb());
    },
    advance: (ms: number) => {
      clock += ms;
    },
    periods: () => periods,
    liveIntervals: () => intervals.size,
    liveListeners: () => listeners.size,
  };
}

test("ticks call fn while visible, and not on creation by default", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps);
  assert.equal(calls, 0);
  f.tick();
  f.tick();
  assert.equal(calls, 2);
});

test("immediate: true calls fn once at creation", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps, { immediate: true });
  assert.equal(calls, 1);
});

test("ticks are skipped while hidden", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps, { refreshOnReturn: false });
  f.setVisible(false);
  f.tick();
  f.tick();
  assert.equal(calls, 0);
});

test("returning to visible refreshes once (refreshOnReturn defaults on)", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps);
  f.setVisible(false);
  assert.equal(calls, 0); // going hidden never fires fn
  f.setVisible(true);
  assert.equal(calls, 1);
  f.tick();
  assert.equal(calls, 2);
});

test("refreshOnReturn: false does not refresh on return", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps, { refreshOnReturn: false });
  f.setVisible(false);
  f.setVisible(true);
  assert.equal(calls, 0);
});

test("stop() clears the interval, unsubscribes, and prevents further calls", () => {
  const f = fakeDeps();
  let calls = 0;
  const p = createPoller(() => calls++, 1000, f.deps);
  assert.equal(f.liveIntervals(), 1);
  assert.equal(f.liveListeners(), 1);
  p.stop();
  assert.equal(f.liveIntervals(), 0);
  assert.equal(f.liveListeners(), 0);
  f.tick();
  f.setVisible(false);
  f.setVisible(true);
  assert.equal(calls, 0);
});

test("setInterval is armed with the requested period", () => {
  const f = fakeDeps();
  createPoller(() => {}, 1234, f.deps);
  assert.deepEqual(f.periods(), [1234]);
});

test("return-refresh within intervalMs of the last call is skipped", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps);
  f.tick(); // last call at t=0
  assert.equal(calls, 1);
  f.advance(400);
  f.setVisible(false);
  f.setVisible(true); // 400ms since the tick: too soon
  assert.equal(calls, 1);
  f.tick(); // ticks are not rate-limited
  assert.equal(calls, 2);
});

test("return-refresh after intervalMs since the last call fires", () => {
  const f = fakeDeps();
  let calls = 0;
  createPoller(() => calls++, 1000, f.deps, { immediate: true }); // t=0
  f.advance(1000);
  f.setVisible(false);
  f.setVisible(true);
  assert.equal(calls, 2);
  // ...and that return-refresh itself counts as the last call.
  f.advance(10);
  f.setVisible(false);
  f.setVisible(true);
  assert.equal(calls, 2);
});

test("an fn returning a pending promise is not called again until it settles", async () => {
  const f = fakeDeps();
  let calls = 0;
  let settle: () => void = () => {};
  createPoller(
    () => {
      calls++;
      return new Promise<void>((r) => {
        settle = r;
      });
    },
    1000,
    f.deps,
    { immediate: true },
  );
  assert.equal(calls, 1);
  f.tick();
  f.advance(5000);
  f.setVisible(false);
  f.setVisible(true); // return-refresh is skipped too
  assert.equal(calls, 1);
  settle();
  await new Promise((r) => setImmediate(r)); // let the in-flight flag clear
  f.tick();
  assert.equal(calls, 2);
});

test("a rejected in-flight promise clears the guard", async () => {
  const f = fakeDeps();
  let calls = 0;
  let fail: () => void = () => {};
  createPoller(
    () => {
      calls++;
      return new Promise<void>((_, rej) => {
        fail = () => rej(new Error("x"));
      });
    },
    1000,
    f.deps,
    { immediate: true },
  );
  fail();
  await new Promise((r) => setImmediate(r));
  f.tick();
  assert.equal(calls, 2);
});

test("stop() works while a call is in flight", () => {
  const f = fakeDeps();
  let calls = 0;
  const p = createPoller(
    () => {
      calls++;
      return new Promise<void>(() => {});
    },
    1000,
    f.deps,
    { immediate: true },
  );
  p.stop();
  assert.equal(f.liveIntervals(), 0);
  assert.equal(f.liveListeners(), 0);
  f.tick();
  assert.equal(calls, 1);
});
