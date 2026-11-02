// Following one "Back up now" job through the maintenance registry until it
// ends. DOM-free so it runs on Node's test runner (tests/backupTrack.node.test.ts).
//
// The registry's `backup_now.last_run` is the newest job row for the task. A
// failed fetch, an empty registry (the projection degrades to no `last_run`)
// or a row OLDER than our job (not caught up yet) tells us nothing, so it is
// "unknown" and polling continues; only a terminal status on our job (or a
// newer one) is an outcome. Too many unknowns in a row is reported as lost
// rather than guessed at.

/** Consecutive unknown checks before giving up (× the 3 s poll ≈ 30 s). */
export const BACKUP_MAX_UNKNOWN = 10;

export type BackupTrack = {
  /** The job we follow; null after a 409 "already queued" (no id returned). */
  jobId: number | null;
  /** Consecutive checks that told us nothing. */
  unknown: number;
};

export type BackupFetch =
  | { ok: false }
  | { ok: true; lastRun: { job_id: number; status: string } | null | undefined };

export type BackupVerdict =
  | { kind: "pending"; track: BackupTrack }
  | { kind: "finished" }
  | { kind: "failed" }
  | { kind: "cancelled" }
  | { kind: "ended"; status: string }
  | { kind: "lost"; checks: number };

export function startTrack(jobId: number | null): BackupTrack {
  return { jobId, unknown: 0 };
}

/** Procrastinate states that are still waiting, running or winding down. */
const IN_PROGRESS = new Set(["todo", "doing", "aborting"]);

export function assessBackup(track: BackupTrack, fetched: BackupFetch): BackupVerdict {
  const run = fetched.ok ? fetched.lastRun : null;
  if (!run || (track.jobId !== null && run.job_id < track.jobId)) {
    const unknown = track.unknown + 1;
    return unknown >= BACKUP_MAX_UNKNOWN
      ? { kind: "lost", checks: unknown }
      : { kind: "pending", track: { ...track, unknown } };
  }
  if (IN_PROGRESS.has(run.status)) return { kind: "pending", track: { jobId: run.job_id, unknown: 0 } };
  if (run.status === "succeeded") return { kind: "finished" };
  if (run.status === "failed") return { kind: "failed" };
  if (run.status === "cancelled" || run.status === "aborted") return { kind: "cancelled" };
  return { kind: "ended", status: run.status };
}
