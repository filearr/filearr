# Frontend P1 — Shell baseline & quick wins (implementation plan)

**Date:** 2026-10-30 · **Spec:** `memory/projects/frontend-ui-review.md` (section "P1 — Shell baseline & quick wins")
**Branch:** `claude/frontend-p1-shell-baseline` (off `claude/zealous-hypatia-yq43ut`, PR #25)
**Scope:** `frontend/` only. No backend changes.

## Global constraints

- Svelte 5 runes (`$state`, `$derived`, `$effect`), Tailwind 4, Vite 8. No new npm dependencies.
- Every task leaves all four green, run from `frontend/`:
  `npm run check` (0 errors, 0 warnings) · `npm run check:tests` · `npm test` · `npm run build`.
- Never add a `svelte-ignore` comment. Never remove an existing `aria-*` attribute.
- Dark mode (`.dark` on `<html>`) must keep working on every touched element.
- Pure logic goes in a DOM-free `.ts` module with a Node test under `frontend/tests/*.node.test.ts`
  (same shape as `tests/osFormat.node.test.ts`: `node:test` + `node:assert/strict`, import with `.ts` extension).
- Keep the codebase's comment style: a short "why" comment on each non-obvious decision.
- One commit per task, message prefixed `frontend:`.

Baseline measured on `f9c6b7c`: `dist/assets/index-*.js` 871.48 kB (250.49 kB gz); check clean; 191 tests pass.

---

## Task 1: Lazy-load routes and heavy panels; drop the unused `meilisearch` dependency

**Files:** `frontend/src/App.svelte`, `frontend/src/lib/SearchPage.svelte`, `frontend/src/lib/BrowsePage.svelte`, `frontend/package.json`, `frontend/package-lock.json`.

1. In `App.svelte`, keep `LoginPage` and the shell eager. Every other page (`AdminPage`, `LibrariesPage`, `JobsPage`, `AlertsPage`, `BrowsePage`, `SearchPage`, `TimelinePage`, `ReportsPage`, `AgentsPage`, `TaxonomyPage`, `FilterBuilderPage`, `HelpPage`, `AboutPage`, `AccountPage`) becomes a dynamic `import()` loaded only when its route is active. Use Svelte's `{#await}` with the module's `default` export; pass the same props each page receives today.
   - Pending state: `<p class="py-8 text-sm text-slate-500">Loading…</p>`.
   - Rejected state (stale chunk hash after a deploy is the realistic cause): a short message plus a "Reload" `<button>` that calls `location.reload()`.
   - Do not re-create the loader promise on every render: cache one promise per page in a module-level map so revisiting a route does not re-fetch or flash "Loading…".
2. Inside `SearchPage.svelte` and `BrowsePage.svelte`, lazy-load `MapPanel`, `ItemDetail` and `FolderTreemap` the same way (they are only shown on demand). Leave the other parents listed below alone this task — they are already route-chunked: `FilterBuilderPage`, `CustomFieldInput`, `LibraryEditModal`, `CustomReportsPage`, `ReportsPage`, `BulkEditBar`, `ArchiveSection`.
3. Remove `"meilisearch"` from `dependencies` in `package.json` (zero imports in `src/`). Keep `bits-ui` (used by P2). Run `npm install --no-audit --no-fund` so `package-lock.json` follows.
4. Acceptance: `npm run build` reports the entry chunk `dist/assets/index-*.js` under 200 kB uncompressed, and one chunk per page. Record the before/after sizes in the report.

No pure-logic module is introduced, so no new Node test. Existing tests must still pass.

---

## Task 2: App shell landmarks, nav links, route titles, focus management, live region

**Files:** `frontend/src/App.svelte`, new `frontend/src/lib/routes.ts` additions, new `frontend/src/lib/announce.svelte.ts`, new `frontend/tests/routes.node.test.ts`.

1. **Route metadata** in `routes.ts` (DOM-free): export `type Page` (move the union out of `App.svelte`), `routeFromHash(hash: string): { page: Page; browseLib: string; browsePath: string }` (move the existing function out of `App.svelte` so it is testable; same behaviour, `#/search` and empty hash → `search`), and `pageTitle(page: Page): string` returning `"<Label> · Filearr"` with labels: search "Search", libraries "Libraries", admin "Admin", jobs "Jobs", alerts "Alerts", browse "Browse", timeline "Timeline", reports "Reports", agents "Agents", taxonomy "Taxonomy", filter-builder "Filter builder", help "Help", about "About", account "Account".
   Test in `tests/routes.node.test.ts`: every hash listed above maps to its page; unknown hash → `search`; a browse hash still parses; `pageTitle` for three pages.
2. **Skip link + main landmark** in `App.svelte`: first child of the app `<div>` is `<a href="#main" class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow dark:focus:bg-slate-900">Skip to main content</a>`. Wrap the page `{#if}` chain in `<main id="main" tabindex="-1" class="outline-none">`. The skip link's `href="#main"` must not be swallowed by the hash router: `routeFromHash("#main")` must return the current page unchanged — handle this in `onHashChange` by ignoring a hash of exactly `#main` (and restore the previous hash with `history.replaceState` so the URL stays a valid route).
3. **Nav as links**: every nav `<button onclick={() => goto(x)}>` becomes `<a href="#/x">`. Add `aria-label="Primary"` to `<nav>`. Active item gets `aria-current="page"` and `font-semibold` in addition to its current colour classes, so the active state is not colour-only. Inactive items keep `text-slate-500` for now (Task 4 handles contrast). Remove `goto()` if nothing else uses it; `hashchange` already drives `page`.
4. **Title + focus on route change**: an `$effect` that sets `document.title = pageTitle(page)`. On a route change *after* the initial render, move focus to `<main>` (`main.focus({ preventScroll: false })`). Do not steal focus on first load or while `gated` is true.
5. **Live region + announce helper**: `announce.svelte.ts` exports `announce(message: string)` and a `$state` object `announcement` with `{ text: string }`. `announce()` clears then sets the text on the next tick (so repeated identical messages are re-read). `App.svelte` renders, once, outside `<main>`: `<div role="status" aria-live="polite" aria-atomic="true" class="sr-only">{announcement.text}</div>`. Call `announce(pageTitle(page).replace(" · Filearr", "") + " page")` on route change (not on first load). No visual toast UI in this task — the helper is the integration point P2's toast component will reuse.
6. Theme button: add `aria-pressed={isDark}` (derive from `theme.mode` and the media query the same way `applyTheme` does) and `aria-label="Toggle dark theme"`. Keep the visible label "Theme".

---

## Task 3: Global focus ring and reduced-motion; retire bare `outline-none`

**Files:** `frontend/src/app.css`, the 13 `outline-none` sites found by `grep -rn "outline-none" frontend/src`.

1. In `app.css` after the `html.dark` block:
   ```css
   /* One keyboard focus indicator for the whole app. :focus-visible leaves mouse
      clicks alone; the ring uses the accent so it stays visible on both themes. */
   :focus-visible {
     outline: 2px solid var(--accent, #4f46e5);
     outline-offset: 2px;
   }
   @media (prefers-reduced-motion: reduce) {
     *, *::before, *::after {
       animation-duration: 0.01ms !important;
       animation-iteration-count: 1 !important;
       transition-duration: 0.01ms !important;
       scroll-behavior: auto !important;
     }
   }
   ```
2. At each `outline-none` site: if the element relies on a border-colour change as its only focus cue, replace `outline-none` with `focus-visible:ring-2 focus-visible:ring-[var(--accent)] focus-visible:outline-none`. If it is the `<main tabindex="-1">` from Task 2 or a programmatic-focus-only target, keep `outline-none` and add a one-line comment saying why. Report the per-site decision.
3. Acceptance: `grep -rn "outline-none" frontend/src` shows only the justified sites; check/build green.

---

## Task 4: Contrast — secondary text, default accent, on-accent foreground

**Files:** `frontend/src/lib/theme.svelte.ts`, `frontend/src/app.css`, new `frontend/src/lib/color.ts`, new `frontend/tests/color.node.test.ts`, every `.svelte` under `frontend/src` with a bare `text-slate-400` or `bg-[var(--accent)]`.

1. **`color.ts`** (DOM-free): `export function relativeLuminance(hex: string): number` (WCAG 2.x sRGB formula) and `export function onAccentColor(hex: string): "#ffffff" | "#0f172a"` returning white when contrast against white ≥ contrast against `#0f172a`, else slate-950. Tests: `#6366f1` → white; `#4f46e5` → white; `#facc15` → `#0f172a`; `#ffffff` → `#0f172a`; `#000000` → white; luminance of `#ffffff` is 1 and `#000000` is 0.
2. **`theme.svelte.ts`**: default accent becomes `#4f46e5` (both the `$state` default and any other literal `#6366f1`). `applyTheme()` also sets `--accent-fg` to `onAccentColor(theme.accent)`. `app.css` `@theme` gains `--color-accent-fg: var(--accent-fg, #ffffff);` and its `--color-accent` fallback becomes `#4f46e5`.
3. **On-accent text**: everywhere an element has `bg-[var(--accent)]` together with `text-white` (96 sites), replace `text-white` with `text-[var(--accent-fg)]`. Where `text-white/70` or similar opacity variants sit on an accent background, use `text-[var(--accent-fg)] opacity-70`. Leave `text-white` alone where the background is not the accent.
4. **Secondary text**: every bare `text-slate-400` (206 sites; `dark:text-slate-400` is already correct and stays) becomes `text-slate-600 dark:text-slate-400`, except:
   - if the same class list already carries a `dark:text-*`, change only the light class to `text-slate-600` and keep the existing dark class;
   - if the element sits on a dark or accent background in light mode (e.g. inside a `bg-slate-900`/`bg-[var(--accent)]` parent on the same element), leave it unchanged and list it in the report.
   `FolderTreemap.svelte` trips `grep`'s binary heuristic; use `grep -a`.
5. Acceptance: `grep -rn "text-slate-400" frontend/src | grep -v "dark:text-slate-400"` lists only the justified exceptions; `grep -rn "#6366f1" frontend/src` is empty; check/build green; tests pass including the new ones.

---

## Task 5: Shared `usePolling` helper; pollers pause when the tab is hidden

**Files:** new `frontend/src/lib/polling.ts`, new `frontend/tests/polling.node.test.ts`, `frontend/src/lib/AgentsPage.svelte`, `frontend/src/lib/LibrariesPage.svelte`, `frontend/src/lib/LogsPanel.svelte`, `frontend/src/lib/ExportsPanel.svelte`, `frontend/src/lib/JobsPage.svelte`.

1. **`polling.ts`** (DOM-free core, injectable clock and visibility):
   ```ts
   export type PollerDeps = {
     setInterval: typeof globalThis.setInterval;
     clearInterval: typeof globalThis.clearInterval;
     isVisible: () => boolean;
     onVisibility: (cb: () => void) => () => void; // subscribe; returns unsubscribe
   };
   export type Poller = { stop(): void };
   export function createPoller(fn: () => void, intervalMs: number, deps: PollerDeps, opts?: { immediate?: boolean; refreshOnReturn?: boolean }): Poller;
   ```
   Semantics: ticks call `fn` only while `isVisible()`; when visibility returns to visible and `refreshOnReturn` (default `true`) is set, call `fn` once immediately; `immediate` (default `false`) calls `fn` once at creation; `stop()` clears the interval and unsubscribes. Tests with a fake timer and a fake visibility switch cover: tick while visible, skipped tick while hidden, refresh on return, no refresh on return when disabled, stop prevents further calls.
   Also export the browser wrapper `usePolling(fn, intervalMs, opts?)` that builds `deps` from `document.visibilityState` and a `visibilitychange` listener, and returns the `Poller`. Keep it in the same file but reference `document` only inside `usePolling` so the test import stays DOM-free (the test imports `createPoller` only).
2. Migrate, preserving each site's interval and behaviour:
   - `AgentsPage.svelte:201-215` — 15 s tick running `refreshSummary`, `refreshSweeps`, `reloadRollouts`.
   - `LibrariesPage.svelte:641-653` — 30 s `refresh`.
   - `LogsPanel.svelte:60-63` — 10 s `load` while `auto` is on (keep the `$effect` that starts/stops it).
   - `ExportsPanel.svelte:49-54` — 4 s, only when `anyRunning`.
   - `JobsPage.svelte:669-689` — the existing visibility-aware poller moves onto the helper (drop the hand-rolled `onVisible` listener), and the 1 s `now` tick also goes through the helper so it pauses when hidden.
   Each `onDestroy` calls `.stop()`. Remove the now-unused `ReturnType<typeof setInterval>` variables.
3. Acceptance: `grep -rn "setInterval" frontend/src --include=*.svelte` shows no polling sites outside `polling.ts` (a `setTimeout` debounce is fine); check/build/tests green.

---

## Out of scope for P1 (ruled, see ledger)

- Visual toast component → P2 UI kit.
- Isolating JobsPage's elapsed-time counters into a component → P3 page split.
- `text-slate-500` dark-mode nav contrast → P2 tokens.
