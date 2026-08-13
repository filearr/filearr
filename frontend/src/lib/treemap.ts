// IN-T3 — squarified treemap layout + presentation helpers, DOM-FREE.
//
// Why a hand-rolled algorithm: the frontend has ZERO chart dependencies (the
// TimelinePage bar chart is hand-rolled for the same reason) and the design
// contract forbids adding one. A treemap is a small, well-specified algorithm —
// Bruls / Huizing / van Wijk, "Squarified Treemaps" (2000) — so the cost of
// owning it is one file and a test suite, not a supply-chain surface.
//
// Everything here is pure: no DOM, no Svelte, no fetch. That is deliberate —
// the layout invariants (proportional areas, no overlap, exact fill) and the
// degenerate cases (zero-byte folders, a single child, an empty level) are the
// parts most likely to regress, and they are only unit-testable on Node if the
// module never touches a browser global. `FolderTreemap.svelte` is then a thin
// SVG renderer over these outputs.

/** One node to lay out. ``value`` is bytes in practice; any non-negative
 *  magnitude works. ``key`` is opaque and comes back on the rect so the caller
 *  can map a rect to its source row without relying on array order. */
export interface TreemapItem {
  key: string;
  value: number;
}

/** A laid-out node in the same coordinate space as the box passed to
 *  ``squarify`` (origin top-left, x→right, y→down — SVG's convention). */
export interface TreemapRect {
  key: string;
  x: number;
  y: number;
  w: number;
  h: number;
}

/** Internal working record: the item's key plus its target AREA (not its raw
 *  value) so the row-packing math is all in one unit. */
interface Scaled {
  key: string;
  area: number;
}

/** The "worst" (largest) aspect ratio in a row of areas laid along ``side``.
 *
 *  This is the paper's `worst()` function verbatim: for a row whose areas sum to
 *  ``s`` laid along a side of length ``side``, the row's thickness is s/side, so
 *  each member's aspect ratio is max(side²·a/s², s²/(side²·a)). Only the row's
 *  min and max members can produce the extreme, hence the two-term max.
 *
 *  Returns Infinity for a degenerate row so an empty/zero row never looks
 *  "better" than a real one and the caller's comparison stays total. */
function worst(rowMin: number, rowMax: number, rowSum: number, side: number): number {
  if (rowSum <= 0 || side <= 0 || rowMin <= 0) return Infinity;
  const s2 = rowSum * rowSum;
  const w2 = side * side;
  return Math.max((w2 * rowMax) / s2, s2 / (w2 * rowMin));
}

/**
 * Lay ``items`` out inside a ``width`` × ``height`` box using the squarified
 * treemap algorithm.
 *
 * Guarantees (pinned by tests):
 *  - Every positive-valued item gets area proportional to its value, and the
 *    positive rects together fill the box EXACTLY (no gaps, no overlap) —
 *    row thickness and the last member of every row absorb float error.
 *  - Items are laid out largest-first (the algorithm requires descending order
 *    to produce square-ish cells), so the returned order is by value desc,
 *    ties broken by input order (stable).
 *  - Zero/negative values get a ZERO-AREA rect parked at the leftover origin
 *    rather than being dropped: a caller that renders them can decide to skip
 *    them, but a caller that counts rects still sees every input. Proportional
 *    area is preserved literally — zero bytes is zero pixels.
 *  - When EVERY value is zero (a folder of empty files — real, and the report
 *    still wants to show its shape) we fall back to EQUAL shares so the level
 *    renders instead of collapsing to nothing. This is the one place the
 *    proportionality rule is deliberately suspended; it is unambiguous because
 *    the alternative (a blank panel) carries strictly less information.
 *  - A non-positive box or an empty item list returns [].
 */
