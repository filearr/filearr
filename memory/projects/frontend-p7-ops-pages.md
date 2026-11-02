# Frontend P7 — Ops pages correctness (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Date:** 2026-11-02 · **Rulings:** owner-accepted 2026-11-02 · **Spec:** `memory/projects/frontend-review-2.md` §"P7" (the B-series) + the **P7 checklist** in its P6 status line · **Findings detail:** `memory/projects/review-2/B-libraries-jobs-reports.md` (B1–B53; line numbers there are stale — the 2026-11-02 audit below is current).
**Branch:** `claude/frontend-p7-ops-pages` (off `main` after the build-identity and compat-removal commits).
**Goal:** Libraries, Jobs, Alerts, Reports/Exports/Schedules, Timeline and the treemap behave correctly under real use: saves never lose or clobber data, every action error is shown beside its action with the server's reason, scan state is right even with many scoped runs, schedules never silently drop or shift, downloads contain everything they promise, and pollers never fight each other.

**Architecture:** pure modules carry the logic with Node tests written first (`libraryPayload.ts`, `scheduleNext.ts` + schedule vectors test, `sse.ts`, `timelineFill.ts`, `reportQuery.ts`, `alertConfig.ts`, `jobsRate.ts`); pages become thin callers. **Backend changes are in scope this phase** (ruling 1).

