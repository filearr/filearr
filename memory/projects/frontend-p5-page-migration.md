# Frontend P5 — Migrate the pages onto the kit with correct states (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Date:** 2026-11-01 · **Spec:** `memory/projects/frontend-review-2.md` §"P5" plus the **P5 checklist** in its status line (P4 outcome) · **Kit API:** `frontend/src/lib/ui/index.ts` header + each component's header comment; gallery `#/kit` shows every pattern in use.
**Branch:** `claude/frontend-p5-page-migration` (off `main` at c8eba8d, after PR 31).
**Goal:** every page renders its forms, tables, badges, dialogs, confirmations, errors and loading/empty states through `src/lib/ui/`, so the states are correct (errors next to the action and persistent, loading without blanking, empty with guidance) and the live-axe `select-name`/`label-title-only`/`empty-table-header`/`nested-interactive` counts drop to zero.

**Architecture:** one kit-prep task (Task 1) adds what the migration needs and nothing else; Tasks 2–8 migrate one page cluster each, file by file, keeping each page's behaviour and public props unchanged; Task 9 sweeps the shell leftovers and runs the browser acceptance (Playwright + axe on the live stack) whose findings are fixed in the same task. Hand-rolled dialogs become `Modal`; `window.confirm` becomes `confirm()`; `window.prompt` becomes `promptText()`; raw inputs/selects/textareas become `Field` + control; flat list tables become `DataTable`; status chips become `Badge tone={toneForStatus(...)}`; red banners become `Alert tone="danger"` beside the action (dismissable when they report an action) or `StateBlock state="error" onRetry` when they report a load; "Loading…"/"No … yet" become `StateBlock`.

**Rulings (orchestrator):**
1. **Page splits move to P9.** The C-report split (Agents/Search/Jobs/Libraries into stores + sub-components) follows the sub-routes P9 introduces; splitting by component now and by route later is the same work twice. P5 migrates in place.
2. **Buttons:** action buttons (submit, row actions, toolbars, dialog footers) become `Button`; bespoke interactive surfaces (result tiles, tree nodes, breadcrumbs, nav, segment toggles, icon-only chrome) stay raw with token classes. Reviewer checks the judgment, not a count.
3. **Palette sweep stays in P9.** Replace palette utilities only on markup this phase rewrites anyway (a migrated control carries its tokens). No standalone `text-slate-500 → text-muted` pass.
4. **Tables that stay raw** (key/value, nested detail, card content: AboutPage, RawView, `cards/TrackTable`, LibraryDiagnoseModal, ItemDetail) get `<caption class="sr-only">`, `scope="col"`/`scope="row"`, and no empty `<th>` (`<th scope="col"><span class="sr-only">Actions</span></th>`).
5. **DeleteLibraryDialog is retired** in favour of `confirm({ typedName, tone: "danger", details })`; the post-delete 404/409/422 classification moves to the caller and renders as a dismissable `Alert` above the libraries table.
6. **Search result rows** switch from `role="listbox"/"option"` to the ARIA grid pattern (`role="grid" aria-multiselectable="true"`, rows `role="row" aria-selected={inSelection}`, each child wrapped in `role="gridcell"`), keeping the `aria-activedescendant` roving cursor. Grid cells may contain widgets, so the row checkbox and buttons stop being `nested-interactive`.
7. **Dirty-check pattern:** `Modal` gains `dirty?: () => boolean` (built-in "Discard changes?" confirm for every user dismissal) and passes `{ close }` to its `children`/`footer` snippets; Cancel buttons call `close()` so they run the check, Save sets `open = false` and skips it. No caller writes its own `onBeforeClose` for a plain dirty check.
8. Whole-branch review runs in two halves (Tasks 1–4, Tasks 5–9) so each reviewer holds its diff in context; one fix wave after both.

