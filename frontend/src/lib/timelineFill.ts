// B38 — Timeline x-axis helpers, DOM-free so Node tests import them directly.
//
// `GET /stats/timeline` returns only NON-EMPTY buckets, truncated in UTC
// (`date_trunc(bucket, mtime AT TIME ZONE 'UTC')`, backend api/stats.py), and
// the bar click filters Search by that bucket's exact UTC [start, end) window.
// Drawn as adjacent columns, a sparse list makes a ten-year gap look like two
// consecutive months, so the page zero-fills between the first and last bucket.
//
// Labels stay UTC on purpose: the bucket a bar counts and the range its click
// applies are both UTC periods, so a local-time label would describe a window
// the bar does not contain. The page says "UTC" once in its caption.

/** Structural copy of api.ts `TimelineBucket` (kept local so this module stays
 *  importable without the browser-only request layer). */
export interface Bucket {
  start: string;
  start_epoch: number;
  end_epoch: number;
  count: number;
}

export type BucketUnit = "month" | "year";

/** Beyond this many columns the chart is unreadable anyway (and a 1601 or 1970
 *  junk mtime would otherwise produce thousands of empty bars): the list stays
 *  sparse and the page says gaps are not drawn. 100 years of months. */
export const MAX_FILLED_BUCKETS = 1200;

function nextBoundaryMs(ms: number, unit: BucketUnit): number {
  const d = new Date(ms);
  return unit === "year"
    ? Date.UTC(d.getUTCFullYear() + 1, 0, 1)
    : Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1);
}

function spanCount(firstMs: number, lastMs: number, unit: BucketUnit): number {
  const a = new Date(firstMs);
  const b = new Date(lastMs);
  const years = b.getUTCFullYear() - a.getUTCFullYear();
  return unit === "year" ? years + 1 : years * 12 + (b.getUTCMonth() - a.getUTCMonth()) + 1;
}

/** Zero-fill missing buckets between the earliest and latest one. Real buckets
 *  are returned as the same objects; synthesized ones have `count: 0`. When the
 *  span exceeds MAX_FILLED_BUCKETS the sorted sparse list comes back with
 *  `sparse: true`. */
export function fillTimeline(
  buckets: readonly Bucket[],
  unit: BucketUnit,
): { buckets: Bucket[]; sparse: boolean } {
  const sorted = [...buckets].sort((a, b) => a.start_epoch - b.start_epoch);
  if (sorted.length < 2) return { buckets: sorted, sparse: false };
  const firstMs = sorted[0].start_epoch * 1000;
  const lastMs = sorted[sorted.length - 1].start_epoch * 1000;
  if (spanCount(firstMs, lastMs, unit) > MAX_FILLED_BUCKETS) {
    return { buckets: sorted, sparse: true };
  }
  const byStart = new Map(sorted.map((b) => [b.start_epoch, b] as const));
  const out: Bucket[] = [];
  for (let ms = firstMs; ms <= lastMs; ms = nextBoundaryMs(ms, unit)) {
    const real = byStart.get(ms / 1000);
    if (real) {
      out.push(real);
      continue;
    }
    const end = nextBoundaryMs(ms, unit);
    out.push({
      start: new Date(ms).toISOString(),
      start_epoch: ms / 1000,
      end_epoch: end / 1000,
      count: 0,
    });
  }
  return { buckets: out, sparse: false };
}

/** The bucket's UTC period: `2026-03` (month) or `2026` (year). */
export function bucketLabel(startEpoch: number, unit: BucketUnit): string {
  const d = new Date(startEpoch * 1000);
  const y = String(d.getUTCFullYear());
  return unit === "year" ? y : `${y}-${String(d.getUTCMonth() + 1).padStart(2, "0")}`;
}

/** A count that fits a 32px column: verbatim below 1,000, else compact
 *  (`12.3K`, `123K`, `1.09M`). The full value goes in the bar's title. */
export function compactCount(n: number, locale?: string): string {
  if (Math.abs(n) < 1000) return String(n);
  return new Intl.NumberFormat(locale, { notation: "compact", maximumSignificantDigits: 3 }).format(n);
}
