# Review B — Libraries / Jobs / Alerts / Timeline / Reports / Exports

Scope: `frontend/src/lib/{LibrariesPage,LibraryEditModal,LibraryDiagnoseModal,DeleteLibraryDialog,DiagnoseSection,FolderPicker,ScheduleField,JobsPage,AlertsPage,TimelinePage,ReportsPage,CustomReportsPage,ReportSchedulesPanel,ExportsPanel,FolderTreemap,ArchiveSection,RetrievePanel}.svelte`, `treemap.ts`, `polling.ts`, `schedule.ts`, and the `api.ts` calls they make, against the backend routers/schemas. Line numbers are as of branch `main` today. Read-only review; nothing was modified (one scratch script was run from the scratchpad to confirm the cron findings).

## Summary
- 2 critical, 35 important, 16 minor findings (B1-B53). The two criticals are a guaranteed Save crash in the library form and silent truncation of report downloads.
- Contract drift is small (types match schemas); the damage is in behaviour: numeric `bind:value` misuse, dead controls (extension groups, include globs, "Restart", "plain" SMTP), unhandled uniqueness errors, and undisclosed cascades on delete.
- Cross-cutting: every error is `String(e)` of an `ApiError` (`"ApiError: 422: {"detail":…}"`), no `detail` parsing, no 401 handling, no request timeout/abort, no response sequencing; errors are wiped by the next poll.
- Real-time paths (scan SSE, jobs polling) are mostly well built, but they depend on `GET /scans` (hard-capped at 50 rows across all libraries), and a few races/leaks remain.
- DOM-free unit coverage in scope is only `polling.ts` and `treemap.ts`; `schedule.ts` (cron local/UTC) has no frontend test even though it claims parity with backend vectors.

## Findings

### Critical

**B1 · Library form crashes on Save as soon as a numeric field is touched** · `LibraryEditModal.svelte:183, 255, 280` (inputs at ~`:520-540`, `:545-560`)
- What happens: `hashCeiling` and every `overrideInputs[...]` value are `$state` strings bound with `bind:value` to `<input type="number">`. In Svelte 5 a bound number input writes a **number** (or `null` when emptied) — `node_modules/svelte/src/internal/client/dom/elements/bindings/input.js:30,74,287` (`to_number`: `'' → null`, else `+value`). `save()` then calls `hashCeiling.trim()` (`:255` create, `:280` edit) and `(overrideInputs[f.key] ?? "").trim()` (`:183`). A number or `null` has no `.trim` → `TypeError: hashCeiling.trim is not a function` / `… .trim is not a function`, caught by the `catch` and shown as a red line at the TOP of the modal (see B5), far above the Save button.
- Why it matters: it is impossible to set, change or clear the full-hash ceiling or any of the 8 extraction limits from the UI. An untouched field still works (it is still the seeded string), which is why this survives casual testing. There is no component test.
- Fix: keep the draft as `number | null` and convert at the boundary (`const n = hashCeiling == null ? null : Number(hashCeiling)`), or bind to `type="text" inputmode="numeric"` and parse. Validate integer > 0 for the ceiling (`hash_full_max_bytes` is `int, gt=0`; `1.5` currently gives a 422) and positive finite for overrides.
- Test idea: extract `buildLibraryPayload(draft)` (pure) and test number/null/string inputs for each field.

**B2 · "Download" on a canned report is silently capped at one page of rows** · `ReportsPage.svelte:157` (`limit: pageSize`), `api.ts:2191-2202` (`downloadReport`), `backend/filearr/api/reports.py:126-150` (`_stream_params`), `reports.py:1894-2039` (`default_limit=1000`)
- What happens: the streaming endpoint treats `limit` as an optional ROW CAP on non-capped reports ("omit `limit` = whole result", `reports.py:~22-24`). The UI passes `limit: pageSize` (= `selected.default_limit`, 1000 for most reports), so the CSV/NDJSON/XML/XLSX contains at most 1000 rows, while the UI, the `_FORMAT_DOC` and the page header comment ("export the full result") promise everything. Nothing tells the user the file is truncated.
- Why it matters: a duplicate-files or stale-files export fed to a cleanup script is incomplete with no warning. (The background export path, `queueBackground`, correctly sends no limit, so the two paths disagree.)
- Fix: drop `limit` from `downloadReport` for non-capped reports (keep it only if `meta.is_capped`), or send nothing and let the server decide.
- Test idea: unit-test a small `downloadQuery(meta, opts)` helper: `is_capped=false` → no `limit` param.

### Important

**B3 · Creating or renaming a library to an existing name returns a bare 500** · `backend/filearr/api/libraries.py:352-367, 371-466`; `models.py:110` (`name … unique=True`)
- `create_library` and `update_library` commit with no `IntegrityError` handling (other routers — custom_reports, alerts, saved_searches — do map it to 409). The user sees `ApiError: 500: Internal Server Error` (not JSON) at the top of the modal (B5). Same for a PATCH rename.
- Fix: catch `IntegrityError` → `HTTPException(409, "a library named … already exists")`; map 409 to an inline Name error. Also consider rejecting a `root_path` already used by another library.

**B4 · Edit path allows blank name/root_path and sends them untrimmed; no root validation anywhere** · `LibraryEditModal.svelte:617` (button disabled only when `creating`), `:268-269` vs create `:244-245` (`.trim()`); backend `schemas.py:33-34` (`name: str`, `root_path: str`, no `min_length`), `libraries.py:352-367` (no existence/dir/readability check)
- Editing lets you save `name=""`/`root_path=""`/`"  foo "`. A trailing-space name later makes the typed-name delete confirmation (`DeleteLibraryDialog.svelte:20`, exact `===`, backend `libraries.py` exact match) unguessable since whitespace is invisible. Neither create nor edit verifies that the path exists (the folder picker is allow-listed, free text is not), so a typo gives a "successful" create followed by a red library or an N→0 scan guard.
- Fix: `min_length=1` + strip validators on `LibraryIn/LibraryUpdate`; apply the same `.trim()` and disabled-state on edit; optionally return a non-blocking warning from the API when the path does not exist.