## Global constraints
- Svelte 5 runes + snippets, Tailwind 4, bits-ui 2.18. No new npm dependencies. **Never add `svelte-ignore`** (existing ones may be deleted when the markup they covered is replaced). Tokens, never raw palette, in anything new.
- Every task leaves all four green from `frontend/`: `npm run check` (0 errors / 0 warnings) · `npm run check:tests` · `npm test` · `npm run build`; entry chunk free of bits-ui: `grep -c "bits-ui\|AlertDialog" dist/assets/index-*.js` → 0; `ls dist/assets | grep -c KitPage` → 0.
- Behaviour-preserving: same API calls, same state transitions, same copy unless the spec names a change. Public props of `.svelte` components used elsewhere do not change (mount-style dialogs keep `onClose`/`onDeleted`/…).
- Migrated control rules (P5 checklist): one control per `Field`; `Field required` sets native `required`, so a `<form>` with its own validation adds `novalidate` (Login keeps native validation); `Checkbox label` is a string — a label with markup stays a raw `<label>`+`<input type=checkbox>` with token classes; `promptText`/`confirm` are awaited, never nested inside another dialog's `onBeforeClose`.
- Error placement: an action error renders as `<Alert tone="danger" onDismiss>` immediately below the control that triggered it and persists until dismissed or the action is retried; a load error renders as `<StateBlock state="error" message onRetry>` in place of the content; `toast()` only for fire-and-forget successes and for errors of actions whose control has already unmounted.
- Loading: `StateBlock state="loading"` (or `DataTable loading`) in place of the content; a refresh of already-shown data must not blank it (`DataTable keepRowsWhileLoading`, or keep the rows and set `aria-busy`).
- Empty: `StateBlock state="empty" message="…"` with one sentence that says what to do next ("Add a library to start indexing."), never a bare "None".
- Every `<select>` and `<input>` has a name: through `Field label`, or `aria-label` when the label is visually implied (toolbar filters, select-all boxes).
- Each task: one commit, `frontend:` prefix, new commit (never amend) once a docs commit sits above. Per-task **verification block** (run from `frontend/`, paste the numbers in the report):
  ```bash
  npm run check && npm run check:tests && npm test && npm run build
  grep -c "bits-ui\|AlertDialog" dist/assets/index-*.js; ls dist/assets | grep -c KitPage
  F="<files of this task>"; for p in '<input' '<select' '<textarea' '<table' 'window.confirm\|[^a-zA-Z.]confirm(' 'prompt(' 'bg-red-\|text-red-\|border-red-' 'var(--accent)' 'fixed inset-0' 'svelte-ignore'; do printf '%-40s' "$p"; grep -c -- "$p" $F | tr '\n' ' '; echo; done
  ```
  Expected: `<input`/`<select`/`<textarea`/`<table`/`confirm(`/`prompt(`/`bg-red-…`/`var(--accent)`/`fixed inset-0` → 0 in every file of the task except the exceptions the task lists; `svelte-ignore` not above the task's starting count. Browser smoke on the live stack (see Task 9 for `start.sh`) of every migrated dialog and form: open, Escape, dirty-check veto, Cancel, Save, focus return.

## Review focus
1. A modal with unsaved edits closed by Escape, backdrop, ×, **and Cancel** must ask; Save must not. (Task 1 gallery + each migrated modal.)
2. A failed action must leave its error visible next to the control after the user looks away and back; a refresh must not clear it until retried. (Each cluster; Task 9 checks three.)
3. Nested dialogs (FolderPicker inside LibraryEditModal; confirm inside any Modal): a click inside the inner one must not dismiss the outer; Escape closes only the inner. (Task 1 guard, Task 4 smoke.)
4. Keyboard on Search results after the grid change: arrows move the cursor, Enter opens, Space/click toggles selection, Tab leaves the grid; axe `nested-interactive` on a result page is 0. (Task 7, Task 9 acceptance.)
5. Numeric and required fields keep their P3 typing (`number | null`, `numOrNull`) after moving to `Input`; `Field required` does not re-enable native bubbles on forms that validate themselves. (Each cluster; reviewer greps `numOrNull|intOrNull` sites are intact.)

---

## Task 1: Kit prep — Modal `dirty`/`close`, nested-dialog guard, `promptText()`

