// P6 Task 5 (A3, A26) — the bulk-edit run, with the network injected.
//
// A3: tag arithmetic must start from the item's LIVE tags (GET /items/{id}),
// never the search-index snapshot on the hit — an item edited in the detail
// pane moments ago would otherwise lose its new tags under a success toast.
// A26: a request-level failure on chunk k must still report (and let the page
// apply locally) what chunks 1..k−1 committed.

import assert from "node:assert/strict";
import { test } from "node:test";

import { createSettleGate, runBulk, type BulkApi } from "../src/lib/bulkOrchestrate.ts";
import { EMPTY_OPS, type BulkOps, type BulkTarget, type ItemPatchBody } from "../src/lib/bulkEdit.ts";

const target = (id: string, tags: string[] = []): BulkTarget => ({
  id,
  tags,
  file_category: "video",
  library_id: "lib1",
});

const ops = (over: Partial<BulkOps>): BulkOps => ({ ...EMPTY_OPS, ...over });

/** A fake api that records every call. `getItem` answers from `live`; a
 *  missing id rejects. `batchPatch` answers "ok" for every key unless
 *  `failChunk` names the (0-based) request that should throw. */
function fakeApi(opts: {
  live?: Record<string, string[]>;
  failChunk?: number;
  onBatch?: (n: number) => void;
} = {}) {
  const gets: string[] = [];
  const batches: Record<string, ItemPatchBody>[] = [];
  const api: BulkApi = {
    async getItem(id) {
      gets.push(id);
      const tags = opts.live?.[id];
      if (!tags) throw new Error(`404 ${id}`);
      return { id, tags };
    },
    async batchPatch(body) {
      const n = batches.length;
      batches.push(body);
      opts.onBatch?.(n);
      if (n === opts.failChunk) throw new Error("boom");
      return { results: Object.fromEntries(Object.keys(body).map((k) => [k, "ok"])) };
    },
  };
  return { api, gets, batches };
}

test("tag ops start from the live tags, not the hit's snapshot", async () => {
  const { api, gets, batches } = fakeApi({ live: { x: ["a", "b"] } });
  const out = await runBulk([target("x", ["a"])], ops({ tagsAdd: ["c"] }), api, {});
  assert.deepEqual(gets, ["x"]);
  assert.equal(batches.length, 1);
  assert.deepEqual(batches[0], { x: { tags: ["a", "b", "c"] } });
  assert.deepEqual(out.summary.ok, ["x"]);
  assert.deepEqual(out.patchesApplied, { x: { tags: ["a", "b", "c"] } });
  assert.equal(out.failed, null);
  assert.equal(out.aborted, false);
});

test("every target is re-read when tags are touched, 20 at a time", async () => {
  const live: Record<string, string[]> = {};
  const targets: BulkTarget[] = [];
  for (let i = 0; i < 45; i++) {
    live[`i${i}`] = ["keep"];
    targets.push(target(`i${i}`, ["keep"]));
  }
  let inFlight = 0;
  let peak = 0;
  const api: BulkApi = {
    async getItem(id) {
      inFlight++;
      peak = Math.max(peak, inFlight);
      await new Promise((r) => setTimeout(r, 1));
      inFlight--;
      return { tags: live[id] };
    },
    async batchPatch(body) {
      return { results: Object.fromEntries(Object.keys(body).map((k) => [k, "ok"])) };
    },
  };
  const out = await runBulk(targets, ops({ tagsRemove: ["keep"] }), api, {});
  assert.equal(peak, 20);
  assert.equal(out.summary.ok.length, 45);
});

test("a getItem failure is listed under unresolved and not patched", async () => {
  const { api, batches } = fakeApi({ live: { x: ["a"] } });
  const out = await runBulk(
    [target("x", ["a"]), target("gone", ["a"])],
    ops({ tagsAdd: ["c"] }),
    api,
    {},
  );
  assert.deepEqual(Object.keys(batches[0]), ["x"]);
  assert.deepEqual(out.unresolved.map((u) => u.id), ["gone"]);
  assert.ok(out.unresolved[0].reason.length > 0);
  assert.ok(!("gone" in out.patchesApplied));
});

