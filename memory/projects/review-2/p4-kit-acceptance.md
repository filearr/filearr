# P4 UI-kit acceptance — `#/kit` gallery

- Repo `/home/user/repo`, branch `claude/frontend-p4-kit-api` @ 99e45fc (read-only; `git status --short` empty at end).
- Vite dev server `http://127.0.0.1:5173/#/kit` (gallery is dev-only), API :8000 with auth on. Logged in through the login wall as `admin`.
- Script: `accept.mjs` (Playwright/Chromium, `serviceWorkers: 'block'`). Raw log `run.log`, per-assertion data `results.json`, field attributes `field-info.json`, console `console.json`, axe JSON in `axe/`, screenshots in `shots/`.
- Viewports: desktop 1440×900, phone 390×844; light and dark via the header Theme button (`html.dark` checked).
- Interactive checks ran at desktop light (DataTable sort also at phone). Dark got the initialFocus re-check, the token read-out, and full-page and section screenshots. axe ran in all four viewport/theme combinations, plus separate scans with the edit modal, the discard confirm and the popover open.

## Summary

| # | Check | Result | Key evidence | Screenshots |
|---|---|---|---|---|
| 1 | Field short form + snippet form | **PASS** (5/5) | Library name `required`, no describedby. Scan schedule → `cN-hint` (rendered). Share path → `cN-error` + `aria-invalid="true"`. Size limit (snippet) → hint. Every `<label for>` matches `control.labels`. | c1-forms-desktop-light.png |
| 2 | Checkbox / Switch | **PASS** (11/12, 1 assertion script-side, see note) | The Tab order covers every enabled control and skips both disabled ones. Space toggles a Checkbox; Enter does not. Space **and** Enter toggle a Switch (`aria-checked` flips). `indeterminate === true`. The Field-wrapped checkbox has `aria-describedby="c8-hint"`. Accessible name is **"Notifications Email me when a scheduled scan fails"**: the two labels concatenate, as expected. | c2-checkbox-switch-desktop-light.png, c2-forms-phone-*.png |
| 3 | Modal dismissible={false} + dirty + initialFocus | **FAIL** (2 of 19 assertions) | initialFocus = `input#kit-edit-path` in light and dark. Escape and backdrop do nothing, as documented. Cancel and Close prompt when dirty. Confirm Cancel keeps the typed text. Discard closes and focus returns to the opener. **Save on a dirty form raises "Discard changes?"** (defect D1). | c3-*.png, c3-DEFECT-save-prompts-discard-desktop-light.png |
| 4 | Original Modal regression | **PASS** (2/2) | Open md: Escape closes, focus returns to "Open md", "Modal dismissed" toast. Open lg: backdrop closes, focus returns to "Open lg". | c4-original-modal-desktop-light.png |
| 5 | confirm({ details }) | **PASS** (3/3) | `<ul>` with 3 `<li>` inside the aria-describedby node. Cancel focused first. Escape gives the "Cancelled" toast, and focus returns to the opener. | c5-confirm-details-desktop-light.png |
| 6 | Popover shorthand | **FAIL** (1 of 6 assertions) | The trigger is a single button (`aria-haspopup="dialog"`, `aria-expanded` true while open, `aria-controls` points at the panel). The panel has `aria-label="Filters"`. Escape closes it and focus returns to the trigger, and the snippet form behaves the same. **The panel has no `role="dialog"`** (defect D2). | c6-popover-*.png |
| 7 | DataTable states + phone sort | **PASS** (9/9) | Loading: 8 rows stay, tbody opacity 0.6, wrapper `aria-busy="true"` (back to false when off). Error: `role=alert` with Retry, which gives the "Retried" toast. Empty: "No files scanned yet.". Empty+Loading: loading block, not busy. Phone: Sort by select + direction button; Size sorts asc and then desc. Back on desktop: `Size ▼` has `aria-sort=descending`, Name has `none`, and the first row matches the first card. | c7-*.png |
| 8 | Tokens | **PASS** (4/4) | Light `--accent-text` = **#4f46e5**: 6.29 / 6.01 / 5.74 on surface / -2 / -3. Dark `--accent-text` = **#8d87ee**: 6.56 / 5.81 / **4.76** (surface-3 #1e293b, ≥ 4.5). | c8-tokens-desktop-{light,dark}.png |
| 9 | axe-core | **FAIL (minor)** | No `nested-interactive`, `select-name`, `label`, `aria-*` or `color-contrast` hit on any new component. The only hit touching a new component is `region` (moderate), 1 node per theme: the Popover's portal wrapper `[data-bits-floating-content-wrapper]` sits outside landmarks. | axe/*.json |
| 10 | Console | **PASS** | After login: 0 console errors, 0 pageerrors, 0 failed requests. Before login there were 2 expected entries: `401 GET /api/v1/auth/me` (the auth gate probe) and its "Failed to load resource" console line. The SW-blocked warning is ignored. | console.json |

