# P5 page-migration acceptance — every route, both themes, two widths

- Repo `/home/user/repo`, branch `claude/frontend-p5-page-migration`. Code under test: the Task 9 commits plus the fixes listed below (the last of them lands in the same commit as this revision of the report).
- Stack: Vite dev `http://127.0.0.1:5173` (hash routes), API :8000 with auth on, one seeded library ("Sample Media", 38 items), no agents. Logged in through the login wall as `admin`.
- Stack change for this run: `FILEARR_BROWSE_ROOTS='["/data","<LIVE>"]'` added to `review2/live/env.sh` (the setting is `browse_roots` in `backend/filearr/config.py`, default `["/data"]`); API and worker restarted with `start.sh`. Before it, FolderPicker could not list the seeded library's root (`422 path is outside the allowed browse roots`).
- Script and outputs (scratchpad, not in the repo): `/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/p5-accept/` — `accept.mjs` (Playwright/Chromium, `serviceWorkers: 'block'`, axe-core 4.10.3 from `frontend/node_modules`), `run3.log` (the run reported here; `run1.log`/`run2.log` and `run2-archive/` are the earlier runs), `results.json`, `console.json`, `mocks.json`, `axe/*.json` (one file per scan, `axe/summary.json`, `axe/totals.json`), `shots/*.png`.
- Viewports 1440×900 and 390×844; light and dark via the header Theme control (with a dialog open, the `html.dark` class is toggled directly, as `applyTheme` does — both accent-text variables are always set).
- Every number below comes from one full run (run 3), after all fixes.

## axe per rule — review-2 baseline → now

Baseline: `live-axe-summary.md` (100 scans on the review-2 preview build). Now: **106 scans, 69 clean** —
login wall 4 · 15 routes × 4 (light/dark × 1440/390) = 60 · Search results list + grid × 4 = 8 · dialogs at 1440 in both themes (library edit, discard confirm, FolderPicker nested, typed-name delete, custom-field edit, taxonomy, config group, item detail, saved-search prompt) = 18 · **main dialogs at 390 in both themes** (library edit, taxonomy, config group, item detail) = 8 · below-table panels (libraries failing items, users editors, RBAC members, jobs cron, alerts test, agents details) = 6 · item detail with AgentStatusPanel × 2 themes = 2.

| Rule | Impact | Baseline nodes (scans) | Target | **Now** nodes (scans) |
|---|---|---|---|---|
| `select-name` | critical | 60 (16) | 0 | **0** |
| `nested-interactive` | serious | 148 (12) | 0 | **0** |
| `scrollable-region-focusable` | serious | 10 (10) | 0 | **0** |
| `label-title-only` | serious | 8 (4) | 0 | **0** |
| `empty-table-header` | minor | 32 (20) | 0 | **0** |
| `color-contrast` | serious | 2,450 (74) | < baseline | **180** (37) |
| `heading-order` | moderate | 24 (24) | — | **0** |
| `landmark-one-main` | moderate | 4 (4) | — | **0** |
| `region` | moderate | 20 (4) | — | **0** |

`color-contrast` 180 by scan group (all `text-slate-500`-family palette text, P9 palette sweep, ruling 3; none on a kit component):

| Where | Nodes |
|---|---|
| `#/jobs` dark (1440 + 390) | 102 |
| `#/admin` dark (1440 + 390) | 18 |
| `#/account` dark (1440 + 390) | 6 |
| Item detail dialog, 1440 + 390, light + dark, plus the AgentStatusPanel pair (card labels `text-[10px] text-slate-500`, 4.34:1) | 21 |
| Other dark dialog scans (page behind the overlay: library edit ×2 widths, discard confirm, FolderPicker, typed delete, custom field — 2 each; saved-search prompt 1) | 13 |
| Page intro lines on Libraries (dark, 2 each width), Search / Browse / Filter builder / Search results list + grid (dark, 1 each width), Help (1 each in 4 scans), Kit (light, 1 each width) | 20 |
| **Total** | **180** |

Dialogs at 390: library edit light 0 / dark 2, taxonomy 0 / 0, config group 0 / 0, item detail 3 / 4 — the same nodes as at 1440 (color-contrast only).

`nested-interactive` on a Search results page: **0** in list and grid views, both themes, both widths (8 rows / tiles each).

