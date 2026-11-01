// Pure pointer rules for map/MapPanel.svelte (A27), DOM-free so they run on Node.

/** Whether a pointerdown on the map svg starts a pan/select drag. Only a
 *  primary-button press on the background does: a press inside a marker or a
 *  cluster glyph (`[data-marker]`) is left to the marker's own click handler,
 *  so it neither clears the active area nor takes pointer capture. */
export function shouldStartDrag(target: { inMarker: boolean; button: number }): boolean {
  return target.button === 0 && !target.inMarker;
}
