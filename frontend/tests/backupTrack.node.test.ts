// backupTrack: following one backup job through the maintenance registry
// until it ends, without ever inventing an outcome (P7 review item 8).

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  BACKUP_MAX_UNKNOWN,
  assessBackup,
  startTrack,
  type BackupFetch,
  type BackupTrack,
} from "../src/lib/backupTrack.ts";

const run = (job_id: number, status: string): BackupFetch => ({ ok: true, lastRun: { job_id, status } });

test("queued / running / aborting keep polling and reset the unknown count", () => {
  for (const status of ["todo", "doing", "aborting"]) {
    const v = assessBackup({ jobId: 7, unknown: 3 }, run(7, status));
    assert.deepEqual(v, { kind: "pending", track: { jobId: 7, unknown: 0 } }, status);
  }
});

test("terminal states are reported as what they are", () => {
  assert.equal(assessBackup(startTrack(7), run(7, "succeeded")).kind, "finished");
  assert.equal(assessBackup(startTrack(7), run(7, "failed")).kind, "failed");
  assert.equal(assessBackup(startTrack(7), run(7, "cancelled")).kind, "cancelled");
  assert.equal(assessBackup(startTrack(7), run(7, "aborted")).kind, "cancelled");
  assert.deepEqual(assessBackup(startTrack(7), run(7, "weird")), { kind: "ended", status: "weird" });
});

test("a failed fetch or an empty registry is unknown, never 'finished'", () => {
  for (const f of [{ ok: false } as BackupFetch, { ok: true, lastRun: null } as BackupFetch]) {
    const v = assessBackup(startTrack(7), f);
    assert.deepEqual(v, { kind: "pending", track: { jobId: 7, unknown: 1 } });
  }
});

test("an OLDER run in the registry (not caught up yet) is unknown, not our outcome", () => {
  const v = assessBackup(startTrack(7), run(6, "succeeded"));
  assert.deepEqual(v, { kind: "pending", track: { jobId: 7, unknown: 1 } });
});

test("a NEWER run is followed (ours ended and another started)", () => {
  assert.deepEqual(assessBackup(startTrack(7), run(9, "doing")), {
    kind: "pending",
    track: { jobId: 9, unknown: 0 },
  });
});

test("no job id (a 409 'already queued') follows whatever the registry shows", () => {
  assert.deepEqual(assessBackup(startTrack(null), run(4, "doing")), {
    kind: "pending",
    track: { jobId: 4, unknown: 0 },
  });
  assert.equal(assessBackup(startTrack(null), run(4, "succeeded")).kind, "finished");
});

test(`${BACKUP_MAX_UNKNOWN} unknown checks in a row → lost (an error), not a hang`, () => {
  let t: BackupTrack = startTrack(7);
  for (let i = 1; i < BACKUP_MAX_UNKNOWN; i++) {
    const v = assessBackup(t, { ok: false });
    assert.equal(v.kind, "pending");
    if (v.kind === "pending") t = v.track;
  }
  assert.deepEqual(assessBackup(t, { ok: false }), { kind: "lost", checks: BACKUP_MAX_UNKNOWN });
});
