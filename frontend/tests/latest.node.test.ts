// latest(): only the newest run's result lands; older runs are aborted and
// resolve undefined. Promises are resolved by hand so ordering is explicit.

import assert from "node:assert/strict";
import { test } from "node:test";

import { latest } from "../src/lib/latest.ts";

type Pending<T> = {
  signal: AbortSignal;
  arg: string;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
};

function harness<T>() {
  const calls: Pending<T>[] = [];
  const l = latest((signal: AbortSignal, arg: string) =>
    new Promise<T>((resolve, reject) => {
      calls.push({ signal, arg, resolve, reject });
    }),
  );
  return { l, calls };
}

test("out-of-order resolution: the older run resolves undefined", async () => {
  const { l, calls } = harness<string>();
  const first = l.run("a");
  const second = l.run("b");
  assert.deepEqual(
    calls.map((c) => c.arg),
    ["a", "b"],
  );
  calls[1].resolve("B");
  calls[0].resolve("A"); // lands last, must be ignored
  assert.equal(await second, "B");
  assert.equal(await first, undefined);
});

test("each run aborts the previous run's signal", () => {
  const { l, calls } = harness<string>();
  void l.run("a");
  assert.equal(calls[0].signal.aborted, false);
  void l.run("b");
  assert.equal(calls[0].signal.aborted, true);
  assert.equal(calls[1].signal.aborted, false);
});

test("an error from the newest run propagates", async () => {
  const { l, calls } = harness<string>();
  const p = l.run("a");
  calls[0].reject(new Error("boom"));
  await assert.rejects(p, /boom/);
});

test("an error from a superseded run is swallowed", async () => {
  const { l, calls } = harness<string>();
  const first = l.run("a");
  const second = l.run("b");
  calls[0].reject(new Error("stale failure"));
  calls[1].resolve("B");
  assert.equal(await first, undefined);
  assert.equal(await second, "B");
});

test("an AbortError from a superseded run resolves undefined", async () => {
  const { l, calls } = harness<string>();
  const first = l.run("a");
  void l.run("b");
  calls[0].reject(new DOMException("aborted", "AbortError"));
  assert.equal(await first, undefined);
});

test("cancel() aborts the current run and it resolves undefined", async () => {
  const { l, calls } = harness<string>();
  const p = l.run("a");
  l.cancel();
  assert.equal(calls[0].signal.aborted, true);
  calls[0].resolve("A");
  assert.equal(await p, undefined);
});

test("cancel() with an abort-rejecting fn still resolves undefined", async () => {
  const { l, calls } = harness<string>();
  const p = l.run("a");
  l.cancel();
  calls[0].reject(new DOMException("aborted", "AbortError"));
  assert.equal(await p, undefined);
});

test("a run after cancel() works normally", async () => {
  const { l, calls } = harness<string>();
  void l.run("a");
  l.cancel();
  const p = l.run("b");
  calls[1].resolve("B");
  assert.equal(await p, "B");
});