**B5 · Save errors are rendered out of view and as raw `ApiError: 422: {json}`; no field mapping** · `LibraryEditModal.svelte:328` (error line) vs Save button `:617` (≈ 290 lines lower, inside a scrolling modal); `:297-301` (`error = String(e)`); same pattern at `AlertsPage.svelte:182,320`, `CustomReportsPage.svelte:197`, `ReportSchedulesPanel.svelte:62`, `LibrariesPage.svelte:222,…`
- The user scrolls to the bottom, clicks Save, and nothing visibly happens. When it does show, pydantic 422 bodies are `{"detail":[{"type":…,"loc":["body","hash_full_max_bytes"],"msg":…}]}` and are displayed verbatim; `loc` is never mapped to a field. `ApiError.message` is `"<status>: <body>"` and `String(e)` prefixes `ApiError: `.
- Fix: one shared `apiErrorMessage(e)` (parse `detail` string | pydantic list | `{validation:[…]}` object) in a DOM-free module; show the banner next to the Save button (and `scrollIntoView`/`role="alert"`); map `loc[-1]` to the field. `JobsPage.apiDetail` and `FolderTreemap.errDetail` already reimplement half of this (and `apiDetail` returns an array for pydantic 422s → "[object Object]", `JobsPage.svelte:261-270`).
- Test idea: table-driven tests for that helper (string detail, list detail, `{validation}`, non-JSON, network error).

**B6 · "Extension groups" section of the library form is a dead control** · `LibraryEditModal.svelte:585-610`, `help.ts:134-136`; `backend/filearr/presets.py:421-424` ("SUPERSEDED … no longer called by the scan walk"), `models.py:129-131`; only `provenance.py:78` reads the column
- The text says "Enabling a group narrows that media type to the listed extensions". The scan ignores `enabled_extension_groups` entirely (W8-B moved gating to taxonomy `enabled_groups`). Ticking boxes changes nothing about what is indexed, but does change the library's provenance fingerprint (`provenance.py:78`), which may flag hashes stale (see Questions). The hard-coded `ALL_TYPES` list (`:51-53`) is a leftover from the removed MediaType vocabulary.
- Fix: remove the section (and send `[]`), or relabel it as "legacy/no effect" until the backend drops the column.

**B7 · "Include globs" are not an allow-list, contradicting the help text and placeholder** · `help.ts:119-122` ("ONLY files matching an include glob are considered"), `LibraryEditModal.svelte` placeholder "(blank = include all)", `help.ts:124-127` ("Applied after includes"); `backend/filearr/presets.py:337-340, 402-407` (includes are appended as `!pattern` gitignore negations AFTER excludes)
- A user entering `**/*.mkv` expecting "only mkv" gets everything (negations only re-include things already excluded; they also cannot re-include below an excluded directory). The help text for exclude globs also has the order backwards. The UI neither validates glob syntax nor previews the effect; the library API does not validate globs either (alert rule globs are validated, `alerts.py:~222`).
- Fix: either implement allow-list semantics in the walk, or rename the field ("Re-include (override excludes)") and fix the help; add a "test a path against these rules" affordance using the same `GitIgnoreSpec`.

**B8 · "Restart" on a running scan does nothing** · `LibrariesPage.svelte:~968` (label), `:~486-503` (`runScan`), `api.ts:905-906` (typed `{job_id:number}`); `backend/filearr/api/libraries.py:803-814`
- While a scan is running/stopping the primary button says "Restart", but `trigger_scan` returns `{"job_id": null, "already_running": true}` (scan job exists in todo/doing) without restarting anything. The UI ignores `already_running`, flips the row to "queued" (`isQueued`) and gives no feedback. After a "Cancel (abort)" the job may still be `doing`, so the next "Scan" click is also a silent no-op until the worker ends the job.
- Fix: label it "Scan" and disable while running, or surface "already running" via a toast; type the response as `{job_id:number|null; already_running?:boolean}`.

**B9 · Row state is derived from `GET /scans`, which is a 50-row global window** · `backend/filearr/api/scans.py:58` (`order_by(started_at.desc()).limit(50)`); `LibrariesPage.svelte:449-451` (`latestScan`), `:267-293` (`mergeScanRows`/`syncStreams`), `:~1152` (Recent scans), `schemas.py:499-506` (`ScanOut` has no `rel_path`)
- Hot-folder/targeted scans create many small `ScanRun`s. Once 50 newer runs exist, a still-running (or `stopping`) full scan of another library drops out of the list: no Stop/Cancel/Force-clear buttons, no live SSE stream, wrong `running`/`stopping` flags and `isQueued` logic. Recent-scans cannot show which runs were scoped because `rel_path` is not in `ScanOut`.
- Fix: add `?status=running,stopping` (or `library_id`) filters and `rel_path` to `ScanOut`; poll the running set separately.

**B10 · "Last scan" can be a scoped run, and the "slower than usual" badge then compares apples to oranges** · `backend/filearr/api/libraries.py:195-202` (DISTINCT ON over all runs) vs `:206-222` (median only `rel_path IS NULL`); `LibrariesPage.svelte:~560-575` (`slowScan`)
- With hot folders or "Targeted…" scans, `lib.last_scan` is the newest run of ANY scope, so counts show e.g. "seen 12, new 0" for a 500k-file library, and its small-subtree files/s is compared with the full-scan median.
- Fix: return `last_scan` (newest full) and `last_scoped_scan` separately, or at least include `rel_path` and have `slowScan` ignore scoped runs.

**B11 · A failed scan's reason never reaches the Libraries page** · `LibrariesPage.svelte:356-370` (SSE `error` handler); `backend/filearr/api/scans.py:379-384`
- For a failed scan the server sends `event: error {detail}` then `done`. The client treats any `error` event carrying `data` as a "server frame": `es.close(); closeStream(id); refresh(); return` — the following `done` is never read and `detail` is dropped. The row just says `failed`. Only the Diagnose dialog shows `scans[].error`.
- Fix: capture `ev.data` detail into per-scan state and show it under the status badge (and the `stats.error` already present on the row after the refresh).

**B12 · Stop / Cancel / Force-clear swallow every error and Cancel is unconfirmed** · `LibrariesPage.svelte:913, 919, 936` (`.catch(() => {})`)
- 403 (read-only key), 404, 409 ("not running", "still active; use stop") all vanish; the button just does nothing and the row refreshes unchanged. "Cancel (abort)" (discards the in-flight batch) fires immediately with no confirm while the adjacent "Clear failed" does confirm.
- Fix: route through the page error banner/toast; confirm the abort; show the server `detail` for 409.