test("non-tag ops make no getItem calls", async () => {
  const { api, gets, batches } = fakeApi();
  const out = await runBulk(
    [target("x", ["a"]), target("y")],
    ops({ yearMode: "set", year: 1999 }),
    api,
    {},
  );
  assert.deepEqual(gets, []);
  assert.deepEqual(batches[0], { x: { year: 1999 }, y: { year: 1999 } });
  assert.deepEqual(out.summary.ok, ["x", "y"]);
});

test("chunk 2 of 3 throws: chunk 1 is reported, chunk 3 is not attempted", async () => {
  const targets = Array.from({ length: 1200 }, (_, i) => target(`i${i}`));
  const { api, batches } = fakeApi({ failChunk: 1 });
  const progress: [number, number][] = [];
  const out = await runBulk(targets, ops({ yearMode: "clear" }), api, {
    onChunk: (done, total) => progress.push([done, total]),
  });
  assert.equal(batches.length, 2, "chunk 3 is never sent");
  assert.equal(out.summary.ok.length, 500);
  assert.deepEqual(out.summary.ok, targets.slice(0, 500).map((t) => t.id));
  assert.ok(out.failed);
  assert.equal(out.failed.chunkIndex, 1);
  assert.equal((out.failed.error as Error).message, "boom");
  assert.equal(out.notAttempted.length, 700);
  assert.equal(Object.keys(out.patchesApplied).length, 500);
  assert.ok(!("i500" in out.patchesApplied));
  assert.deepEqual(progress, [[1, 3]]);
});

test("an aborted run token stops between chunks without applying more", async () => {
  const targets = Array.from({ length: 1200 }, (_, i) => target(`i${i}`));
  const ac = new AbortController();
  const { api, batches } = fakeApi({ onBatch: (n) => n === 0 && ac.abort() });
  const out = await runBulk(targets, ops({ yearMode: "set", year: 2001 }), api, {
    signal: ac.signal,
  });
  assert.equal(out.aborted, true);
  assert.equal(batches.length, 1, "no chunk is sent after the abort");
  // The chunk already in flight committed server-side, so it is reported.
  assert.equal(Object.keys(out.patchesApplied).length, 500);
  assert.equal(out.notAttempted.length, 700);
  assert.equal(out.failed, null);
});

test("an abort during tag resolution sends nothing", async () => {
  const ac = new AbortController();
  const api: BulkApi = {
    async getItem(_id, signal) {
      ac.abort();
      if (signal?.aborted) throw new DOMException("aborted", "AbortError");
      return { tags: [] };
    },
    async batchPatch() {
      throw new Error("must not be called");
    },
  };
  const out = await runBulk([target("x")], ops({ tagsAdd: ["c"] }), api, { signal: ac.signal });
  assert.equal(out.aborted, true);
  assert.deepEqual(out.unresolved, [], "an abort is not an unresolved item");
  assert.deepEqual(out.patchesApplied, {});
});

test("nothing to change: no request is made", async () => {
  const { api, batches } = fakeApi({ live: { x: ["a"] } });
  const out = await runBulk([target("x", [])], ops({ tagsAdd: ["A"] }), api, {});
  assert.equal(batches.length, 0);
  assert.deepEqual(out.patches, {});
  assert.deepEqual(out.summary, { ok: [], failures: [] });
});

test("per-item failures inside a 200 are kept in the summary", async () => {
  const api: BulkApi = {
    async getItem() {
      return { tags: [] };
    },
    async batchPatch(body) {
      const [first, ...rest] = Object.keys(body);
      return {
        results: {
          [first]: "error: 403 path not granted",
          ...Object.fromEntries(rest.map((k) => [k, "ok"])),
        },
      };
    },
  };
  const out = await runBulk([target("x"), target("y")], ops({ yearMode: "clear" }), api, {});
  assert.deepEqual(out.summary.ok, ["y"]);
  assert.deepEqual(out.summary.failures, [{ id: "x", reason: "403 path not granted" }]);
  assert.deepEqual(Object.keys(out.patchesApplied), ["x", "y"]);
});

