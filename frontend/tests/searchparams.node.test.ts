// searchparams.ts: the #/search?… hash codec.

import assert from "node:assert/strict";
import { test } from "node:test";

import { encodeSearchHash, isSearchHash, parseSearchHash } from "../src/lib/searchparams.ts";

test("parseSearchHash: repeated keys join with commas (API-style links, A31)", () => {
  assert.deepEqual(parseSearchHash("#/search?file_category=a&file_category=b"), { file_category: "a,b" });
  assert.deepEqual(parseSearchHash("#/search?q=x&tags=one&tags=two&tags=three"), { q: "x", tags: "one,two,three" });
});

test("parseSearchHash: single values, empty query and non-search hashes", () => {
  assert.deepEqual(parseSearchHash("#/search?q=hello+world&sort=newest"), { q: "hello world", sort: "newest" });
  assert.deepEqual(parseSearchHash("#/search"), {});
  assert.deepEqual(parseSearchHash("#/jobs?q=x"), {});
});

test("encodeSearchHash: sorted, empties dropped, round-trips through parse", () => {
  const p = { q: "a b", file_category: "video,audio", view: "", size_gte: "1024" };
  const h = encodeSearchHash(p);
  assert.equal(h, "#/search?file_category=video%2Caudio&q=a+b&size_gte=1024");
  assert.deepEqual(parseSearchHash(h), { file_category: "video,audio", q: "a b", size_gte: "1024" });
  assert.equal(encodeSearchHash({}), "#/search");
  assert.ok(isSearchHash(h));
});

test("parseSearchHash: Object.prototype names are ordinary keys", () => {
  assert.deepEqual(parseSearchHash("#/search?toString=x&toString=y"), { toString: "x,y" });
  assert.deepEqual(parseSearchHash("#/search?constructor=1"), { constructor: "1" });
  const r = parseSearchHash("#/search?__proto__=a&__proto__=b&q=z");
  assert.equal(Object.getPrototypeOf(r), Object.prototype, "prototype untouched");
  assert.ok(Object.hasOwn(r, "__proto__"));
  assert.deepEqual(Object.entries(r), [["__proto__", "a,b"], ["q", "z"]]);
});
