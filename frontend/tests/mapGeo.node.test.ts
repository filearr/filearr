// Pure-geometry tests for the photo GPS map (R8-UI).
//
// Same setup as the other modules here: Node's built-in runner with native
// TypeScript type-stripping (`npm test` from frontend/), no bundler and no DOM.
// Everything under test lives in ../src/lib/map/geo.ts precisely so it can be
// exercised this way — the SVG component only consumes these functions.
//
// Focus, in the order the bugs would hurt:
//   * projection round-trip — a point must land back on itself, or a drawn box
//     would not be the box the user saw;
//   * bounding-box normalisation — the UI must never SEND the shapes the API
//     answers 422 to (inverted edges, antimeridian crossing, out of range);
//   * clustering — the render bound has to hold, and points must separate as you
//     zoom in (that is the whole drill-down affordance).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_LAT,
  boxError,
  boxParams,
  boundsOf,
  clusterPoints,
  decodePolyline,
  formatBox,
  geoOf,
  makeViewport,
  normalizeBox,
  parseBox,
  project,
  unproject,
  worldSize,
  wrapLng,
  zoomForBox,
  type ScreenPoint,
} from "../src/lib/map/geo.ts";

// --------------------------------------------------------------------------
// projection
// --------------------------------------------------------------------------

const SAMPLES: [number, number][] = [
  [0, 0],
  [37.7749, -122.4194], // San Francisco
  [51.5074, -0.1278], // London
  [-33.8688, 151.2093], // Sydney
  [64.1466, -21.9426], // Reykjavík
  [-1.2921, 36.8219], // Nairobi
  [MAX_LAT, 180],
  [-MAX_LAT, -180],
];

test("project/unproject round-trips at every zoom", () => {
  for (const zoom of [0, 3, 8, 14, 18]) {
    for (const [lat, lng] of SAMPLES) {
      const p = project(lat, lng, zoom);
      const back = unproject(p.x, p.y, zoom);
      assert.ok(
        Math.abs(back.lat - lat) < 1e-9,
        `lat ${lat} at z${zoom} -> ${back.lat}`,
      );
      assert.ok(
        Math.abs(back.lng - lng) < 1e-9,
        `lng ${lng} at z${zoom} -> ${back.lng}`,
      );
    }
  }
});

test("the projected world is square and axis-separable", () => {
  // Mercator's separability is WHY a screen rectangle can become an exact
  // lat/lng box: x must depend only on longitude, y only on latitude.
  const a = project(10, 42, 5);
  const b = project(-60, 42, 5);
  assert.equal(a.x, b.x);
  const c = project(10, -170, 5);
  assert.equal(a.y, c.y);
  // Corners of the clipped world map to the corners of the pixel square.
  const size = worldSize(5);
  const nw = project(MAX_LAT, -180, 5);
  const se = project(-MAX_LAT, 180, 5);
  assert.ok(Math.abs(nw.x) < 1e-6 && Math.abs(nw.y) < 1e-6);
  assert.ok(Math.abs(se.x - size) < 1e-6 && Math.abs(se.y - size) < 1e-6);
});

test("viewport screen round-trip puts the centre in the middle", () => {
  const vp = makeViewport({
    center: { lat: 48.8566, lng: 2.3522 },
    zoom: 11,
    width: 800,
    height: 600,
  });
  const mid = vp.toScreen(48.8566, 2.3522);
  assert.ok(Math.abs(mid.x - 400) < 1e-6);
  assert.ok(Math.abs(mid.y - 300) < 1e-6);
  const back = vp.fromScreen(123.5, 456.25);
  const fwd = vp.toScreen(back.lat, back.lng);
  assert.ok(Math.abs(fwd.x - 123.5) < 1e-6);
  assert.ok(Math.abs(fwd.y - 456.25) < 1e-6);
});

test("wrapLng folds any longitude back into [-180, 180]", () => {
  assert.equal(wrapLng(0), 0);
  assert.equal(wrapLng(190), -170);
  assert.equal(wrapLng(-190), 170);
  assert.equal(wrapLng(540), -180);
});

test("zoomForBox frames the box inside the viewport", () => {
  const box = { top: 38, right: -122, bottom: 37, left: -123 };
  const z = zoomForBox(box, 800, 600);
  const vp = makeViewport({
    center: { lat: 37.5, lng: -122.5 },
    zoom: z,
    width: 800,
    height: 600,
  });
  const nw = vp.toScreen(box.top, box.left);
  const se = vp.toScreen(box.bottom, box.right);
  assert.ok(nw.x >= 0 && nw.y >= 0, "north-west corner is on screen");
  assert.ok(se.x <= 800 && se.y <= 600, "south-east corner is on screen");
});

