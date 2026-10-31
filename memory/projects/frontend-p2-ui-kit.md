# Frontend P2 — UI kit + tokens (implementation plan)

**Date:** 2026-10-31 · **Spec:** `memory/projects/frontend-ui-review.md` §"P2 — UI kit (`src/lib/ui/`) + tokens"
**Branch:** `claude/frontend-p2-ui-kit` (off `claude/frontend-p1-shell-baseline`, PR #26)
**Scope:** `frontend/` only. P2 *builds* the kit and tokens and proves them on one gallery route; **migrating existing pages onto the kit is P3** and is out of scope here, except where a task says otherwise.

## Global constraints

- Svelte 5 runes + snippets, Tailwind 4, Vite 8, bits-ui 2.18 (already installed; read its types under `node_modules/bits-ui/dist/bits/<name>/types.d.ts` — do not guess the API). No new npm dependencies.
- Every task leaves all four green, run from `frontend/`: `npm run check` (0 errors, 0 warnings) · `npm run check:tests` · `npm test` · `npm run build`.
- Never add a `svelte-ignore` comment. Every primitive must be keyboard-operable and carry the ARIA the spec names.
- Pure logic lives in DOM-free `.ts` modules under `src/lib/ui/` with Node tests in `frontend/tests/*.node.test.ts` (shape of `tests/color.node.test.ts`).
- One component per file under `src/lib/ui/`, exported through `src/lib/ui/index.ts`. Components take a `class` prop (string, appended last) so callers can adjust layout without forking styles.
- Use the **tokens below** inside the kit — no raw `slate-*`/`red-*` colour utilities in `src/lib/ui/`. (Outside the kit, existing pages are untouched until P3.)
- Keep the codebase's comment style: a short "why" comment on non-obvious decisions. One commit per task, message prefixed `frontend:`.

### Tokens (Task 1 defines them; every later task uses exactly these names)

Defined in `app.css`. Pattern: `@theme { --color-surface: var(--surface); … }` with the raw values set on `:root` and overridden on `.dark` (Tailwind 4 `@theme` values are static, so the light/dark switch has to go through a plain custom property).

| Utility name | Light | Dark | Use |
|---|---|---|---|
| `surface` | `#ffffff` | `#020617` (slate-950) | page background |
| `surface-2` | `#f8fafc` (slate-50) | `#0f172a` (slate-900) | cards, modals, popovers |
| `surface-3` | `#f1f5f9` (slate-100) | `#1e293b` (slate-800) | hover rows, neutral badges, inputs on surface-2 |
| `border` | `#e2e8f0` (slate-200) | `#1e293b` (slate-800) | dividers, card edges |
| `border-strong` | `#cbd5e1` (slate-300) | `#334155` (slate-700) | control borders |
| `fg` | `#0f172a` | `#f1f5f9` | body text |
| `muted` | `#475569` (slate-600) | `#94a3b8` (slate-400) | secondary text |
| `accent` / `accent-fg` | existing | existing | unchanged |
| `accent-text` | `var(--accent)` | `color-mix(in srgb, var(--accent) 70%, white)` | accent-coloured text/links (closes the P1 deferral) |
| `ok` / `ok-bg` | `#047857` / `#d1fae5` | `#6ee7b7` / `#064e3b` | success |
| `warn` / `warn-bg` | `#b45309` / `#fef3c7` | `#fcd34d` / `#78350f` | warning |
| `danger` / `danger-bg` | `#b91c1c` / `#fee2e2` | `#fca5a5` / `#7f1d1d` | error / destructive |
| `info` / `info-bg` | `#0369a1` / `#e0f2fe` | `#7dd3fc` / `#0c4a6e` | informational |

Non-colour: `--radius-control: 0.5rem` (→ `rounded-control`), `--radius-panel: 1rem` (→ `rounded-panel`), `--text-2xs: 0.6875rem` with `--text-2xs--line-height: 1rem` (→ `text-2xs`, replaces the `text-[10px]/[11px]` sprawl later).

`<html>` and `html.dark` in `app.css` switch to `bg-surface text-fg`. Nothing else outside the kit changes colour in P2.

### Tone type (shared)
`export type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "accent";` in `src/lib/ui/tone.ts`.

---

## Task 1: Tokens, `tone.ts`, Button, Badge, Alert, StateBlock

**Files:** `src/app.css`, new `src/lib/ui/tone.ts`, `src/lib/ui/Button.svelte`, `src/lib/ui/Badge.svelte`, `src/lib/ui/Alert.svelte`, `src/lib/ui/StateBlock.svelte`, `src/lib/ui/index.ts`, new `tests/tone.node.test.ts`.

1. **Tokens** exactly as the table above. Keep the existing `--color-accent`/`--color-accent-fg` lines. Switch the `html` / `html.dark` rules to the new tokens (same rendered colours as today except slate-950 page bg in dark, which is already the case).
2. **`tone.ts`** (DOM-free): `Tone` type; `toneClasses(tone: Tone): string` returning the badge/alert colour utilities for each tone, built only from the tokens (`bg-ok-bg text-ok` etc.; neutral → `bg-surface-3 text-muted`; accent → `bg-accent/15 text-accent-text`); `toneForStatus(status: string | null | undefined): Tone` mapping case-insensitively: `critical|error|failed|failure|fatal|offline|revoked|disabled` → danger; `warning|warn|degraded|stale|pending|queued|held` → warn; `ok|success|succeeded|completed|done|healthy|online|active|enabled|verified` → ok; `running|info|in_progress|scanning|syncing` → info; empty/unknown → neutral. Tests: at least two inputs per tone, mixed case, `null` → neutral, unknown string → neutral.
3. **`Button.svelte`**: props `variant: "primary" | "secondary" | "danger" | "ghost" = "secondary"`, `size: "sm" | "md" = "md"`, `type = "button"`, `disabled`, `href?` (renders `<a>` when set, with `aria-disabled` instead of `disabled`), `class`, `children` snippet, rest props spread onto the element. Styles: `rounded-control`, `inline-flex items-center gap-1.5 font-medium disabled:opacity-50`, md = `px-3 py-1.5 text-sm`, sm = `px-2 py-1 text-xs`; primary = `bg-accent text-accent-fg hover:opacity-90`; secondary = `border border-border-strong bg-transparent text-fg hover:bg-surface-3`; danger = `bg-danger text-white hover:opacity-90`; ghost = `text-muted hover:bg-surface-3 hover:text-fg`. Minimum hit area 32 px tall at md (`min-h-8`).
4. **`Badge.svelte`**: props `tone: Tone = "neutral"`, `class`, `children`. `inline-flex items-center rounded-full px-1.5 py-0.5 text-2xs font-medium` + `toneClasses(tone)`.
5. **`Alert.svelte`**: props `tone: Exclude<Tone, "neutral" | "accent"> = "info"`, `title?`, `onDismiss?`, `class`, `children`. `role="alert"` for warn/danger, `role="status"` otherwise. Dismiss button (when `onDismiss`) is a real `<button aria-label="Dismiss">` with the `×` glyph `aria-hidden`. Styles `rounded-control px-3 py-2 text-sm` + `toneClasses(tone)`.
6. **`StateBlock.svelte`**: props `state: "loading" | "empty" | "error"`, `message?` (defaults: "Loading…", "Nothing here yet.", "Something went wrong."), `onRetry?` (renders a secondary `Button` "Retry" only for `error`), `compact?` (`py-3` vs `py-8`), `class`. Loading uses `role="status"`; error uses `role="alert"`; empty has no live role. Centered `text-sm text-muted`.
7. **`index.ts`** re-exports everything above (and later tasks add theirs).

---

## Task 2: Field, Input, Select, Textarea

**Files:** `src/lib/ui/Field.svelte`, `src/lib/ui/Input.svelte`, `src/lib/ui/Select.svelte`, `src/lib/ui/Textarea.svelte`, `src/lib/ui/index.ts`.

1. **`Field.svelte`**: props `label` (required string), `hint?`, `error?`, `required?`, `id?` (generated via `$props.id()` when absent), `class`, `children` snippet called with `{ id, describedBy, invalid }` where `describedBy` is the space-joined ids of the rendered hint/error nodes (or `undefined`) and `invalid = !!error`. Renders `<label for={id}>` (with a `text-danger` asterisk + `sr-only` "required" when `required`), the control slot, `<p id … class="text-2xs text-muted">` hint, `<p id … class="text-2xs text-danger" role="alert">` error. Labels are always visible — no placeholder-as-label.
2. **`Input.svelte`**, **`Select.svelte`**, **`Textarea.svelte`**: thin styled wrappers. Props: `value` (`$bindable`), `invalid?`, `size: "sm" | "md" = "md"`, `class`, rest props (incl. `id`, `aria-describedby`, `type`, `placeholder`, `disabled`, `required`, `autocomplete`). Shared control classes: `w-full rounded-control border bg-surface text-fg placeholder:text-muted focus-visible:ring-2 focus-visible:ring-accent focus-visible:outline-hidden disabled:opacity-50`, `border-border-strong` normally, `border-danger` when `invalid` plus `aria-invalid="true"`; md = `px-3 py-1.5 text-sm`, sm = `px-2 py-1 text-xs`. `Select` renders `children` as its `<option>`s. `Textarea` adds `rows = 3` and `resize-y`.
3. Field + control usage is: `<Field label="Name" error={err}>{#snippet children({ id, describedBy, invalid })}<Input {id} aria-describedby={describedBy} {invalid} bind:value={name} />{/snippet}</Field>`. Document this in a comment at the top of `Field.svelte`.

No pure-logic module; no new Node test. All four checks green.

---

## Task 3: Modal, ConfirmDialog + `confirm()` helper, Toast + `toast()` helper

**Files:** `src/lib/ui/Modal.svelte`, `src/lib/ui/ConfirmDialog.svelte`, `src/lib/ui/confirm.svelte.ts`, `src/lib/ui/ConfirmHost.svelte`, `src/lib/ui/Toast.svelte`, `src/lib/ui/toast.svelte.ts`, `src/lib/ui/ToastHost.svelte`, `src/lib/ui/index.ts`, `src/App.svelte` (mount the two hosts once, outside `<main>`).

1. **`Modal.svelte`** on `bits-ui` `Dialog`: props `open` (`$bindable`), `title` (required), `description?`, `size: "sm" | "md" | "lg" | "xl" = "md"` (`max-w-sm/lg/2xl/4xl`), `onClose?`, `class`, `children`, `footer?` snippet. Structure: `Dialog.Root bind:open` → `Dialog.Portal` → `Dialog.Overlay class="fixed inset-0 z-50 bg-black/50"` → `Dialog.Content class="fixed inset-0 z-50 overflow-y-auto"` wrapping a panel `mx-auto my-8 w-full {max} rounded-panel bg-surface-2 p-5 shadow-xl` that holds `Dialog.Title` (`<h2 class="text-lg font-semibold">`), optional `Dialog.Description` (`text-sm text-muted`), the body, the footer, and a `Dialog.Close` button in the top-right with `aria-label="Close"`. bits-ui provides focus trap, Escape, scroll lock, focus return and `aria-labelledby/describedby`; do not reimplement them. All modals share **one** z-index (50) and **one** backdrop (`/50`).
2. **`ConfirmDialog.svelte`** on `bits-ui` `AlertDialog`: props `open` (`$bindable`), `title`, `message?` (or `children`), `confirmLabel = "Confirm"`, `cancelLabel = "Cancel"`, `tone: "default" | "danger" = "default"`, `typedName?: string` (when set, an `Input` labelled "Type **{typedName}** to confirm" must match exactly before the confirm `Button` enables), `onConfirm`, `onCancel?`. Cancel is the initially focused control. Danger tone uses the danger `Button`.
3. **`confirm.svelte.ts`**: `export function confirm(opts: { title: string; message?: string; confirmLabel?: string; cancelLabel?: string; tone?: "default" | "danger"; typedName?: string }): Promise<boolean>` backed by a `$state` queue (one dialog at a time; a second call waits for the first). **`ConfirmHost.svelte`** renders the active request through `ConfirmDialog`. This is the drop-in for the ~37 native `confirm()` calls P3 will migrate: `if (!(await confirm({ title: "Delete agent?", tone: "danger", typedName: agent.name }))) return;`.
4. **`toast.svelte.ts`**: `export function toast(message: string, opts?: { tone?: Exclude<Tone, "neutral" | "accent">; durationMs?: number; actionLabel?: string; onAction?: () => void }): void`; `$state` list; default 5 s, errors (`danger`) persist until dismissed; max 3 visible (older ones drop). Every `toast()` also calls `announce(message)` from `src/lib/announce.svelte.ts` so the message is spoken — the visual toast list itself is `aria-hidden="true"` to avoid double announcement. **`Toast.svelte`** renders one entry (`Alert` with `onDismiss` and optional action `Button`); **`ToastHost.svelte`** renders the stack `fixed bottom-4 right-4 z-[60] flex w-80 max-w-[calc(100vw-2rem)] flex-col gap-2`. Pause the auto-dismiss timer while hovered.
5. **`App.svelte`**: render `<ConfirmHost />` and `<ToastHost />` once, next to the live region, outside `<main>`. Nothing else in App changes.

No pure-logic module beyond the two `.svelte.ts` stores (rune-based, not Node-testable). All four checks green.

---

## Task 4: Tabs, Popover

**Files:** `src/lib/ui/Tabs.svelte`, `src/lib/ui/Popover.svelte`, `src/lib/ui/index.ts`.

1. **`Tabs.svelte`** on `bits-ui` `Tabs`: props `tabs: { value: string; label: string; badge?: string | number; disabled?: boolean }[]`, `value` (`$bindable`), `ariaLabel` (required), `class`, `children` snippet called with the active `value` (callers render the panel; `Tabs.Content` wraps it so `role=tabpanel`/`aria-labelledby` are correct). Trigger styles: `rounded-control px-3 py-1.5 text-sm text-muted hover:text-fg data-[state=active]:bg-accent data-[state=active]:text-accent-fg data-[state=active]:font-semibold`, list `flex flex-wrap gap-1 border-b border-border pb-2`. Arrow-key navigation comes from bits-ui. Optional `badge` renders a `Badge`.
2. **`Popover.svelte`** on `bits-ui` `Popover`: props `open` (`$bindable`), `side = "bottom"`, `align = "start"`, `class`, `trigger` snippet (rendered inside `Popover.Trigger`; callers pass a `Button`), `children`. Content: `z-50 rounded-panel border border-border bg-surface-2 p-3 shadow-lg` with `collisionPadding={8}` and `max-w-[calc(100vw-1rem)]` so it never clips at 360 px (the P1 review's `w-72` finding). Escape/outside-click close come from bits-ui.

No Node test. All four checks green.

---

## Task 5: DataTable + `sort.ts`

**Files:** `src/lib/ui/sort.ts`, `src/lib/ui/DataTable.svelte`, `src/lib/ui/index.ts`, new `tests/sort.node.test.ts`.

1. **`sort.ts`** (DOM-free): `export type SortDir = "asc" | "desc"; export type SortState = { key: string; dir: SortDir } | null; export function nextSort(current: SortState, key: string): SortState` (click cycles none → asc → desc → none for the same key; a different key starts at asc); `export function compareValues(a: unknown, b: unknown): number` (numbers numerically, strings with `localeCompare(undefined, { numeric: true, sensitivity: "base" })`, `null`/`undefined` last, booleans false<true, Dates by time); `export function sortRows<T>(rows: readonly T[], state: SortState, accessor: (row: T, key: string) => unknown): T[]` (stable; returns the input array when `state` is null). Tests cover each rule including stability and null-last in both directions.
2. **`DataTable.svelte`** with `<script lang="ts" generics="T">`: props `columns: { key: string; label: string; align?: "left" | "right"; sortable?: boolean; width?: string; cell?: Snippet<[T]> }[]`, `rows: readonly T[]`, `rowKey: (row: T) => string`, `caption` (required; `captionHidden?` renders it `sr-only`), `sort` (`$bindable<SortState>`, default `null`), `accessor?: (row: T, key: string) => unknown` (default `(r, k) => (r as Record<string, unknown>)[k]`), `emptyMessage?`, `class`, `onRowClick?`. Renders: `<div class="overflow-x-auto rounded-control border border-border">` → `<table class="w-full text-sm">` with `<caption>`, `<thead>` whose `<th scope="col">` carry `aria-sort` (`ascending|descending|none` only when `sortable`) and, when sortable, a `<button>` that applies `nextSort`; `<tbody>` rows via `sortRows` keyed by `rowKey`; a `StateBlock state="empty"` row when there are no rows. **Mobile fallback:** below `md` the table is hidden and each row renders as a card `rounded-control border border-border bg-surface-2 p-3` with a `<dl>` of `<dt class="text-2xs text-muted">{label}</dt><dd>` pairs (same `cell` snippets). Use Tailwind `hidden md:block` / `md:hidden` — no JS media queries.

---

## Task 6: Kit gallery route `#/kit`

**Files:** `src/lib/KitPage.svelte`, `src/lib/routes.ts` (+ `tests/routes.node.test.ts`), `src/App.svelte`.

1. Add `kit` to `Page`/`PAGE_LABELS` ("UI kit") and a lazy loader for `KitPage` in `App.svelte`; **not** linked from the nav or footer (URL only). Extend the routes test with `#/kit`.
2. `KitPage.svelte` renders every primitive with realistic content, grouped under `<h2>`s: token swatches (each colour token as a labelled square, light/dark via the existing theme toggle); Buttons (all variants × sizes, disabled, as link); Badges (all tones, plus `toneForStatus` demo for a list of status strings); Alerts (all tones, one dismissible); StateBlocks (three states, error with Retry that toasts); Field/Input/Select/Textarea (one valid, one with error, one required); Modal (open button; sizes); ConfirmDialog via `confirm()` (default and danger with `typedName`), result shown via `toast()`; Toasts (all tones, one with action, one persistent); Tabs (three tabs with a badge); Popover; DataTable (8 sample rows, 4 columns, two sortable, one right-aligned numeric with a custom `cell` snippet, caption visible) — resize the window to see the card fallback.
3. The gallery is the acceptance surface for P2: open `#/kit` in light and dark, Tab through it, and check Escape/focus-return on the modal.

---

## Out of scope for P2 (ruled, see ledger)

- Migrating existing modals, `confirm()` calls, inputs, tables, badges → P3 (the kit must exist first).
- Replacing `text-[var(--accent)]` with `text-accent-text` across pages → P3 mechanical pass.
- Playwright/axe → P6.