**B13 · Concurrent, unsequenced `refresh()` calls overwrite newer state with older** · `LibrariesPage.svelte:217-258` (`refresh`), `:398-402` (`failedPage`), callers: 30 s poll, SSE `done`, `pollUntilStarted` (5×), every action; `JobsPage.svelte:360-382` (`refreshFailures`/`failuresPage`); `ReportsPage.svelte:151-170` (`load`), `TimelinePage.svelte:30-40`, `FolderTreemap.svelte:80-96`, `AlertsPage.svelte:453-470`, `FolderPicker.svelte:20-30`
- None of these tag requests with a generation id or abort the previous one. Concrete case: click "Next" on the failed-jobs pager while the 30 s poll (started with the old `failedOffset`) is in flight → the poll's response lands last and replaces the rows with page 1 while the pager text says "26–50". In Reports, selecting report B right after A can render A's rows under B's title; in Timeline/Treemap quick filter/drill changes can show a stale level.
- Fix: a tiny `latest(fn)` helper (counter or `AbortController`) used by all `load()`s; pass `signal` through `request()`.
- Test idea: DOM-free helper with fake promises resolving out of order.

**B14 · Action errors are transient: every poll/refresh clears them** · `LibrariesPage.svelte:222` (`error = ""` at the top of `refresh`), `JobsPage.svelte:338` (every 4 s), `AlertsPage.svelte:464`, `ExportsPanel.svelte:35`
- `runScan`, `retryFailed`, `runRehash`, `clearFailed` etc. set `error`, and the next refresh (≤4 s on Jobs, immediate on the `await refresh()` right after in several handlers, e.g. `clearFailed` `:404-419`) erases it. `toggleMaintenance`, `clearFailed`, `reap` failures on Jobs vanish within 4 s.
- Fix: separate "load error" from "last action error"; keep action errors until dismissed or the next action.

**B15 · Libraries page polls two heavy aggregates every 30 s (and on every refresh)** · `LibrariesPage.svelte:224-227`; `backend/filearr/api/libraries.py:288-340` (`/libraries/hash-status`: full `GROUP BY` over `items` with `NOT LIKE`), `system.py:238-330` (`/stats`: by_type, queues, meili, semantic, thumbs, disk, key fingerprints)
- The page comment (`:60-64`) justifies keeping `/stats/libraries` OFF the poll as "a million-row sum every 30s is real DB load", yet `hash-status` is the same class of query and `/stats` runs ~7 sections, merely to read `extract_errors` and `degraded`. `refresh()` is also invoked 5× in the post-scan burst, after every SSE `done`, every pager click and every action. `listShareMap` (static deploy data) is refetched each time too.
- Fix: poll only `/libraries` + a light running-scans endpoint; fetch hash-status/stats on mount + button; add an `extract_errors`-only endpoint or cache.

**B16 · Deleting a library silently cascades to alert rules, RBAC path grants and hot folders, which the dialog never mentions** · `DeleteLibraryDialog.svelte:60-64` (copy mentions only "indexed items … scan history"); `backend/filearr/models.py:777` (scan_paths), `:913` (`alert_rules.library_id … CASCADE` — and their events), `:1596` (`path_grants`); `:997` (events SET NULL)
- Operators lose library-scoped alert rules (and delivery history) and permission grants with no warning; other rules' `inhibited_by` UUID arrays are left dangling.
- Fix: have the API return a pre-delete impact summary (`GET /libraries/{id}/delete-impact`) and list it in the dialog.

**B17 · Delete guard and request shape are weaker than the dialog implies** · `backend/filearr/api/libraries.py:738-766`; `DeleteLibraryDialog.svelte:22-47, 53-55`; `api.ts:832-843`
- The 409 only checks `ScanRun.status in (running, stopping)`; a scan job that is queued (`todo`, no ScanRun yet — exactly the state the UI shows as "queued") and pending extract/thumb/index jobs are not considered, so deleting leaves orphaned jobs that later fail into "Failed jobs". The whole delete (cascade of potentially millions of rows, Python list of every item id, Meili prune loop) runs inside ONE request; a proxy timeout shows an error while the delete continues, and the dialog (no `busy` guard on Escape/backdrop, `:53-55`) can be closed mid-request. A 404 (already deleted elsewhere) shows `Error: 404: {"detail":…}` and never calls `onDeleted`, so the dialog is stuck on a library that is gone. Error classification by `msg.includes("409:")` can match a library name containing `409:` in the 422 text.
- Fix: refuse when a scan job is pending too; run the Meili prune as a deferred job; treat 404 as success; use `ApiError.status` (deleteLibrary throws a plain `Error`, `api.ts:832-843`).

**B18 · A weekly schedule with no day selected silently saves "no schedule"** · `schedule.ts:125-128` (`days.length===0 → null`); `ScheduleField.svelte` (onChange → `scanCron = c ?? ""`); `LibraryEditModal.svelte:259,282`
- Switching an existing library to Weekly and unticking all days shows "Pick at least one day." but Save is enabled and the stored `scan_cron` becomes null; the schedule is dropped without any confirmation. (`ReportSchedulesPanel` guards this with a "schedule required" check; the library form does not.)
- Fix: surface a field error and disable Save while `mode !== "off"` and the generated cron is null.

**B19 · Monthly schedules shift day-of-month without regard to month length** · `schedule.ts:136-146`, `:79` (`wrapDom`)
- Verified with `generateCron`: local day 1 at 01:00 in UTC+2 → `0 23 31 * *` (fires on the 31st only — skipped in Feb/Apr/Jun/Sep/Nov); local day 31 at 23:30 in UTC−5 → `30 4 1 * *` (fires on the 1st of EVERY month, i.e. local last-day-of-previous-month, not "day 31"). The UI only warns that "Day 29-31 is skipped in months without that day".
- Fix: when the conversion crosses midnight for monthly mode, force Advanced (or refuse) with an explanatory message; or store a timezone and let the backend evaluate in-zone (`schedule.py:_due_occurrence_tz` already supports `tz`).
- Test idea: port `shared/schedule-vectors.json` to a frontend node test and add boundary-month vectors.