// --------------------------------------------------------------------------
// bounding boxes — the 422 cases the UI must prevent
// --------------------------------------------------------------------------

test("normalizeBox sorts the corners, so a drag can never invert the box", () => {
  const downRight = normalizeBox({ lat: 38, lng: -123 }, { lat: 37, lng: -122 });
  const upLeft = normalizeBox({ lat: 37, lng: -122 }, { lat: 38, lng: -123 });
  assert.deepEqual(downRight, upLeft);
  assert.deepEqual(downRight, { top: 38, bottom: 37, left: -123, right: -122 });
  assert.equal(boxError(downRight), null);
});

test("normalizeBox clamps latitude and folds longitude into range", () => {
  // A drag near the Mercator clip, plus a pan that ran past the antimeridian.
  const b = normalizeBox({ lat: 95, lng: 200 }, { lat: -120, lng: -30 });
  assert.equal(b.top, 90);
  assert.equal(b.bottom, -90);
  assert.ok(b.left >= -180 && b.right <= 180);
  assert.equal(boxError(b), null);
});

test("boxError rejects an inverted box (the API's 422)", () => {
  const err = boxError({ top: 37, right: -122, bottom: 38, left: -123 });
  assert.match(String(err), /north edge is below the south edge/i);
});

test("boxError rejects an antimeridian-crossing box", () => {
  // west = 170E, east = 170W: the box the user means wraps past 180, which
  // Meilisearch's _geoBoundingBox cannot express — two searches, not one.
  const err = boxError({ top: 10, right: -170, bottom: -10, left: 170 });
  assert.match(String(err), /180th meridian/i);
});

test("boxError rejects out-of-range coordinates and degenerate areas", () => {
  assert.match(String(boxError({ top: 91, right: 1, bottom: 0, left: 0 })), /-90 and 90/);
  assert.match(String(boxError({ top: 1, right: 181, bottom: 0, left: 0 })), /-180 and 180/);
  assert.match(String(boxError({ top: 1, right: 1, bottom: 1, left: 0 })), /no width or height/);
  assert.match(
    String(boxError({ top: Number.NaN, right: 1, bottom: 0, left: 0 })),
    /all four or none/,
  );
});

test("boxParams emits all four edges, or nothing at all", () => {
  const good = boxParams({ top: 38.5, right: -122.25, bottom: 37.5, left: -123.25 });
  assert.deepEqual(good, {
    geo_top_lat: "38.5",
    geo_right_lng: "-122.25",
    geo_bottom_lat: "37.5",
    geo_left_lng: "-123.25",
  });
  // A box the API would refuse degrades to "no geo filter", never half a box.
  assert.deepEqual(boxParams({ top: 0, right: -170, bottom: 10, left: 170 }), {});
  assert.deepEqual(boxParams(null), {});
});

test("boxParams/parseBox round-trip through the flat param record", () => {
  const box = { top: 38.123456789, right: -122.2, bottom: 37.9, left: -123.4 };
  const parsed = parseBox(boxParams(box));
  assert.ok(parsed);
  // Values are fixed to 6 dp (~11 cm) so the deep-link hash stays readable.
  assert.equal(parsed.top, 38.123457);
  assert.equal(parsed.right, -122.2);
  assert.equal(parsed.bottom, 37.9);
  assert.equal(parsed.left, -123.4);
});

test("parseBox refuses a PARTIAL box (a half-filter is not a filter)", () => {
  assert.equal(parseBox({}), null);
  assert.equal(parseBox({ geo_top_lat: "38", geo_bottom_lat: "37" }), null);
  assert.equal(
    parseBox({
      geo_top_lat: "38",
      geo_bottom_lat: "37",
      geo_left_lng: "-123",
      geo_right_lng: "",
    }),
    null,
  );
  assert.equal(
    parseBox({
      geo_top_lat: "38",
      geo_bottom_lat: "37",
      geo_left_lng: "-123",
      geo_right_lng: "not-a-number",
    }),
    null,
  );
});

test("boundsOf frames every point, and formatBox reads as coordinates", () => {
  const b = boundsOf(
    [
      { lat: 37.7, lng: -122.5 },
      { lat: 37.8, lng: -122.4 },
    ],
    0,
  );
  assert.deepEqual(b, { top: 37.8, bottom: 37.7, left: -122.5, right: -122.4 });
  assert.equal(boundsOf([]), null);
  assert.equal(formatBox(b!), "37.70°N 122.50°W – 37.80°N 122.40°W");
});

// --------------------------------------------------------------------------
// clustering
// --------------------------------------------------------------------------

