# P5 page-migration acceptance — every route, both themes, two widths

- Repo `/home/user/repo`, branch `claude/frontend-p5-page-migration`. Code under test: 2ff3b6b (Task 9 code commit) plus the acceptance fixes listed below, which are in the same commit as this report.
- Stack: Vite dev `http://127.0.0.1:5173` (hash routes), API :8000 with auth on, one seeded library ("Sample Media", 38 items), no agents. Logged in through the login wall as `admin`.
- Script and outputs (scratchpad, not in the repo): `/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/p5-accept/` — `accept.mjs` (Playwright/Chromium, `serviceWorkers: 'block'`, axe-core 4.10.3 from `frontend/node_modules`), `run1.log` / `run2.log`, `results.json`, `console.json`, `mocks.json`, `axe/*.json` (one file per scan) and `axe/final-totals.json`, `shots/*.png`.
- Viewports 1440×900 and 390×844; light and dark via the header Theme control (with a dialog open, the `html.dark` class is toggled directly, as `applyTheme` does — both accent-text variables are always set).
- Runs: run 1 (full) found the failures below; fixes; run 2 (full, 94 scans); re-runs of the routes that still failed (`login`, `about`, `account`). Final numbers are the run-2 scans with those routes' scans replaced by the re-run.

## axe per rule — review-2 baseline → now

Baseline: `live-axe-summary.md` (100 scans on the review-2 preview build). Now: 94 scans (4 login + 15 routes × 4 + Search results list/grid × 4 + 22 dialog/panel scans), **61 clean**.

| Rule | Impact | Baseline nodes (scans) | Target | Run 1 | **Now** (scans) |
|---|---|---|---|---|---|
| `select-name` | critical | 60 (16) | 0 | 0 | **0** |
| `nested-interactive` | serious | 148 (12) | 0 | 0 | **0** |
| `scrollable-region-focusable` | serious | 10 (10) | 0 | 0 | **0** |
| `label-title-only` | serious | 8 (4) | 0 | 0 | **0** |
| `empty-table-header` | minor | 32 (20) | 0 | 0 | **0** |
| `color-contrast` | serious | 2,450 (74) | < baseline | 646 | **169** (33) |
| `heading-order` | moderate | 24 (24) | — | 4 | **0** |
| `landmark-one-main` | moderate | 4 (4) | — | 4 | **0** |
| `region` | moderate | 20 (4) | — | 20 | **0** |