**Files:** `src/lib/ui/Modal.svelte`, `src/lib/ui/confirm.svelte.ts`, `src/lib/ui/ConfirmDialog.svelte`, `src/lib/ui/ConfirmHost.svelte`, `src/lib/ui/promptRules.ts` (new), `tests/promptRules.node.test.ts` (new), `src/lib/ui/index.ts`, `src/lib/KitPage.svelte`.

**Interfaces produced:**
- `Modal` props add `dirty?: () => boolean`. Snippets: `children: Snippet<[{ close: () => void }]>`, `footer?: Snippet<[{ close: () => void }]>` (zero-arg snippets still type-check). `CloseReason` adds `"cancel"` (a user dismissal: runs the check, calls `onClose`).
- `promptText(opts: PromptOptions): Promise<string | null>` from `./confirm.svelte.ts` and the barrel; `PromptOptions = { title: string; message?: string; label: string; initial?: string; placeholder?: string; confirmLabel?: string; cancelLabel?: string; required?: boolean /* default true */; validate?: (value: string) => string | undefined /* error copy */ }`. Resolves the trimmed value, or `null` on Cancel/Escape.
- `promptRules.ts`: `export function checkPromptValue(raw: string, opts: { required?: boolean; validate?: (v: string) => string | undefined }): { value: string; error?: string }` — trims; `required` (default true) and empty → `error: "Enter a value."`; else `validate(value)` message if any.

- [ ] **Step 1: Tests for `checkPromptValue`** in `tests/promptRules.node.test.ts`: empty + required → error "Enter a value."; empty + `required:false` → `{ value: "" }`; `"  Movies "` → `value: "Movies"`; `validate` returning "Taken" → `error: "Taken"`; `validate` returning undefined → no error. Run `npm test` → fails (module missing).
- [ ] **Step 2: Implement `promptRules.ts`**; `npm test` green.
- [ ] **Step 3: `confirm.svelte.ts`**: `type ConfirmRequest = (ConfirmOptions & { kind: "confirm"; resolve: (ok: boolean) => void }) | (PromptOptions & { kind: "prompt"; resolve: (v: string | null) => void })`; `confirm()` pushes `kind: "confirm"`; add `promptText()`; `settleConfirm(req, ok)` becomes `settleConfirm(req, result: boolean | string | null)` (host-only); `cancelAllConfirms()` resolves prompts with `null`. Keep the header comment accurate.
- [ ] **Step 4: `ConfirmDialog`/`ConfirmHost`**: for `kind === "prompt"` render `<Field label={label} error={err}><Input bind:value autocomplete="off" /></Field>` under the message, `initialFocus` on it, Enter submits; confirm action runs `checkPromptValue` and shows the error inline instead of closing; Cancel/Escape resolve `null`. Reuse the `typedName` input wiring (same component, a second mode), do not duplicate the dialog.
- [ ] **Step 5: `Modal`**: add `"cancel"` to `CloseReason`; `dirty` prop: when `onBeforeClose` is absent and `dirty` is set, `requestClose(reason)` for `reason !== "caller"` awaits `confirm({ title: "Discard changes?", message: "Your edits will be lost.", confirmLabel: "Discard", tone: "danger" })` when `dirty()` is true (import `confirm` from `./confirm.svelte.ts`); pass `{ close: () => void requestClose("cancel") }` to `children` and `footer`. **Nested-dialog guard:** in `onInteractOutside`, also return when `e.target` is inside a `[role="dialog"], [role="alertdialog"]` other than this panel (covers FolderPicker-in-LibraryEditModal and confirm-over-Modal). Update the header comment: the `dirty` prop is the default dirty check; `onBeforeClose` is for anything more; Cancel calls `close()`.
- [ ] **Step 6: Gallery**: the existing edit-modal demo switches to `dirty={() => dirty}` and `{#snippet footer({ close })}<Button variant="secondary" onclick={close}>Cancel</Button>…`; add a "Prompt" button demonstrating `promptText({ title: "Name this search", label: "Name", validate: v => v.length > 40 ? "Keep it under 40 characters." : undefined })` toasting the result; add a nested demo: a Modal whose body button opens a second small Modal (click inside the inner must not close the outer).
- [ ] **Step 7: Verification block** (files: the ui ones + KitPage; `<input`/`<select` exceptions: none expected). Browser: gallery modal Escape/backdrop/×/Cancel ask when dirty, Save does not; nested demo; prompt Enter/Escape/empty.
- [ ] **Step 8: Commit** `frontend: kit prep for P5 — Modal dirty/close, nested-dialog guard, promptText()`.

