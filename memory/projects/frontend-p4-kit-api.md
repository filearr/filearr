# Frontend P4 — Kit API prep (implementation plan)

**Date:** 2026-11-01 · **Spec:** `memory/projects/frontend-review-2.md` §"P4" (= the P2 whole-branch review's kit recommendations) plus the P3 carry-overs named there
**Branch:** `claude/frontend-p4-kit-api` (off `main` at 40172fe)
**Scope:** `frontend/src/lib/ui/`, `app.css`, `theme.svelte.ts`/`color.ts`, the `#/kit` gallery, and two small `api.ts`/`LibraryEditModal` carry-overs. **No page migrations** (P5). Every API change must be backwards compatible with the kit's current callers (the gallery and the few P3 sites).

## Global constraints

- Svelte 5 runes + snippets, Tailwind 4, bits-ui 2.18. No new npm dependencies. Tokens only inside `src/lib/ui/` (no raw palette utilities). Never add `svelte-ignore`.
- Every task leaves all four green from `frontend/`: `npm run check` (0/0) · `npm run check:tests` · `npm test` · `npm run build`; the entry chunk must stay free of bits-ui (`grep -c "bits-ui\|AlertDialog" dist/assets/index-*.js` = 0).
- Pure logic in DOM-free `.ts` modules with Node tests. Components get exercised on the `#/kit` gallery (Task 5), which is the acceptance surface.
- Keep the kit conventions: `class: className = ""` appended last, `{...rest}` spread, short "why" comments. One commit per task, `frontend:` prefix.

---

## Task 1: Field wiring via context; Checkbox and Switch

**Files:** `src/lib/ui/Field.svelte`, `src/lib/ui/fieldContext.ts` (new, DOM-free types + key), `src/lib/ui/Input.svelte`, `src/lib/ui/Select.svelte`, `src/lib/ui/Textarea.svelte`, `src/lib/ui/Checkbox.svelte` (new), `src/lib/ui/Switch.svelte` (new), `src/lib/ui/index.ts`, `tests/fieldContext.node.test.ts` (new).

1. **`fieldContext.ts`**: `export const FIELD_CTX = Symbol("field")`; `export type FieldCtx = { readonly id: string; readonly describedBy: string | undefined; readonly invalid: boolean; readonly required: boolean }`; `export function controlAttrs(ctx: FieldCtx | undefined, own: { id?: string; "aria-describedby"?: string; "aria-invalid"?: string | boolean; required?: boolean; invalid?: boolean }): { id?: string; "aria-describedby"?: string; "aria-invalid"?: "true" | undefined; required?: boolean; invalid: boolean }` — own props win; context fills the gaps; `invalid` = own `invalid` ?? ctx.invalid. Tests: no ctx; ctx only; own overrides each field; `aria-invalid` only when invalid.
2. **`Field.svelte`**: `setContext(FIELD_CTX, { get id() {…}, get describedBy() {…}, get invalid() {…}, get required() {…} })` using getters over the existing `$derived` values so children see updates. `children` becomes optional in the snippet-argument form: `{#if children}{@render children({ id, describedBy, invalid, required })}{/if}` — the snippet now also receives `required`. Document both usages in the header comment: short form `<Field label="Name" error={err} required><Input bind:value={name} /></Field>` and the explicit snippet form for custom controls.
3. **`Input`/`Select`/`Textarea`**: `const field = getContext<FieldCtx | undefined>(FIELD_CTX)`; compute `const attrs = $derived(controlAttrs(field, { id: rest.id, "aria-describedby": rest["aria-describedby"], "aria-invalid": rest["aria-invalid"], required: rest.required, invalid }))` and spread `{...rest} {...attrs}` so context-derived `id`, `aria-describedby`, `aria-invalid`, `required` land on the element, with explicit props still winning. `controlClass(size, attrs.invalid)`.
4. **`Checkbox.svelte`**: props `checked` (`$bindable(false)`), `label` (required string), `description?`, `indeterminate?`, `disabled?`, `size: "sm" | "md" = "md"`, `class`, rest → `<input type="checkbox">`. Markup: `<label class="inline-flex items-start gap-2 {size…} {className}"><input type="checkbox" bind:checked {disabled} class="mt-0.5 size-4 shrink-0 rounded border-border-strong accent-[var(--accent)] focus-visible:outline-hidden" {...rest} /><span><span class="text-sm text-fg">{label}</span>{#if description}<span class="block text-2xs text-muted">{description}</span>{/if}</span></label>`; `indeterminate` set via `bind:indeterminate` (Svelte supports it). Reads the Field context the same way (`id`, `describedBy`, `invalid`, `required`).
5. **`Switch.svelte`**: props `checked` (`$bindable(false)`), `label` (required), `description?`, `disabled?`, `class`, rest → the `<button>`. Markup: `<label class="inline-flex items-center gap-2 …"><button type="button" role="switch" aria-checked={checked} {disabled} onclick={() => (checked = !checked)} class="relative h-5 w-9 rounded-full transition-colors {checked ? 'bg-accent' : 'bg-surface-3'} border border-border-strong focus-visible:outline-hidden" {...rest}><span class="absolute top-0.5 size-4 rounded-full bg-surface shadow transition-transform {checked ? 'translate-x-4' : 'translate-x-0.5'}" aria-hidden="true"></span></button><span>…label/description…</span></label>`. Space/Enter toggle natively (it is a button).
6. `index.ts` exports `Checkbox`, `Switch`, `FIELD_CTX`, `controlAttrs`, `FieldCtx`.

---

## Task 2: Modal dismissal control and initial focus; `confirm({ details })`; Popover label shorthand

**Files:** `src/lib/ui/Modal.svelte`, `src/lib/ui/ConfirmDialog.svelte`, `src/lib/ui/confirm.svelte.ts`, `src/lib/ui/ConfirmHost.svelte`, `src/lib/ui/Popover.svelte`, `src/lib/ui/index.ts`.

1. **`Modal`**: new props `dismissible: boolean = true` (false → bits-ui `interactOutsideBehavior="ignore"` and `escapeKeydownBehavior="ignore"` on `Dialog.Content`; the Close button still works), `onBeforeClose?: () => boolean | Promise<boolean>` (called for every close path — Escape, outside click, Close button, `open = false` from the caller via `onOpenChange`; returning `false` keeps the modal open; use it for dirty-checks together with `confirm()`), `initialFocus?: () => HTMLElement | null | undefined` (wired to `onOpenAutoFocus`: `e.preventDefault(); el?.focus()` when it returns an element). Read the bits-ui types for the exact prop names (`node_modules/bits-ui/dist/bits/dialog/types.d.ts`, `.../utilities/dismissible-layer/types.d.ts`, `.../focus-scope/types.d.ts`).
2. **`confirm()`**: `ConfirmOptions` gains `details?: string[]`; `ConfirmDialog` renders them as `<ul class="mt-2 list-disc space-y-0.5 pl-5 text-sm text-muted">` under the message (the spec's "consequence summary" for destructive actions). `ConfirmHost` passes it through.
3. **`Popover`**: new optional props `label?: string`, `variant?: ButtonProps["variant"]`, `size?: ButtonProps["size"]`; when `label` is given and no `trigger` snippet, render `<Button {variant} {size} {...props}>{label}<span aria-hidden="true"> ▾</span></Button>` inside `Popover.Trigger`'s `child`. The `trigger({ props })` snippet form stays for custom triggers. `ariaLabel` default: when `label` is set and `ariaLabel` omitted, use `label`.
4. All existing callers (gallery, any P3 site) keep working unchanged.

---

## Task 3: DataTable states, keys and mobile sort

**Files:** `src/lib/ui/DataTable.svelte`, `src/lib/ui/sort.ts` (+ `tests/sort.node.test.ts` if new pure logic), `src/lib/ui/index.ts`.

1. Props: `loading?: boolean`, `error?: string | null`, `onRetry?: () => void` — rendered through `StateBlock` inside the table frame (one full-width row in the table; a block in the card list): `loading` → `StateBlock state="loading" compact`; `error` → `StateBlock state="error" message={error} {onRetry} compact`; precedence `error` > `loading` > empty > rows. Rows are still rendered beneath a `loading` state only if `rows.length > 0` and a new prop `keepRowsWhileLoading = true` (so a refresh does not blank the table; a subtle `opacity-60` on the body while loading).
2. `rowKey: (row: T) => string | number`.
3. `Column.key` typed `(keyof T & string) | (string & {})` so known keys autocomplete and typos surface, while computed keys remain allowed.
4. **Mobile sort**: below `md`, when any column is `sortable`, render above the card list a row with a `Select` labelled "Sort by" (visually hidden label via `Field`? keep it simple: `<label class="sr-only" for=…>Sort by</label><Select …>`) listing the sortable columns plus a direction `Button` (`variant="ghost" size="sm"`, `aria-label="Toggle sort direction"`, glyph ▲/▼) bound to the same `sort` state. Uses `nextSort`/existing `SortState`; no new pure logic unless a tiny `toggleDir()` helper is extracted (then test it).
5. Header comment documents the state precedence and the duplicate-id caveat (already there).

---

## Task 4: Tokens computed from the accent; kit gallery dev-only; request carry-overs

**Files:** `src/lib/color.ts` + `tests/color.node.test.ts`, `src/lib/theme.svelte.ts`, `src/app.css`, `src/App.svelte`, `src/lib/api.ts` + `tests/request.node.test.ts`, `src/lib/LibraryEditModal.svelte`.

1. **`color.ts`**: `export function contrastRatio(a: string, b: string): number` (WCAG, from `relativeLuminance`); `export function accentTextColor(accent: string, surface: string, min = 4.5): string` — returns the accent itself when `contrastRatio(accent, surface) >= min`; otherwise mixes the accent toward white (dark surfaces, luminance < 0.5) or toward black (light surfaces) in 5 % steps until the ratio is met (cap at 100 %), returning `#rrggbb`. Tests: `#4f46e5` on `#ffffff` → unchanged; `#4f46e5` on `#0f172a` → a lighter colour with ratio ≥ 4.5; a pale accent `#facc15` on white → darker with ratio ≥ 4.5; monotone inputs; output always 7-char hex.
2. **`theme.svelte.ts`** `applyTheme()`: set `--accent-text-light = accentTextColor(accent, "#ffffff")` and `--accent-text-dark = accentTextColor(accent, "#0f172a")` on `<html>`. **`app.css`**: `:root { --accent-text: var(--accent-text-light, var(--accent)); }` and `.dark { --accent-text: var(--accent-text-dark, color-mix(in srgb, var(--accent) 70%, white)); }` (keep the `color-mix` as the no-JS fallback). Remove the fixed 70 % mix as the primary.
3. **`border-strong`** ruling: light `#7c8ba1` (≈3.2:1 on white), dark `#64748b` (≈4.3:1 on slate-950) so control edges meet 1.4.11; verify both ratios with `contrastRatio` in the test file (assert ≥ 3.0) and update the token table comment in `app.css`.
4. **`#/kit` dev-only**: in `App.svelte`'s `pageLoaders`, `kit: () => import.meta.env.DEV ? import("./lib/KitPage.svelte") : Promise.resolve({ default: KitUnavailable })` where `KitUnavailable` is a tiny inline component (or a snippet-rendered `StateBlock state="empty" message="The UI kit gallery is only available in development builds."`). The route stays so links do not 404 into Search. Confirm the production build no longer emits the KitPage chunk (`ls dist/assets | grep -c KitPage` = 0) — report it.
5. **`send` response mode** (P3 carry-over): `expect: "response"` resolves `{ status: number; headers: Headers; body: T | undefined }` (body parsed as JSON when the content type is JSON, else `undefined`); tests: a 200 JSON returns body + headers; a 204 returns `body: undefined` with status 204.
6. **`hashCeiling` bad input** (P3 carry-over): in `LibraryEditModal`, read the input's `validity.badInput` via `bind:this` on change; if true show the inline error "Enter a whole number of bytes." and block Save (today "1e" binds `null` and silently saves the global default).

---

## Task 5: Gallery and docs for everything above

**Files:** `src/lib/KitPage.svelte`, `src/lib/ui/index.ts` header comment, `tests/routes.node.test.ts` (unchanged unless the route changes).

1. Gallery sections updated/added: Field short form (three fields, one required, one with error) and the explicit snippet form; Checkbox (checked/unchecked/indeterminate/disabled/with description) and Switch (on/off/disabled); Modal with `dismissible={false}` + an `onBeforeClose` that calls `confirm()` when a demo field is dirty + `initialFocus` on the second input; `confirm({ details })` demo; Popover `label` shorthand next to the snippet form; DataTable with `loading`, `error` + Retry, and the mobile sort row (resize to see); a token swatch pair showing `accent-text` on `surface`/`surface-2` in both themes with the computed ratio printed via `contrastRatio`.
2. `index.ts` header comment lists the kit rules (class append, barrel vs entry chunk) plus the new Field context rule ("controls inside `<Field>` wire themselves; pass `id`/`aria-describedby` explicitly only for custom controls").
3. Acceptance: open `#/kit` in dev, light and dark, Tab through the new controls, check the modal cannot be dismissed by Escape/backdrop when `dismissible={false}`, the dirty-check prompt appears, the Switch toggles with Space, the DataTable error Retry calls back.

---

## Out of scope (ruled)
Page migrations (P5); mobile layout/CLS work (P9); `Tabs` panel label; `DataTable` virtualisation (P5/P9).