export function squarify(
  items: readonly TreemapItem[],
  width: number,
  height: number,
): TreemapRect[] {
  if (!items.length || !(width > 0) || !(height > 0)) return [];

  // Negative values are nonsense for a byte total; clamp rather than reject so a
  // malformed row cannot break the whole level.
  const clamped = items.map((it) => ({
    key: it.key,
    value: Number.isFinite(it.value) && it.value > 0 ? it.value : 0,
  }));
  const total = clamped.reduce((s, it) => s + it.value, 0);
  const allZero = total <= 0;
  const area = width * height;

  // Descending by value; stable within ties (index tiebreak) so the layout is
  // reproducible across re-renders of the same data.
  const ordered = clamped
    .map((it, i) => ({ ...it, i }))
    .sort((a, b) => b.value - a.value || a.i - b.i);

  const scaled: Scaled[] = ordered.map((it) => ({
    key: it.key,
    // All-zero fallback: equal shares. Otherwise: value's share of the box.
    area: allZero ? area / ordered.length : (it.value / total) * area,
  }));

  const positives = scaled.filter((s) => s.area > 0);
  const zeros = scaled.filter((s) => s.area <= 0);

  const out: TreemapRect[] = [];
  // The remaining free sub-rectangle; every packed row is sliced off one edge.
  let fx = 0;
  let fy = 0;
  let fw = width;
  let fh = height;

  let i = 0;
  while (i < positives.length) {
    // Rows are laid along the SHORTER side — that is the whole trick: it keeps
    // each row's thickness comparable to its members' lengths.
    const side = Math.min(fw, fh);
    let rowSum = 0;
    let rowMin = Infinity;
    let rowMax = 0;
    let j = i;
    while (j < positives.length) {
      const a = positives[j].area;
      const nextMin = Math.min(rowMin, a);
      const nextMax = Math.max(rowMax, a);
      // Keep growing the row while doing so IMPROVES (lowers) the worst aspect
      // ratio. The first member always joins — a row of one is the baseline.
      if (
        j > i &&
        worst(nextMin, nextMax, rowSum + a, side) > worst(rowMin, rowMax, rowSum, side)
      ) {
        break;
      }
      rowSum += a;
      rowMin = nextMin;
      rowMax = nextMax;
      j++;
    }

    const isLastRow = j >= positives.length;
    if (fw >= fh) {
      // Free box is wide → the row is a vertical COLUMN sliced off the left.
      // The final row takes the whole remaining width so float drift cannot
      // leave a hairline gap at the right edge.
      const t = isLastRow ? fw : Math.min(fw, rowSum / fh);
      let y = fy;
      for (let k = i; k < j; k++) {
        const last = k === j - 1;
        const h = last ? fy + fh - y : Math.min(fh, positives[k].area / (t || 1));
        out.push({ key: positives[k].key, x: fx, y, w: t, h });
        y += h;
      }
      fx += t;
      fw -= t;
    } else {
      // Free box is tall → the row is a horizontal BAND sliced off the top.
      const t = isLastRow ? fh : Math.min(fh, rowSum / fw);
      let x = fx;
      for (let k = i; k < j; k++) {
        const last = k === j - 1;
        const w = last ? fx + fw - x : Math.min(fw, positives[k].area / (t || 1));
        out.push({ key: positives[k].key, x, y: fy, w, h: t });
        x += w;
      }
      fy += t;
      fh -= t;
    }
    i = j;
  }

  // Zero-valued items: real rows, zero pixels. Parked at the (now empty)
  // leftover origin so their coordinates are still inside the box.
  for (const z of zeros) {
    out.push({ key: z.key, x: Math.min(fx, width), y: Math.min(fy, height), w: 0, h: 0 });
  }
  return out;
}

// --------------------------------------------------------------------------- //
// Label fitting                                                                //
// --------------------------------------------------------------------------- //

/** Mean glyph advance as a fraction of font size, for the UI sans stack. A
 *  measured constant beats `getComputedTextLength()` here: measuring needs the
 *  element in the DOM (two layout passes per cell, per re-render) and this
 *  decision only has to be right enough to avoid drawing text that overflows
 *  its rect. Erring slightly WIDE means we drop a borderline label rather than
 *  spill one. */
export const GLYPH_ADVANCE = 0.58;

