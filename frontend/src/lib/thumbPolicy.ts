// P6 (A22, ruling 6) — which items ask the server for a thumbnail, and the one
// retry. DOM-free so it is Node-tested; Thumb.svelte applies it.
//
// Mirrors backend/filearr/tasks/thumbs.py: image / video / audio and
// three-d-cad are rendered on request (`_render_for` — 3D through the software
// rasterizer), `document` only for a `.pdf`. Everything else would 404, so it
// gets the placeholder and no request at all.

const ALWAYS = new Set(["image", "video", "audio", "three-d-cad"]);

export function thumbable(category: string | null | undefined, filename?: string | null): boolean {
  if (!category) return false;
  if (category === "document") return !!filename && filename.toLowerCase().endsWith(".pdf");
  return ALWAYS.has(category);
}

/** A video poster the worker has not made yet is a 404 ("queued; retry
 *  shortly"); one retry after this delay catches the common case. */
export const RETRY_DELAY_MS = 2000;

/** The retry URL: a distinct query so the browser does not reuse the 404. */
export function retryUrl(url: string): string {
  return url + (url.includes("?") ? "&" : "?") + "r=1";
}

/** The placeholder's short category text ("doc", "vid", "3d"). */
export function placeholderText(category: string | null | undefined): string {
  if (!category) return "";
  if (category === "three-d-cad") return "3d";
  return category.slice(0, 3);
}