**B20 · Fractional extraction overrides are rounded on every save** · `LibraryEditModal.svelte:185` (`Math.round(n * f.scale)`), floats are legitimate for `ffprobe_timeout_s` and `doc_decompression_ratio` (`config.py:1519-1531`)
- A stored `doc_decompression_ratio` of 12.5 comes back as 13 on any unrelated save (the field is a seeded string, so B1 does not trip), and `ffprobe_timeout_s = 0.4` → `0` → backend 422. Overrides that fail the `Number.isFinite(n) && n > 0` check are dropped silently instead of flagged.
- Fix: round only for byte fields (`scale > 1`); flag invalid entries inline.

**B21 · Queueing a background export never refreshes the Exports panel** · `ReportsPage.svelte:200-216` (`queueBackground`), `ExportsPanel.svelte:19-21, 50-57` (polls only while `anyRunning`, which is false for an already-loaded empty/finished list)
- The banner says "see the Exports panel below", but the new export does not appear until the user finds and clicks the panel's Refresh (the panel sits below Custom reports and Scheduled reports). Scheduled/foreign exports likewise never appear automatically.
- Fix: shared store/event (`exports:changed`) or have the page pass a `bind:this` refresh; poll the list at a slow cadence even when idle.
- Related: `enqueueCustomReportExport` (`api.ts:2245`) is never used, so custom reports have no background export, though the sync download path is the memory-bound one.

**B22 · The "alerts failed to deliver" banner is empty until the Events tab has been opened** · `AlertsPage.svelte:447` (`summary` is only set in `loadEvents`), `:456`, `:504-514`, `$effect :472-481`
- On first load `summary` is `null`, so the "⚠ N alerts failed to deliver" call-out — the one place a broken channel is meant to be noticed — never renders on Channels/Rules, and it is never refreshed afterwards. Also the header counts ignore the active rule filter while the table is filtered.
- Fix: fetch `alertEventsSummary()` in `refresh()` and on a slow poll.

**B23 · Alert-channel edit is lossy and the "plain (insecure)" SMTP option can never work** · `AlertsPage.svelte:155-185` (`buildConfig`), `:624` (option); `backend/filearr/alerts/dispatch.py:326, 332-338`; `alerts.py:~330-345` (PATCH replaces `config` wholesale)
- Backend refuses `security="plain"` unless `config.allow_insecure` is true; the UI offers "plain (insecure)" but has no way to send `allow_insecure`, so such a channel always fails ("refusing plaintext SMTP"). Because PATCH replaces `config`, saving an edit also drops any key the UI doesn't know (e.g. an `allow_insecure` set via API). A cleared Port (number input → `null`) is sent as `port: null` and crashes `int(config.get("port", 587))` at delivery. There is also no way to REMOVE a stored secret (blank always means "keep").
- Fix: send only changed keys (merge server-side) or round-trip unknown keys; hide/disable "plain" or add the explicit opt-in checkbox; default empty port to 587.

**B24 · Alert events: 100-row window, no pagination, no file shown, no retry** · `api.ts:1938-1948` (`limit` default 100, no `offset`), `alerts.py:~680-720` (`limit ≤ 200`, no `offset`); `AlertsPage.svelte:~905-960`
- The summary may say 5,000 failed while only the newest 100 are reachable; the table shows rule/event/library but not the item or path (`item_id` is returned but unused); failed deliveries cannot be retried or cleared from the UI.
- Fix: add `offset`/`before` cursor to the API and "load more"; show path via item id lookup; add "retry failed".

**B25 · No global 401/session-expiry handling and no request timeout/abort** · `api.ts:104-115` (`request`), only `AccountPage.svelte:28` inspects 401
- When a session cookie/API key expires, the Jobs (4 s) and Libraries (30 s) pollers keep showing `ApiError: 401: …` forever instead of redirecting to login. `fetch` has no timeout/`AbortController`; combined with `JobsPage.svelte:335` (`if (loading) return`) a single hung request (half-open connection after a backend restart) stops the Jobs poller permanently with the spinner stuck on.
- Fix: central 401 hook (clear key, route to login), default `AbortSignal.timeout`, and make `loading` auto-expire.

**B26 · Jobs actions' "refresh afterwards" is silently skipped while a poll is in flight** · `JobsPage.svelte:335` (`if (loading) return`) used by `applyPriority :82`, `clearFailed :380-400`, `toggleMaintenance :405-430`, `reap :432-447`
- With a 4 s cadence and a summary that "can take several seconds", the post-action `await refresh()` frequently returns immediately, leaving the UI stale until the next tick (e.g. failed list still showing cleared rows).
- Fix: `refresh({force:true})` that queues one trailing run.

**B27 · Backups panel is coupled to the maintenance fetch** · `JobsPage.svelte:349` (`if (Date.now() - maintFetched < 1000) await refreshBackups()`), `:154-163`, `:223-236`
- `refreshBackups` only runs in the same tick in which `listMaintenance` just succeeded. If `/system/maintenance` fails (swallowed in `refreshMaint :170-174`) backups never load; after "Back up now" only one fixed `setTimeout(…, 4000)` nudge is scheduled (never cleared on destroy), so a dump longer than 4 s only appears at the next 30 s maintenance refresh with no "in progress" indicator, and a second click can queue another.
- Fix: independent throttled fetch; poll while a backup job is queued/doing.

**B28 · All downloads are buffered in memory and the object URL is revoked synchronously** · `api.ts:2172-2183` (`saveBlob`), used by `downloadBackup :1511`, `downloadExport :2262`, `downloadReport`, `downloadCustomReport`, `downloadTransfer :373`
- `await res.blob()` holds the full file (pg_dump bundles, multi-GB retrieved agent files, 100k-row XLSX) in the tab; `URL.revokeObjectURL` immediately after `a.click()` can abort the save in some browsers (usually deferred with `setTimeout`). Failures throw a plain `Error` (status lost), and there is no progress indicator or cancel.
- Fix: for large artifacts navigate to a short-lived signed URL (like the SSE stream token) or use `showSaveFilePicker`/streams; defer revoke by a few seconds.