/** Does ``text`` fit inside a ``w`` × ``h`` cell at ``fontSize``? */
export function labelFits(
  w: number,
  h: number,
  text: string,
  fontSize = 11,
  pad = 4,
): boolean {
  if (!text) return false;
  if (h < fontSize + pad * 2) return false;
  return text.length * fontSize * GLYPH_ADVANCE <= w - pad * 2;
}

/** ``text`` trimmed (with an ellipsis) to what fits in ``w``, or "" when even a
 *  meaningful stub does not fit. Callers render nothing for "" — a one-character
 *  label is noise, not information. */
export function truncateLabel(text: string, w: number, fontSize = 11, pad = 4): string {
  if (!text) return "";
  const maxChars = Math.floor((w - pad * 2) / (fontSize * GLYPH_ADVANCE));
  if (maxChars < 3) return "";
  if (text.length <= maxChars) return text;
  return `${text.slice(0, maxChars - 1)}…`;
}

// --------------------------------------------------------------------------- //
// Color                                                                        //
// --------------------------------------------------------------------------- //

/** FNV-1a over the key, folded to a hue. Stable across sessions and machines
 *  (no Math.random, no index dependence), so a library keeps its color as the
 *  user drills in and out — that stability IS the encoding. */
export function hashHue(key: string): number {
  let h = 0x811c9dc5;
  for (let i = 0; i < key.length; i++) {
    h ^= key.charCodeAt(i);
    // FNV prime 16777619, via shifts to stay in 32-bit int math.
    h = (h + ((h << 1) + (h << 4) + (h << 7) + (h << 8) + (h << 24))) >>> 0;
  }
  return h % 360;
}

// Saturation/lightness bands chosen to read on BOTH themes: the panel behind the
// SVG is near-white in light mode and near-slate-900 in dark, so mid-lightness
// fills keep contrast either way. (A palette tuned for one theme would vanish in
// the other — the artifacts/theme rule applies to the app's own pages too.)
const ROOT_SAT = 52;
const ROOT_LIGHT = 48;
const RAMP_SAT = 55;
const RAMP_LIGHT_MIN = 36; // largest cell — darkest
const RAMP_LIGHT_MAX = 66; // smallest cell — lightest

/** Root (all-libraries) view: one stable hue per library. */
export function rootColor(libraryId: string): string {
  return `hsl(${hashHue(libraryId)} ${ROOT_SAT}% ${ROOT_LIGHT}%)`;
}

/** Inside a library: the library's hue, with lightness graded by SIZE RANK
 *  (0 = largest). Single-hue by design — a per-child categorical palette would
 *  imply a category dimension the folder-tree endpoint does not have, and
 *  inventing one is exactly the kind of decorative color the design contract
 *  defers. The ramp re-encodes size, which is already the area, so it reads as
 *  emphasis rather than as a second variable. */
export function rampColor(libraryId: string, rank: number, count: number): string {
  const span = Math.max(1, count - 1);
  const t = Math.min(1, Math.max(0, rank / span));
  const l = RAMP_LIGHT_MIN + t * (RAMP_LIGHT_MAX - RAMP_LIGHT_MIN);
  return `hsl(${hashHue(libraryId)} ${RAMP_SAT}% ${l.toFixed(1)}%)`;
}

/** Readable label color for a fill produced above: white on the darker half of
 *  the band, near-black on the lighter half. Parses the lightness back out of
 *  the hsl() string so callers never have to track which generator made a fill. */
export function labelColor(fill: string): string {
  const m = /([\d.]+)%\s*\)?$/.exec(fill.trim());
  const l = m ? Number(m[1]) : 50;
  return l >= 55 ? "#0f172a" : "#ffffff";
}

/** Human byte size for tooltips/captions. Local to the treemap so the module
 *  stays importable from Node tests without pulling a Svelte/DOM dependency. */
export function fmtBytes(b: number): string {
  if (!isFinite(b) || b <= 0) return "0 B";
  const u = ["B", "KB", "MB", "GB", "TB", "PB"];
  const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
  return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
}
