// R8-UI — DOM-free geometry for the photo GPS map (see MapPanel.svelte).
//
// Everything here is a pure function so the parts that are easy to get subtly
// wrong — the projection, the bounding-box rules the API answers 422 to, and the
// clustering that keeps the browser from drawing tens of thousands of dots — are
// unit-testable on Node without a DOM (tests/mapGeo.node.test.ts).
//
// PROJECTION: spherical Web Mercator (EPSG:3857), the slippy-map convention.
// Two reasons, in order:
//   1. A rectangle dragged on screen must become an exact lat/lng box, because
//      that is what `GET /search` takes. Mercator is separable — x depends only
//      on longitude, y only on latitude — so the four screen edges unproject to
//      the four box edges with no corner-vs-edge discrepancy. (Equirectangular
//      shares that property; an oblique or conic projection does not.)
//   2. The OPTIONAL tile layer is XYZ/Web-Mercator. Choosing anything else would
//      mean the tiles and the bundled outline could not both be right.
// The cost is the usual one: latitude is clipped at ±85.0511° (Antarctica's
// interior is off the map) and high-latitude areas look inflated.

/** Slippy-map tile edge in CSS px. World width at zoom z is TILE_SIZE·2^z. */
export const TILE_SIZE = 256;

/** The latitude where the Mercator world becomes square — the standard clip. */
export const MAX_LAT = 85.05112877980659;

export interface LatLng {
  lat: number;
  lng: number;
}

/** A geographic rectangle in the exact vocabulary `GET /search` uses, so the UI
 *  never has to re-derive edge names on the way out. */
export interface GeoBox {
  top: number; // northern edge (max latitude)
  right: number; // eastern edge (max longitude)
  bottom: number; // southern edge (min latitude)
  left: number; // western edge (min longitude)
}

export const clampLat = (lat: number): number =>
  Math.min(MAX_LAT, Math.max(-MAX_LAT, lat));

/** Fold a longitude back into [-180, 180]. Panning east past the antimeridian is
 *  normal on a slippy map; the *filter* still has to be expressed in range. */
export function wrapLng(lng: number): number {
  const x = ((lng + 180) % 360 + 360) % 360;
  return x - 180;
}

export const worldSize = (zoom: number): number => TILE_SIZE * 2 ** zoom;

/** Geographic point → world pixel at `zoom` (origin: top-left, 180°W / 85°N). */
export function project(lat: number, lng: number, zoom: number): { x: number; y: number } {
  const size = worldSize(zoom);
  const phi = (clampLat(lat) * Math.PI) / 180;
  return {
    x: ((lng + 180) / 360) * size,
    y: ((1 - Math.log(Math.tan(phi) + 1 / Math.cos(phi)) / Math.PI) / 2) * size,
  };
}

/** World pixel → geographic point. Exact inverse of {@link project} within the
 *  clipped latitude band (round-trip is asserted in the tests). */
export function unproject(x: number, y: number, zoom: number): LatLng {
  const size = worldSize(zoom);
  const n = Math.PI - 2 * Math.PI * (y / size);
  return {
    lat: (180 / Math.PI) * Math.atan(0.5 * (Math.exp(n) - Math.exp(-n))),
    lng: (x / size) * 360 - 180,
  };
}

export interface ViewportSpec {
  center: LatLng;
  zoom: number;
  width: number;
  height: number;
}

/** A viewport's screen<->world mapping, precomputed once per render.
 *
 *  Kept as a plain object of closures rather than a class so the render code can
 *  destructure it and the tests can build one in a line. */
export interface Viewport {
  toScreen(lat: number, lng: number): { x: number; y: number };
  fromScreen(x: number, y: number): LatLng;
  /** World-pixel offset of the screen origin — the tile layer needs it raw. */
  originX: number;
  originY: number;
  zoom: number;
  width: number;
  height: number;
}

