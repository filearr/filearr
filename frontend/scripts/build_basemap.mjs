#!/usr/bin/env node
// Regenerate src/lib/map/basemapData.ts — the OFFLINE world basemap.
//
// WHY a generator instead of a runtime dependency: the console must render the
// photo map with ZERO external requests (no tile server, no CDN), so the outline
// has to be *in the bundle*. Shipping a mapping library that fetches tiles would
// break that; shipping the raw Natural Earth TopoJSON would need a TopoJSON
// decoder at runtime. So we decode, simplify and quantize ONCE, here, and commit
// a plain coordinate table the map component can draw with a `for` loop.
//
// Source: world-atlas@2.0.2 `countries-110m.json` (npm, ISC © Mike Bostock),
// a redistribution of Natural Earth 4.1.0 Admin-0 1:110m — Natural Earth data is
// PUBLIC DOMAIN. Provenance/licence is restated in the generated file's header
// so it travels with the data.
//
// Usage (network needed ONCE, at authoring time — never at build or run time):
//   npm pack world-atlas@2.0.2 && tar xzf world-atlas-2.0.2.tgz
//   node scripts/build_basemap.mjs package/countries-110m.json
//
// Output shape (see basemapData.ts):
//   LAND    — closed rings, filled, giving the land/sea distinction.
//   BORDERS — internal country boundaries only (arcs shared by two countries),
//             stroked. Splitting them this way means every coastline segment is
//             stored ONCE: the rings carry it, the border table does not repeat it.
// Both are ENCODED POLYLINES (see encode() below) rather than JSON number
// arrays: the same geometry costs ~3x less as zigzag-varint deltas, which is the
// difference between a ~40 KB asset and a ~130 KB one for identical detail.

import { readFileSync, writeFileSync } from "node:fs";

const src = process.argv[2] ?? "package/countries-110m.json";
const out = process.argv[3] ?? "src/lib/map/basemapData.ts";

// Douglas-Peucker tolerance in DEGREES and coordinate precision (2 dp ≈ 1.1 km at
// the equator). Both are chosen against what the map can actually show: the whole
// world across ~1000 css px is ~0.36°/px, so sub-0.05° detail is invisible, and
// the source is 1:110m anyway — it is already a country-scale outline, not a
// survey. Pushing either further starts eating small islands.
const EPSILON = 0.08;
const PRECISION = 2;
const SCALE = 10 ** PRECISION;

const topo = JSON.parse(readFileSync(src, "utf8"));
const [sx, sy] = topo.transform.scale;
const [tx, ty] = topo.transform.translate;

/** TopoJSON stores arcs delta-encoded on a quantized integer grid; undo both. */
function decodeArc(arc) {
  let x = 0;
  let y = 0;
  const pts = [];
  for (const [dx, dy] of arc) {
    x += dx;
    y += dy;
    pts.push([x * sx + tx, y * sy + ty]);
  }
  return pts;
}

/** Iterative Douglas-Peucker (recursion would be fine at this size, but the flat
 *  form keeps the worst case obvious). Endpoints are always kept, so arcs still
 *  meet exactly at the junctions they share with their neighbours. */
function simplify(pts, eps) {
  if (pts.length < 3) return pts;
  const keep = new Uint8Array(pts.length);
  keep[0] = keep[pts.length - 1] = 1;
  const stack = [[0, pts.length - 1]];
  while (stack.length) {
    const [lo, hi] = stack.pop();
    let far = -1;
    let best = eps;
    const [x1, y1] = pts[lo];
    const [x2, y2] = pts[hi];
    const dx = x2 - x1;
    const dy = y2 - y1;
    const len = Math.hypot(dx, dy);
    for (let i = lo + 1; i < hi; i++) {
      const [px, py] = pts[i];
      const d =
        len === 0
          ? Math.hypot(px - x1, py - y1)
          : Math.abs(dy * px - dx * py + x2 * y1 - y2 * x1) / len;
      if (d > best) {
        best = d;
        far = i;
      }
    }
    if (far !== -1) {
      keep[far] = 1;
      stack.push([lo, far], [far, hi]);
    }
  }
  return pts.filter((_, i) => keep[i]);
}

/** Google's encoded-polyline algorithm (the ubiquitous one): quantize to
 *  PRECISION decimals, delta against the previous point, zigzag the sign into
 *  bit 0, then emit 5-bit groups as printable ASCII (+63, continuation bit 0x20).
 *  Small deltas — which is nearly all of them once the geometry is simplified —
 *  cost one character. Mirrored by decodePolyline() in src/lib/map/geo.ts; the
 *  round-trip is unit-tested there. */
function encode(pts) {
  let out = "";
  let prevX = 0;
  let prevY = 0;
  let n = 0;
  const chunk = (delta) => {
    let v = delta < 0 ? ~(delta << 1) : delta << 1;
    while (v >= 0x20) {
      out += String.fromCharCode((0x20 | (v & 0x1f)) + 63);
      v >>= 5;
    }
    out += String.fromCharCode(v + 63);
  };
  for (const [lng, lat] of pts) {
    const x = Math.round(lng * SCALE);
    const y = Math.round(lat * SCALE);
    // Points the quantizer collapsed onto their predecessor cost bytes and draw
    // nothing — but never drop the FIRST point, which anchors the deltas.
    if (n > 0 && x === prevX && y === prevY) continue;
    chunk(x - prevX);
    chunk(y - prevY);
    prevX = x;
    prevY = y;
    n++;
  }
  return { s: out, n };
}

