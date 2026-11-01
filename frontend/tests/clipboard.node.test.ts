// copyText(): the textarea fallback reports failure honestly, cleans up its
// textarea, and hands focus back to whatever had it. DOM is stubbed by hand.

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import { copyText } from "../src/lib/clipboard.ts";

type Fake = { focused: number; removed: number };

function stubDom(execResult: boolean | "throw") {
  const log: Fake = { focused: 0, removed: 0 };
  const body = { appendChild: () => {} };
  const prev = { isConnected: true, focus: () => log.focused++ };
  const g = globalThis as Record<string, unknown>;
  g.document = {
    body,
    activeElement: prev,
    createElement: () => ({
      value: "",
      style: {},
      setAttribute: () => {},
      select: () => {},
      setSelectionRange: () => {},
      remove: () => log.removed++,
    }),
    execCommand: () => {
      if (execResult === "throw") throw new Error("nope");
      return execResult;
    },
  };
  return log;
}

afterEach(() => {
  delete (globalThis as Record<string, unknown>).document;
});

test("copyText fallback: success returns true, removes textarea, restores focus", async () => {
  const log = stubDom(true);
  assert.equal(await copyText("x"), true);
  assert.deepEqual(log, { focused: 1, removed: 1 });
});

test("copyText fallback: execCommand false → false, still restores focus", async () => {
  const log = stubDom(false);
  assert.equal(await copyText("x"), false);
  assert.deepEqual(log, { focused: 1, removed: 1 });
});

test("copyText fallback: execCommand throws → false, no textarea left behind", async () => {
  const log = stubDom("throw");
  assert.equal(await copyText("x"), false);
  assert.deepEqual(log, { focused: 1, removed: 1 });
});

test("copyText without a DOM → false (never throws)", async () => {
  assert.equal(await copyText("x"), false);
});