export function makeViewport(spec: ViewportSpec): Viewport {
  const { x: cx, y: cy } = project(spec.center.lat, spec.center.lng, spec.zoom);
  const originX = cx - spec.width / 2;
  const originY = cy - spec.height / 2;
  return {
    originX,
    originY,
    zoom: spec.zoom,
    width: spec.width,
    height: spec.height,
    toScreen(lat, lng) {
      const p = project(lat, lng, spec.zoom);
      return { x: p.x - originX, y: p.y - originY };
    },
    fromScreen(x, y) {
      return unproject(x + originX, y + originY, spec.zoom);
    },
  };
}

/** The zoom at which `box` fits inside a `width`×`height` viewport, with a small
 *  margin. Used by "fit to results" and by cluster drill-down. */
export function zoomForBox(box: GeoBox, width: number, height: number): number {
  const nw = project(box.top, box.left, 0);
  const se = project(box.bottom, box.right, 0);
  const dx = Math.max(1e-6, se.x - nw.x);
  const dy = Math.max(1e-6, se.y - nw.y);
  const z = Math.min(Math.log2((width * 0.9) / dx), Math.log2((height * 0.9) / dy));
  return Math.max(0, Math.min(18, z));
}

// ---------------------------------------------------------------------------
// Bounding boxes
//
// The API refuses (422) a partial box, an inverted box, and a box that crosses
// the antimeridian — Meilisearch's _geoBoundingBox cannot express the last one.
// The UI's job is to never send one: a drag is normalised so it CANNOT be
// inverted, and the numeric inputs (the keyboard path, and how you paste
// coordinates) are validated before they can run a search.
// ---------------------------------------------------------------------------

/** Latitude clamp for FILTER values — the full [-90, 90] the API accepts, not
 *  the Mercator draw limit. A drag can only reach ±85, but a pasted coordinate
 *  can legitimately be a pole. */
const clampToLat = (lat: number): number => Math.min(90, Math.max(-90, lat));

/** Build a box from two dragged corners. Sorting the pair means an up-left drag
 *  and a down-right drag produce the same box, so "inverted" is unreachable from
 *  the pointer path by construction. Longitudes are folded into [-180, 180]
 *  first, then sorted, so panning across the antimeridian still yields an
 *  in-range (if geographically surprising) box the API will accept. */
export function normalizeBox(a: LatLng, b: LatLng): GeoBox {
  const lats = [clampToLat(a.lat), clampToLat(b.lat)].sort((m, n) => m - n);
  const lngs = [wrapLng(a.lng), wrapLng(b.lng)].sort((m, n) => m - n);
  return { bottom: lats[0], top: lats[1], left: lngs[0], right: lngs[1] };
}

/** Why this box cannot be sent, or `null` when it is fine. The messages mirror
 *  the backend's own 422 reasons so the two never tell a different story. */
export function boxError(box: GeoBox): string | null {
  const finite = [box.top, box.right, box.bottom, box.left].every(Number.isFinite);
  if (!finite) return "Every edge needs a number — a box is all four or none.";
  if (box.top < -90 || box.top > 90 || box.bottom < -90 || box.bottom > 90)
    return "Latitudes must be between -90 and 90.";
  if (box.left < -180 || box.left > 180 || box.right < -180 || box.right > 180)
    return "Longitudes must be between -180 and 180.";
  if (box.top < box.bottom)
    return "The north edge is below the south edge — swap them.";
  if (box.left > box.right)
    return "The west edge is east of the east edge. A box cannot cross the 180th meridian; use two searches.";
  if (box.top === box.bottom || box.left === box.right)
    return "That area has no width or height — drag a larger box.";
  return null;
}

/** The four flat search params for a box (empty when the box is unusable, so a
 *  bad box degrades to "no geo filter" rather than a 422). Values are fixed to
 *  6 dp — ~11 cm, past any consumer GPS — which also keeps the deep-link hash
 *  from carrying 17 digits of float noise. */
