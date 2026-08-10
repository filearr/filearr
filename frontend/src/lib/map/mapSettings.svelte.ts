// R8-UI — the map's one persisted preference: the OPTIONAL third-party tile
// layer. Same runes + localStorage shape as lib/theme.svelte.ts.
//
// Filearr is deliberately offline-capable — the manual is baked into the image
// and Swagger is vendored so a deployment with no outbound internet still works
// end to end. A tile layer breaks that: every pan makes the VIEWER'S browser
// request images from a third-party server, which then learns their IP address
// and, tile by tile, exactly which places they are looking at in their own photo
// library. That is a real disclosure, so it is OFF by default, never enabled as
// a side effect of anything else, and the map says out loud which state it is in.

/** OpenStreetMap's standard tile server. Only ever contacted after an explicit
 *  opt-in; operators pointing at their own tile server just replace the URL. */
export const DEFAULT_TILE_URL = "https://tile.openstreetmap.org/{z}/{x}/{y}.png";

export const mapSettings = $state({
  // Default OFF. Read as an exact "on" so any other stored value (including a
  // half-written one) fails closed.
  tiles: localStorage.getItem("mapTiles") === "on",
  tileUrl: localStorage.getItem("mapTileUrl") || DEFAULT_TILE_URL,
});

export function saveMapSettings() {
  localStorage.setItem("mapTiles", mapSettings.tiles ? "on" : "off");
  localStorage.setItem("mapTileUrl", mapSettings.tileUrl);
}

/** Host shown in the "tiles are on" warning, so the user can see WHO they are
 *  talking to rather than a template string. Falsy for an unparseable URL. */
export function tileHost(url: string): string {
  try {
    return new URL(url).host;
  } catch {
    return "";
  }
}