## Flows — 90 rows, 90 PASS, 0 FAIL

84 check rows plus 6 fixture rows (create/delete of the custom field, RBAC group and alert channel used by the checks — all created through the API and deleted afterwards). Nothing is marked N/A or excluded.

### Dialogs with a dirty check (each: clean Escape + focus return · dirty Escape / backdrop / × ask "Discard changes?" and its Cancel keeps the dialog · dirty Cancel asks, keeps, then Discard closes + focus return · Save)

| Dialog | Clean Esc + focus | Dirty Esc | Dirty backdrop | Dirty × | Dirty Cancel → keep → Discard + focus | Save | Extra |
|---|---|---|---|---|---|---|---|
| Library edit | PASS | PASS | PASS | PASS | PASS | PASS (closes, no prompt, focus → Edit) | Save renamed the library; restored (PASS) · axe both themes (PASS) · discard confirm axe (PASS) |
| Custom-field edit (fixture field) | PASS | PASS | PASS | PASS | PASS | PASS (closes, focus → Edit) | axe both themes (PASS) |
| Taxonomy (New category) | PASS | PASS | PASS | PASS | PASS | PASS (creates, closes, focus → New category) | axe both themes (PASS) |
| Agents config group (mocked list + create) | PASS | PASS | PASS | PASS | PASS | PASS (closes, focus → New group) | axe both themes (PASS) |
| Item detail, edit tab | PASS (focus → results grid) | PASS | PASS | PASS | PASS (dialog-level Close in the header) | PASS (no prompt; Save stays in the dialog by design) | after Save, Escape closes with no prompt + focus → grid (PASS) · edit-tab Cancel returns to the card with no prompt, a tab-level cancel by design (PASS) · year restored (PASS) · axe both themes (PASS) |

### Dialogs without a dirty check (Escape + Cancel/Close + focus return)

| Dialog | Escape + focus | Cancel / Close + focus | Extra |
|---|---|---|---|
| FolderPicker nested in library edit | PASS (only the picker closes; focus → Browse…) | PASS (Cancel; edit stays open; focus → Browse…) | opens on the library root and lists its folders, no error alert (PASS) · axe both themes |
| Targeted scan | PASS (focus → Targeted…) | PASS (footer Close) | — |
| Diagnose | PASS (focus → Diagnose) | PASS (footer Close) | — |
| Typed-name library delete (confirm) | PASS (focus → Delete) | PASS | Delete disabled until the name is typed; axe both themes (PASS) · library still present (PASS) |
| Taxonomy delete (confirm) | PASS (focus → delete) | PASS | the created category is listed (PASS) · confirm deletes it (PASS, cleanup) |
| Saved-search prompt (`promptText`) | PASS (focus → Save current search) | PASS | axe both themes (PASS) |

### Main dialogs at 390 wide
Library edit, taxonomy, config group, item detail each open at 390×844 and were scanned in both themes: 4 rows PASS (axe numbers in the table above).

### Below-table panels (extra 10)

| Panel | Check | Result |
|---|---|---|
| Libraries failing items | open focuses the panel; Hide returns focus to the row button | PASS |
| Users details | open focuses the panel | PASS |
| Users reset password | open focuses the editor; cancel returns focus to its row button | PASS |
| RBAC members (fixture group `p5-probe-group`) | open focuses the editor (axe) | PASS |
| RBAC members | the row button toggles it closed, focus stays on the button | PASS |
| Jobs cron editor | open focuses the panel | PASS |
| Alerts test (fixture channel `p5-probe-channel`, webhook to 127.0.0.1:9) | firing focuses the result panel; the result ("blocked:loopback") renders | PASS |
| Alerts test | dismissing the result returns focus to the test button | PASS |
| Agents details (mocked) | open focuses the heading; Hide returns focus to the row button | PASS |

### Search and other

| Check | Result |
|---|---|
| Search grid: ArrowDown moves `aria-activedescendant` | PASS |
| Search grid: Space sets `aria-selected=true`, Space again clears it | PASS |
| Search grid: Enter opens item detail, Escape returns focus to the grid | PASS |
| `nested-interactive` = 0 on results, list + grid × light/dark × 1440/390 | PASS ×8 |
| AgentStatusPanel renders for a (mocked) agent-hosted item; axe both themes | PASS |
| Console: no errors after login | PASS |