const arcs = topo.arcs.map((a) => simplify(decodeArc(a), EPSILON));

/** TopoJSON arc references: `~i` means "arc i, reversed". */
const arcIndex = (i) => (i < 0 ? ~i : i);
function ringPoints(indices) {
  const pts = [];
  for (const idx of indices) {
    const arc = arcs[arcIndex(idx)];
    const seq = idx < 0 ? [...arc].reverse() : arc;
    // Adjacent arcs in a ring share an endpoint — skip the duplicate join.
    for (let i = pts.length ? 1 : 0; i < seq.length; i++) pts.push(seq[i]);
  }
  return pts;
}

/** Natural Earth stores a few shapes (Russia, Fiji, Antarctica) with vertices on
 *  both sides of the 180th meridian. d3-geo draws those on a sphere and never
 *  notices; a FLAT projection joins the two sides with a horizontal smear right
 *  across the map — the exact artefact this undoes.
 *
 *  Fix in two steps, at generation time so the renderer stays a dumb loop:
 *  1. UNWRAP — walk the shape adding ±360 whenever a step jumps more than 180°,
 *     so the geometry becomes continuous (possibly running past ±180).
 *  2. REPEAT — emit the shape once per 360° offset whose span still overlaps
 *     [-180, 180], so Chukotka appears on the left edge and mainland Russia on
 *     the right. The viewport clips whatever falls outside.
 *  Shapes that never cross come back unchanged, as a single copy. */
function unwrapAndRepeat(pts) {
  if (pts.length < 2) return [pts];
  const unwrapped = [];
  let prev = null;
  for (const [lng, lat] of pts) {
    let x = lng;
    if (prev !== null) {
      while (x - prev > 180) x -= 360;
      while (prev - x > 180) x += 360;
    }
    unwrapped.push([x, lat]);
    prev = x;
  }
  let min = Infinity;
  let max = -Infinity;
  for (const [x] of unwrapped) {
    if (x < min) min = x;
    if (x > max) max = x;
  }
  const out = [];
  for (let k = Math.floor((-180 - max) / 360); k <= Math.ceil((180 - min) / 360); k++) {
    if (min + k * 360 > 180 || max + k * 360 < -180) continue;
    out.push(k === 0 ? unwrapped : unwrapped.map(([x, y]) => [x + k * 360, y]));
  }
  return out.length ? out : [unwrapped];
}

const geoms = topo.objects.countries.geometries;
const polygons = (g) => (g.type === "Polygon" ? [g.arcs] : g.arcs);

// An arc referenced by two different countries is an internal border; one
// referenced once is a coastline (already drawn as part of a filled ring).
const users = new Map();
for (const g of geoms) {
  for (const poly of polygons(g)) {
    for (const ring of poly) {
      for (const idx of ring) {
        const k = arcIndex(idx);
        if (!users.has(k)) users.set(k, new Set());
        users.get(k).add(g.id ?? g.properties?.name ?? Math.random());
      }
    }
  }
}

let points = 0;
const LAND = [];
for (const g of geoms) {
  for (const poly of polygons(g)) {
    // poly[0] is the exterior ring; holes (poly[1..]) at 1:110m are the Caspian
    // and a handful of lakes — keeping them would need even-odd fill bookkeeping
    // for a couple of pixels of blue, so they are dropped deliberately.
    for (const copy of unwrapAndRepeat(ringPoints(poly[0]))) {
      const { s, n } = encode(copy);
      if (n < 3) continue; // fewer than 3 points is not an area
      LAND.push(s);
      points += n;
    }
  }
}

const BORDERS = [];
for (const [idx, owners] of users) {
  if (owners.size < 2) continue;
  for (const copy of unwrapAndRepeat(arcs[idx])) {
    const { s, n } = encode(copy);
    if (n < 2) continue;
    BORDERS.push(s);
    points += n;
  }
}

const header = `// GENERATED by scripts/build_basemap.mjs — do not edit by hand.
//
// Offline world basemap for the photo GPS map (frontend/src/lib/map/). The
// console renders it with zero network requests: no tile server, no CDN, no
// font. See MapPanel.svelte for why that constraint exists.
//
// PROVENANCE / LICENCE
//   Geometry: Natural Earth 4.1.0, Admin 0 countries, 1:110m scale.
//     Natural Earth is in the PUBLIC DOMAIN (no permission needed, no credit
//     required — https://www.naturalearthdata.com/about/terms-of-use/).
//   Packaging: world-atlas@2.0.2 (npm), ISC © 2013-2019 Michael Bostock, a
//     TopoJSON redistribution of the above.
//   Transform: TopoJSON arcs decoded, Douglas-Peucker simplified at ${EPSILON}°
//     and quantized to ${PRECISION} decimal places (~1.1 km) by the generator.
//
// Each shape is one ENCODED POLYLINE string of (lng, lat) pairs in WGS-84
// degrees — lng first, matching the [x, y] order the projection wants. Decode
// with decodePolyline() from ./geo.
/** Closed land rings (filled). Coastlines are these rings' outlines. */
export const LAND: readonly string[] = `;

const body =
  header +
  JSON.stringify(LAND) +
  `;\n\n/** Internal country borders (stroked) — arcs shared by two countries, so no\n *  coastline segment is stored twice. */\nexport const BORDERS: readonly string[] = ` +
  JSON.stringify(BORDERS) +
  ";\n";

writeFileSync(out, body);
console.log(
  `${out}: ${LAND.length} land rings + ${BORDERS.length} border lines, ` +
    `${points} points, ${(body.length / 1024).toFixed(1)} KiB`,
);