test("an abort during an in-flight chunk that then rejects reports aborted, not failed", async () => {
  const targets = Array.from({ length: 700 }, (_, i) => target(`i${i}`));
  const ac = new AbortController();
  let n = 0;
  const api: BulkApi = {
    async getItem() {
      return { tags: [] };
    },
    async batchPatch(body) {
      if (n++ === 1) {
        ac.abort();
        throw new Error("network dropped");
      }
      return { results: Object.fromEntries(Object.keys(body).map((k) => [k, "ok"])) };
    },
  };
  const out = await runBulk(targets, ops({ yearMode: "clear" }), api, { signal: ac.signal });
  assert.equal(out.aborted, true);
  assert.equal(out.failed, null);
  assert.equal(out.summary.ok.length, 500);
  assert.equal(out.notAttempted.length, 200);
});

// ---- final-review item 2: cancel then re-apply ------------------------------
// The cancelled run's in-flight chunk still commits server-side. A re-apply
// that read tags before that commit would overwrite the tag the cancelled run
// added, so the page holds the cancelled run in a settle gate and the next run
// waits on it before resolving live tags.

function deferred<T>() {
  let resolve!: (v: T) => void;
  const promise = new Promise<T>((r) => { resolve = r; });
  return { promise, resolve };
}

test("cancel then re-apply: the second run waits for the first to settle and reads its commit", async () => {
  const live: Record<string, string[]> = { x: ["a"] };
  const release = deferred<void>();
  let firstBatch = true;
  const gets: string[][] = [];
  const api: BulkApi = {
    async getItem(id) {
      gets.push([...live[id]]);
      return { id, tags: [...live[id]] };
    },
    async batchPatch(body) {
      if (firstBatch) { firstBatch = false; await release.promise; }
      for (const [id, p] of Object.entries(body)) if (p.tags) live[id] = p.tags;
      return { results: Object.fromEntries(Object.keys(body).map((k) => [k, "ok"])) };
    },
  };
  const gate = createSettleGate();
  assert.equal(gate.busy, false);

  // Run 1 adds "b"; its chunk is in flight when the user cancels.
  const c1 = new AbortController();
  const run1 = runBulk([target("x")], ops({ tagsAdd: ["b"] }), api, { signal: c1.signal });
  await new Promise((r) => setImmediate(r)); // run 1 is now inside batchPatch
  c1.abort();
  const held = gate.hold(run1);
  assert.equal(gate.busy, true, "Apply stays disabled while the cancelled run settles");

  // Run 2 adds "c": it must not resolve tags until run 1's commit landed.
  let run2Started = false;
  const run2 = (async () => {
    await gate.wait();
    run2Started = true;
    return runBulk([target("x")], ops({ tagsAdd: ["c"] }), api, {});
  })();
  await new Promise((r) => setImmediate(r));
  assert.equal(run2Started, false, "re-apply waits on the cancelled run");
  assert.equal(gets.length, 1, "only run 1 has read tags so far");

  release.resolve();
  const out1 = await run1;
  await held;
  assert.equal(out1.aborted, true);
  assert.deepEqual(out1.patchesApplied, { x: { tags: ["a", "b"] } }, "the in-flight chunk committed");
  assert.equal(gate.busy, false);

  const out2 = await run2;
  assert.deepEqual(gets[1], ["a", "b"], "run 2 read the post-commit tags");
  assert.deepEqual(out2.patchesApplied, { x: { tags: ["a", "b", "c"] } }, "run 1's tag survives");
  assert.deepEqual(live.x, ["a", "b", "c"]);
});

test("settle gate: wait() resolves at once when idle; two holds settle together; rejections count as settled", async () => {
  const gate = createSettleGate();
  await gate.wait();
  const a = deferred<void>();
  const b = deferred<void>();
  gate.hold(a.promise);
  gate.hold(b.promise.then(() => { throw new Error("late failure"); }));
  let waited = false;
  const w = gate.wait().then(() => { waited = true; });
  a.resolve();
  await new Promise((r) => setImmediate(r));
  assert.equal(waited, false, "still waiting on the second hold");
  assert.equal(gate.busy, true);
  b.resolve();
  await w;
  assert.equal(gate.busy, false);
});