**Counts:** checks 7 PASS / 3 FAIL (3, 6, 9). Assertions: 56 PASS / 4 FAIL.

Note on check 2: the first run's "Tab order" FAIL was a bug in my script's matcher. The Field-wrapped checkbox's name starts with "Notifications", and the matcher used startsWith. After the fix the sequence is correct (see the evidence below).

Note on check 3 expectations: the brief suggested "untouched Escape closes" and "backdrop with a dirty field prompts". The demo text and the Modal prop doc both say `dismissible={false}` means **Escape and outside clicks do nothing**, and bits-ui's escape layer returns early on `"ignore"`. The run confirmed the documented behaviour: Escape and backdrop are inert whether the form is dirty or not, and Cancel and Close are the ways out that prompt. Those assertions were therefore judged against the documented spec and PASS.

## axe summary

| Scan | Violations (rule: impact × nodes) | Touching new components |
|---|---|---|
| kit-desktop-light | none | 0 |
| kit-desktop-dark | color-contrast: serious × 18 | 0 (all app header/footer: `text-slate-500` #62748e on #020617 = 4.23; path-format select 3.06) |
| kit-phone-light | none | 0 |
| kit-phone-dark | color-contrast: serious × 17 | 0 (same shell nodes) |
| edit-modal-desktop-light | none | 0 |
| edit-modal-desktop-dark | color-contrast × 18 | 0 (shell) |
| discard-confirm-desktop-light | none | 0 |
| discard-confirm-desktop-dark | color-contrast × 18 | 0 (shell) |
| popover-open-desktop-light | region: moderate × 1 | **1**: Popover portal wrapper |
| popover-open-desktop-dark | color-contrast × 18, region: moderate × 1 | **1**: Popover portal wrapper |

Total violation nodes touching new components: **2**, both `region` (moderate) on the Popover portal wrapper, one per theme. The dark color-contrast hits come from the pre-existing app shell (header nav, Paths select, footer), not from the kit. axe also did not flag the `aria-label` on the role-less popover `<div>`: it has `tabindex="-1"`, so axe treats it as focusable. A screen reader will still ignore the name, so D2 stands.

## Console summary

- Pre-login: `401 GET /api/v1/auth/me` plus `console.error: Failed to load resource … 401`. This is the auth gate's session probe before sign-in, which is expected.
- Post-login (the whole run, including 10 axe scans and both viewports and themes): nothing.

## Defects found

**D1. Saving the dirty-check modal triggers "Discard changes?" (the demo's dirty flag goes stale).**
Repro: `#/kit` → "Edit with unsaved changes" → change Library name → **Save**. The "Library saved" toast appears, **and at the same time** a "Discard changes?" alert dialog opens over the still-open modal. If you press Cancel there, the already-saved modal stays open, and Close with no further edits prompts again. Only Discard gets out.
Cause, in `frontend/src/lib/KitPage.svelte`: `savedEdit` is a plain object, not `$state`. `dirty = $derived(editName !== savedEdit.name || …)` does not re-run when `saveEdit()` mutates `savedEdit`, and reassigning `editName` to the same string does not signal. So `onBeforeClose` still sees `dirty === true`.
Possible fixes: make `savedEdit` a `$state` object, or have `saveEdit` close via a flag that `onBeforeClose` honours. The Modal component itself behaves as documented.
Screenshot: `c3-DEFECT-save-prompts-discard-desktop-light.png`. Assertions 3(f) and 3(f2).

**D2. The Popover panel has no `role="dialog"`.**
Repro: `#/kit` → Popover section → click "Filters ▾" (either form) → inspect `[data-popover-content]`. It renders as `<div forcemount="false" aria-label="Filters" tabindex="-1" data-popover-content …>` with **no role**. The trigger still advertises `aria-haspopup="dialog"`.
The `Popover.svelte` doc comment says "bits-ui renders it as role=dialog", but bits-ui 2.18.2 (installed) sets no role on Popover.Content. As a result the `aria-label`/`ariaLabel` on the panel is not announced, since a name on a generic div is ignored.
Possible fix: pass `role="dialog"` on `<Popover.Content>` in `Popover.svelte`.
Minor, also visible here: bits-ui leaks a `forcemount="false"` attribute onto the DOM node.
Screenshot: `c6-popover-shorthand-desktop-light.png`.

**D3 (minor, axe `region`, moderate).** The open Popover's portal wrapper `[data-bits-floating-content-wrapper]` is appended to `<body>` outside any landmark. This is common to portalled popovers. Low priority; it would go away if the portal target were inside `<main>`.

Not a kit defect, noted for completeness: the app shell's dark-mode `text-slate-500` on `#020617` (4.23:1) and the path-format select (3.06:1) fail color-contrast. These are pre-existing and also in review2.

## Stack

Left **running** for reuse:
- Postgres 16 :5433
- Meilisearch :7700
- API :8000 (auth on)
- worker
- `vite preview` :4173
- Vite dev :5173

Started via `review2/live/start.sh`; stop with `review2/live/stop.sh`. Login `admin` / `<review-password>`.

## Per-assertion evidence

### Check 1: PASS (5/5)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | Library name: id, label for matches, required, no aria-invalid, no describedby (no hint/error) | {"for":"c1","tag":"input","id":"c1","describedby":null,"refs":[],"ariaInvalid":null,"required":true,"labelsMatch":true} |
| PASS | Scan schedule: describedby -> rendered hint, not invalid, not required | {"for":"c2","tag":"select","id":"c2","describedby":"c2-hint","refs":[{"id":"c2-hint","rendered":true,"text":"Nightly scans start at 02:00 server time."}],"ariaInvalid":null,"required":false,"labelsMatch":true} |
| PASS | Share path: describedby -> rendered error, aria-invalid="true" | {"for":"c3","tag":"input","id":"c3","describedby":"c3-error","refs":[{"id":"c3-error","rendered":true,"text":"Path must be absolute, e.g. /mnt/media/movies."}],"ariaInvalid":"true","required":false,"labelsMatch":true} |
| PASS | Size limit (snippet form): id/for match, describedby -> hint, not invalid | {"for":"c4","tag":"input","id":"c4","describedby":"c4-hint","refs":[{"id":"c4-hint","rendered":true,"text":"Custom control: Input plus a unit. Larger files are skipped during scans."}],"ariaInvalid":null,"required":false,"labelsMatch":true} |
| PASS | Other Fields in section (Notes, Small, Disabled, Notifications) have matching label/for | Notes: for=c5 match=true invalid=null; Small input: for=c6 match=true invalid=null; Disabled input: for=c7 match=true invalid=null; Notifications: for=c8 match=true invalid=null |

### Check 2: PASS (11/11)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | Tab order reaches every enabled Checkbox/Switch in order | <input type=checkbox> "Email me when a scan fails" -> <input type=checkbox> "Weekly digest" -> <input type=checkbox> "Select all libraries Indeterminate: some libraries are selected." -> <input type=checkbox> "Follow symlinks Scans the target of each link. Can loop on misconfigured shares." -> <input#c8 type=checkbox> "Notifications Email me when a scheduled scan fails" -> <button role=switch type=button> "Watch fold |
| PASS | Disabled Checkbox and Switch skipped by Tab | not in tab sequence: "Sync to cloud", "Hardware transcoding" |
| PASS | Disabled ones are inert (disabled attr, click does nothing) | {"cbDisabled":true,"swDisabled":true,"afterClick":{"cb":false,"sw":"false"}} |
| PASS | Indeterminate checkbox reports indeterminate === true | {"indeterminate":true,"checked":false} |
| PASS | Space toggles Checkbox "Email me when a scan fails" (Enter does not) | checked true -> Space false -> Enter false |
| PASS | Space toggles Checkbox "Weekly digest" (Enter does not) | checked false -> Space true -> Enter true |
| PASS | Space toggles Checkbox "Follow symlinks" (Enter does not) | checked false -> Space true -> Enter true |
| PASS | Space and Enter toggle Switch "Watch folders for changes" | aria-checked true -> Space false -> Enter true |
| PASS | Space and Enter toggle Switch "Read-only mode" | aria-checked false -> Space true -> Enter false |
| PASS | Checkbox in <Field label hint>: aria-describedby -> hint | {"id":"c8","describedby":"c8-hint","hint":["Sent to the address on your account."],"labels":["Notifications","Email me when a scheduled scan fails"]} |
| PASS | Accessible name of Field-wrapped Checkbox (two labels concatenated expected — informational) | ariaSnapshot: - checkbox "Notifications Email me when a scheduled scan fails" [checked] ; labels: ["Notifications","Email me when a scheduled scan fails"] |

### Check 3: FAIL (16/18)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | initialFocus lands on the second input (#kit-edit-path) | <input#kit-edit-path> "Share path" |
| PASS | (a) untouched + Escape: modal stays open (dismissible={false}: "Escape and outside clicks do nothing") | open after Escape = true; brief's alternative expectation "untouched Escape closes" does NOT hold by design |
| PASS | (a2) untouched + backdrop click: modal stays open | open=true |
| PASS | (a3) untouched + Close: closes with no prompt, focus returns to opener | closed=true prompt=0 focus=<button type=button> "Edit with unsaved changes" |
| PASS | (b1) dirty + Escape: no close (and per design no prompt either) | open=true confirmShown=0 status="Unsaved changes" |
| PASS | (b2) dirty + footer Cancel: "Discard changes?" confirm appears on top, Cancel focused | confirm visible=true, focus=<button#bits-c37 type=button> "Cancel" |
| PASS | (b3) confirm Cancel: modal still open with typed text | open=true value="/mnt/media/movies-typed" |
| PASS | (b3b) focus after confirm Cancel stays inside the modal | <button type=button> "Cancel" |
| PASS | (b4) dirty + Escape again: still open, no prompt | open=true prompt=0 |
| PASS | (c) dirty + backdrop click: modal stays open (per design no prompt; brief expected prompt) | open=true prompt=0 |
| PASS | (d) dirty + Close button: "Discard changes?" prompt | confirm visible=true |
| PASS | (d2) Discard: modal closes, focus returns to opener | closed=true focus=<button type=button> "Edit with unsaved changes" |
| PASS | (e) Escape on the Discard confirm cancels it; modal stays open with edit | open=true confirm=0 name="Movies edited" |
| **FAIL** | (f) dirty + Save: closes without prompt, "Library saved" toast, focus to opener | toast=true discardPromptAfterSave=1 modalOpen=true focus=<button#bits-c62 type=button> "Cancel" |
| **FAIL** | (f2) after Save, closing an unedited (saved) modal should not prompt | after stray-prompt Cancel modalOpen=true; Close with no further edits -> prompt=1 |
| PASS | (f3) after Discard on the stray prompt, focus returns to opener | <button type=button> "Edit with unsaved changes" |
| PASS | (g) reopen shows the saved value (state reset on open) | name="Movies edited" |
| PASS | Dark: initialFocus also lands on #kit-edit-path | <input#kit-edit-path> "Share path" |

### Check 4: PASS (2/2)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | Open md: opens, Escape closes, focus returns to "Open md", onClose toast | initial focus=<input#c84> "Library name"; closed=true; focus after=<button type=button> "Open md"; toast "Modal dismissed"=true |
| PASS | Open lg: backdrop click closes, focus returns to "Open lg" | closed=true focus=<button type=button> "Open lg" |

### Check 5: PASS (3/3)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | details render as <ul> with 3 <li> (inside aria-describedby) | ["Removes 1,204 indexed items","Cancels 2 queued jobs","Keeps the files on disk"]; ul inside describedby=true |
| PASS | Cancel focused first | <button#bits-c93 type=button> "Cancel" |
| PASS | Escape resolves -> "Cancelled" toast; focus returns to opener | closed=true toast=true focus=<button type=button> "Remove library (with details)…" |

### Check 6: FAIL (5/6)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | label="Filters" button opens a panel with aria-label="Filters" | trigger={"text":"Filters▾","ariaExpanded":"false","haspopup":"dialog"} -> expanded=true; panel aria-label="Filters"; aria-controls=null -> panel=false |
| **FAIL** | panel has role="dialog" (trigger advertises aria-haspopup="dialog"; Popover.svelte doc says bits-ui renders role=dialog) | panel role=null; outerHTML: <div forcemount="false" aria-label="Filters" class="z-50 max-w-[calc(100vw-1rem)] rounded-panel border border-border bg-surface-2 p-3 shadow-lg " tabindex="-1"  |
| PASS | shorthand trigger is a single button (no nested button) | nested buttons=0 |
| PASS | Escape closes shorthand popover; focus returns to trigger | <button#bits-c15 type=button> "Filters▾" (is first Filters: true) |
| PASS | Snippet-form Popover opens (aria-label), Escape closes, focus returns | aria-label="Filters" role=null focus=<button#bits-c16 type=button> "Filters" back=true |
| PASS | Snippet-form Apply closes via bind:open | visible after Apply=false |

### Check 7: PASS (9/9)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | Loading: rows remain, tbody dimmed, wrapper aria-busy="true" | {"rows":8,"opacity":"0.6","busy":"true"} |
| PASS | Loading off: aria-busy back to "false" | aria-busy="false" |
| PASS | Error: role="alert" block with Retry button | {"count":1,"text":"Could not load files from nas-01. Retry","retry":1} |
| PASS | Retry click shows "Retried" toast | toast region: "Modal dismissed × Cancelled × Retried ×" |
| PASS | Empty: StateBlock "No files scanned yet." | No files scanned yet. |
| PASS | Empty+Loading: loading block shown, aria-busy not set (live region not muted) | tbody="Loading…" aria-busy=false |
| PASS | Phone: "Sort by" <select> and direction button visible | select visible=true (value="name"), dir button visible=true |
| PASS | Phone: choosing Size sorts cards asc; direction button flips to desc | asc: 403.0 KB, 1.5 MB, 3.0 MB, 60.0 MB, 2.6 GB, 7.6 GB, 14.0 GB, 39.0 GB \| desc: 39.0 GB, 14.0 GB, 7.6 GB, 2.6 GB, 60.0 MB, 3.0 MB, 1.5 MB, 403.0 KB |
| PASS | Desktop after resize: Size th aria-sort="descending", first row matches cards | th: Name=none \| Type=null \| Size ▼=descending \| Status=null; first row "Blade Runner 2049 (2017).mkv" vs first card "Blade Runner 2049 (2017).mkv" |

### Check 8: PASS (4/4)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | Light: accent-text ratios printed | --accent-text=#4f46e5; Accent text on surface 6.29:1 · meets AA \| Accent text on surface-2 6.01:1 · meets AA \| Accent text on surface-3 5.74:1 · meets AA |
| PASS | Dark: surface-3 ratio >= 4.5 | --accent-text=#8d87ee (surface-3=#1e293b); Accent text on surface 6.56:1 · meets AA \| Accent text on surface-2 5.81:1 · meets AA \| Accent text on surface-3 4.76:1 · meets AA |
| PASS | All six printed ratios meet AA | surface 6.29:1 · meets AA; surface-2 6.01:1 · meets AA; surface-3 5.74:1 · meets AA; surface 6.56:1 · meets AA; surface-2 5.81:1 · meets AA; surface-3 4.76:1 · meets AA |
| PASS | --accent-text differs per theme and equals --accent-text-light/-dark | light=#4f46e5 (light var #4f46e5); dark=#8d87ee (dark var #8d87ee) |

### Check 9: FAIL (0/1)

| Result | Assertion | Evidence |
|---|---|---|
| **FAIL** | axe: no violations on new components (Checkbox, Switch, Modal demo, DataTable sort row, Popover) | 2 node hits; total rules across scans: 7 |

### Check 10: PASS (1/1)

| Result | Assertion | Evidence |
|---|---|---|
| PASS | No console.error / pageerror / failed requests after login (pre-login 401 on /auth/me is the auth gate probe; SW warning ignored) | 2 entries: [pre-login][http] 401 GET http://127.0.0.1:5173/api/v1/auth/me \|\| [pre-login][console.error] Failed to load resource: the server responded with a status of 401 (Unauthorized) |
