# Frontend review 2 — functionality, security, performance, usability, aesthetics

**Date:** 2026-11-01 · **Scope:** `frontend/src` (34.6k lines Svelte 5 + TS) against `backend/filearr` · **Branch reviewed:** `main` at 1b240e9 + P2 merge (PR #27)
**Supersedes the plan in `frontend-ui-review.md` from P3 onward.** P1 and P2 of that plan are merged; its a11y/layout/design findings still stand where not fixed.
**Status (2026-11-01):** P3 implemented on `claude/frontend-p3-request-auth` (plan `frontend-p3-request-auth.md`): one `send()` request core + `errorMessage()`, fail-closed auth with a 401 hook, `latest()` sequencing at 13 sites, typed numeric inputs, the search keyboard gate, honest clipboard, `safeHref`. Closes C1/D1, C2/D2, A1, B1/C3, A4, B8, A10/B5/I3, A11/I10, A16, A18, A28, A33/D10, B13, B25/B26, D8, D9/M26, D12, D18/A23, I1, I4, M2/D20, M14, M15, M18/A30. Carried into P4+: `send` response-headers mode, `hashCeiling` badInput, nested row-button keys, combobox arrows from the query box, LogsPanel load-older, backend tickets §10.

## Method and evidence

| Source | What | Evidence file (this folder: `review-2/`) |
|---|---|---|
| A | Static: Search, Browse, ItemDetail, cards, map vs `api/search.py`, `items.py`, `libraries.py` tree, transfers | `A-search-browse-detail.md` (45 findings) |
| B | Static: Libraries, LibraryEditModal, Jobs, Alerts, Timeline, Reports, Exports, Retrieve vs their routers | `B-libraries-jobs-reports.md` (53) |
| C | Static: AgentsPage (4k lines), every Admin panel, auth/account/login, Taxonomy, Filter builder vs routers; client-side security | `C-agents-admin-auth.md` (62) |
| D | Static cross-cutting: `api.ts` request layer diffed against the generated OpenAPI (224 paths), build/PWA/Caddy, types, tests, CI | `D-cross-cutting.md` (26) |
| L | **Live**: full stack run locally (PG 16 + `uuidv7()` shim, Meilisearch 1.53.1, worker, prod build), 40-file seed library scanned; 353 screenshots × 3 viewports × 2 themes, 100 axe-core scans, 20 console/network logs, 20 perf captures, 10 scripted flows, 3 keyboard passes | `live-perf.md`, `live-axe-summary.md`, `live-flows.md`, `live-keyboard.md`, `live-stack.md` (how to rebuild the stack), `shots/` (curated) |

Previous review covered a11y/layout/design-system/bundle; it is **not** repeated here except where the live run measured it. Finding IDs below (A1…, B1…, C1/I1/M1…, D1…, L1…) point into the evidence files, which carry file:line, repro reasoning and fix sketches. **186 static + ~40 live findings; nothing was dropped**, only grouped.

## Headline (the ten that matter most)

1. **Password change always shows an error and kills every session** (C1/D1). `request()` parses the 204 as JSON → `SyntaxError`; the server has already revoked all sessions. Verified in Node.
2. **No 401 handling anywhere; the auth probe fails open** (C2/D2/A10/B25/I1). Session expiry mid-use leaves pollers hammering 401s with raw-JSON banners; a transient `/auth/me` failure drops the login wall entirely. Live: the login page itself fires 6 calls that 401 before the wall renders.
3. **Space, Enter and arrow keys are hijacked on the Search page once results exist** (A1). A window-level handler never checks the target: no multi-word queries or tags, Space in the bulk bar silently changes the selection, Enter opens the previous query's top hit.
4. **`<input type="number">` bound to string state then `.trim()`-ed → TypeError on Save** in LibraryEditModal (hash ceiling + 8 extraction limits), UsersPanel (session timeouts), TaxonomyPage (sort order), AgentsPage group editor (max entries) (B1/C3). Touch the field once and the form can never be saved.
5. **Bulk tag edit overwrites fresher tags** (A3): computed from the stale search-index copy and sent as a full replacement. Silent data loss with a success toast.
6. **Report downloads silently capped at 1000 rows** (B2) while the UI promises the full result.
7. **Saving the AD directory-sync panel erases stored bind passwords** (C4) because the redacted JSON is PUT back verbatim.
8. **The Search "Query syntax" help documents a DSL that `/search` never parses** (A2); every example chip returns nothing useful. The Filter builder's "Open in search" drops `kind:` silently (I22).
9. **Live: every non-image item requests a thumbnail and 404s** (L1): 36 failed requests on one Search page, 385 across the run; failures latch forever, no placeholder (A22). Dark mode: **2,289 contrast failures** (L3), mostly `text-slate-500` nav/captions at 4.23:1, plus 60 `<select>`s with no accessible name (axe critical).
10. **Live: layout shift is far above budget on every data page** (L4): CLS 1.28 Admin, 1.39 Jobs (phone), 1.07 Libraries, 1.0 Agents — content pops in after fetches with no reserved space. And the raw pydantic 422 JSON really does land in users' faces (`shots/flow-bootstrap-shortpw-desktop-light.png`).

## Findings by dimension

### 1. Functionality — request layer and auth (fix first; everything else inherits it)
- `request()` assumes JSON on 2xx (C1/D1); 33 hand-rolled `fetch` copies, 13 throw plain `Error` so `e.status` checks silently miss (D8, M20); `DeleteLibraryDialog` classifies errors by `msg.includes("409:")` (B17).
- No 401 hook, fail-open probe, pollers never stop on 401, `doLogout` unguarded (C2/D2/I1/B25).
- Errors shown as `ApiError: 422: {"detail":[…]}` almost everywhere; six local `errDetail` copies, `friendlyError` wording bug ("…to load logs this item.") (A10/B5/I3/D8).
- No timeout/abort, no retry, `usePolling` stacks overlapping calls, `if (loading) return` can freeze the Jobs poller after one hung request (B25/B26/D12).
- No request sequencing: Libraries/Jobs pagers, Reports, Timeline, Treemap, FolderPicker, Audit, Logs, Browse, ItemDetail all let an older response overwrite a newer one (B13/A16/A18/M14/M15).
- Unguarded `localStorage` at module load can blank the app in blocked-storage browsers (A33/D10). Dead `apiKey` path puts a bearer key in every thumbnail URL if ever set (A23/D18/I28).
- Action errors are wiped by the next poll (B14); transient-only error messages (A10).

### 2. Functionality — forms and inputs
- Number-input `.trim()` TypeErrors (B1/C3) and `Number(null) → 0` traps in AuthProviders, Sessions, MapPanel (I12c/M8/A28); fractional overrides rounded on every save (B20).
- LibraryEditModal: blank/untrimmed name on edit, no path validation, duplicate name → bare 500 (B3/B4); error banner 290 lines above the Save button (B5); full-form PATCH clobbers concurrent edits, no dirty check on Escape/backdrop (B31); extension-groups section is a dead control (B6); include globs are not an allow-list despite the help text (B7); weekly schedule with no day silently saves null, monthly schedules shift day-of-month across UTC midnight (B18/B19/B32).
- Saved search 422 from grid/map view because UI-only `view` is sent (A4). ItemDetail: unsaved edits lost on Escape, list rows never updated after save (A17).
- Alerts channel edit is lossy (PATCH replaces config), "plain (insecure)" SMTP can never work, cleared port → crash at delivery (B23); weak numeric coercion (B42).
- Config-group editor: description can't be cleared (server ignores null), no version precondition (last write wins), two-PATCH reorder is non-atomic, inventory block silently dropped (I19/I20); raw-JSON pane is a stale snapshot (I25).
- Role clone discards manual edits, default ceiling empty (I8); API-key picker can mint `["write"]` keys that can't read (I9); preset fork is two-step non-atomic (I24); AuthProviders pins every field as "gui" on first save, duplicate attr keys across panels, secrets can't be cleared (I12).
- Login: bootstrap failure modes misreported, every non-429 failure reads "Invalid username or password", short password → raw 422, no confirm field for the only admin (I4). OIDC return drops the deep link (M2/D20).

### 3. Functionality — search, browse, detail
- Keyboard hijack (A1); DSL help unimplemented (A2); bulk-tag clobber (A3); Forward navigation ignored and Back-to-empty leaves stale results (A5/A6); deep-link range filters invisible/unremovable (A7); initial search waits on `/stats` (up to 10 s) then overwrites what the user typed (A8); every debounced keystroke pushes history (A9); `loadMore` re-reads live form state and never de-dupes ids (A12); commas in principals/tags split values (A31/D11); `searchTags` sends `type`, backend reads `file_category` (A31).
- Card registry keyed by retired categories so 3D and spreadsheet cards are dead; curated exclude lists hide fields no card renders; `_extract_error` never shown (A13/A14). Detail pane and rows omit size, date, tags, title, status; `highlight` never rendered (A15). Live: the detail of a CSS file reads "No additional details extracted." with a long permissions paragraph (`shots/search-detail-desktop-dark.png`).
- ItemDetail/Browse have no stale-response guard (A16/A18); Browse rejects folders whose 2nd char is `:`, mangles `\` (A19); lists tombstoned items with no marker (A20); offers Rescan where the backend always refuses (A21); Thumb failure latches, no placeholder, layout shifts (A22, L1).
- `copyText()` returns false but six callers report "Copied!" anyway; ApiKeys/LlmKeys panels call `navigator.clipboard` directly and throw on plain http (A11/I10).
- Map: marker pointerdown starts a select-drag and clears the area filter; blank field → 0 (A27/A28). Retrieve: silent download failure, whole file buffered in memory, reconnect loop, Cancel no-op (A29/B28/B29/D15).
- Live `#/nope` silently renders Search (L5); zero results shows "0 results" and an empty frame (`shots/search-zero-desktop-light.png`).

### 4. Functionality — libraries, jobs, reports, agents, admin
- Scan UI: "Restart" is a no-op while running (`already_running` ignored) (B8); row state comes from `GET /scans`, a 50-row global window (B9); "last scan" may be a scoped run (B10); failed-scan reason never reaches the page (B11); Stop/Cancel/Force-clear swallow errors, Cancel unconfirmed (B12); SSE stream leaks after unmount (B43).
- Exports panel never refreshes after queueing (B21); alerts-failed banner empty until Events tab opened (B22); alert events capped at 100, no pagination/retry (B24); schedules can't carry params or be edited (B30); per-library error list capped at 50 with no pager (B35); retry-all hidden exactly when stats are degraded (B36); Timeline omits empty buckets (B38); treemap hides zero folders but counts them (B39).
- AgentsPage never re-fetches the agent table, so online dots and "applied" badges freeze; errors render at the top of a 4k-line page whose actions are at the bottom; no success feedback (I17); effective-config cache never invalidated (I18); actions offered for pending agents (M21); purge flow keyed on error text (M22).
- Admin-only UI shown to every role, 403 with raw JSON: Agents nav/page, RBAC, LLM keys, custom fields, Updates (I2). Users: no last-admin guard server-side, no "(you)", select not reverted on failure (I7). Audit filter lists 21 of ~75 event types (I13). RbacPanel: `startsWith(root)` yields a wrong ACL scope for sibling paths, grant warnings dropped, ceilings unused (I14). Account page wipes in-progress edits on any `onUpdated` (I5); header Theme/Paths revert on reload for users with saved prefs (I6).
- Filter builder: `kind:` mapped to a non-existent `type` param, warning never seen, quoting breaks on `"` and unquoted `meta.`/`cf.` values (I22/I23).

### 5. Security (client side and the backend edges the UI exposes)
- Agent-supplied `share_url` and library `share_prefix` rendered as `href` with no scheme check → `javascript:` links from a hostile agent (D9). Source/doc/about links not scheme-checked (M26).
- No CSP, HSTS, frame-ancestors, nosniff or Referrer-Policy anywhere (Caddy or backend) (D7). CSRF rests on SameSite=lax only; `none` is a reachable setting (M5).
- Read-scope endpoints leak admin-grade data: `/system/logs` (full log incl. failed-login IPs), `/agent-commands` payloads/results, `/system/about` fingerprints (I15). Fleet commands need only `write` (I16).
- Show-once secrets stay in component state; Windows one-liner fetches a script over http into an elevated shell (M19/M23). Bearer key in thumbnail URLs if `apiKey` is ever set (A23/D18).
- Stream tokens are per-process; `--workers>1` breaks SSE silently (I28).

### 6. Performance (measured, `live-perf.md`)
- **CLS**: Admin 1.28, Jobs 0.80 desktop / 1.39 phone, Libraries 0.66 / 1.07, Agents 0.51 / 1.0, Reports 0.70 / 0.85, Filter builder 0.85 phone (budget 0.1). Cause: every section renders empty then fills; no skeletons or reserved heights.
- **Thumbnail 404 storm** (L1): 36 failed `/thumb` requests on Search with 38 items; non-image kinds should not request thumbs, and misses should not be retried per render.
- **Polling load** (B15): Libraries polls `/libraries/hash-status` (full `GROUP BY` over items) and `/stats` (7 sections, 10 s budget) every 30 s and on every action; `refresh()` runs 5× in the post-scan burst; share-map refetched each time. Search mounts fetch `/stats` just to read one boolean (A8/A44). Registered-agents list is N+1 server-side (M24).
- JS per route 157–355 KB decoded (AgentsPage 355 KB, LibrariesPage 279 KB, Reports 283 KB); entry 52 KB. LCP fine locally (< 300 ms). One long task (123 ms) on Search.
- Taxonomy page renders 3,424 DOM nodes; Jobs 967 (maintenance table of 40 rows + logs) — fine for now, but Jobs is also a 3,800 px page.
- All downloads buffered in memory (`res.blob()`), object URL revoked synchronously (B28/A29). Timeline/Treemap refetch without abort.
- PWA: `autoUpdate` with `skipWaiting` and no reload flow, no `vite:preloadError` handler, no `Cache-Control` from the backend → old tabs break after a deploy (D5/D6); precache ships every chunk incl. the dev gallery (D19). Dockerfile `npm install` not `npm ci`; footer always says "dev" (D3/D4).

### 7. Usability (live + static)
- Error presentation: raw JSON on login, library save, alerts, custom reports, taxonomy; banners far from the triggering control (B5/I17); action results vanish on the next poll (B14).
- Native `prompt()`/`confirm()` still used 33× (saved-search name, deletes) though the kit `confirm()` exists (A41/D25).
- Libraries page is three admin surfaces in one scroll (library table, custom-fields form permanently expanded, exclusion presets, recent scans); Jobs is tiles + 40-row maintenance table + backups + features + updates + logs on one 3,825 px page; Admin is nine panels on one 3,095 px page (`shots/jobs-default-desktop-light.png`, `shots/admin-default-desktop-light.png`). No in-page navigation.
- Phone: Libraries row actions sit off-screen in a 1,178 px scroller (`shots/libraries-default-phone-light.png`); header takes four rows before content; Search titles truncate to ~15 chars (`shots/search-results-list-phone-light.png`). Tablet: Filter builder forces 1,702 px, Browse 806 px (`shots/filter-builder-default-tablet-light.png`). Desktop Browse: a long filename pushes "Rescan" outside the list frame (`shots/browse-misc-default-desktop-light.png`).
- Keyboard: Libraries edit drawer and item detail modal do not move focus in, do not trap it, and lose it to `<body>` on Escape (`live-keyboard.md`); the kit's Modal does this right. Grid-view arrow keys don't scroll the active tile; hover-only controls invisible on touch (A35).
- Missing feedback: queued agent commands, "already running" scans, bulk-apply partial failures (A26), export queued but panel not refreshed (B21), report runs with no cancel (B40).
- Flows all passed (10/10) on the happy path; the theme toggle has no "System" option once clicked (also in review 1).

### 8. Aesthetics (screenshot review, `shots/`)
- The shell is competent but flat: one accent, one grey, no elevation hierarchy, every control the same weight. The header crams brand, 9 nav links, account chip, Sign out, a Paths select and a text-only "Theme" button on one line (four rows on phone).
- Search: a fixed-height empty results frame dominates the page with 2 results; rows show a truncated absolute path instead of library + relative path; no size/date; 36 px thumbnails; "2 results" and the selection hint are tiny grey text. Grid tiles are good.
- Item detail: top-anchored narrow modal, three tab buttons, boilerplate permissions paragraph, no key facts; the result list behind it is barely dimmed.
- Admin/Jobs/Libraries read as settings dumps: long sections, tiny `text-[10px]` captions, inline forms always open, section headings with no grouping cards. Jobs' tile row is the one place with visual hierarchy.
- Dark mode: nav inactive, captions and table meta sit at 4.2:1 grey; accent text and chips on `surface-3` under AA (P2 review).
- Fit-and-finish inconsistencies: six byte formatters and two relative-time formatters with different units and zero handling (A32/B46/D22); dates mix UTC and local (A32/B38); "(s)" plurals; `[object Object]` in some table cells (B40).

### 9. Accessibility (live axe, `live-axe-summary.md`; adds to review 1)
- `color-contrast` serious: 2,450 nodes over 74/100 scans (dark mode 2,289). `select-name` critical: 60 nodes (Admin RBAC selects, Libraries edit, Filter builder). `nested-interactive`: 148 (Search `div role=option` rows with checkboxes/buttons inside). `scrollable-region-focusable`: 10 (table wrappers, log pane). `label-title-only`: Jobs log filters. `heading-order`: Account/Browse/detail. `empty-table-header`: 32.
- Hand-rolled dialogs remain in AgentsPage, TaxonomyPage, CustomFieldsPanel, Libraries edit/diagnose/delete, ItemDetail (M28, live keyboard).

### 10. Backend defects surfaced by the frontend (file as backend tickets)
Duplicate library name → 500 (B3); no last-admin guard (I7); delete-library cascades to alert rules, grants, hot folders with no impact summary; 409 guard misses queued scans; delete runs in one request (B16/B17); `GET /scans` hard 50-row cap, no filters, no `rel_path` (B9/B10); `last_scan` mixes scoped runs (B10); config-group description null ignored, no version precondition, no reorder endpoint (I19); directory sync `has_bind_password` round-trip (C4); include-glob semantics vs help (B7); `enabled_extension_groups` dead column still in provenance (B6); alert events no offset (B24); report schedules `params` unused by UI (B30); `scanLibrary` returns `{job_id:null, already_running}` untyped (B8/D13); `AuthStatus.ldap_enabled`, `GrantOut.warnings`, `AgentCommandKind.reconcile` missing from TS (D13); 117/250 operations have no `response_model` (D13); no cache/security headers (D5/D7); read-scope leaks (I15/I16); `uuidv7()` dependency (PG 18 only, needs a documented shim or fallback for PG 16 — `live-stack.md`).

### 11. Missing UI for implemented backend features / dead code
No UI: hot-folder scan paths, principal aliases, directory sync status/trigger, effective-access preview, federated search, on-demand digests, rebuild-index/embed-backfill/rbac-backfill, backups list, group rename, agent share maps, agent rename, command cancel/history, `force_empty` rescan (B51/I21/D "unused backend endpoints"). Dead `api.ts` exports: `getTransfer`, `maintenanceMode`, `systemDisk`, `runningJobs`, `enqueueCustomReportExport`, `getExport`, `updateAgent`, `cancelAgentCommand`, share-map CRUD, `getConfigGroup`, `apiKey()` (B52/D). Dead card registry entries, `fmtBytes` in `cards/format.ts`, `FolderTreemap` `limit` prop, NUL bytes in `FolderTreemap.svelte` making it binary to git (A42/D22).

### 12. Tests and guardrails
Covered: 17 pure modules, 236 tests. Not covered: `api.ts` entirely (request/ApiError/errorMessage/search param expansion), `pathlinks.ts`, `schedule.ts`, `searchparams.ts`, `help.ts`, `cards/*`, `clipboard.ts`, every `.svelte.ts` store, every component. No component/e2e layer, no coverage report, no `npm audit`, Dependabot ignores npm, `vite.config.ts` type-checked by nothing, no lint (D14/D16/D21). Each of C1, D8, D11 would have been caught by a ten-line test. The live stack recipe in `live-stack.md` (PG 16 + Meilisearch via `mirror.gcr.io` + seed script) is reusable for Playwright/axe in CI.

## Revised plan (replaces P3–P6 of `frontend-ui-review.md`)

Each phase is independently shippable, one PR, run with the same implementer/reviewer process as P1–P2. Phases 3 and 4 are small and unblock everything after them; 5–8 are the bulk; 9–10 restructure; 11 adds guardrails. Backend tickets (§10) run in parallel on their own branch.

**P3 — Request layer and auth hardening** (fixes 1, 2, 4 and most raw-JSON errors)
1. `request()`: 204/empty/non-JSON bodies, `AbortSignal.timeout` default, always `ApiError`; fold all 33 raw `fetch` copies onto one `send()`; `errorMessage(e)` parsing string/list/`{validation}` details with per-status sentences; delete the six local copies.
2. `onUnauthorized` hook → `App` sets `me = null`, re-probes, stops pollers; probe fails **closed** with a "can't reach server — retry" state; `doLogout` guarded; `/auth/me` not called while already gated.
3. `latest()` sequencing helper + `createPoller` skip-if-in-flight; apply to every `load()`/`refresh()` in B13/A16/A18/M14/M15.
4. `safeStorage` wrapper; delete the `apiKey` localStorage path and `?api_key=` on thumbnails.
5. `numStr()`/`number | null` state at the four `.trim()` sites and the `Number(null)` sites; `view` stripped from saved searches; `scanLibrary` typed with `already_running` and surfaced.
6. Search keydown handler gated on target (A1); `copyText` boolean honoured at all callers; ApiKeys/LlmKeys via `copyText`.
7. `safeHref()` scheme allowlist hoisted to `pathlinks.ts` and used for `share_url`, `share_prefix`, source/doc/about links.
Tests: `request`/`errorMessage`/401-hook/`latest`/`safeStorage`/`safeHref`/`numStr` all DOM-free.

**P4 — Kit API prep** (from the P2 whole-branch review; unchanged)
Field wiring via context (+ `required` forwarded), `Checkbox`/`Switch`, Modal `dismissible`/`initialFocus`, `confirm({ details })`, Popover `label` shorthand, DataTable `loading`/`error`/`onRetry` + numeric `rowKey` + mobile sort; `accent-text` computed from the accent; `border-strong` decision; `#/kit` dev-only.

**P5 — Migrate onto the kit with correct states** (the original P3, now with error/empty/loading discipline)
Modals (11 → `Modal`; dirty-check on close), `confirm()`/`prompt()` (33 + saved-search name → `ConfirmDialog`/a `PromptDialog`), inputs (218 → `Field` family; number inputs typed), tables (49 → `DataTable` with `scope`/`caption`/`aria-sort`), badges (`toneForStatus`), error banners (20+ → `Alert` placed **next to the action**, action errors persist until dismissed), empty/loading (→ `StateBlock`; zero-result state with suggestions), `text-[var(--accent)]` → `text-accent-text`, `select-name`/`label-title-only` axe fixes, Search listbox rows (A6/nested-interactive). Split AgentsPage/SearchPage/JobsPage/LibrariesPage per the C-report proposal (store for `groups`/`effective`/`agents`, one poller).

**P6 — Search and detail correctness** (A-series)
DSL: translate `kind/ext/size/modified/tag/meta/cf` to flat params in a shared `dslToParams()` (or remove the help); hash reducer with Forward/Back fixed and `replaceState` while typing; pending-range chips; `applyParams(initial)` first, `/stats` probe replaced; `loadMore` snapshot + id de-dupe; comma-safe multi-values; `searchTags` → `file_category`; ItemDetail token guard + `onChanged` + facts header (size, mtime local/UTC, tags, title, status, `highlight`); bulk tags resolved from `GET /items/{id}` + partial-failure summary + run token; card registry keyed by category, `_extract_error` row, CURATED built from rendered keys; Thumb placeholder + single retry + **no thumb request for non-thumbable kinds**; Browse stale guard, folder-page assembly, `status` badge, Rescan gated on `source_agent_id`/role; map marker pointerdown, blank-field NaN; Retrieve streaming download + terminal error + cancel; one `format.ts` (bytes/dates/durations/relative, tested).

**P7 — Ops pages correctness** (B-series)
`buildLibraryPayload()` pure + tested (numbers, trim, blanks, rounding only bytes); 409 duplicate-name inline; errors beside Save; dirty-check; changed-fields PATCH; extension-groups removed/relabelled; include-glob wording or allow-list; schedule guards (no-day, monthly midnight) + next-runs preview; `downloadReport` without `limit`; scan button states from `already_running`, failed reason from SSE `error` frame, Stop/Cancel errors + confirm, shared `connectSse()` with cancel flag and capped backoff; Exports refresh after queue + idle poll; alerts summary on load, events pager, channel config merge, "plain" SMTP opt-in; schedules with params + edit + human cron; Jobs `refresh({force})`, backups decoupled, priority input; Timeline zero-fill; treemap small-node list; Libraries poll only `/libraries` + running scans, heavy aggregates on demand.

**P8 — Admin and agents correctness** (C-series)
RBAC gating of admin-only UI by `isAdminPrincipal(me)` (Agents nav, RBAC/LLM panels, custom fields, Updates, Logs); AuthProviders sentinel round-trip + changed-fields-only + secret clearing + `oidc_link_by_email` confirm; Users "(you)", revert on failure, email edit; role clone fix + default ceiling; key scope expansion; preset fork body; audit vocabulary from backend + cursor keyed to filters; RbacPanel `relPathUnder()`, warnings, ceilings; AgentsPage: poll `listAgents`, server online flag, effective cache invalidation, per-row toasts, no actions on pending, structured purge error, description clear, `if_version`, reorder endpoint, inventory block emitted when non-default, rollout-live warning, policy validators aligned; Filter builder `file_category` + unmapped toast + quoting; Login/bootstrap error mapping + confirm password + re-probe; OIDC deep-link restore; Account seed-once; header prefs saved to account.

**P9 — Page structure and mobile** (the original P4, widened)
Admin → tabbed sub-routes (`#/admin/users|roles|access|keys|auth|sessions|audit`); Jobs → `#/jobs/queues|maintenance|backups|logs`; Libraries → library table only, custom fields + presets move under Admin; Agents → `#/agents/fleet|enroll|groups|rollouts`; card layouts below `md` for Libraries/Agents/Jobs/Fields tables; nav collapses to a menu below `sm`, utility controls into the account menu; filter-bar widths, popovers capped, `grid-cols-1` bases, `dvh`; long-name truncation with `title` and min-width 0 (Browse/Search rows); remove global `overflow-x:hidden`; Search: results frame sized to content, library + relative path, size/date columns, larger thumbs, result count as real text; detail modal centred, key-facts header, permissions paragraph collapsed; per-page section cards and heading hierarchy; dark-mode nav/caption tokens (`text-slate-500` → `muted`); **CLS budget 0.1**: skeletons/`min-h` for every async section.

**P10 — Performance, PWA, delivery**
Backend `Cache-Control` (no-cache for shell, immutable for `/assets`); SW `registerType: "prompt"` + reload toast + `vite:preloadError`; security headers in Caddy and backend (CSP as sketched in D7); `npm ci` + `FILEARR_VERSION` + runtime `app_version`; precache trims; thumbnail policy (L1); polling load (B15/A44); streaming downloads; `<svelte:boundary>` + error/rejection log in About's copy-as-Markdown (D17); dark-mode flash fix in `index.html` (D24).

**P11 — Tests and guardrails** (the original P6, widened)
Node tests for everything in §12; `openapi-typescript` (or the D-report field-diff script) in CI once dict routes get `response_model`; Playwright on the `live-stack.md` recipe running axe (both themes, 3 widths) + the 10 flows + a CLS/bundle budget; `npm audit` + Dependabot npm; lint; `vite.config.ts` type-checked; `svelte-ignore a11y_*` requires a justification comment.

**Backend tickets** (§10) — file separately; P7/P8 depend on B3, B9/B10, B16/B17, I7, I19, C4-server-side, D13 `response_model`, D5/D7 headers.

## Carry-overs still open from review 1 and P1/P2
Visual toast focus-move, Tabs value fallback (done), `announce()` timer cancel, isDark reactive to OS, skip-link history entry, JobsPage elapsed-time component, hover colour leftovers (Breadcrumbs/Browse), accent ping dot, `--accent-text`/`border-strong` contrast, theme "System" option, manifest `theme_color`, maskable icon check.
