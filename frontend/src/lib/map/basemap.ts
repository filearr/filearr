// R8-UI — lazy loader for the bundled offline basemap.
//
// The encoded coordinate table (basemapData.ts, ~46 KB) is pulled in with a
// dynamic import so it lands in its OWN chunk: someone who never opens the map
// tab never downloads it, and the main bundle is unchanged. Decoding runs once
// per page load and the result is cached here, because the map component
// re-derives its SVG paths on every pan/zoom frame and re-decoding 11 600 points
// each time would be the one avoidable cost in that loop.

import { decodePolyline } from "./geo";

export interface Shape {
  /** Flat [lng, lat, lng, lat, …] in degrees. */
  pts: number[];
  /** Geographic bounds, precomputed so the renderer can cull whole shapes that
   *  are off-screen with four comparisons instead of walking their vertices —
   *  at city zoom that skips ~99% of the world. */
  minLng: number;
  maxLng: number;
  minLat: number;
  maxLat: number;
}

export interface Basemap {
  land: Shape[];
  borders: Shape[];
}

function toShape(encoded: string): Shape {
  const pts = decodePolyline(encoded);
  let minLng = 180;
  let maxLng = -180;
  let minLat = 90;
  let maxLat = -90;
  for (let i = 0; i < pts.length; i += 2) {
    if (pts[i] < minLng) minLng = pts[i];
    if (pts[i] > maxLng) maxLng = pts[i];
    if (pts[i + 1] < minLat) minLat = pts[i + 1];
    if (pts[i + 1] > maxLat) maxLat = pts[i + 1];
  }
  return { pts, minLng, maxLng, minLat, maxLat };
}

let cached: Promise<Basemap> | null = null;

/** Decode the bundled outline (cached). Never touches the network: the data is
 *  a JS module in this bundle, not a fetch. */
export function loadBasemap(): Promise<Basemap> {
  cached ??= import("./basemapData").then((m) => ({
    land: m.LAND.map(toShape),
    borders: m.BORDERS.map(toShape),
  }));
  return cached;
}
