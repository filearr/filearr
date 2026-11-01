// Accent contrast helpers. DOM-free so the Node test runner can import it.
// The accent is user-configurable, so text on accent backgrounds cannot be a
// fixed white: a pale accent (yellow, white) would make it unreadable.

const SLATE_900 = "#0f172a";

const HEX = /^#[0-9a-fA-F]{6}$/;
// indigo-600: the old indigo-500 default fell short of 4.5:1 for white text on it.
export const DEFAULT_ACCENT = "#4f46e5";
// The previous default. The only place the literal may appear in src/.
const LEGACY_DEFAULT_ACCENT = "#6366f1";

/** A stored accent (localStorage or server preferences) → the accent to use,
 *  or null when it is not a `#rrggbb` colour. The old default was the shipped
 *  value, not a user choice (applyTheme saved it for everyone), so it
 *  migrates to the current default instead of pinning the low-contrast one. */
export function normalizeAccent(raw: unknown): string | null {
  if (typeof raw !== "string" || !HEX.test(raw)) return null;
  return raw.toLowerCase() === LEGACY_DEFAULT_ACCENT ? DEFAULT_ACCENT : raw;
}

/** WCAG 2.x relative luminance of a `#rrggbb` colour (0 = black, 1 = white). */
export function relativeLuminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  const channel = (c: number) => {
    const s = c / 255;
    return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4;
  };
  const r = channel((n >> 16) & 0xff);
  const g = channel((n >> 8) & 0xff);
  const b = channel(n & 0xff);
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: number, b: number): number {
  return (Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05);
}

/** WCAG 2.x contrast ratio between two `#rrggbb` colours (1–21, order-free). */
export function contrastRatio(a: string, b: string): number {
  return contrast(relativeLuminance(a), relativeLuminance(b));
}

/** Channel-wise sRGB blend of `a` toward `b` by `t` (0 = a, 1 = b), as `#rrggbb`.
 *  Plain sRGB like CSS `color-mix(in srgb, …)`, so the no-JS fallback matches. */
export function mixHex(a: string, b: string, t: number): string {
  const x = parseInt(a.slice(1), 16);
  const y = parseInt(b.slice(1), 16);
  let out = "#";
  for (const shift of [16, 8, 0]) {
    const ca = (x >> shift) & 0xff;
    const cb = (y >> shift) & 0xff;
    out += Math.round(ca + (cb - ca) * t).toString(16).padStart(2, "0");
  }
  return out;
}

/** The accent as readable text on `surface`: the accent itself when it already
 *  meets `min`, else the smallest 5 % step toward white (dark surface) or black
 *  (light surface) that does. The accent is user-chosen, so a fixed mix would
 *  over-correct some accents and under-correct others. */
export function accentTextColor(accent: string, surface: string, min = 4.5): string {
  if (contrastRatio(accent, surface) >= min) return accent;
  const target = relativeLuminance(surface) < 0.5 ? "#ffffff" : "#000000";
  for (let i = 1; i < 20; i++) {
    const c = mixHex(accent, target, i / 20);
    if (contrastRatio(c, surface) >= min) return c;
  }
  return target;
}

/** Foreground for text on the accent: white unless slate-900 contrasts better.
 *  Ties go to white so the default indigo family keeps its familiar look. */
export function onAccentColor(hex: string): "#ffffff" | "#0f172a" {
  const l = relativeLuminance(hex);
  return contrast(l, 1) >= contrast(l, relativeLuminance(SLATE_900)) ? "#ffffff" : SLATE_900;
}