const pt = (id: string, x: number, y: number): ScreenPoint => ({
  id,
  label: id,
  lat: 0,
  lng: 0,
  x,
  y,
});

test("clustering merges neighbours and keeps every point", () => {
  const pts = [pt("a", 10, 10), pt("b", 12, 14), pt("c", 300, 300)];
  const cs = clusterPoints(pts, 48);
  assert.equal(cs.length, 2);
  assert.equal(
    cs.reduce((n, c) => n + c.count, 0),
    3,
  );
  const big = cs.find((c) => c.count === 2)!;
  // Centroid, not cell centre.
  assert.equal(big.x, 11);
  assert.equal(big.y, 12);
});

test("a lone point clusters to exactly itself", () => {
  const [c] = clusterPoints([pt("solo", 123.5, 77.25)], 48);
  assert.equal(c.count, 1);
  assert.equal(c.x, 123.5);
  assert.equal(c.y, 77.25);
});

test("zooming in separates points that shared a cluster", () => {
  // Two places ~1.4 km apart, drawn in the same 800x600 viewport at two zooms.
  const a = { lat: 37.7749, lng: -122.4194 };
  const b = { lat: 37.7869, lng: -122.4094 };
  const screen = (zoom: number) => {
    const vp = makeViewport({ center: a, zoom, width: 800, height: 600 });
    return [
      { ...pt("a", 0, 0), ...vp.toScreen(a.lat, a.lng) },
      { ...pt("b", 0, 0), ...vp.toScreen(b.lat, b.lng) },
    ];
  };
  assert.equal(clusterPoints(screen(9), 48).length, 1, "one cluster at city zoom");
  assert.equal(clusterPoints(screen(15), 48).length, 2, "two marks at street zoom");
});

test("the render bound holds: marks never exceed the number of grid cells", () => {
  // 5000 points scattered over an 800x600 viewport must not become 5000 marks.
  const pts: ScreenPoint[] = [];
  for (let i = 0; i < 5000; i++) {
    pts.push(pt(`p${i}`, (i * 37) % 800, (i * 53) % 600));
  }
  const cells = Math.ceil(800 / 48) * Math.ceil(600 / 48);
  const cs = clusterPoints(pts, 48);
  assert.ok(cs.length <= cells, `${cs.length} marks for ${cells} cells`);
  assert.equal(
    cs.reduce((n, c) => n + c.count, 0),
    5000,
    "no point is dropped, only merged",
  );
});

// --------------------------------------------------------------------------
// search-hit plumbing + basemap decoding
// --------------------------------------------------------------------------

test("geoOf reads Meili's _geo, and treats a missing one as normal", () => {
  assert.deepEqual(geoOf({ _geo: { lat: 1.5, lng: -2.5 } }), { lat: 1.5, lng: -2.5 });
  // No _geo is the EXPECTED case for a library with expose_gps off.
  assert.equal(geoOf({ id: "x" }), null);
  assert.equal(geoOf({ _geo: null }), null);
  assert.equal(geoOf({ _geo: { lat: "1", lng: 2 } }), null);
  assert.equal(geoOf({ _geo: { lat: 91, lng: 2 } }), null);
  assert.equal(geoOf({ _geo: { lat: Number.NaN, lng: 2 } }), null);
});

test("decodePolyline is the exact inverse of the generator's encoder", () => {
  // Mirrors scripts/build_basemap.mjs so a change to either side breaks here
  // rather than silently drawing a scrambled coastline.
  const encode = (pairs: [number, number][]): string => {
    let out = "";
    let px = 0;
    let py = 0;
    const chunk = (delta: number) => {
      let v = delta < 0 ? ~(delta << 1) : delta << 1;
      while (v >= 0x20) {
        out += String.fromCharCode((0x20 | (v & 0x1f)) + 63);
        v >>= 5;
      }
      out += String.fromCharCode(v + 63);
    };
    for (const [lng, lat] of pairs) {
      const x = Math.round(lng * 100);
      const y = Math.round(lat * 100);
      chunk(x - px);
      chunk(y - py);
      px = x;
      py = y;
    }
    return out;
  };
  const pairs: [number, number][] = [
    [-122.42, 37.77],
    [-122.41, 37.78],
    [151.21, -33.87],
    [0, 0],
    [180, -85.05],
  ];
  const decoded = decodePolyline(encode(pairs));
  assert.equal(decoded.length, pairs.length * 2);
  pairs.forEach(([lng, lat], i) => {
    assert.ok(Math.abs(decoded[i * 2] - lng) < 1e-9);
    assert.ok(Math.abs(decoded[i * 2 + 1] - lat) < 1e-9);
  });
});
