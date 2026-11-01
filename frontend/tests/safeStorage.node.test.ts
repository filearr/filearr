// safeStorage: localStorage that never throws, with an in-memory fallback for
// private mode / blocked storage / Node. Each test installs its own stub.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import * as safeStorage from "../src/lib/safeStorage.ts";

const g = globalThis as { localStorage?: unknown };
const original = Object.getOwnPropertyDescriptor(globalThis, "localStorage");

function install(value: unknown): void {
  Object.defineProperty(globalThis, "localStorage", { value, configurable: true, writable: true });
}

afterEach(() => {
  if (original) Object.defineProperty(globalThis, "localStorage", original);
  else delete g.localStorage;
});

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => (m.has(k) ? m.get(k)! : null),
    setItem: (k: string, v: string) => void m.set(k, String(v)),
    removeItem: (k: string) => void m.delete(k),
    map: m,
  };
}

const boom = () => {
  throw new DOMException("denied", "SecurityError");
};

test("uses a working localStorage", () => {
  const ls = memoryStorage();
  install(ls);
  safeStorage.set("a", "1");
  assert.equal(ls.map.get("a"), "1");
  assert.equal(safeStorage.get("a"), "1");
  safeStorage.remove("a");
  assert.equal(ls.map.has("a"), false);
  assert.equal(safeStorage.get("a"), null);
});

test("falls back to memory when every localStorage call throws", () => {
  install({ getItem: boom, setItem: boom, removeItem: boom });
  assert.equal(safeStorage.get("t1"), null);
  safeStorage.set("t1", "x");
  assert.equal(safeStorage.get("t1"), "x");
  safeStorage.remove("t1");
  assert.equal(safeStorage.get("t1"), null);
});

test("falls back to memory when merely touching localStorage throws", () => {
  Object.defineProperty(globalThis, "localStorage", { get: boom, configurable: true });
  assert.doesNotThrow(() => safeStorage.set("t2", "y"));
  assert.equal(safeStorage.get("t2"), "y");
  safeStorage.remove("t2");
  assert.equal(safeStorage.get("t2"), null);
});

test("works with no localStorage at all", () => {
  delete g.localStorage;
  install(undefined);
  assert.equal(safeStorage.get("t3"), null);
  safeStorage.set("t3", "z");
  assert.equal(safeStorage.get("t3"), "z");
  safeStorage.remove("t3");
  assert.equal(safeStorage.get("t3"), null);
});

test("a failed write (quota) is not shadowed by the stale stored value", () => {
  const ls = memoryStorage();
  ls.map.set("t4", "old");
  install({ ...ls, setItem: boom });
  safeStorage.set("t4", "new");
  assert.equal(safeStorage.get("t4"), "new");
  safeStorage.remove("t4");
  assert.equal(safeStorage.get("t4"), null);
});
