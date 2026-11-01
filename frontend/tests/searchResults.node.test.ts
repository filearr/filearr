// searchResults.ts: result paging integrity (A12) — de-dupe on append, the
// params snapshot loadMore pages with, and the bounds probe for a filtered
// deep link. configCache.ts: the page-load shared-promise helpers behind
// semanticProbe.ts and the custom-field / profile caches (A44).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BOUNDS_PROBE_LIMIT,
  boundsProbePairs,
  boundsProbeParams,
  createPageSession,
  mergeHits,
} from "../src/lib/searchResults.ts";
import { sharedByKey, sharedOnce, waitWithSignal } from "../src/lib/configCache.ts";

type H = { id: string; n?: number };
const idOf = (h: H) => h.id;

// ---- mergeHits --------------------------------------------------------------

test("mergeHits: disjoint pages append in order, nothing dropped", () => {
  const r = mergeHits<H>([{ id: "a" }, { id: "b" }], [{ id: "c" }, { id: "d" }], idOf);
  assert.deepEqual(r.hits.map(idOf), ["a", "b", "c", "d"]);
  assert.equal(r.dropped, 0);
});

test("mergeHits: an overlapping page (rows shifted between pages) drops the repeats", () => {
  const existing: H[] = [{ id: "a" }, { id: "b" }, { id: "c" }];
  const incoming: H[] = [{ id: "c", n: 2 }, { id: "d" }, { id: "b", n: 2 }, { id: "e" }];
  const r = mergeHits(existing, incoming, idOf);
  assert.deepEqual(r.hits.map(idOf), ["a", "b", "c", "d", "e"]);
  assert.equal(r.dropped, 2);
  // The row already on screen is kept (first wins), not replaced.
  assert.equal(r.hits[2].n, undefined);
});

test("mergeHits: duplicates inside the incoming page itself are dropped too", () => {
  const r = mergeHits<H>([], [{ id: "x" }, { id: "y" }, { id: "x" }], idOf);
  assert.deepEqual(r.hits.map(idOf), ["x", "y"]);
  assert.equal(r.dropped, 1);
});

test("mergeHits: empty incoming returns the existing rows unchanged", () => {
  const existing: H[] = [{ id: "a" }, { id: "b" }];
  const r = mergeHits(existing, [], idOf);
  assert.deepEqual(r.hits, existing);
  assert.equal(r.dropped, 0);
});

test("mergeHits: never mutates its inputs", () => {
  const existing: H[] = [{ id: "a" }];
  const incoming: H[] = [{ id: "a" }, { id: "b" }];
  mergeHits(existing, incoming, idOf);
  assert.deepEqual(existing, [{ id: "a" }]);
  assert.deepEqual(incoming, [{ id: "a" }, { id: "b" }]);
});

// ---- createPageSession ----------------------------------------------------

test("page session: pages with the params of the last start, not live state", () => {
  const s = createPageSession();
  const live: Record<string, string> = { q: "cat", file_category: "image" };
  s.start(live);
  // The user types / drags a slider after the first page landed.
  live.q = "dog";
  live.size_gte = "1024";
  assert.deepEqual(s.pageParams("c1"), { q: "cat", file_category: "image", cursor: "c1" });
});

test("page session: a new start replaces the snapshot", () => {
  const s = createPageSession();
  s.start({ q: "cat" });
  s.start({ q: "dog", tags: "x" });
  assert.deepEqual(s.pageParams("c2"), { q: "dog", tags: "x", cursor: "c2" });
});

test("page session: no next page without a start or a cursor", () => {
  const s = createPageSession();
  assert.equal(s.pageParams("c1"), null);
  s.start({ q: "cat" });
  assert.equal(s.pageParams(null), null);
  assert.equal(s.pageParams(""), null);
  s.clear();
  assert.equal(s.pageParams("c1"), null);
});

test("page session: the returned params are a fresh copy each time", () => {
  const s = createPageSession();
  s.start({ q: "cat" });
  const a = s.pageParams("c1");
  assert.ok(a);
  a.q = "mutated";
  assert.deepEqual(s.pageParams("c1"), { q: "cat", cursor: "c1" });
});