export function boxParams(box: GeoBox | null): Record<string, string> {
  if (!box || boxError(box)) return {};
  const f = (v: number) => String(Number(v.toFixed(6)));
  return {
    geo_top_lat: f(box.top),
    geo_right_lng: f(box.right),
    geo_bottom_lat: f(box.bottom),
    geo_left_lng: f(box.left),
  };
}

/** Read a box back out of a flat param record (deep link, saved search, or
 *  browser back/forward). Returns `null` unless all four edges are present and
 *  numeric — a partial box is treated as no box, never as a half-filter. */
export function parseBox(p: Record<string, string>): GeoBox | null {
  const keys = ["geo_top_lat", "geo_right_lng", "geo_bottom_lat", "geo_left_lng"] as const;
  const nums: number[] = [];
  for (const k of keys) {
    const raw = p[k];
    if (raw == null || raw === "") return null;
    const v = Number(raw);
    if (!Number.isFinite(v)) return null;
    nums.push(v);
  }
  const [top, right, bottom, left] = nums;
  return { top, right, bottom, left };
}

/** Smallest box containing every point, padded a little so markers at the edge
 *  are not clipped. `null` for an empty list. */
export function boundsOf(points: readonly LatLng[], padDeg = 0.05): GeoBox | null {
  if (!points.length) return null;
  let top = -90;
  let bottom = 90;
  let left = 180;
  let right = -180;
  for (const p of points) {
    top = Math.max(top, p.lat);
    bottom = Math.min(bottom, p.lat);
    left = Math.min(left, p.lng);
    right = Math.max(right, p.lng);
  }
  return {
    top: Math.min(90, top + padDeg),
    bottom: Math.max(-90, bottom - padDeg),
    left: Math.max(-180, left - padDeg),
    right: Math.min(180, right + padDeg),
  };
}

/** Human-readable box, e.g. `37.70N -122.51W … 37.81N -122.36W`. Used in the
 *  active-filter chip and the map caption. */
export function formatBox(box: GeoBox): string {
  const lat = (v: number) => `${Math.abs(v).toFixed(2)}°${v < 0 ? "S" : "N"}`;
  const lng = (v: number) => `${Math.abs(v).toFixed(2)}°${v < 0 ? "W" : "E"}`;
  return `${lat(box.bottom)} ${lng(box.left)} – ${lat(box.top)} ${lng(box.right)}`;
}

// ---------------------------------------------------------------------------
// Clustering
//
// A photo library can hold tens of thousands of geo-bearing files, and an <svg>
// with that many <circle>s is a slideshow. Two independent bounds apply:
//
//   * the FETCH is capped (MAP_POINT_CAP below) and the caller reports "showing
//     N of M" — see MapPanel/SearchPage. We never silently truncate.
//   * the RENDER is capped by this grid: markers are bucketed into fixed-size
//     screen cells, so the number of drawn marks can never exceed
//     ceil(width/cell)·ceil(height/cell) — about 300 for a 1200×600 map at the
//     48 px default — no matter how many points went in. Zooming in spreads
//     points across more cells, which is exactly the drill-down behaviour you
//     want, and it costs one pass over the points (no quadtree to maintain).
// ---------------------------------------------------------------------------

/** How many geo-bearing hits the map will fetch for one query, across paged
 *  requests. 1000 is a deliberate compromise: enough that a country-scale view
 *  is representative, small enough that the fetch is a few hundred ms and the
 *  clustering pass is trivial. Anything beyond it is reported, not hidden. */
export const MAP_POINT_CAP = 1000;

/** Default cluster cell in CSS px. Roughly two marker diameters — big enough
 *  that overlapping dots merge, small enough that neighbouring towns stay apart
 *  at street zoom. */
export const CLUSTER_CELL_PX = 48;

export interface MapPoint extends LatLng {
  id: string;
  label: string;
}

export interface ScreenPoint extends MapPoint {
  x: number;
  y: number;
}