## Audit (2026-11-02, current code)
- **Fixed already:** B1 (typed numbers; no pure payload test yet), B8, B13, B20 (rounding; invalid still silent), B25. configCache invalidation and Thumb `category`/`filename` checklist items are satisfied.
- **Partial:** B17 (frontend done; backend guard/prune open), B31 (dirty check done; full-form PATCH open), B37, B40, B42, B44, B49, B53, format.ts sweep (dates still `toLocaleString()` in Exports/Alerts/Diagnose/Libraries/Jobs; `ArchiveSection` imports `cards/format`).
- **Open:** B2, B3, B4, B6, B7, B9, B10, B11, B12, B15, B16, B18, B19, B21, B22, B23, B24, B26, B27, B30, B32, B34, B35, B36, B38, B39, B41, B43, B45, B47, B48, B50; 429 `Retry-After` (headers discarded on non-2xx; exports 429 sends none).
- **Answers to the report's open questions:** (1) `enabled_extension_groups` changes the provenance string but nothing compares full fingerprints → no stale-hash banner; the column is dead. (2) `Library.enabled=false` suppresses cron scans (incl. hot-folder crons), the watch supervisor and upcoming-scans; NOT manual/targeted scans or search. (4) failed `ScanRun.stats.error` is set by the crash handler and stalled reaper only; the orphan reconciler writes `reconcile_note`, trigger_scan's pre-defer reap writes nothing. (5) `GitIgnoreSpec.from_lines` raises on `\`, `foo\`, `!`, `**/[z-a]` — library globs are never validated, so a bad glob fails every scan.

## Rulings (orchestrator)
1. **Backend fixes ride in this branch.** Single deployment, one image: the frontend and its backend contract change together, no forward-compat shims, no "older backend" tolerance. Each backend change has pytest coverage.
2. **Extension groups are deleted end to end (B6):** UI section, `ALL_TYPES`, help text, the `enabled_extension_groups` column (alembic drop), schema fields and the provenance canonical key. Provenance `_SCHEME` is NOT bumped (stale detection keys on the scheme prefix only; nothing compares full fingerprints, verified).
3. **Include globs (B7): wording, not semantics.** The field is relabelled "Re-include (overrides excludes)" with corrected help (excludes first, includes re-include, cannot re-include below an excluded directory). Library globs are validated at the API with the same `GitIgnoreSpec` the scan uses → 422 naming the bad line. Allow-list semantics would re-scope existing libraries silently; out of scope.
4. **`Library.enabled` (B45):** copy states the real behaviour (cron, hot-folder cron and watch are paused; manual and targeted scans still run). No backend behaviour change.
5. **Scan list (B9/B10):** `GET /scans` gains `status` (comma list), `library_id`, `limit` (≤500) and `ScanOut.rel_path`; Libraries polls `/libraries` + `GET /scans?status=running,stopping` and reads history on demand. `LibraryOut.last_scan` = newest **full** run; `last_scoped_scan` added. `slowScan` uses full runs only.
6. **Failed reason (B11):** the row shows SSE `error.detail` when captured, else `stats.error ?? stats.reconcile_note`, else "failed (no reason recorded)". trigger_scan's pre-defer reap writes `stats.error` too.
7. **Delete (B16/B17):** `GET /libraries/{id}/delete-impact` returns counts (items, scans, alert rules + events, path grants, hot folders, pending jobs); the confirm lists them. `delete_library` refuses (409) while a scan job is queued/doing; the Meili prune becomes a deferred job.
8. **One SSE helper:** `sse.ts` `connectSse({ mint, url, onFrame, onTerminal, signal })` modelled on RetrievePanel's P6 guards — destroyed/stale check after the awaited mint, data-bearing `error` = terminal, one owner of the backoff timer (capped exponential, cancelled by `signal`). Libraries and RetrievePanel both use it.
9. **Retry-After:** `ApiError` gains `retryAfter: number | null` parsed from the header on every non-2xx; the 429 sentence includes it ("try again in N s"). Backend exports 429 sends `Retry-After`.
10. **Schedules:** monthly mode refuses a local time whose UTC conversion crosses midnight (inline error + "use Advanced") rather than storing a shifted day; weekly with no day blocks Save; hourly minute is rounded+clamped; a pure `nextRuns(cron, n, now)` gives a 3-run local preview under every schedule field (library + report schedules). `shared/schedule-vectors.json` gets a frontend node test.
11. **Report downloads (B2):** `reportQuery(meta, opts)` sends `limit` only when `meta.is_capped`. Custom reports get a background export via the existing `enqueueCustomReportExport`.

## Global constraints
- Svelte 5 runes, Tailwind 4, bits-ui 2.18. No new npm dependencies. **Never add `svelte-ignore`.** Tokens only; kit components for every control/state (`Field`, `Alert` beside the action, `StateBlock`, `Badge`/`toneForStatus`, `confirm()`/`promptText()`, `Modal` with `dirty`).
- Every task leaves green, from `frontend/`: `npm run check` (0/0) · `npm run check:tests` · `npm test` · `npm run build`; and for backend tasks, from `backend/`: the touched pytest modules + `uv run ruff check filearr tests` (format only touched files). Full backend suite at the end of Task 1 and at acceptance.
- Pure logic first with Node tests (RED → GREEN evidence in the report). No `{@html}`.
- Every date/byte/duration through `format.ts`; after Task 8: `grep -rn "toLocaleString()\|toLocaleDateString()\|toLocaleTimeString()" src/lib --include=*.svelte` → 0 for the ops pages.
- Each task: one commit, `frontend:` or `backend:` prefix.

## Review focus
1. Editing a library while the scheduler flips `enabled` must not revert the flip (changed-fields PATCH). Duplicate name → inline Name error, not a banner.
2. With 60 scoped runs newer than a running full scan, its row still shows Stop/Cancel and a live stream.
3. A failed scan shows its reason on the row without opening Diagnose.
4. A monthly 01:00 schedule in UTC+2 is refused with an explanation, never stored as day 31.
5. A 5,000-row canned report download contains 5,000 rows.

---

## Task 1: Backend contract fixes
**Files:** `backend/filearr/api/libraries.py`, `schemas.py`, `api/scans.py`, `api/alerts.py`, `api/exports.py`, `presets.py` (glob validation helper), `provenance.py`, `models.py`, `tasks/archives.py`, new alembic revision, worker task for deferred Meili prune; tests per item.
- [x] B3: `IntegrityError` on create/rename → 409 `a library named "<name>" already exists`.
- [x] B4: `LibraryIn`/`LibraryUpdate` strip + `min_length=1` on `name`/`root_path`; globs validated via `GitIgnoreSpec` → 422 with the offending line.
- [x] B6: drop `enabled_extension_groups` (model, schemas, provenance canonical, presets superseded helper, alembic drop).
- [x] B9/B10: `/scans` filters + `rel_path`; `last_scan` full-only + `last_scoped_scan`.
- [x] B11: pre-defer reap writes `stats.error`.
- [x] B16/B17: `delete-impact` route; queued/doing scan-job guard; deferred Meili prune.
- [x] B23: channel PATCH merges `config` (secrets keep `SECRET_UNCHANGED` semantics); empty port → 587 server-side.
- [x] B24: `/alert-events` `offset`.
- [x] B50: archive listing sets `truncated` when capped.
- [x] Exports 429 sends `Retry-After`.
- [x] Full backend suite green (3563 passed). Commit `backend: P7 contract fixes for libraries, scans, alerts, exports` (f108122).

## Task 2: Library form — `libraryPayload.ts`
**Files:** `src/lib/libraryPayload.ts` (new) + test, `LibraryEditModal.svelte`, `help.ts`, `api.ts` types.
- [x] `buildLibraryPayload(draft, original?)` pure: trims, blanks → field errors, numbers typed, round only byte fields, invalid overrides flagged (not dropped), **changed fields only** on edit (B1 test, B4, B20, B31).
- [x] 409 → Name field error; 422 `loc` → field error; banner beside Save.
- [x] Extension groups section removed; include-glob relabel + help; Enabled copy (B6, B7, B45).
- [x] Commit `frontend: library form payload, field errors, changed-fields PATCH` (c375c7e). 28 new node tests (589 total).

## Task 3: Schedules — vectors, guards, next runs
**Files:** `src/lib/schedule.ts`, `src/lib/scheduleNext.ts` (new) + tests (incl. `shared/schedule-vectors.json`), `ScheduleField.svelte`, `LibraryEditModal.svelte`, `ReportSchedulesPanel.svelte`.
- [x] B18 (weekly no-day blocks Save), B19 (monthly midnight crossing refused), B32 (minute rounding, 3-run preview).
- [x] Commit `frontend: schedule guards, vector tests and next-run preview` (d922fa8). 159 new node tests (748 total); shared vectors 20 → 28 (`refused` tag on day-shifting monthly; backend port mirrors the Advanced parse, 114 passed). ScheduleField `onValidity` prop; preview only in fixed-UTC mode (AgentsPage verbatim crons hidden: `preview` prop to opt in).

## Task 4: `sse.ts` + Libraries scan lifecycle
**Files:** `src/lib/sse.ts` (new) + test (fake EventSource/timers), `LibrariesPage.svelte`, `RetrievePanel.svelte`, `api.ts` (`ScanRun.stats: Record<string, unknown>`, `listScans` filters).
- [x] `connectSse` (ruling 8); RetrievePanel ported with no behaviour change (fixed 2 s reconnect kept via `backoff: {2000, 2000}`; data `error` now closes instead of waiting for `done` — same final state).
- [x] B11 reason on row, B12 Stop/Cancel/Force-clear errors + Cancel confirm, B43, B9/B10 consumption, B15 poll split (heavy aggregates on mount + refresh button), B34 typed stats + locale numbers, B35 errors pager + `retried`, B36 retry-all when degraded, B53 failed-jobs catch.
- [x] Commit `frontend: one SSE helper; Libraries scan state, errors and polling` (d9f2bad). Row logic in `libraryScanState.ts`; 28 new node tests (776 total). Failed jobs + failing lists are no longer on the 30 s poll (refresh button / actions reload them). Browser smoke skipped (no local stack).

## Task 5: Library delete impact
- [x] Confirm lists `delete-impact` counts; 409 queued-scan message; 404 = done (kept).
- [x] Commit `frontend: library delete lists what it removes` (5635769). Pure `libraryDelete.ts` + 9 node tests (785 total). `scan_job_pending` → delete not offered, reason beside the row's actions; impact fetch failure → delete allowed, "Counts unavailable" bullet; delete errors per library beside the action (table-level banner removed); success toast notes background index cleanup.

## Task 6: Reports, exports, schedules
**Files:** `src/lib/reportQuery.ts` (new) + test, `ReportsPage.svelte`, `ExportsPanel.svelte`, `CustomReportsPage.svelte`, `ReportSchedulesPanel.svelte`, `apiErrors.ts`/`api.ts` (`retryAfter`).
- [x] B2, B21 (refresh after queue + idle slow poll; report title not key), B40 (independent loads, object cells, download cancel), B41 (sequenced validate, `vOk` starts false, dead default-format removed, custom background export), B30 (params carried, Edit, human schedule + next runs), B53 (per-call catch), ruling 9.
- [x] Commit `frontend: reports download everything; exports, schedules and custom reports fixed` (182221d). 15 new node tests (800 total). `reportQuery`/`reportScheduleParams` pure; `ApiError.retryAfter` + `parseRetryAfter`; ExportsPanel `refresh()` via bind:this, 4 s/60 s single ticker; custom-report format omitted (API defaults csv); schedule Edit panel uses `rowPanel` focus; browser smoke skipped (no local stack).

## Task 7: Alerts
**Files:** `src/lib/alertConfig.ts` (new) + test, `AlertsPage.svelte`.
- [x] B22 summary on load + slow poll; B23 changed-keys config, "plain" opt-in checkbox → `allow_insecure`, port default; B24 pager; B42 NaN/null guards, bulk replace confirm + result, edit focus via `rowPanel`.
- [x] Commit `frontend: alerts summary, channel config merge, events pager` (252dc39). 26 new node tests (826 total). `alertConfig.ts` diffs the form against the stored config seen through the same form (legacy `from`/`to_addrs` not re-sent); emptied keys → `null` on edit (cleared stored port → 587), omitted on create; `allow_insecure` owned only while plain (leaving plain keeps a stored opt-in; plain + unticked = field error). Cleared group wait / inhibit window → explicit 30 / 900. `detectWebhookFormat` moved to `alertConfig.ts` (re-exported from api.ts). Events: "Load 100 more" via offset, total only without a rule filter (summary is per-status, unfiltered by rule); item path NOT shown (no batch item lookup; would be N `getItem` calls per page). Browser smoke skipped (no local stack).

## Task 8: Jobs + format sweep
**Files:** `src/lib/jobsRate.ts` (new) + test, `JobsPage.svelte`, plus the date sweep across ops pages.
- [x] B26 `refresh({ force })` with one trailing run; B27 backups decoupled + poll while a backup job runs; B37 copy; B47 rate across purge/hidden gaps; B48 priority input; B49 cron validation + blank = explicit reset confirm + already-queued; dates → `format.ts`.
- [x] Commit `frontend: jobs refresh, backups, rate maths; dates through format.ts` (b756bba). 20 new node tests (856 total). `refreshGate.ts` (plain call joins the in-flight run, forced call = one trailing run, resolves after it). `jobsRate.ts` (`pushSample`/`rateOf`/`queueEta`; falling counter or gap > 3 polls restarts the window → "—"). `scheduleNext.cronProblem` (6-field crons left to the server: cronsim accepts a seconds field nextRuns does not model). Backups: own throttle + error; poll every 3 s while `backup_now` last_run is todo/doing (maintenance registry), reporting finished/failed. `maintRun`/backup 409 → server detail as a note. `format.fmtClock` (HH:mm:ss) added for the log tail; `relTime(x, now)` replaces relFuture (coarser: "in 2 h"). Sweep also covered Agents/Users/Sessions/Audit/keys/AuthProviders/ItemDetail/Updates; remaining `toLocaleString()` hits are numbers only. AgentsPage `untilTime` countdown kept (deliberately finer). Backend: failed_jobs_view docstring corrected (no behaviour change). Browser smoke skipped (no local stack).

## Task 9: Timeline, treemap, archive
**Files:** `src/lib/timelineFill.ts` (new) + test, `TimelinePage.svelte`, `FolderTreemap.svelte`, `ArchiveSection.svelte`, `LibraryDiagnoseModal.svelte`.
- [x] B38 zero-fill (cap 1200 columns, sparse note beyond); labels kept UTC (backend buckets with `date_trunc … AT TIME ZONE 'UTC'` and the click range is that UTC window, so local labels would misdescribe the bar) with "Periods are UTC calendar months/years" caption; compact counts; refetch indicator; sidecar-exclusion caption. B39 `partitionDrawable` + "N more small folders" list; trail committed with data. B50 "showing N of M members (list capped)". B53 diagnose timer + `safeHref` (http/https + same-origin path) + `fmtDateTime`.
- [x] Commit `frontend: timeline zero-fill, treemap small folders, archive truncation` (772456a). 10 new node tests (836 isolated / 856 with Task 8 WIP).

## Task 10: Acceptance
- [ ] Browser flows on a local stack for the five review-focus items + each task's flows; axe 0 new vs P6.
- [x] Whole-branch review (two halves: backend+libraries; reports/alerts/jobs/timeline). No critical findings; 1 important (null PATCH on NOT NULL column → false duplicate-name 409) + 9 minor/security fixed in f8a18b9/036a87b: 409 only for `libraries_name_key`, explicit null on NOT NULL library fields → 422; alert channel edits that move a stored secret (email host/port/security, webhook scheme/host/port) must re-enter or clear it; `/alert-events` keyset cursor (`before_ts`+`before_id`, offset removed); Libraries `destroyed` guard; glob lines verbatim; Audit/Sessions seconds; schedule edits keep stored params; `backupTrack.ts`; ExportsPanel lookup memo; events loading flags. Suites after fixes: backend 3640, frontend 871.
- [x] Update `frontend-review-2.md` status line with the P7 outcome and the P8 checklist.