---

## Task 2: Admin panels A

**Files:** `src/lib/UsersPanel.svelte`, `src/lib/RolesPanel.svelte`, `src/lib/RoleForm.svelte`, `src/lib/RbacPanel.svelte`, `src/lib/ServiceAccountsPanel.svelte`, `src/lib/SessionsPanel.svelte`, `src/lib/AuditPanel.svelte`.

- [ ] **Step 1:** Inputs/selects/textareas → `Field` + `Input`/`Select`/`Textarea`/`Checkbox` (inline-edit rows in UsersPanel use `Field` with `class` for width; toolbar filters in AuditPanel/SessionsPanel use `aria-label`). Forms with own validation get `novalidate`.
- [ ] **Step 2:** `confirm(...)` strings → `await confirm({ title, message, tone: "danger", confirmLabel })` (UsersPanel delete, RolesPanel delete, ServiceAccounts delete/revoke, SessionsPanel sign-out-all ×2, RbacPanel group delete). Keep the exact consequence copy as `message`/`details`.
- [ ] **Step 3:** List tables → `DataTable` with `caption`, `rowKey`, `cell` snippets for the action column (UsersPanel users, RolesPanel roles, RbacPanel groups + grants, ServiceAccounts, AuditPanel events, SessionsPanel sessions). Expanded inline-edit rows (UsersPanel) stay as a `cell`-rendered toggle whose editor renders **below** the table (keyed by the selected user), not as a nested row.
- [ ] **Step 4:** Status chips → `Badge tone={toneForStatus(status)}`; red banners → `Alert`/`StateBlock` per the global rules; "Loading…"/"No … yet" → `StateBlock`.
- [ ] **Step 5:** Verification block; browser: add user, delete user (confirm), role save error placement, audit filter labels announced (inspect `aria-label`).
- [ ] **Step 6: Commit** `frontend: admin panels A onto the kit (users, roles, RBAC, service accounts, sessions, audit)`.

---

## Task 3: Admin panels B, taxonomy, account, login

**Files:** `src/lib/ApiKeysPanel.svelte`, `src/lib/LlmKeysPanel.svelte`, `src/lib/AuthProvidersPanel.svelte`, `src/lib/PresetsPanel.svelte`, `src/lib/CustomFieldsPanel.svelte`, `src/lib/TaxonomyPage.svelte`, `src/lib/AccountPage.svelte`, `src/lib/LoginPage.svelte`.

