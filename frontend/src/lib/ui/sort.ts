// Column sorting for DataTable. DOM-free so tests/sort.node.test.ts runs it
// on Node's test runner.

export type SortDir = "asc" | "desc";
export type SortState = { key: string; dir: SortDir } | null;

/** Header click: none -> asc -> desc -> none on the same key; a new key starts at asc. */
export function nextSort(current: SortState, key: string): SortState {
  if (current === null || current.key !== key) return { key, dir: "asc" };
  return current.dir === "asc" ? { key, dir: "desc" } : null;
}

// NaN and invalid Dates have no place in an order (every comparison with NaN
// is false), so they count as missing and sort with null/undefined.
const isNil = (v: unknown): boolean =>
  v === null ||
  v === undefined ||
  (typeof v === "number" && Number.isNaN(v)) ||
  (v instanceof Date && Number.isNaN(v.getTime()));

// Relational rather than `a - b`, which gives NaN for Infinity - Infinity.
const cmp = (a: number, b: number): number => (a < b ? -1 : a > b ? 1 : 0);

/** Ascending comparator; null, undefined, NaN and invalid Dates sort after every value. */
export function compareValues(a: unknown, b: unknown): number {
  if (isNil(a) || isNil(b)) return isNil(a) ? (isNil(b) ? 0 : 1) : -1;
  if (typeof a === "number" && typeof b === "number") return cmp(a, b);
  if (typeof a === "boolean" && typeof b === "boolean") return Number(a) - Number(b);
  if (a instanceof Date && b instanceof Date) return cmp(a.getTime(), b.getTime());
  // Strings, and the fallback for mixed types: natural order ("file2" < "file10"),
  // ignoring case and accents.
  return String(a).localeCompare(String(b), undefined, { numeric: true, sensitivity: "base" });
}

/** Stable sort by `state.key`; returns `rows` itself when there is no sort. */
export function sortRows<T>(
  rows: readonly T[],
  state: SortState,
  accessor: (row: T, key: string) => unknown,
): T[] {
  if (state === null) return rows as T[];
  const { key, dir } = state;
  const sign = dir === "asc" ? 1 : -1;
  // Array.prototype.sort is stable, so ties keep their input order in both
  // directions. Nulls skip the direction flip so they stay at the bottom.
  return [...rows].sort((x, y) => {
    const a = accessor(x, key);
    const b = accessor(y, key);
    return isNil(a) || isNil(b) ? compareValues(a, b) : sign * compareValues(a, b);
  });
}
