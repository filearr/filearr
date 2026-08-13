// IN-T3 — layout invariants for the hand-rolled squarified treemap.
//
// These are the properties a treemap has to hold for the picture to MEAN
// anything: area proportional to bytes, nothing overlapping (or a cell would be
// double-counted by the eye), and the box completely filled (or the remaining
// white space would read as "unaccounted-for bytes"). They are cheap to state
// and easy to break during a refactor, which is exactly what makes them worth
// pinning. Runs on Node's built-in runner over the DOM-free module.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  fmtBytes,
  hashHue,
  labelColor,
  labelFits,
  rampColor,
  rootColor,
  squarify,
  truncateLabel,
  type TreemapItem,
  type TreemapRect,
} from "../src/lib/treemap.ts";

const EPS = 1e-6;

function area(r: TreemapRect): number {
  return r.w * r.h;
}

/** True when two rects overlap on a positive-area region (edge contact is fine
 *  — adjacent cells necessarily share edges). */
function overlaps(a: TreemapRect, b: TreemapRect): boolean {
  const xo = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
  const yo = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
  return xo > EPS && yo > EPS;
}

function assertNoOverlap(rects: TreemapRect[]) {
  for (let i = 0; i < rects.length; i++) {
    for (let j = i + 1; j < rects.length; j++) {
      assert.ok(
        !overlaps(rects[i], rects[j]),
        `rects ${rects[i].key} and ${rects[j].key} overlap`,
      );
    }
  }
}

function assertInsideBox(rects: TreemapRect[], w: number, h: number) {
  for (const r of rects) {
    assert.ok(r.x >= -EPS && r.y >= -EPS, `${r.key} starts outside the box`);
    assert.ok(r.x + r.w <= w + EPS, `${r.key} overflows the width`);
    assert.ok(r.y + r.h <= h + EPS, `${r.key} overflows the height`);
    assert.ok(r.w >= -EPS && r.h >= -EPS, `${r.key} has a negative dimension`);
  }
}

const sample: TreemapItem[] = [
  { key: "a", value: 600 },
  { key: "b", value: 300 },
  { key: "c", value: 60 },
  { key: "d", value: 30 },
  { key: "e", value: 10 },
];

test("squarify: every rect is inside the box, none overlap, they fill it", () => {
  const W = 1000;
  const H = 520;
  const rects = squarify(sample, W, H);
  assert.equal(rects.length, sample.length);
  assertInsideBox(rects, W, H);
  assertNoOverlap(rects);
  const total = rects.reduce((s, r) => s + area(r), 0);
  // No overlap + inside the box + total area == box area ⟹ exact tiling.
  assert.ok(Math.abs(total - W * H) < 1e-6 * W * H, `filled ${total} of ${W * H}`);
});

test("squarify: area is proportional to value", () => {
  const W = 400;
  const H = 300;
  const rects = squarify(sample, W, H);
  const byKey = new Map(rects.map((r) => [r.key, r]));
  const totalValue = sample.reduce((s, i) => s + i.value, 0);
  for (const it of sample) {
    const expected = (it.value / totalValue) * W * H;
    const got = area(byKey.get(it.key)!);
    assert.ok(
      Math.abs(got - expected) < 1e-6 * expected,
      `${it.key}: expected area ${expected}, got ${got}`,
    );
  }
});

test("squarify: output is ordered largest-first, stable within ties", () => {
  const rects = squarify(
    [
      { key: "small", value: 1 },
      { key: "tie1", value: 5 },
      { key: "tie2", value: 5 },
      { key: "big", value: 20 },
    ],
    100,
    100,
  );
  assert.deepEqual(
    rects.map((r) => r.key),
    ["big", "tie1", "tie2", "small"],
  );
});

test("squarify: aspect ratios stay reasonable (that is the point of squarifying)", () => {
  // A naive slice-and-dice layout produces slivers here; squarified should not.
  const items = Array.from({ length: 24 }, (_, i) => ({
    key: `k${i}`,
    value: 100 - i * 3,
  }));
  const rects = squarify(items, 1000, 520);
  for (const r of rects) {
    const ratio = Math.max(r.w / r.h, r.h / r.w);
    assert.ok(ratio < 12, `${r.key} is a sliver (aspect ${ratio.toFixed(1)})`);
  }
});

test("squarify: a single child fills the whole box", () => {
  const rects = squarify([{ key: "only", value: 42 }], 200, 80);
  assert.equal(rects.length, 1);
  assert.equal(rects[0].x, 0);
  assert.equal(rects[0].y, 0);
  assert.ok(Math.abs(rects[0].w - 200) < EPS);
  assert.ok(Math.abs(rects[0].h - 80) < EPS);
});

test("squarify: empty input and a degenerate box produce no rects", () => {
  assert.deepEqual(squarify([], 100, 100), []);
  assert.deepEqual(squarify(sample, 0, 100), []);
  assert.deepEqual(squarify(sample, 100, 0), []);
  assert.deepEqual(squarify(sample, -5, 100), []);
});

test("squarify: zero-byte children keep a row but take no area", () => {
  const rects = squarify(
    [
      { key: "real", value: 100 },
      { key: "emptyA", value: 0 },
      { key: "emptyB", value: 0 },
    ],
    300,
    200,
  );
  assert.equal(rects.length, 3);
  const byKey = new Map(rects.map((r) => [r.key, r]));
  assert.ok(Math.abs(area(byKey.get("real")!) - 300 * 200) < EPS);
  assert.equal(area(byKey.get("emptyA")!), 0);
  assert.equal(area(byKey.get("emptyB")!), 0);
  assertNoOverlap(rects);
  assertInsideBox(rects, 300, 200);
});