- [ ] **Step 1:** Dialogs → `Modal`: CustomFieldsPanel `editing` (`open={!!editing}`, `onClose={() => (editing = null)}`, `dirty`), TaxonomyPage `dialog` (same; `size="md"`; Cancel calls `close()`).
- [ ] **Step 2:** Controls → `Field` family (AuthProvidersPanel keeps the P3 sentinel/`numOrNull` logic; TaxonomyPage's two unlabeled selects get `Field label`). Checkboxes with string labels → `Checkbox`.
- [ ] **Step 3:** `confirm` → `confirm()` (ApiKeys revoke, LlmKeys revoke, Presets delete, CustomFields delete, Taxonomy delete ×2). Tables → `DataTable` (ApiKeys, LlmKeys, Presets, CustomFields). Badges, alerts, states per global rules. LoginPage: `Field`+`Input`+`Button` only; **no** `novalidate` (it relies on `minlength`/`required`).
- [ ] **Step 4:** Verification block; browser: taxonomy create/edit dialog dirty-check, custom-field edit, login error placement.
- [ ] **Step 5: Commit** `frontend: admin panels B, taxonomy, account and login onto the kit`.

---

## Task 4: Libraries cluster

**Files:** `src/lib/LibrariesPage.svelte`, `src/lib/LibraryEditModal.svelte`, `src/lib/LibraryDiagnoseModal.svelte`, `src/lib/FolderPicker.svelte`, `src/lib/ScheduleField.svelte`, delete `src/lib/DeleteLibraryDialog.svelte`.

- [ ] **Step 1: `LibraryEditModal` → `Modal`** (mount-style: `<Modal open onClose={onClose} size="lg" dirty={() => isDirty()} title=…>`; body unchanged inside; footer Cancel = `close()`; Save sets a local `open=false`? No: mount-style components call `onClose()` after a successful save (parent unmounts) — so give `Modal` `bind:open={open}` with `let open = $state(true)` and on Save set `open = false` then `onSaved()`). Keep the `hashCeiling` bad-input logic and the `showPicker` Escape guard (now unnecessary: delete it, the nested guard handles it). `FolderPicker` → `Modal size="md"` mount-style.
- [ ] **Step 2: `LibraryDiagnoseModal` → `Modal size="xl"`** mount-style; its three tables stay raw (ruling 4: caption/scope).
- [ ] **Step 3: Delete flow:** LibrariesPage calls `confirm({ title: "Delete library?", tone: "danger", typedName: lib.name, message: <existing paragraph>, details: ["All indexed items and scan history are removed", "Search results for these items disappear", "This cannot be undone"] , confirmLabel: "Delete library" })`, then `deleteLibrary(id, lib.name)`; map 404 → treat as deleted, 409 → "A scan is currently running for this library. Cancel or wait for it to finish, then delete.", 422 → "Confirmation name did not match.", else `errorMessage(e, { verb: "delete this library" })` into `deleteError` rendered as a dismissable `Alert` above the table. Remove `DeleteLibraryDialog.svelte`.
- [ ] **Step 4:** LibrariesPage `targeting` dialog → `Modal`; the two bulk `confirm`s → `confirm()` with `details`; the libraries table and the failed-jobs table → `DataTable` (library row expanders/scan progress render in `cell` snippets); error chips → `Badge tone="danger"`; `agentStatusClass` → `toneForStatus`; red banners → `Alert` (the fingerprint-mismatch banner keeps its title via `Alert title`). `ScheduleField` inputs/selects → kit controls with `aria-label`s where the row layout has no room for labels.
- [ ] **Step 5:** Verification block (exceptions: LibraryDiagnoseModal `<table` ×3 raw). Browser: edit modal dirty-check through all five paths incl. FolderPicker open → click inside picker → outer stays; delete with typed name, 409 copy (start a scan first).
- [ ] **Step 6: Commit** `frontend: libraries cluster onto the kit; retire DeleteLibraryDialog`.

---

## Task 5: Jobs cluster

**Files:** `src/lib/JobsPage.svelte`, `src/lib/LogsPanel.svelte`, `src/lib/ExportsPanel.svelte`, `src/lib/UpdatesPanel.svelte`, `src/lib/AgentStatusPanel.svelte`.

- [ ] **Step 1:** JobsPage `confirm` (line ~380) → `confirm()`, `prompt` (~406) → `promptText()` with `validate` for the numeric/priority input it collects (keep the current parse rule as the validator message). Six tables → `DataTable` where rows are flat (queues, jobs, backups, …); a table whose rows embed a progress widget renders it via `cell`. Stalled/failed counters → `Badge tone="warn"|"danger"`.
- [ ] **Step 2:** LogsPanel's two unlabeled selects → `Field`/`aria-label` + `Select`; the log pane keeps `tabindex="0"` with `aria-label` (scrollable-region-focusable). ExportsPanel/UpdatesPanel/AgentStatusPanel: tables, badges, alerts, states per global rules.
- [ ] **Step 3:** Verification block; browser: queue action with confirm, priority prompt validation, failed-job badge tones in dark mode.
- [ ] **Step 4: Commit** `frontend: jobs cluster onto the kit (jobs, logs, exports, updates, agent status)`.

---

## Task 6: Reports, alerts, timeline

**Files:** `src/lib/ReportsPage.svelte`, `src/lib/CustomReportsPage.svelte`, `src/lib/ReportSchedulesPanel.svelte`, `src/lib/TimelinePage.svelte`, `src/lib/AlertsPage.svelte`.

- [ ] **Step 1:** AlertsPage (39 controls, 3 tables, 2 forms): channel/rule forms → `Field` family (`novalidate`); the select-all checkbox gets `aria-label="Select all rules"` instead of `title`; the bulk toolbar selects get `aria-label`s; channels/rules/events tables → `DataTable`; `confirm` ×2 → `confirm()`; "Loading…"/"No channels yet." → `StateBlock` ("Add a channel to receive alerts."); errors → `Alert` beside Save.
- [ ] **Step 2:** ReportsPage/CustomReportsPage/ReportSchedulesPanel: selects labelled, tables → `DataTable`, `confirm` → `confirm()`, states. TimelinePage: the one select labelled; red banners → `Alert`/`StateBlock`.
- [ ] **Step 3:** Verification block; browser: create a rule with a missing field (error placement), delete channel confirm, schedules table sort.
- [ ] **Step 4: Commit** `frontend: reports, alerts and timeline onto the kit`.

---

## Task 7: Search cluster

**Files:** `src/lib/SearchPage.svelte`, `src/lib/ItemDetail.svelte`, `src/lib/BulkEditBar.svelte`, `src/lib/BrowsePage.svelte`, `src/lib/FilterBuilderPage.svelte`, `src/lib/RetrievePanel.svelte`, `src/lib/map/MapPanel.svelte`.

- [ ] **Step 1: Result rows → grid pattern** (ruling 6) in both the virtual list and the tile grid: container `role="grid" aria-multiselectable="true" aria-label=… aria-activedescendant` (unchanged id), row `role="row" aria-selected={selectedIds.has(id)}` with the cursor shown by class only, children wrapped in `<div role="gridcell">` (checkbox cell, name cell, path cell, actions cell). Delete the `svelte-ignore` comments the old markup needed where the new markup no longer triggers them. Keyboard handling at the container is unchanged.
- [ ] **Step 2:** Saved searches: `window.prompt` ×2 → `promptText({ title: "Name this search" | "Rename search", label: "Name", initial, validate: duplicate-name check if the list is loaded })`; `window.confirm` → `confirm()`. The two suggestion `listbox`es (principal, tag) stay.
- [ ] **Step 3:** Controls → `Field` family (the sort/per-page selects get `aria-label`s; FilterBuilderPage's seven unlabeled selects get `Field label` or `aria-label` naming the clause part: "Field", "Operator", "Value"); `text-[var(--accent)]`/`ring-[var(--accent)]`/`border-[var(--accent)]`/`bg-[var(--accent)]` → `text-accent-text`/`ring-accent`/`border-accent`/`bg-accent`; BulkEditBar and RetrievePanel errors → `Alert` beside the action.
- [ ] **Step 4: `ItemDetail` → `Modal size="xl"`** mount-style (`open` state + `onClose`), `initialFocus` on the panel heading region as today; its two tables stay raw (ruling 4). The backdrop `<button>`, `svelte:window` Escape handler and `role="dialog"` wrapper go.
- [ ] **Step 5:** BrowsePage: the folder form → `Field` family, `confirm`-free; states → `StateBlock`. MapPanel: three inputs → `Input` with `aria-label` (keep the P3 NaN/blank handling).
- [ ] **Step 6:** Verification block (exceptions: ItemDetail `<table` ×2; two suggestion listboxes). Browser: results keyboard (arrows, Enter, Space toggles selection), axe on a results page → `nested-interactive` 0; save/rename search via prompt; detail modal Escape + focus return to the row.
- [ ] **Step 7: Commit** `frontend: search cluster onto the kit; result rows as an ARIA grid`.

---

## Task 8: AgentsPage

**Files:** `src/lib/AgentsPage.svelte` (4034 lines; two commits allowed: controls+tables, then dialog+confirms+states).

- [ ] **Step 1:** 60 controls → `Field` family (policy editors keep their `numOrNull`/validator logic; the collector "escape hatch" input keeps its Enter handler via `onkeydown` on `Input`). Seven tables → `DataTable` where flat (fleet, tokens, groups, rollouts, commands…); a table with per-row expanders renders the expander in a `cell` and the detail below the table.
- [ ] **Step 2:** The config-group dialog → `Modal size="xl"` with `dirty`, Cancel = `close()`. Fourteen `confirm` sites → `confirm()` with `tone: "danger"` for revoke/delete/purge and `typedName` for the permanent delete; the "deliberately not confirm-guarded" action stays unguarded (keep its comment).
- [ ] **Step 3:** 24 red sites → `Alert` beside the action / `StateBlock` for loads; 17 chips → `Badge tone={toneForStatus(...)}` (online/offline/pending map through `toneForStatus` already); 10 loading/empty strings → `StateBlock` with next-step copy ("Enrol an agent to see it here.").
- [ ] **Step 4:** Verification block; browser: config-group dialog dirty-check, revoke confirm, a per-row action error staying visible, fleet table on a phone width (mobile sort row).
- [ ] **Step 5: Commit(s)** `frontend: AgentsPage onto the kit — controls and tables` / `… — dialog, confirms, states`.

---

## Task 9: Shell leftovers, dead code, browser acceptance

**Files:** `src/App.svelte` (the unlabeled `<select>` at ~341 gets `aria-label`; accent classes → tokens), `src/lib/AboutPage.svelte`, `src/lib/HelpPage.svelte`, `src/lib/Breadcrumbs.svelte`, `src/lib/FolderTreemap.svelte`, `src/lib/RawView.svelte`, `src/lib/cards/TrackTable.svelte`, `src/lib/CustomFieldInput.svelte`, `src/lib/TaxonomySelector.svelte`, `src/lib/api.ts` (remove exports that lost their last caller), `memory/projects/review-2/p5-acceptance.md` (new).

- [ ] **Step 1:** Raw tables that stay (ruling 4) get caption/scope/no-empty-`th`; `CustomFieldInput`'s controls → kit controls (it is rendered inside `Field`s by callers; it must forward the Field context, i.e. use `Input`/`Select`/`Checkbox` directly); remaining `var(--accent)` → tokens; `grep -rn "<select" src --include=*.svelte | grep -v "aria-label\|lib/ui"` → only selects inside a `Field`.
- [ ] **Step 2: Acceptance run** (orchestrator-dispatched agent, same recipe as P4): start the stack (`/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/review2/live/start.sh`, seeded library, `vite` dev server on :5173), Playwright + axe on every route in both themes at 1440 and 390 wide, logged in as admin; per-rule totals vs the review-2 baseline (`review-2/live-axe-summary.md`: `select-name` 60, `nested-interactive` 148, `scrollable-region-focusable` 10, `label-title-only` 1+, `empty-table-header` 32, `color-contrast` 2,450). Targets: `select-name` 0, `nested-interactive` 0, `label-title-only` 0, `empty-table-header` 0, `scrollable-region-focusable` 0, `color-contrast` lower than baseline (P9 finishes it). Keyboard flows: five migrated modals (Escape, dirty veto, Cancel, Save, focus return), the prompt dialog, Search grid keys, nested picker. Console: no errors on any route.
- [ ] **Step 3:** Fix every failure in the owning file; re-run the failing checks; write `review-2/p5-acceptance.md` (per-rule table before/after, flows pass/fail, shots list).
- [ ] **Step 4: Commit** `frontend: shell leftovers, dead exports, P5 browser acceptance`.

---

## Self-review notes
- Spec coverage: modals (T1,3,4,7,8), confirm/prompt (T1–8), inputs (T2–9), tables (T2–9 + ruling 4), badges (T2,4,5,8), error banners (all), empty/loading (all), accent class (T7,9), axe `select-name`/`label-title-only` (T3,5,6,7,9), Search rows (T7), page splits → P9 (ruling 1). P5 checklist: Cancel/dirty (ruling 7, T1), Checkbox string label (constraints), `expect:"response"` 429 — unused by P5, stays a P7 item; `Field required`/`novalidate` (constraints).
- Types: `close: () => void` in both snippets; `promptText` returns `string | null`; `checkPromptValue` returns `{ value; error? }`.