export interface Cluster {
  /** Stable key across re-renders at the same zoom (the grid cell). */
  key: string;
  x: number;
  y: number;
  lat: number;
  lng: number;
  count: number;
  points: ScreenPoint[];
}

/** Bucket already-projected points into a fixed screen grid and collapse each
 *  cell to its centroid. Order is stable (first-seen cell order) so Svelte's
 *  keyed each-block does not reshuffle the DOM on unrelated updates. */
export function clusterPoints(
  points: readonly ScreenPoint[],
  cellPx: number = CLUSTER_CELL_PX,
): Cluster[] {
  const cell = Math.max(1, cellPx);
  const byCell = new Map<string, Cluster>();
  for (const p of points) {
    const key = `${Math.floor(p.x / cell)}:${Math.floor(p.y / cell)}`;
    const c = byCell.get(key);
    if (c) {
      c.points.push(p);
      c.count++;
    } else {
      byCell.set(key, { key, x: 0, y: 0, lat: 0, lng: 0, count: 1, points: [p] });
    }
  }
  const out: Cluster[] = [];
  for (const c of byCell.values()) {
    // Centroid, not cell centre: a single point must land exactly on itself, and
    // a cluster should sit over its mass rather than snap to an invisible grid.
    for (const p of c.points) {
      c.x += p.x / c.count;
      c.y += p.y / c.count;
      c.lat += p.lat / c.count;
      c.lng += p.lng / c.count;
    }
    out.push(c);
  }
  return out;
}

// ---------------------------------------------------------------------------
// Search-hit plumbing
// ---------------------------------------------------------------------------

/** Pull the Meilisearch `_geo` point off a search hit, or `null`.
 *
 *  A hit only carries `_geo` when its library has `expose_gps` on — that gate is
 *  applied at PROJECTION time, server-side. Missing coordinates are therefore
 *  the normal, expected case, never a parse failure to report. */
export function geoOf(hit: Record<string, unknown>): LatLng | null {
  const g = hit["_geo"];
  if (!g || typeof g !== "object") return null;
  const { lat, lng } = g as { lat?: unknown; lng?: unknown };
  if (typeof lat !== "number" || typeof lng !== "number") return null;
  if (!Number.isFinite(lat) || !Number.isFinite(lng)) return null;
  if (lat < -90 || lat > 90 || lng < -180 || lng > 180) return null;
  return { lat, lng };
}

// ---------------------------------------------------------------------------
// Basemap decoding
// ---------------------------------------------------------------------------

/** Decode one Google-style encoded polyline into a flat [lng, lat, …] array.
 *
 *  The bundled basemap ships encoded (scripts/build_basemap.mjs) because the
 *  same 11 600 points cost ~46 KB this way and ~140 KB as JSON numbers — and the
 *  whole point of the asset is that it is small enough to bundle instead of
 *  fetching tiles. `scale` is 10^precision; the generator uses 2 dp (~1.1 km),
 *  which is finer than 1:110m source geometry warrants. */
export function decodePolyline(encoded: string, scale = 100): number[] {
  const out: number[] = [];
  let i = 0;
  let x = 0;
  let y = 0;
  // One varint: 5-bit little-endian groups, high bit = continuation, +63 to keep
  // every byte printable ASCII; bit 0 of the assembled value is the sign
  // (zigzag). Inlined rather than looping over an [x, y] pair to avoid an array
  // allocation per point — this runs over every basemap vertex.
  const varint = (): number => {
    let result = 0;
    let shift = 0;
    let byte = 0;
    do {
      byte = encoded.charCodeAt(i++) - 63;
      result |= (byte & 0x1f) << shift;
      shift += 5;
    } while (byte >= 0x20);
    return result & 1 ? ~(result >> 1) : result >> 1;
  };
  while (i < encoded.length) {
    x += varint();
    y += varint();
    out.push(x / scale, y / scale);
  }
  return out;
}