## Console

- Pre-login: `401 GET /api/v1/auth/me` and its "Failed to load resource" line — the auth gate's session probe (expected, as in P4).
- After login, across every route, dialog and panel above: **no console errors, page errors or HTTP ≥ 400.** (Run 1/2 had a `422` from `/fs/browse` — fixed by the browse-roots stack change — and run-3 trials surfaced the taxonomy `422` fixed below.)

## Fixes made from the acceptance runs

| Failure | Owning file | Fix |
|---|---|---|
| `landmark-one-main` + `region` ×5 on the login wall (4 scans) | `LoginPage.svelte` | Root `<div>` → `<main>` (the wall renders outside the shell's `<main>`). |
| `heading-order` on `#/account` (4 scans) | `AccountPage.svelte` | Section headings `h4` → `h3` under the page `h2` (token `text-muted`). |
| `color-contrast` from the shell (nav, Paths label, footer) on every dark scan | `App.svelte` | Palette → `text-muted` / `bg-surface-3` / `border-border`. |
| `color-contrast` ×142 on `#/about` | `AboutPage.svelte` | `text-slate-500` family → `text-muted`; amber → `text-warn`. |
| FolderPicker `422` on the library root | stack (`review2/live/env.sh`) | `FILEARR_BROWSE_ROOTS` includes the library's parent; API restarted. |
| Taxonomy create/edit Save → `422 extractor must be one of [...] or null` whenever the Extractor select is "none" (pre-existing, not a P5 regression; every edit of a category without an extractor failed the same way) | `TaxonomyPage.svelte`, `api.ts` (types) | "none" is sent as `null`; the two taxonomy category API types accept `string \| null`. |

Script-side fixes between runs: theme toggle with a dialog open (header button under the overlay); `go()` reloads when the hash is unchanged (a same-hash `goto` does not refetch, which hid freshly created fixtures).

## Mocks (Playwright route interception, `mocks.json`)

Used in run 3:
- `GET /api/v1/agents?…` → 3 agents (`nas-01` active/online, `laptop-02` active/stale with an update available, `old-box` revoked), full `AgentOut` shape.
- `GET /api/v1/agents/summary` → `{total 3, connected 1, disconnected 1, pending 0, revoked 1}`.
- `GET /api/v1/agents/config-groups` → the real list plus one group "Workstations" (priority 100, 2 members, version 3).
- `POST /api/v1/agents/config-groups` → the created group echoed back (`mock-group-2`), so the config-group Save check writes nothing.
- `GET /api/v1/agents/{id}/effective-config` → one-group document with provenance.
- `GET /api/v1/items/{id}` → the real item plus `source_agent_id: "mock-agent-1"` (one item, only for the AgentStatusPanel scan).
- `GET /api/v1/items/{id}/agent-status` → hosted by `nas-01`, online, verified now.

Defined but not hit: `/agents/config-groups/mock-group-1[/history]`, `/agents/{id}/about` (404), `/agent-share-maps` (`[]`).

Real writes made by the run, all reverted: library renamed and renamed back; one item's year set to 1999 and cleared; custom field `p5_probe`, taxonomy category `p5probe`, RBAC group `p5-probe-group` and alert channel `p5-probe-channel` created and deleted.

## Shots (`shots/`, 89)

Routes (each `-desktop-light`, `-desktop-dark`, `-phone-light`, `-phone-dark`): `login`, `search`, `browse`, `libraries`, `jobs`, `reports`, `alerts`, `timeline`, `taxonomy`, `filter-builder`, `admin`, `agents`, `account`, `about`, `help`, `kit`, `search-results-list`, `search-results-grid` (72).
Dialogs and panels: `dlg-library-edit`, `dlg-library-discard-confirm`, `dlg-folderpicker-nested`, `dlg-library-delete-typed`, `dlg-taxonomy`, `dlg-agents-config-group`, `dlg-item-detail`, `dlg-saved-search-prompt`, `dlg-library-edit-phone`, `dlg-taxonomy-phone`, `dlg-agents-config-group-phone`, `dlg-item-detail-phone`, `panel-users-editors`, `panel-rbac-members`, `panel-alerts-test`, `agents-mocked-desktop`, `dlg-item-detail-agent-status` (17).
