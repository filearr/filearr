// Accent contrast helpers. DOM-free so the Node test runner can import it.
// The accent is user-configurable, so text on accent backgrounds cannot be a
// fixed white: a pale accent (yellow, white) would make it unreadable.

const SLATE_950 = "#0f172a";

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

/** Foreground for text on the accent: white unless slate-950 contrasts better.
 *  Ties go to white so the default indigo family keeps its familiar look. */
export function onAccentColor(hex: string): "#ffffff" | "#0f172a" {
  const l = relativeLuminance(hex);
  return contrast(l, 1) >= contrast(l, relativeLuminance(SLATE_950)) ? "#ffffff" : SLATE_950;
}
