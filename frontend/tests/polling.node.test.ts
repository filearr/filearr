// Visibility-aware poller: ticks only while the tab is visible, refreshes on
// return. Runs on Node's built-in test runner (`npm test` from frontend/);
// createPoller takes its clock and visibility as deps, so a hand-rolled fake
// timer and a boolean switch stand in for the browser.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createPoller, type PollerDeps } from "../src/lib/polling.ts";

// Fake clock + visibility: intervals are stored, `tick()` fires each live one
// once; `setVisible()` flips the switch and notifies subscribers like a
// `visibilitychange` event would.
function fakeDeps() {
  const intervals = new Map<number, () => void>();
  const listeners = new Set<() => void>();
  let nextId = 1;
  let visible = true;
  const deps: PollerDeps = {
    setInterval: ((cb: () => void) => {
      const id = nextId++;
      intervals.set(id, cb);
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
  };
  return {
    deps,
    tick: () => [...intervals.values()].forEach((cb) => cb()),
    setVisible: (v: boolean) => {
      visible = v;
      [...listeners].forEach((cb) => cb());
    },
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