**B29 · Retrieve panel can reconnect forever and misreports cancel** · `RetrievePanel.svelte:128-175` (esp. `:164-174`), `:199-205`; `backend/filearr/api/transfers.py:431` (unknown id → `error` frame then close)
- For an unknown/purged transfer the server sends only `error {detail}` and ends; the browser's follow-up bare `error` event is neither `terminated` nor guarded by the current phase, so the panel re-mints a token and reconnects every 2 s indefinitely while showing "failed". The reconnect timer also doesn't check that the component is still mounted for that item (stale closure; `es` assigned after `await` can outlive the item-change `$effect` cleanup and then apply another item's frames). `cancel()` swallows errors and unconditionally shows "cancelled" even if the DELETE failed (403/network) and the transfer keeps running. A `staged` transfer keeps its SSE stream open for the whole TTL after reaching "ready".
- Fix: treat a data-bearing `error` followed by close as terminal; re-check `transferId === id` after the awaited mint; show cancel failures; close the stream at `ready/downloaded`.

**B30 · Scheduled reports cannot carry parameters or be edited, and the schedule is shown only as raw UTC cron** · `ReportSchedulesPanel.svelte:56-70, 90-130, 160-165`; backend `report_schedules.py:36-45, 138-165` (accepts `params`, PATCH of name/params/format/cron/channel)
- `createReportSchedule` is never given `params`, so a scheduled `stale_files`/library-scoped report cannot reproduce what the Reports page shows (threshold/library are on-screen-only; scheduled run uses defaults — contradicting the intent stated in `api.ts:2224-2229`). Only enable-toggle and delete exist; cron/format/channel can't be changed. The table prints `0 6 * * *` with "(UTC)" and no human description or next-run time (the `schedule.ts` describer and `next_occurrence` exist). Delete uses native `confirm`.
- Fix: carry the page's `library_id`/`threshold_days` into the schedule form; add Edit; show `describeSchedule` + local time.

**B31 · Library form: no unsaved-changes protection and full-form PATCH can clobber concurrent edits** · `LibraryEditModal.svelte:236-304, 306-308` (Escape), `:310-316` (backdrop), `LibrariesPage.svelte:~1272` (`editing = lib` snapshot)
- Escape (even while typing in a field), backdrop click, Close and Cancel all discard silently. The modal seeds from a snapshot of the `Library` taken at open; `save()` PATCHes ALL fields (`:268-291`), so any change another admin (or the scheduler: `enabled`, etc.) made in between is overwritten with the stale values.
- Fix: dirty-check + confirm on close; send only changed fields (`model_fields_set` is already honoured server-side).

**B32 · Library-form scheduling gaps** · `ScheduleField.svelte:152-155, 188-203`; `LibraryEditModal.svelte:376`
- `hourlyMinute` is not rounded (a typed `1.5` generates `1.5 * * * *`, giving a 422 `invalid scan_cron` shown out of view). `bind:value` on the minute input yields `null` when cleared (handled with `|| 0`, so it silently becomes minute 0). No "next 3 runs" preview even though `schedule.next_occurrence` exists. Local→UTC conversion uses the offset at edit time, so the stored UTC cron drifts ±1h across DST (documented in `help.ts:166-175`); inventory schedules support named time zones (`schedule.py:_due_occurrence_tz`) but library schedules do not.
- Fix: clamp+round, add preview, consider storing `scan_tz`.

**B33 · Folder picker defaults to a path that may not exist and leaves a blank dialog** · `LibraryEditModal.svelte:64` (default `rootPath: "/data/media/"` → `FolderPicker initial`), `FolderPicker.svelte:20-30, 51-53`; `backend/filearr/api/fs.py:80-84` (404 "path is not an existing directory")
- If the default doesn't exist (or the typed text is not a directory) the picker shows only the raw error and an empty body (`view` stays `null`); the user must notice the "roots" crumb. Ancestors above the allowed root appear as crumbs but 422 when clicked; listings are silently truncated at 500 entries (`fs.py:104`); the picked value is the `realpath`, which may differ from what was browsed; rapid navigation is unsequenced (B13).
- Fix: on error fall back to `go("")`; hide crumbs above a root; show "showing first 500".

**B34 · `ScanRun.stats` is typed `Record<string, number>` but carries strings/bools/arrays** · `api.ts:510-517`; `LibrariesPage.svelte:267-283, 541-558`; backend stores `pruned_paths`, `error`, `reconcile_note`, `stopped`, … in the same JSONB
- `mergeScanRows` spreads whole `stats` into `live[id]`, and `fmtStats` renders `${k} ${st[k]}` without locale formatting. Safe today only because the key list is hard-coded. Counts in this table are not `toLocaleString`-ed while the adjacent "excluded" badge is.
- Fix: `Record<string, unknown>` + typed accessors; format with a shared number formatter.

**B35 · Per-library error list is capped at 50 with no pagination while the badge shows the full count** · `LibrariesPage.svelte:381-390` (`libraryErrors(libId)` default `limit=50`), `libraries.py:624-649`; the expanded row also hides the retry result (`retried` count ignored, `:~395-402`)
- A library with 4,000 failing items lists 50 with no "showing 50 of 4,000" and no way to see the rest. `retryFailed` doesn't report how many were requeued.
- Fix: pager/"load more" using `offset`; show `retried`.

**B36 · "Retry all failed" disappears exactly when stats are degraded** · `LibrariesPage.svelte:420, ~690-705`
- `errorTotal` sums `errorCounts` from `/stats`; when `/stats` could not compute `extract_errors` (fallback `{}`, `degraded.extract_errors` set) the total is 0, so the retry-all banner is hidden even though the warning above says counts are unavailable. The sequential `await loadFailing()` loop at `:248` also delays `syncStreams()` until every expanded library's error query finishes.

**B37 · Stale/contradictory UI copy** · `JobsPage.svelte:1432` ("Error text isn't stored in the queue DB … check worker logs") vs the same table's Error column and `LibrariesPage` ("Hover an error for its traceback"); `system.py:failed_jobs_view` docstring; `LibraryEditModal.svelte:36-37` ("Edits collect into a local draft and PATCH once on Save; a 422 body surfaces inline"); `DeleteLibraryDialog` copy omits cascades (B16); `JobsPage` priority "default N" never changes after "apply" (it only re-prioritises pending jobs, `system.py:1166-1200`), which looks like the apply failed.

### Minor

**B38 · Timeline omits empty buckets, so the x-axis lies** · `TimelinePage.svelte:122-143`; `backend/filearr/api/stats.py:98-120`
- Only months/years that have items are returned and drawn as adjacent columns; a 10-year gap looks like consecutive months. Zero-fill between min and max (client side is enough). Counts (`b.count`) beyond ~5 digits overflow the 32 px column; bar labels use UTC while the rest of the UI shows local time (a file at 23:30 local on the 31st lands in the next UTC month; the click range is also UTC, consistent with the bar but surprising). Timeline excludes sidecars while the Libraries "Items" column includes them (`stats.py:96` vs `/stats/libraries`), so totals will not agree. No sequencing (B13) and no loading indicator while refetching with existing data.

**B39 · Treemap hides zero/tiny folders but counts them** · `FolderTreemap.svelte:250` (`r.w > 1 && r.h > 1`), `:231` (count text), `treemap.ts:128-199`
- Zero-byte folders (squarified to `w=h=0`) and very small ones are not drawn or focusable, yet the caption says "N folders". If all are zero the equal-share fallback applies, but a mix of zero and non-zero hides the zero ones with no list fallback. `load()` has no sequencing (B13); the trail is pushed before data arrives, so a stale response can render level X under crumb Y.

**B40 · Reports page rough edges** · `ReportsPage.svelte:217` (mount `Promise.all([listReports(), listLibraries()])` — a 403 on libraries hides all reports), `:321-345` (Download menu never closes on outside click/Escape; stays open across report changes), `:209` (queued banner never clears), `:236` (rows keyed by index), `:183-196` (no abort for multi-minute streaming downloads; "Preparing…" with no cancel), `cell()` prints `[object Object]` for object cells (the custom-report `cell` JSON-stringifies, the canned one does not, `:76-82`).

**B41 · Custom reports** · `CustomReportsPage.svelte`
- "Default format" (`:262`) is stored but never used (downloads always use the menu; `json` is not even an `ExportFormat`); `sort` is supported by the API but not exposed, and the column order is alphabetical (`registry.core` is `sorted()`), so reports can't control column order; `vOk` starts true and is updated 300 ms+RTT after typing, with unsequenced validate responses, so Save can be enabled for an invalid query (the backend returns `422 {"detail":{"validation":[…]}}`, shown raw, B5); "Run" needs `write` scope (`custom_reports.py` `coarse="write"`) though listing is `read` — read-only keys get a raw 403; the filter-builder hand-off opens the form at the bottom of a long page with no scroll/focus (`:226-244`); Edit/Copy/Delete/Run all unguarded against double-clicks.

**B42 · Alerts form polish** · `AlertsPage.svelte`
- Rule/channel "edit" opens the form below the table with no scroll/focus; `Number(rRepeat)` of garbage becomes `NaN → null` silently (`:361`); cleared `rInhibitWindow`/`rGroupWait` send `null` (422); bulk "replace channels" with an empty selection removes every channel from the selected rules with no confirm, the result `{updated, missing}` is ignored (`:233-253`); stale ids stay in `bulkSel` after a rule is deleted; deleting a rule silently deletes its events (`alerts.py delete_rule`); channel/rule delete and toggle use native `confirm`.

**B43 · SSE lifecycle nits in LibrariesPage** · `LibrariesPage.svelte:314-376, 657-660`
- `openStream` awaits the token mint and then always does `new EventSource(...)` — if the component was destroyed (or the scan ended) during the mint, the stream is created after `onDestroy` and leaks. After a transient error the manual backoff timer coexists with `syncStreams()` (30 s poll or any refresh re-opens immediately because `streams` no longer has the id), so the backoff is bypassed and the later timer fire closes the healthy replacement. Streams are never reopened if the mint fails and the tokenless attempt 401s until the next poll (acceptable).

**B44 · Targeted-scan dialog** · `LibrariesPage.svelte:1254-1297`
- `role="button" tabindex="-1"` wrapper with Escape on `keydown` only works once focus is inside; no initial focus; no folder picker/validation (leading `/` → raw 422); on success the dialog stays open and `tgtMsg` persists; `coalesced`-vs-new race with `queued` only partly handled. (Pure UI polish; a11y covered by the earlier review.)

**B45 · Library `Enabled` flag wording/behaviour unclear** · `LibraryEditModal.svelte:~340-345`: label says "library participates in scheduled scans"; `trigger_scan` and targeted scans ignore `enabled` entirely (`libraries.py:778-831`). See Questions.

**B46 · `relTime`/formatters diverge and misbehave on bad input** · `LibrariesPage.svelte:530-543` ("just now"/`Nm ago`), `JobsPage.svelte:131-139` (`Ns ago`, no months), `Math.round` pushes 59.5 min to "60m ago"; an unparsable ISO string yields `NaN` → `"NaNy ago"` (`Math.max(0, NaN)` is `NaN`). `fmtBytes` exists in six variants (`LibrariesPage:~74`, `JobsPage:~150` uses KiB/MiB, `ReportsPage`, `CustomReportsPage`, `ExportsPanel`, `treemap.ts`) with different units/labels and `"0"` vs `"0 B"` vs `"—"` for zero. Several numbers (queue depth `JobsPage:~1066`, thumbs `generated`, extract depth, `m.document_count`, backup `item_count`, export `row_count`) are not locale-formatted while neighbours are. Consolidate in one DOM-free `format.ts` and test.

**B47 · Jobs rate/ETA maths** · `JobsPage.svelte:450-466`
- `extract.done` is the count of `succeeded` rows in `procrastinate_jobs`, which `purge_job_history` trims daily (`worker.py:1041+`); a purge makes `dd <= 0`, so ETA reads "stalled" for the next sample window. Samples are only taken on poll ticks (paused while the tab is hidden), so the first rate after returning spans the hidden gap.

**B48 · Jobs priority stepper input** · `JobsPage.svelte:1159`
- `oninput` converts `+value` immediately: clearing the field or typing `-` yields `0` and re-sets the input to "0", making manual entry of negative values awkward (the ± buttons work).

**B49 · Maintenance panel details** · `JobsPage.svelte:280-290, 1527-1560`
- Cron edit has no client-side validation; failures flash for 6 s only (`flashMaint`); Save with a blank box silently means "reset to default"; `maintRun` ignores that the task may already be queued until the 409.

**B50 · Archive section count vs listing** · `ArchiveSection.svelte:44-62`; `backend/filearr/tasks/archives.py:186-216`
- `members` is capped by `max_stored` without setting `truncated`, so the header can say "9,000 files" while the list stops at the stored cap with no note and "Show N more" is based on the stored list only.

**B51 · Missing UI for implemented backend features** · `backend/filearr/api/scan_paths.py` (hot folders), `POST /libraries/{id}/scan?force_empty=true` (`libraries.py:778-831`; the N→0 guard error literally tells the operator to "rescan with force_empty" but no UI exposes it), per-export delete/purge, per-failed-job retry, `/system/embed-backfill`-style on-demand tasks are reachable only via Maintenance "Run now".

**B52 · Dead / unused code** · `api.ts`: `getExport` (`:2263`), `enqueueCustomReportExport` (`:2245`), `runningJobs` (`:1421`), `systemDisk`, `maintenanceMode`, `getTransfer` have no callers; `FolderTreemap` prop `limit` is never passed (`ReportsPage.svelte:370`); `ExportFormat` includes `"xlsx"` but comments (`api.ts:2086-2087`, `downloadCustomReport` docstring) still say csv/ndjson/xml; `LibraryHashStatus.agent_owned`/`hash_scheme` unused; `Library.share_unc_effective` etc. fine. No TODO/FIXME markers found in scope.

**B53 · Misc small correctness items**
- `ScanRun`/`Library` row `{#each … as c}` without keys for category/group chips (`LibrariesPage`), harmless.
- `DiagnoseSection` / `LibraryDiagnoseModal`: `setTimeout(() => copied = "")` never cleared (`:36`); modal can be re-run while open but a closed modal's late response is harmless; `href={v.doc}` is a server-supplied relative `/docs/...` path (fine today; guard against `javascript:` if ever user-influenced).
- `ExportsPanel` shows the canned report KEY, not the report title, and "custom report" for all custom exports (`:104`); export failure reason is only a hover `title` on "ⓘ" (not reachable on touch).
- `ReportSchedulesPanel` loads custom reports/channels once; a failing `listCustomReports` (e.g., permission) blanks the whole schedules list because `Promise.all` has no per-call catch (`:34-40`).
- `LibrariesPage.relTime` for agent heartbeat uses local clock vs server time; skew can show "just now" for stale agents (clamped to ≥ 0).
- `LibrariesPage.svelte:~236`: `jobs` failure is swallowed (`.catch(() => null)`) so the failed-jobs section silently freezes on stale data.

## API contract table

| UI call | Backend route | Match? | Notes |
|---|---|---|---|
| `listLibraries` | `GET /libraries` (`libraries.py:182`) | Yes | `LibraryOut` ⊇ TS `Library`; `last_scan` includes scoped runs (B10) |
| `createLibrary` | `POST /libraries` (`:350`, admin) | Partly | `enabled` not accepted on create (UI correctly hides it); duplicate name → 500 (B3); no path validation (B4); fractional `hash_full_max_bytes` → 422 |
| `updateLibrary` | `PATCH /libraries/{id}` (`:369`) | Partly | `model_fields_set` semantics honoured; full-form PATCH (B31); `null` coerced for arrays; agent-owned: only truthy cron/watch rejected |
| `deleteLibrary` | `DELETE /libraries/{id}?confirm=` (`:690`) | Yes | exact match; 204; 409 only for running/stopping ScanRun (B17); cascades (B16); throws plain `Error` |
| `scanLibrary` | `POST /libraries/{id}/scan` (`:778`) | Partly | response may be `{job_id:null, already_running:true}` — TS type and UI ignore (B8); `force_empty` unsupported by UI (B51); 409 during maintenance |
| `targetedScan` | `POST /libraries/{id}/scan/targeted` (`:872`) | Yes | `scan_id` null when coalesced handled; 404 on missing path shown raw |
| `rehashLibrary` / `libraryHashStatus` | `POST /libraries/{id}/rehash`, `GET /libraries/hash-status` | Yes | `already_queued` handled implicitly; heavy poll (B15) |
| `libraryErrors` | `GET /libraries/{id}/errors` | Yes | UI never sends `offset` > 0 (B35) |
| `retryExtracts` / `retryAllExtracts` | `POST /libraries/{id}/retry-extracts`, `POST /system/retry-extracts` | Yes | `retried` count ignored |
| `diagnoseLibrary` | `GET /libraries/{id}/diagnose` (write scope) | Yes | shape matches `LibraryDiagnosis`; `doc` is relative `/docs/...` |
| `browseFs` | `GET /fs/browse?path=` (admin) | Yes | 404 not-a-dir / 422 outside roots / 403 unreadable shown raw; 500-entry silent cap (B33) |
| `listScans` | `GET /scans` (`scans.py:58`) | Partly | hard 50-row global cap; no `rel_path` in `ScanOut` (B9) |
| `cancelScan` / `stopScan` / `forceClearScan` | `POST /scans/{id}/{cancel,stop,force-clear}` | Yes | cancel only valid for `running`; errors swallowed (B12) |
| `mintScanEventsToken` + `scanEventsUrl` | `POST /scans/{id}/events-token`, `GET /scans/{id}/events` | Yes | `error` event (failed scan) drops `detail` (B11); stream ends after `done` |
| `stats` | `GET /stats` | Yes | `extract_errors`, `degraded` read; heavy (B15) |
| `libraryStats` | `GET /stats/libraries` (search_metadata) | Yes | failure → "—" silently |
| `timeline` | `GET /stats/timeline?bucket&library` | Yes | `pattern ^(month|year)$`; UTC buckets; sparse buckets (B38) |
| `failedJobs` / `clearFailedJobs` | `GET /system/failed-jobs`, `POST /system/jobs/clear-failed` | Yes | `limit` cap 100; `deleted` count ignored |
| `jobsSummary` | `GET /system/jobs/summary` | Yes | all fields typed; `stalled.total` read without guard |
| `reapStalledJobs` | `POST /system/jobs/reap` | Yes | |
| `setJobPriority` | `POST /system/jobs/priority` | Yes | pending-only semantics (B37) |
| `setMaintenanceMode` | `POST /system/maintenance-mode` | Yes | `reason` ≤ 500 chars not enforced client-side |
| `listMaintenance` / `updateMaintenance` / `runMaintenance` | `GET/PATCH /system/maintenance[/{key}]`, `POST …/{key}/run` | Yes | 409/422 detail via `apiDetail` (array case, B5) |
| `runBackup` / `listBackups` / `downloadBackup` | `POST /system/backup`, `GET /system/backups[/{name}]` | Yes | admin; blob download (B28) |
| `listReports` / `runReport` | `GET /reports`, `GET /reports/{id}` | Yes | `library_id`, `threshold_days` encoded via URLSearchParams |
| `downloadReport` | `GET /reports/{id}?format=` | **No (behaviour)** | passes `limit` which the server treats as a cap (B2) |
| `enqueueReportExport` | `POST /reports/{id}/export` | Yes | returns export row; UI doesn't use it (B21); 429 per-principal cap shown raw |
| `folderTree` | `GET /reports/folder-tree` | Yes | route declared before `/{report_id}`; `truncated` honoured |
| `listExports` / `downloadExport` | `GET /exports`, `GET /exports/{id}/download` | Yes | list capped at 100 (default), no offset in UI; 409/410 on download |
| `listReportSchedules` / `create…` / `update…` / `delete…` | `/report-schedules` | Partly | `params` and PATCH unused (B30); format enum matches (`csv|ndjson|xml|xlsx`) |
| custom reports (`listCustomReports`, `getColumnRegistry`, `validate…`, `create…`, `update…`, `delete…`, `copy…`, `runCustomReport`, `downloadCustomReport`) | `/custom-reports…` | Yes | `format` enum on definitions `csv|json|ndjson|xml` vs export formats; 422 body is `{detail:{validation:[]}}`; run needs write scope (B41); `enqueueCustomReportExport` unused (B21) |
| `listAlertChannels` etc. | `/alert-channels…` (admin) | Partly | PATCH replaces `config` (B23); 409 on duplicate name; 503 when no secret key |
| `listAlertRules` / `createAlertRule` / `updateAlertRule` / `bulkUpdateAlertRules` / `deleteAlertRule` | `/alert-rules…` | Yes | `group_by` normalised server-side; bulk result ignored (B42) |
| `listAlertEvents` / `alertEventsSummary` | `GET /alert-events[/summary]` | Partly | `limit ≤ 200`, no offset (B24); summary unfiltered |
| `initiateTransfer` / `mintTransferEventsToken` / `transferEventsUrl` / `cancelTransfer` / `downloadTransfer` | `/items/{id}/transfer`, `/transfers/{id}…` | Yes | 409 id extracted by regex; blob download (B28/B29) |
| `listPresets` / `getTaxonomy` / `listShareMap` | `GET /presets`, `/taxonomy`, `/system/share-map` | Yes | share-map refetched every refresh |

## Missing test coverage (DOM-free candidates)

Existing in scope: `tests/polling.node.test.ts`, `tests/treemap.node.test.ts` only.

- `schedule.ts`: no frontend test at all, although its header says it is pinned by `shared/schedule-vectors.json` — only `backend/tests/test_schedule_vectors_uit11.py` reads that file. Add a node test over the vectors plus round-trip (`parseCron(generateCron(x)) == x`), midnight-crossing weekly/monthly, 30-minute offsets (India), and the B18/B19 cases.
- A shared `apiErrorMessage(e)` helper (B5) — and extract the duplicated `apiDetail`/`errDetail`.
- `buildLibraryPayload(draft)` extracted from `LibraryEditModal.save/overridesPayload/linesToArray` (B1, B20): numbers vs strings vs null, trimming, rounding, unknown override keys.
- `isPresetActive/togglePreset` vs backend `resolve_effective_presets` (a vector file like the schedule one would keep them in sync).
- `resolveShareHint` (exported from `api.ts`, pure): longest-prefix, subfolder remainder, UNC join.
- `reportQuery`/`enqueueReportExport`/`folderTree` query builders (B2): export the pure builders.
- Library-row state machine (`mergeScanRows`, `syncStreams`, `isQueued`, backoff) extracted from `LibrariesPage` so reconnect/duplicate-stream behaviour (B9, B43) can be tested with fake EventSource/timers.
- Formatters: one `format.ts` for `fmtBytes`, `fmtDuration`, `relTime`, `relFuture`, `attemptsView`, `queueEta`/`recordRate` (currently six `fmtBytes` copies, two `relTime`s, no tests; B46, B47).
- Zero-fill/aggregation for Timeline (B38) once moved into a pure function.
- Request-sequencing helper (B13) and the poller's overlap behaviour (`createPoller` has no guard against `fn` still running when the next tick fires).
- Alert `buildConfig` (secret `UNCHANGED` semantics, B23) and `detectWebhookFormat`.

## Questions I could not resolve from code alone
1. Does toggling `enabled_extension_groups` bump the provenance fingerprint (`provenance.py:78`) and mark every item's hashes "stale" (the Libraries page would then offer a re-hash)? If so B6 has a user-visible side effect beyond being a no-op.
2. What does `Library.enabled=false` actually suppress (scheduler only? watch supervisor? hot folders? manual scans)? The UI label says "participates in scheduled scans"; manual/targeted scans ignore it (B45).
3. Is `/api/v1/system/failed-jobs` `error`/`traceback` really populated in production (the `joberrors` middleware exists, but the endpoint docstring and the Jobs footnote say it is null)? One of those texts is stale (B37).
4. Does a failed ScanRun always have `stats.error` populated (so B11 could be fixed from the polled row alone), or only via the SSE frame?
5. Does `GitIgnoreSpec.from_lines` ever raise for user-entered globs at scan time (no API-side validation of `include_globs/exclude_globs`)? If yes a bad glob fails every scan with an error the Libraries page won't show (B11).
6. Is `FILEARR_BROWSE_ROOTS` expected to cover every valid `root_path`? If not, the picker can't reach a legitimate library root and the free-text-only path has no validation (B4, B33).
7. Are Procrastinate `succeeded` rows retained long enough for `extract.done`-based ETA to be meaningful on large installs (purge default window ~48 h per the Jobs panel tooltip)?
