# Frontend UI review — findings & plan

**Date:** 2026-10-30 · **Scope:** `frontend/src` (Svelte 5 + Tailwind 4 + Vite 8, ~23k lines)
**Status:** P1 merged (PR #26, plan in `frontend-p1-shell-baseline.md`); P2 merged (PR #27, plan in `frontend-p2-ui-kit.md`). **The plan from P3 onward is superseded by `frontend-review-2.md`** (2026-11-01: functionality, security, performance, usability and aesthetics review, static + live), which carries the current phase list P3–P11. Line refs below predate P1 — re-verify before editing.
**P2 outcome:** semantic tokens (`surface/-2/-3`, `border/-strong`, `fg`, `muted`, `accent-text`, `ok/warn/danger/info` + `-bg`, `rounded-control/-panel`, `text-2xs`) via `:root`/`.dark` vars → `@theme`; kit in `src/lib/ui/`: Button, Badge, Alert(`live`), StateBlock, Field/Input/Select/Textarea (+`controlClass`), Modal, ConfirmDialog + `confirm()` promise queue (host lazy-mounted), Toast + `toast()` (region landmark, speaks via `announce()`), Tabs, Popover, DataTable (+`sort.ts`, mobile cards); gallery at `#/kit` (URL-only). Entry chunk 52 kB. **P3 Task 0 (before migrating):** Field passes wiring to controls via context (kills the snippet boilerplate and double `required`); add Checkbox/Switch; Modal `dismissible`/`initialFocus`; `confirm({ details: [] })`; Popover `label` shorthand; DataTable `loading`/`error`/`onRetry`, numeric `rowKey`, mobile sort select. **Token follow-ups:** compute `accent-text` from the accent (dark 4.29:1 on surface-3); `border-strong` 1.48:1 edge contrast; optional `@theme inline`. **Known limits:** toast action buttons are mouse-only while a Modal traps focus; hosts sit inside the auth gate; routine toast dropped when 3 persistent toasts are showing.
**P1 outcome:** entry chunk 871 kB → 46 kB; skip link, `<main>`, nav links with `aria-current`, route titles, focus + live region; global `:focus-visible` ring; secondary text `slate-600/dark:slate-400`; accent `#4f46e5` with computed `--accent-fg`; `usePolling` pauses hidden tabs. Deferred to P2: visual toast, `--accent-text` for dark-mode accent text contrast, `text-slate-500` dark nav contrast. Deferred to P3: JobsPage elapsed-time component.

## Key facts (verified)
- `bits-ui` and `meilisearch` are in package.json but **imported nowhere** in src/.
- `App.svelte` statically imports all 15 pages → one **871 kB (250 kB gz)** JS chunk, login included.
- No `<main>`, skip link, per-route `document.title`, global `:focus-visible`, or `prefers-reduced-motion`.
- `app.css` theme defines one token (`--color-accent`); everything else is raw slate-* (2716) + `dark:` (1047).
- `npm run check` is clean, but 6 a11y rules are `svelte-ignore`d (SearchPage 1822/1833/1897, MapPanel 574/575/642).

## Findings

### Accessibility (High)
| # | Issue | Where | Fix |
|---|---|---|---|
| A1 | Modals: no focus-in, no Tab trap, no focus return, page not inert; window-level Escape can close underlying modal | ItemDetail, LibraryEditModal, FolderPicker, LibraryDiagnoseModal, DeleteLibraryDialog (+~6 modals with no `role=dialog`) | Shared `Modal` on bits-ui Dialog |
| A2 | Route change silent: no `<main>`, title, focus move, skip link | App.svelte:160-204 | Shell baseline (see P1) |
| A3 | Nav = `<button>`s, active state colour-only; tabs lack tab semantics | App.svelte:163-190, Admin/Agents sub-tabs | `<a href="#/x" aria-current>`; bits-ui Tabs |
| A4 | ~37 native `confirm()` for destructive actions, same weight for "delete agent" and "remove saved search" | AgentsPage:921, UsersPanel:138, ApiKeysPanel:103, SessionsPanel:122… | `ConfirmDialog` (AlertDialog); typed-name for agent/user delete, key revoke |
| A5 | Unlabelled inputs (main search uses placeholder only; ~8 of 218 inputs labelled via aria/id) | SearchPage:1277, 1500, 1541, 1601; DeleteLibraryDialog:75 | `Field` component; `type=search`, `role=search` |
| A6 | Result rows: `div role=option` with nested checkboxes; result count not announced | SearchPage:1833-1842, 1897-1910 | real buttons/links per row, `role=status` "N results" |
| A7 | Few live regions (15 total); error banners lack `role=alert`; 0 progressbars | AlertsPage:498, toasts, job/scan bars | One persistent app live region; `role=alert`; `<progress>` |

### Accessibility (Med/Low)
- **Focus:** 13+ `outline-none` with a 1 px border-colour change as the only indicator, so there is no global focus ring.
- **Contrast:**
  - `text-slate-400` on white is 2.56:1 (~234 uses).
  - `text-slate-500` on slate-950 is 4.24:1, which affects the nav-inactive style in dark mode.
  - White on the default accent `#6366f1` is 4.47:1. The accent is user-configurable, so the on-accent text colour must be computed from it.
  - There are ~142 sub-12 px text classes (`text-[10px]`/`[11px]`/`[9px]`).
- **Touch targets:** most controls are ~28 px high, and checkboxes are native size.
- **Tables:** 49 tables, 0 `scope`, 0 `<caption>`, no `aria-sort`. Column meaning sits only in `title=`.
- **Tooltips:** 418 `title=` attributes carry info, and these don't show on touch.
- **Theme toggle:** no `aria-pressed`, and no "System" option.
- **Forms:** validation is a top banner only. There is no `aria-invalid` or `aria-describedby`, and focus doesn't move to the first invalid field.
- **Map:** `role=application`. The coordinate-entry alternative isn't visible.
- **Smaller items:**
  - Backdrop `<button>`s add a Tab stop.
  - `target=_blank` links don't say they open a new tab.
  - Glyph-only "×" buttons need labels.
  - Check `alt` on `Thumb.svelte`.
  - The login form needs `autocomplete` attributes.

### Usability
- Destructive actions have no consequence summary and no undo (see A4).
- Loading, empty and error states are inconsistent across pages, and some errors have no Retry.
- Bulk actions (e.g. "Re-extract all failed", LibrariesPage:423) give only a transient message. They should link to the resulting job.
- The mobile header wraps nine nav buttons into several rows. Collapse it to a menu below `sm`.
- `registerType: "autoUpdate"` in the PWA config reloads silently, which can lose form state on deploy. Prompt the user instead.

### Layout / responsiveness
- **11 hand-rolled modal shells.** They use 3 layout patterns, z-indexes from 40 to 70, and backdrops of `/40` or `/50`.
- **Wide tables force 704–1024 px** (`min-w-[44–64rem]`) on phones. Main culprits: LibrariesPage:754, AgentsPage:2444, JobsPage:1253+, CustomFieldsPanel:168.
- **The filter bar uses fixed widths** (SearchPage:1436-1666). The `w-72` popover at :1554 will clip at 360 px.
- **`grid-cols-2` without a `grid-cols-1` base** on form grids: PresetsPanel:109, LlmKeysPanel:177, JobsPage:938, LibraryDiagnoseModal:315.
- **No `dvh` anywhere.** `max-h-[65vh]` (SearchPage:1824) gets cut off by the iOS URL bar.
- **`app.css` sets global `overflow-x:hidden` at ≤640px.** This masks overflow bugs and can break `sticky`.

### Aesthetics / design-system
- **No semantic tokens** (surface, border, fg, muted, status, radius). Shades are picked ad hoc; for example, dark borders use both slate-700 and slate-800.
- **`text-[var(--accent)]` is used ~80×** even though a `text-accent` utility exists.
- **No primitives:** 155 inline-styled buttons, 218 inputs and 66 selects, across two input style families.
  - Radii are mixed: rounded-lg ×377, rounded-full ×140, xl/2xl ×46, plus bare `rounded`.
- **Status badges and colour helpers are duplicated** (levelClass etc.), with 22+ badge instances.
- **Repeated markup that should be components:** error banner (20+), table wrapper (51), card, stat tiles, panel header with a Refresh button.
- **Very large page files** (AgentsPage 4k lines, Search 2k, Jobs 1.7k, Libraries 1.3k) with inline sub-tabs and modals.
- **The manifest `theme_color` is fixed** (`#0f172a`) and ignores the light theme and the accent.

### Performance
| # | Issue | Fix |
|---|---|---|
| F1 | Single 871 kB chunk | Lazy route `import()` in App.svelte; lazy MapPanel/ItemDetail/FolderTreemap/modals. Target main < ~200 kB |
| F2 | 4 of 5 pollers ignore hidden tab (AgentsPage:210 15 s×3 calls, LibrariesPage:653, LogsPanel:62, ExportsPanel:51) | shared `usePolling(fn, ms)` pausing on `visibilitychange` |
| F3 | JobsPage:676 1 s `now` tick re-evaluates whole page, also when hidden | pause when hidden; isolate into small elapsed-time component |
| F4 | SW has no runtimeCaching → thumbnails never cached | CacheFirst rule for thumb URLs (≈500 entries, 7 d); `cleanupOutdatedCaches` |
| F5 | Unbounded DOM: Search thumbnail grid (infinite scroll), Logs load-more, Browse listing | virtua (already used for list view) or windowing |
| F6 | `$effect(() => load())` without `untrack` (BrowsePage:93, AlertsPage:472, AccountPage:52, ItemDetail:344) and `$effect` for one-shot fetches (ItemDetail:414, Api/LlmKeys/Features/Updates panels) | explicit deps + `untrack` (model: LogsPanel:46); `onMount` for one-shots |
| F7 | Thumb.svelte: no `decoding=async`, no dimensions (CLS), failed thumbs never retried | add attrs; retry with backoff |

## Plan (phased, each phase independently shippable)

**P1 — Shell baseline & quick wins (low risk, high leverage)**
1. Lazy-load routes in App.svelte, keeping Login and the shell eager. This fixes F1.
2. App shell (`App.svelte`):
   - `<main id="main" tabindex="-1">` and a skip link.
   - Nav as `<a aria-current>` with `aria-label` on `<nav>`.
   - Per-route `document.title`, and focus moved on route change.
   - One persistent `role=status` live region plus a toast helper.
3. `app.css`:
   - Global `:focus-visible` ring and a `prefers-reduced-motion` rule.
   - Replace bare `outline-none` with `focus-visible:ring-2`.
4. Contrast:
   - Secondary text becomes `text-slate-600 dark:text-slate-400`.
   - Default accent becomes `#4f46e5`.
   - Compute the on-accent text colour from the accent's luminance.
5. `usePolling` helper (F2, F3).
6. Remove the unused `meilisearch` npm dep. Keep `bits-ui` for P2.

**P2 — UI kit (`src/lib/ui/`) + tokens**
- Semantic `@theme` tokens (surface/surface-2/border/fg/muted/accent/ok/warn/danger, radius, `text-2xs`) with `.dark` overrides.
- Primitives:
  - `Modal`, `ConfirmDialog`, `Tabs` and `Popover`, built on bits-ui.
  - `Button` with primary/secondary/danger/ghost variants and sm/md sizes.
  - `Field`, `Input` and `Select` (label, hint, error, aria-invalid/describedby).
  - `Badge` with a status→tone map.
  - `Alert` with `role=alert`.
  - `StateBlock` for loading/empty/error with Retry.
  - `DataTable` with scope, caption, aria-sort and a mobile card fallback.

**P3 — Migrate**
1. Swap the 11 modals onto `Modal`.
2. Swap the ~37 `confirm()` calls onto `ConfirmDialog`, adding typed-name confirms for agent delete, user delete and key revoke.
3. Fix the SearchPage listbox and its labels (A5, A6). Drop the `svelte-ignore`s.
4. Split AgentsPage, SearchPage, JobsPage and LibrariesPage into per-tab components (this also improves chunking).
5. Replace `text-[var(--accent)]` with `text-accent` (mechanical).

**P4 — Mobile pass**
- Card layout below `md` for the Libraries, Agents, Jobs and Fields tables.
- Fix filter-bar widths, popovers capped to the viewport, `grid-cols-1` bases, `dvh`.
- Collapse the nav into a menu below `sm`.
- Then remove the global `overflow-x:hidden`.

**P5 — Perf polish**
- SW thumbnail runtime cache (F4).
- Virtualization for the grid, logs and browse listing (F5).
- Effect hygiene (F6).
- Thumb attributes and retry (F7).
- Prompt-to-reload for PWA updates.

**P6 — Guardrails**
- axe-core via Playwright on the main pages, in light and dark themes, in CI.
- Lint rule or review policy: `svelte-ignore a11y_*` requires a justification comment.
