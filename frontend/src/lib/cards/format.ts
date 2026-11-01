// Small display helpers shared by the per-type detail cards (P4-T10). Pure.

import { fmtBytes as bytes, fmtDuration as duration } from "../format.ts";

// The cards' formatting comes from ../format (A32). These two keep the cards'
// contract on top of it: metadata values are `unknown` (often numeric strings)
// and an absent or non-positive value is null, so Highlights drops the fact
// and ArchiveSection omits the size instead of printing "—" or "0 B".
const positive = (v: unknown): number | null => {
  const n = typeof v === "number" ? v : Number(v);
  return v != null && v !== "" && Number.isFinite(n) && n > 0 ? n : null;
};

/** Human duration from a seconds value; null when absent/non-positive. */
export const fmtDuration = (sec: unknown): string | null => {
  const n = positive(sec);
  return n == null ? null : duration(n);
};

/** Human byte size; null when absent/non-positive. */
export const fmtBytes = (b: unknown): string | null => {
  const n = positive(b);
  return n == null ? null : bytes(n);
};

/** Coerce an arbitrary metadata value to a short display string, or null. */
export function asStr(v: unknown): string | null {
  if (v == null) return null;
  if (typeof v === "string") return v.trim() ? v : null;
  if (typeof v === "number") return Number.isFinite(v) ? String(v) : null;
  if (typeof v === "boolean") return v ? "yes" : "no";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

/** An array of plain objects (rows for a TrackTable), or null. */
export function asRows(v: unknown): Record<string, unknown>[] | null {
  if (!Array.isArray(v) || v.length === 0) return null;
  const rows = v.filter((x) => x != null && typeof x === "object" && !Array.isArray(x));
  return rows.length ? (rows as Record<string, unknown>[]) : null;
}
