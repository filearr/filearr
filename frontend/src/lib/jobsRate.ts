// Rolling completion rate and backlog ETA for the Jobs page (B47). Pure and
// DOM-free, so it is Node-tested.
//
// The counters fed in are counts of `succeeded` rows in procrastinate_jobs,
// which `purge_job_history` trims daily: a counter that goes DOWN is that
// purge, not a stall, so the window restarts at the new value (rate unknown,
// "—", rather than "stalled"). Samples are taken on poll ticks, which pause
// while the tab is hidden; a sample further than `maxGapMs` after the last one
// restarts the window too, so the first rate after returning is not an average
// across the hidden gap.

import { fmtDuration } from "./format.ts";

export type RateSample = { t: number; done: number };
export type RateOpts = { window: number; maxGapMs: number };

/** A new sample list with `{t, done}` appended (input untouched). */
export function pushSample(
  samples: readonly RateSample[],
  t: number,
  done: number,
  { window, maxGapMs }: RateOpts,
): RateSample[] {
  const last = samples[samples.length - 1];
  if (!last || done < last.done || t - last.t > maxGapMs) return [{ t, done }];
  const out = [...samples, { t, done }];
  return out.length > window ? out.slice(out.length - window) : out;
}

/** Per-second rate over the window: null with < 2 samples or no elapsed time;
 *  0 when the counter is flat (stalled). */
export function rateOf(samples: readonly RateSample[]): number | null {
  if (samples.length < 2) return null;
  const first = samples[0];
  const last = samples[samples.length - 1];
  const dt = (last.t - first.t) / 1000;
  if (dt <= 0) return null;
  return Math.max(0, last.done - first.done) / dt;
}

/** Backlog ETA: "0" when nothing is queued, "—" when the rate is unknown,
 *  "stalled" at a zero rate, else depth / rate as a duration. */
export function queueEta(depth: number, rate: number | null): string {
  if (depth <= 0) return "0";
  if (rate == null) return "—";
  if (rate <= 0) return "stalled";
  return fmtDuration(depth / rate);
}
