// Library hard-delete: what the typed-name confirm lists (B16, Ruling 7) and
// when the delete is not offered at all (B17: a queued/running scan job makes
// DELETE /libraries/{id} 409). Pure; tested on Node.

import type { LibraryDeleteImpact } from "./api";

const n = (v: number, locale?: string): string => v.toLocaleString(locale);
const count = (v: number, one: string, many: string, locale?: string): string =>
  `${n(v, locale)} ${v === 1 ? one : many}`;

/** The confirm's consequence bullets: every non-zero count, the pending-jobs
 *  warning, and the irreversibility line. `null` = the impact fetch failed —
 *  delete is still allowed, but the counts are flagged as unavailable. */
export function deleteImpactDetails(impact: LibraryDeleteImpact | null, locale?: string): string[] {
  if (!impact) {
    return [
      "Counts unavailable: could not load what this delete removes",
      "All indexed items, scan history, library alert rules, grants and hot folders are removed",
      "This cannot be undone",
    ];
  }
  const out: string[] = [];
  const { items, scan_runs, alert_rules, alert_events, path_grants, scan_paths, pending_jobs } = impact;
  if (items > 0) {
    out.push(
      items === 1
        ? "1 indexed item (its search result disappears)"
        : `${n(items, locale)} indexed items (their search results disappear)`,
    );
  }
  if (scan_runs > 0) out.push(count(scan_runs, "scan run", "scan runs", locale));
  if (alert_rules > 0) {
    const rules = count(alert_rules, "alert rule", "alert rules", locale);
    out.push(
      alert_events > 0
        ? `${rules} and ${alert_rules === 1 ? "its" : "their"} ${count(alert_events, "delivery event", "delivery events", locale)}`
        : rules,
    );
  }
  if (path_grants > 0) out.push(count(path_grants, "permission grant", "permission grants", locale));
  if (scan_paths > 0) out.push(count(scan_paths, "hot folder", "hot folders", locale));
  if (out.length === 0) {
    out.push("Only the library definition: it has no indexed items, scan runs, alert rules, grants or hot folders");
  }
  if (pending_jobs > 0) {
    out.push(`${count(pending_jobs, "pending job", "pending jobs", locale)} for this library will fail or be dropped`);
  }
  out.push("This cannot be undone");
  return out;
}

/** Why delete is not offered right now, or null. A scan job that is queued or
 *  running makes the server refuse (409), so say so before the typed confirm. */
export function deleteBlockedReason(impact: LibraryDeleteImpact | null): string | null {
  if (!impact?.scan_job_pending) return null;
  return `${impact.name} can't be deleted while a scan is queued or running. Stop or cancel the scan first, then delete.`;
}

/** Appended to the success toast: the Meilisearch prune is a deferred job. */
export const DELETE_SUCCESS_NOTE = "The search index is being cleaned up in the background.";