test("squarify: an ALL-zero level falls back to equal shares rather than nothing", () => {
  const rects = squarify(
    [
      { key: "a", value: 0 },
      { key: "b", value: 0 },
      { key: "c", value: 0 },
    ],
    300,
    300,
  );
  assert.equal(rects.length, 3);
  const areas = rects.map(area);
  for (const a of areas) assert.ok(Math.abs(a - 30000) < 1e-6 * 30000, `equal share, got ${a}`);
  assertNoOverlap(rects);
});

test("squarify: negative / non-finite values are clamped, never propagated", () => {
  const rects = squarify(
    [
      { key: "ok", value: 50 },
      { key: "neg", value: -20 },
      { key: "nan", value: Number.NaN },
    ],
    100,
    100,
  );
  assertInsideBox(rects, 100, 100);
  assertNoOverlap(rects);
  const byKey = new Map(rects.map((r) => [r.key, r]));
  assert.ok(Math.abs(area(byKey.get("ok")!) - 10000) < EPS);
  assert.equal(area(byKey.get("neg")!), 0);
  assert.equal(area(byKey.get("nan")!), 0);
});

test("squarify: a full page of children (the endpoint's 500 cap) still tiles exactly", () => {
  const items = Array.from({ length: 500 }, (_, i) => ({
    key: `k${i}`,
    // A realistic long tail: a few huge folders, many tiny ones.
    value: Math.round(1e9 / (i + 1)),
  }));
  const W = 1000;
  const H = 520;
  const rects = squarify(items, W, H);
  assert.equal(rects.length, 500);
  assertInsideBox(rects, W, H);
  assertNoOverlap(rects);
  const total = rects.reduce((s, r) => s + area(r), 0);
  assert.ok(Math.abs(total - W * H) < 1e-5 * W * H);
});

// ---- labels ---------------------------------------------------------------

test("labelFits: needs both the width for the glyphs and the height for the line", () => {
  assert.ok(labelFits(200, 40, "movies", 13));
  // Too short for the line box even though the width is ample.
  assert.ok(!labelFits(200, 8, "movies", 13));
  // Too narrow for the glyphs.
  assert.ok(!labelFits(20, 40, "a-very-long-folder-name", 13));
  assert.ok(!labelFits(200, 40, "", 13));
});

test("truncateLabel: ellipsises to fit, or gives up rather than render a stub", () => {
  assert.equal(truncateLabel("movies", 400, 13), "movies");
  const t = truncateLabel("a-very-long-folder-name", 80, 13);
  assert.ok(t.endsWith("…"), `expected an ellipsis, got ${t}`);
  assert.ok(t.length < "a-very-long-folder-name".length);
  // Nothing meaningful fits -> render no text at all.
  assert.equal(truncateLabel("movies", 10, 13), "");
  assert.equal(truncateLabel("", 400, 13), "");
});

// ---- color ----------------------------------------------------------------

test("hashHue: stable, in range, and not constant across keys", () => {
  const a = hashHue("11111111-2222-3333-4444-555555555555");
  assert.equal(a, hashHue("11111111-2222-3333-4444-555555555555"));
  assert.ok(a >= 0 && a < 360 && Number.isInteger(a));
  const hues = new Set(
    ["lib-a", "lib-b", "lib-c", "lib-d", "lib-e", "lib-f"].map(hashHue),
  );
  assert.ok(hues.size >= 5, `expected distinct hues, got ${[...hues].join(",")}`);
});

test("rootColor: one stable hsl per library", () => {
  assert.equal(rootColor("lib-a"), rootColor("lib-a"));
  assert.match(rootColor("lib-a"), /^hsl\(\d+ \d+% \d+%\)$/);
  assert.notEqual(rootColor("lib-a"), rootColor("lib-b"));
});

test("rampColor: same hue, lightness rising monotonically with size rank", () => {
  const n = 6;
  const colors = Array.from({ length: n }, (_, i) => rampColor("lib-a", i, n));
  const hue = hashHue("lib-a");
  const lights = colors.map((c) => {
    const m = /^hsl\((\d+) \d+% ([\d.]+)%\)$/.exec(c);
    assert.ok(m, `unparseable color ${c}`);
    assert.equal(Number(m![1]), hue); // single-hue ramp, by design
    return Number(m![2]);
  });
  for (let i = 1; i < lights.length; i++) {
    assert.ok(lights[i] > lights[i - 1], "smaller cells must be lighter");
  }
  // A single-child level must not divide by zero.
  assert.match(rampColor("lib-a", 0, 1), /^hsl\(\d+ \d+% [\d.]+%\)$/);
});

test("labelColor: flips with the fill's lightness", () => {
  assert.equal(labelColor("hsl(200 55% 36.0%)"), "#ffffff");
  assert.equal(labelColor("hsl(200 55% 66.0%)"), "#0f172a");
});

test("fmtBytes: human sizes, and a hard zero for nothing", () => {
  assert.equal(fmtBytes(0), "0 B");
  assert.equal(fmtBytes(-1), "0 B");
  assert.equal(fmtBytes(512), "512 B");
  assert.equal(fmtBytes(1024), "1.0 KB");
  assert.equal(fmtBytes(1024 ** 3 * 2), "2.0 GB");
});