// ---- bounds probe -----------------------------------------------------------

const B = { min: 0, max: 10 };

test("boundsProbePairs: a pending pair with no bounds yet needs the probe", () => {
  assert.deepEqual(boundsProbePairs({ size_gte: "5" }, { size: null, mtime: null }), ["size"]);
  assert.deepEqual(boundsProbePairs({ mtime_lte: "9" }, { size: null, mtime: null }), ["date"]);
  assert.deepEqual(
    boundsProbePairs({ size_lte: "5", mtime_gte: "1" }, { size: null, mtime: null }),
    ["size", "date"],
  );
});

test("boundsProbePairs: nothing pending, or bounds already known → no probe", () => {
  assert.deepEqual(boundsProbePairs({}, { size: null, mtime: null }), []);
  assert.deepEqual(boundsProbePairs({ size_gte: "5" }, { size: B, mtime: null }), []);
  assert.deepEqual(boundsProbePairs({ size_gte: "5", mtime_gte: "1" }, { size: B, mtime: null }), ["date"]);
  assert.deepEqual(boundsProbePairs({ size_gte: "" }, { size: null, mtime: null }), []);
});

test("boundsProbeParams: same params minus the four range keys, smallest limit, no cursor", () => {
  const p = {
    q: "cat",
    file_category: "image,video",
    size_gte: "1",
    size_lte: "2",
    mtime_gte: "3",
    mtime_lte: "4",
    cursor: "abc",
    limit: "200",
  };
  assert.deepEqual(boundsProbeParams(p), {
    q: "cat",
    file_category: "image,video",
    limit: String(BOUNDS_PROBE_LIMIT),
  });
  // Input untouched.
  assert.equal(p.size_gte, "1");
  assert.equal(BOUNDS_PROBE_LIMIT, 0);
});

// ---- configCache helpers ------------------------------------------------------

function deferred<T>() {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

test("sharedOnce: one fetch for every caller of the page load", async () => {
  let calls = 0;
  const get = sharedOnce(async () => {
    calls++;
    return [1, 2];
  });
  const [a, b] = await Promise.all([get(), get()]);
  await get();
  assert.deepEqual(a, [1, 2]);
  assert.equal(a, b);
  assert.equal(calls, 1);
});

test("sharedOnce: a rejection is not cached — the next call retries", async () => {
  let calls = 0;
  const get = sharedOnce(async () => {
    calls++;
    if (calls === 1) throw new Error("boom");
    return "ok";
  });
  await assert.rejects(get(), /boom/);
  assert.equal(await get(), "ok");
  assert.equal(await get(), "ok");
  assert.equal(calls, 2);
});

test("sharedOnce: a caller's abort ends only its own wait, never the shared fetch", async () => {
  let calls = 0;
  const d = deferred<string>();
  const get = sharedOnce(() => {
    calls++;
    return d.promise;
  });
  const ac = new AbortController();
  const first = get(ac.signal);
  const second = get();
  ac.abort();
  await assert.rejects(first, (e: Error) => e.name === "AbortError");
  d.resolve("shared");
  assert.equal(await second, "shared");
  assert.equal(await get(), "shared");
  assert.equal(calls, 1);
});

test("waitWithSignal: an already-aborted signal rejects at once", async () => {
  const ac = new AbortController();
  ac.abort();
  await assert.rejects(waitWithSignal(new Promise(() => {}), ac.signal), (e: Error) => e.name === "AbortError");
});

test("sharedByKey: one fetch per key, rejections retried per key", async () => {
  const calls: string[] = [];
  const get = sharedByKey(async (k: string) => {
    calls.push(k);
    if (k === "bad" && calls.filter((c) => c === "bad").length === 1) throw new Error("nope");
    return k.toUpperCase();
  });
  assert.equal(await get("image"), "IMAGE");
  assert.equal(await get("image"), "IMAGE");
  assert.equal(await get("video"), "VIDEO");
  await assert.rejects(get("bad"), /nope/);
  assert.equal(await get("bad"), "BAD");
  assert.deepEqual(calls, ["image", "video", "bad", "bad"]);
});
