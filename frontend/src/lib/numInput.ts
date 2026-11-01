// Normalise numeric form values. Svelte's bind:value on <input type="number">
// writes a number, or null when the field is emptied (never a string), while
// state seeded from the API or a text input may still be a string. Both
// helpers accept either and return number | null, so callers never .trim() a
// number. DOM-free, so it is Node-tested.

const NUMERIC = /^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$/;

/** Finite number, or null for empty / whitespace / non-numeric / NaN / ±Infinity. */
export function numOrNull(v: unknown): number | null {
  if (typeof v === "number") return Number.isFinite(v) ? v : null;
  if (typeof v !== "string") return null;
  const s = v.trim();
  if (!NUMERIC.test(s)) return null;
  const n = Number(s);
  return Number.isFinite(n) ? n : null;
}

/** Integer within [min, max] (inclusive, each optional), else null. */
export function intOrNull(
  v: unknown,
  { min, max }: { min?: number; max?: number } = {},
): number | null {
  const n = numOrNull(v);
  if (n === null || !Number.isInteger(n)) return null;
  if (min !== undefined && n < min) return null;
  if (max !== undefined && n > max) return null;
  return n;
}