Remaining `color-contrast` (P9 palette sweep, ruling 3): Jobs 102 (`text-slate-500` on the dark page, #62748e on #020617 = 4.23), Admin 18, Account 6, item detail 3–4 per scan (card labels `text-[10px] text-slate-500`, 4.34), the shell-adjacent `text-slate-500` page intros on Help/Libraries/Search/Browse/Filter builder/Kit (2 each in dark), dialogs in dark 1–2 each (the page behind). No hit is on kit components.

`nested-interactive` on a Search results page: **0** in list and grid views, both themes, both widths (8 rows / tiles each).

## Flows (34 assertions: 33 PASS, 1 FAIL — the console check, see below)

| Area | Check | Result |
|---|---|---|
| Library edit | Clean Escape closes with no prompt; focus returns to Edit | PASS |
| Library edit | Dirty: Escape, backdrop click and × each ask "Discard changes?"; Cancel there keeps the dialog | PASS |
| Library edit | Dirty Cancel asks; Discard closes; focus returns to Edit | PASS |
| Library edit | Save closes without a prompt | PASS |
| FolderPicker nested in library edit | Escape closes only the picker; edit stays open; focus back on Browse… | PASS |
| Typed-name delete | Delete disabled until the name is typed; Cancel closes; library kept; focus back on Delete | PASS |
| Taxonomy dialog | Escape closes; focus returns to New category | PASS |
| Agents config-group dialog (mocked) | Escape closes; focus returns to New group | PASS |
| Search grid keys | ArrowDown moves `aria-activedescendant`; Space sets `aria-selected=true`; Enter opens item detail | PASS ×3 |
| Item detail | Escape closes; focus returns to the results grid | PASS |
| Saved-search prompt | Escape cancels; focus returns to Save current search | PASS |
| AgentStatusPanel (mocked agent-hosted item) | Renders agent nas-01 in item detail; axe both themes | PASS |
| Below-table panels (extra 10) | Libraries failing items: open focuses the panel, Hide returns focus to the row button | PASS ×2 |
| | Users details / reset password: open focuses the panel; reset cancel returns focus to its row button | PASS ×3 |
| | Jobs cron editor: open focuses the panel | PASS |
| | Agents details (mocked): open focuses the heading, Hide returns focus | PASS ×2 |
| | RBAC members: no RBAC groups on the stack | N/A (recorded PASS) |
| | Alerts test result: no alert channels on the stack | not exercised |
| Search results | `nested-interactive` = 0, list + grid × light/dark × desktop/phone | PASS ×8 |
| Console | No errors after login | **FAIL (environmental)** |

## Console

- Pre-login: `401 GET /api/v1/auth/me` and its "Failed to load resource" line — the auth gate's session probe (expected, as in P4).
- `dialog:library-edit`: `422 GET /api/v1/fs/browse?path=<library root>` and its "Failed to load resource" line. The stack's browse roots are `["/data"]` and the seeded library lives under the scratchpad, so the server refuses to list it. FolderPicker shows the server's message ("path is outside the allowed browse roots") in its dismissable alert with the roots breadcrumb still usable (`shots/dlg-folderpicker-nested.png`). This is a stack-configuration artifact, not a frontend error; it would recur on any deployment whose library root is outside the browse roots.
- Every route and every other dialog: no console errors, page errors or HTTP ≥ 400.

## Fixes made from the acceptance run

| Failure (run 1) | Owning file | Fix |
|---|---|---|
| `landmark-one-main` + `region` ×5 on the login wall (4 scans) | `LoginPage.svelte` | Root `<div>` → `<main>` (the wall renders outside the shell's `<main>`). |
| `heading-order` on `#/account` (4 scans) | `AccountPage.svelte` | Section headings `h4` → `h3` under the page `h2` (token `text-muted`). |
| `color-contrast` from the shell (nav links, Paths label, footer) on every dark scan | `App.svelte` | `text-slate-500`/palette → `text-muted`, `bg-surface-3`, `border-border` on the nav, account chip, Paths label and footer. |
| `color-contrast` ×142 on `#/about` | `AboutPage.svelte` | `text-slate-500` / `text-slate-600 dark:text-slate-500` → `text-muted`; amber → `text-warn` (the page's tables were rewritten in this task). |

Script-side fix between runs: the theme toggle for scans with a dialog open (the header button is under the overlay).

## Mocks (Playwright route interception, `mocks.json`)

Used in the run:
- `GET /api/v1/agents?…` → 3 agents (`nas-01` active/online, `laptop-02` active/stale with an update available, `old-box` revoked), full `AgentOut` shape.
- `GET /api/v1/agents/summary` → `{total 3, connected 1, disconnected 1, pending 0, revoked 1}`.
- `GET /api/v1/agents/config-groups` → the real list plus one group "Workstations" (priority 100, 2 members, version 3).
- `GET /api/v1/agents/{id}/effective-config` → one-group document with provenance.
- `GET /api/v1/items/{id}` → the real item plus `source_agent_id: "mock-agent-1"` (one item, only for the AgentStatusPanel scan).
- `GET /api/v1/items/{id}/agent-status` → hosted by `nas-01`, online, verified now.

Defined but not hit in this run: `/agents/config-groups/mock-group-1[/history]`, `/agents/{id}/about` (404), `/agent-share-maps` (`[]`).

## Shots (`shots/`, 83)

Routes (each `-desktop-light`, `-desktop-dark`, `-phone-light`, `-phone-dark`): `login`, `search`, `browse`, `libraries`, `jobs`, `reports`, `alerts`, `timeline`, `taxonomy`, `filter-builder`, `admin`, `agents`, `account`, `about`, `help`, `kit`, `search-results-list`, `search-results-grid`.
Dialogs and panels (desktop light): `dlg-library-edit`, `dlg-library-discard-confirm`, `dlg-folderpicker-nested`, `dlg-library-delete-typed`, `panel-users-editors`, `dlg-taxonomy`, `agents-mocked-desktop`, `dlg-agents-config-group`, `dlg-item-detail`, `dlg-saved-search-prompt`, `dlg-item-detail-agent-status`.
