# Frontend P3 — Request layer and auth hardening (implementation plan)

**Date:** 2026-11-01 · **Spec:** `memory/projects/frontend-review-2.md` §"P3" and findings §1–§3 (IDs A/B/C/D/I/M refer to `review-2/*.md`)
**Branch:** `claude/frontend-p3-request-auth` (off `main`)
**Scope:** `frontend/` only. No backend changes. This phase is the foundation every later phase inherits: one request core, one error vocabulary, one auth-failure path, one sequencing helper.

## Global constraints

- Svelte 5 runes, TypeScript, no new npm dependencies. Node tests only (`node:test`, DOM-free modules; stub `globalThis.fetch` for request tests — `tests/bulkEdit.node.test.ts` already imports `api.ts` under Node, so it must stay importable).
- Every task leaves all four green, run from `frontend/`: `npm run check` (0 errors, 0 warnings) · `npm run check:tests` · `npm test` · `npm run build`.
- Behaviour-preserving unless a task says otherwise: existing call sites keep working; `request()` keeps its signature as a wrapper.
- Never add a `svelte-ignore`. Keep "why" comments short, in the codebase's style. One commit per task, message prefixed `frontend:`.
- New pure modules: `src/lib/apiErrors.ts` (ApiError + errorMessage), `src/lib/safeStorage.ts`, `src/lib/latest.ts`, `src/lib/numInput.ts`; `src/lib/pathlinks.ts` gains `safeHref`. Each gets a Node test.

Baseline on `main` (d6cb56f): check clean, 236 tests, build green, 33 raw `fetch` sites and 13 plain-`Error` throws in `api.ts`, 6 local error-formatting helpers in components.

---

## Task 1: One request core, `errorMessage()`, `safeStorage`, drop the dead `apiKey` path

**Files:** `src/lib/apiErrors.ts` (new), `src/lib/safeStorage.ts` (new), `src/lib/api.ts`, `tests/apiErrors.node.test.ts` (new), `tests/request.node.test.ts` (new), `tests/safeStorage.node.test.ts` (new).

1. **`apiErrors.ts`** (DOM-free, imports nothing from `api.ts`):
   - `export class ApiError extends Error { status: number; body: string; detail: unknown }` — constructor `(status, body)` keeps `message = "<status>: <body>"` and `name = "ApiError"`; `detail` is the parsed JSON `detail` field when the body parses, else `undefined`.
   - `export function errorMessage(e: unknown, opts?: { verb?: string }): string` — rules, in order:
     - `ApiError` with `detail` string → that string.
     - `detail` array (pydantic) → each item as `"<loc[-1]>: <msg>"` (skip `"body"`/`"query"` loc heads; if `loc` empty use `msg`), joined with `"; "`.
     - `detail` object with `validation` array → join its items' `msg`/`message`/string with `"; "`; other object → `JSON.stringify`.
     - No usable detail → by status: 400 "The request was invalid.", 401 "Your session has expired. Sign in again.", 403 `You don't have permission to ${verb ?? "do that"}.`, 404 "Not found. It may be outside your access or removed.", 409 "That conflicts with the current state. Reload and try again.", 413 "That is too large.", 422 "Some of the input is invalid.", 429 "Too many requests. Try again shortly.", ≥500 `The server returned an error (${status}).`, else `Request failed (${status}).` For 401/403/404 the status sentence wins **even if** a detail string exists (the backend's "Missing bearer token" is not user language).
     - `TypeError` whose message contains "fetch" or "network" → "Can't reach the server. Check your connection and try again."; `DOMException`/Error named `AbortError` or `TimeoutError` → "The request timed out."
     - Any other `Error` → `e.message`; anything else → `String(e)`. Never returns a string starting with `ApiError:`.
   - `export function errorStatus(e: unknown): number | null`.
   - `export function friendlyError(e, verb = "view")` stays as an alias of `errorMessage(e, { verb })` for existing callers (re-exported from `api.ts`).
   Tests: every rule above, including a pydantic 422 body, a `{validation:[…]}` body, a 502 HTML body, a 401 with detail, a network `TypeError`, a `TimeoutError`.
2. **`safeStorage.ts`**: `get(key): string | null`, `set(key, value): void`, `remove(key): void`, each wrapped in try/catch with an in-memory `Map` fallback used when `localStorage` is missing or throws (private mode, blocked storage, Node). Replace every direct `localStorage.*` read/write in `src/` (`api.ts`, `theme.svelte.ts`, `osFormat.svelte.ts`, `map/mapSettings.svelte.ts`, `SearchPage.svelte`, `ItemDetail.svelte`) with it. Tests: works with a throwing `localStorage` stub and with none at all.
3. **Delete the `apiKey` path** (D18/A23): remove `KEY()`, `apiKey()`, the `Authorization: Bearer` header built from storage, and the `?api_key=` suffix in `thumbUrl()`; `authMe()` no longer special-cases it. The session cookie is the only browser credential. Remove the `REDACTED`/`AGENT_COMMAND_TERMINAL` dead exports if nothing imports them (grep first).
4. **`send()` core** in `api.ts`:
   ```ts
   type SendOpts = { body?: unknown; headers?: Record<string,string>; signal?: AbortSignal; timeoutMs?: number; expect?: "json" | "none" | "blob" | "text" };
   async function send<T>(method: string, path: string, opts: SendOpts = {}): Promise<T>
   ```
   - JSON-encodes `body` when it is not `FormData`/`Blob`/string; sets `Content-Type` only when a body is sent.
   - Timeout: `AbortSignal.any([opts.signal, AbortSignal.timeout(opts.timeoutMs ?? 30_000)])` filtered for defined; downloads/exports pass `timeoutMs: 10 * 60_000`.
   - `!res.ok` → `throw new ApiError(res.status, await res.text())` always (never plain `Error`). On `401` call the unauthorized hook (Task 3 wires it; this task adds `export function setUnauthorizedHandler(fn: ((path: string) => void) | null)` and calls it before throwing, skipping paths under `/auth/`).
   - Success: `expect: "none"` or status 204/205 or `content-length: 0` → `undefined as T`; `"blob"` → `res.blob()`; `"text"` → `res.text()`; default json: if the content-type is not JSON, `text()` and throw `new ApiError(res.status, text)` (a proxy's HTML error page on a 200 must not become `SyntaxError`).
   - `request<T>(path, init?)` becomes a thin wrapper over `send` (method from `init.method`, body passed through as-is since callers already `JSON.stringify`).
   - Fold all 33 raw `fetch` helpers onto `send` (the delete/revoke 204 family → `expect: "none"`; `download*` → `expect: "blob"` with the long timeout; `authMe` → 401 returns `null` as today via a caught `ApiError`; `authLogout` throws on failure; `previewQuery` keeps its signal). `saveBlob` throws `ApiError`.
   Tests (`tests/request.node.test.ts`, stubbing `globalThis.fetch`): 204 resolves `undefined`; 200 with empty body resolves `undefined`; 200 `text/html` body throws `ApiError`; 404 throws `ApiError` with parsed `detail`; 401 invokes the registered handler exactly once and still throws; `/auth/me` 401 does not invoke it; a timeout aborts (use `timeoutMs: 10` and a never-resolving fetch) and throws something `errorMessage` renders as "The request timed out."; `changeMyPassword` resolves on a 204.
5. Acceptance: `grep -c "await fetch(" src/lib/api.ts` is 1 (inside `send`); `grep -c "new Error(" src/lib/api.ts` is 0; `grep -rn "localStorage\." src` only inside `safeStorage.ts`; `grep -rn "api_key\|apiKey" src` empty.

---

## Task 2: Error presentation pass — one vocabulary everywhere

**Files:** every `.svelte` under `src/lib` with a `catch` that renders an error; `src/lib/DeleteLibraryDialog.svelte`; the six local helpers.

1. Delete the local copies: `TaxonomyPage.errDetail`, `SearchPage.errDetail`, `AgentsPage.errDetail`, `JobsPage.apiDetail`, `AccountPage.errText`; keep `FilterBuilderPage.errText`/`CustomReportsPage.errText` only if they format the `ReportValidationError` shape that `errorMessage` now handles — otherwise delete those too. Callers use `errorMessage(e)` (import from `./apiErrors` or `./api`).
2. Replace `String(e)` / `String(err)` / `` `${e}` `` / `e instanceof Error ? e.message : String(e)` inside `catch` blocks that feed an error banner, toast, `error = …`, `dialogError = …`, `msg = …` with `errorMessage(e)` (pass a `verb` where the surrounding code already has one). Do it with a script, then hand-check each site; a `String(e)` used for logging-only or non-error purposes is left alone. Report the count.
3. `DeleteLibraryDialog`: classify by `errorStatus(e) === 409` (B17); treat 404 as "already gone" → call `onDeleted`.
4. `AccountPage.errText` 401/403 wording: 403 → "The current password is incorrect." (backend returns 403 for a wrong current password); 401 → the generic session sentence; success path after a 204 shows "Password changed. You have been signed out everywhere." then calls the app's logout/probe path (do not `location.reload()` blindly; call `onUpdated`/logout as the component API allows — if no prop exists, `location.reload()` after 1.5 s is acceptable this task, Task 3 revisits).
5. `LoginPage`: 401 → "Invalid username or password."; 429 → "Too many attempts. Try again in a minute."; 5xx/network → `errorMessage(e)`; bootstrap: 422 → `errorMessage(e)` (now "password: String should have at least 8 characters"), `minlength="8"` on the field, and after a bootstrap that creates the admin but fails to sign in, re-probe (`onAuthed()`) instead of staying in bootstrap mode (I4a).
6. Acceptance: `grep -rn "String(e)\|String(err)" src --include=*.svelte` lists only justified non-UI sites (report them); `grep -rn "function errDetail\|function apiDetail\|function errText" src` is empty or lists only report-validation formatters with a comment saying why.

---

## Task 3: Auth that fails closed and recovers

**Files:** `src/App.svelte`, `src/lib/api.ts` (hook wiring), `src/lib/LoginPage.svelte` (OIDC return), `src/lib/AccountPage.svelte`.

1. **Probe states**: replace `authReady` with `authState: "probing" | "ready" | "unreachable"`. `probeAuth()`: `authStatus()` failure (network/5xx) → `"unreachable"` (keep `authMode`/`me` as they were; do **not** set `authMode = "disabled"`); `authStatus()` ok and `mode !== "disabled"` → `authMe()`; a 401 there → `me = null` (wall); any other `authMe` failure → `"unreachable"`. `gated = authState === "ready" && authMode !== "disabled" && me === null`.
2. **Splash**: while `"probing"` render a neutral centred `<p role="status">Connecting…</p>` (no shell, no page, no API calls — fixes the 6 pre-wall 401s, I1a). While `"unreachable"` render a `StateBlock state="error" message="Can't reach the Filearr server." onRetry={probeAuth}` centred, with the brand above it.
3. **401 hook**: in `App.svelte` register `setUnauthorizedHandler(() => { if (authMode !== "disabled" && me) { me = null; toast("Your session has expired. Sign in again.", { tone: "warn" }); } })` on mount and clear on destroy. Because `gated` flips, the page tree unmounts and every `usePolling` stops via its `onDestroy` (verify in the report by reading `LibrariesPage`/`JobsPage`/`AgentsPage`/`LogsPanel`). After re-login (`onAuthed={probeAuth}`), the page remounts and reloads.
4. **Logout**: `doLogout` wraps `authLogout()` in try/catch (on failure `toast(errorMessage(e))` and still re-probe).
5. **OIDC deep link** (M2/D20): before leaving for the IdP store `location.hash` in `safeStorage` under `"oidc.returnHash"`; after a successful probe with a principal, if that key exists, set `location.hash` to it (only if it starts with `#/`) and remove it. Clear `?sso_error=` from the URL with `history.replaceState` after showing it once (M1).
6. **Account page**: on password-change success call the new `onSignedOut` prop (App passes `probeAuth`) so the wall appears immediately instead of a timed reload.
7. Tests: none DOM-free beyond what Task 1 added for the hook; document the manual check list in the report (expire a session → wall + toast; stop the backend → unreachable screen → Retry; login wall shows no 401s in the network log).

---

## Task 4: Request sequencing and non-overlapping polls

**Files:** `src/lib/latest.ts` (new), `tests/latest.node.test.ts` (new), `src/lib/polling.ts`, `tests/polling.node.test.ts`, and the sites listed below.

1. **`latest.ts`**: `export function latest<A extends unknown[], R>(fn: (signal: AbortSignal, ...args: A) => Promise<R>): { run(...args: A): Promise<R | undefined>; cancel(): void }` — each `run` aborts the previous in-flight call's `AbortController`, awaits `fn`, and resolves `undefined` (never throws) when a newer run superseded it or it was aborted; errors from the newest run propagate. Tests: out-of-order resolution (first call resolves last → ignored), abort signal passed and fired, error from the newest call propagates, superseded error swallowed.
2. **`polling.ts`**: `createPoller` treats a `fn` that returns a promise as in-flight and skips ticks (and return-refreshes) until it settles; stop still works mid-flight. Test: an `fn` returning a pending promise is not called again until resolved.
3. Apply `latest()` (pass the signal into the `send` call where the API function accepts one; add an optional `signal` parameter to those API functions): `LibrariesPage.refresh` + `failedPage`, `JobsPage.refreshFailures`/`failuresPage`, `ReportsPage.load`, `TimelinePage.load`, `FolderTreemap.load`, `AlertsPage.loadEvents`/`refresh`, `FolderPicker.go`, `LogsPanel.load`, `AuditPanel.load`, `BrowsePage.load` (also snapshot `libraryId/path/offset` at the top of the call — A18), `ItemDetail`'s item-change `$effect` (one token guards all six fetches — A16), `AgentStatusPanel.pollUntilVerified` (stop on destroy — A30/M18).
4. Acceptance: each listed site uses `latest` or a token; `npm test` covers `latest` and the poller guard; report the before/after of `BrowsePage` rapid navigation reasoning.

---

## Task 5: Numeric inputs, saved-search `view`, `scanLibrary` response

**Files:** `src/lib/numInput.ts` (new) + `tests/numInput.node.test.ts`, `src/lib/LibraryEditModal.svelte`, `src/lib/UsersPanel.svelte`, `src/lib/TaxonomyPage.svelte`, `src/lib/AgentsPage.svelte`, `src/lib/AuthProvidersPanel.svelte`, `src/lib/SessionsPanel.svelte`, `src/lib/map/MapPanel.svelte`, `src/lib/SearchPage.svelte`, `src/lib/api.ts`, `src/lib/LibrariesPage.svelte`.

1. **`numInput.ts`**: `export function numOrNull(v: unknown): number | null` — `number` finite → itself; `null`/`undefined`/`""`/whitespace → `null`; numeric string (trimmed, `.` decimal) → number; anything else (incl. `NaN`, `Infinity`) → `null`. `export function intOrNull(v, { min?, max? })` → integer or null, rejecting out-of-range. Tests for each branch.
2. Replace the `.trim()`-on-number sites: `LibraryEditModal` `hashCeiling` (→ `intOrNull`, `> 0`; show an inline field error instead of silently dropping) and `overrideInputs[...]` (`numOrNull`; round only when `f.scale > 1`, keep fractions for `ffprobe_timeout_s`/`doc_decompression_ratio` — B20), `UsersPanel.hoursOrClear` (`numOrNull`, `0` clears), `TaxonomyPage.sortNum`, `AgentsPage.invMaxEntries` (`intOrNull`). Keep the inputs `type="number"`; the `$state` for these becomes `number | null` where the binding allows, else pass through `numOrNull`.
3. `AuthProvidersPanel` numeric fields: `null`/`""` → omit the key (do not send `0`); `SessionsPanel`: an emptied input is `null`, not `0`, and `0` is allowed as "clear" (drop `min` or set `min="0"`); `MapPanel` draft → `numOrNull` per edge, blank → the existing "Every edge needs a number" error (A28).
4. `SearchPage.saveCurrent`: strip `view` (and any key not in the backend `/search` vocabulary — keep a `UI_ONLY_PARAMS = ["view"]` list) before `createSavedSearch`/`updateSavedSearch` (A4).
5. `scanLibrary` typed `{ job_id: number | null; already_running?: boolean }`; `LibrariesPage.runScan` shows `toast("A scan is already running for <name>.", { tone: "info" })` and does not flip the row to queued when `already_running` (B8); the running-state button label is "Scan" and disabled while `running`/`stopping` (no "Restart").
6. Acceptance: `grep -rn "\.trim()" src/lib/LibraryEditModal.svelte src/lib/UsersPanel.svelte src/lib/TaxonomyPage.svelte` shows only string fields; the four forms save after touching the numeric fields (reason it through in the report with the Svelte binding semantics); tests green.

---

## Task 6: Search keyboard gate, honest clipboard, safe links

**Files:** `src/lib/SearchPage.svelte`, `src/lib/searchKeys.ts` (new) + `tests/searchKeys.node.test.ts`, `src/lib/clipboard.ts`, the clipboard callers, `src/lib/pathlinks.ts` + `tests/pathlinks.node.test.ts` (new), `src/lib/ItemDetail.svelte`, `src/lib/Breadcrumbs.svelte`, `src/lib/BrowsePage.svelte`, `src/App.svelte`, `src/lib/HelpPage.svelte`, `src/lib/AboutPage.svelte`.

1. **`searchKeys.ts`**: `export function listKeyAction(e: { key: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean }, target: { tag: string; editable: boolean; inListbox: boolean }, state: { modalOpen: boolean; view: string; hasHits: boolean }): "focusSearch" | "move:-1" | "move:1" | "open" | "copy" | "toggle" | null`. `/` → `focusSearch` only when not editable; Ctrl/Cmd+K → always `focusSearch`; everything else returns `null` when `modalOpen`, `view === "map"`, `!hasHits`, **or when the target is editable or an `INPUT`/`SELECT`/`TEXTAREA`/`BUTTON`/`A` outside the listbox**; Space/Enter act only when `inListbox`; arrows act when `inListbox` or the target is `BODY`/`MAIN`. `SearchPage.onKeydown` becomes a thin adapter. Tests: a table over targets × keys (A1).
2. **Clipboard**: every `copyText` caller checks the boolean (`SearchPage`, `ItemDetail` ×2, `Breadcrumbs`, `BrowsePage`, `RawView`, `FilterBuilderPage`) and shows "Copy failed — select the text and copy it manually." on `false`; `ApiKeysPanel`/`LlmKeysPanel` use `copyText`; the textarea fallback in `clipboard.ts` restores `document.activeElement` afterwards (A11/I10). Optional: `tests/clipboard.node.test.ts` with stubbed `navigator`/`document`.
3. **`safeHref(value: unknown): string | null`** in `pathlinks.ts`: returns the string only when it parses as a URL with scheme in `http, https, smb, sftp, ftp, ftps, nfs, webdav, webdavs, file` (case-insensitive) or is an absolute path/UNC (`/…`, `\\…`), else `null`; `buildOpenUrl` returns `""` for an unsafe prefix. Apply to: `ItemDetail` `share_url` (and replace its local `safeHttpUrl` with a `safeHref` call restricted to http/https via a `schemes` option), `Breadcrumbs`/`BrowsePage` share prefix, `App.svelte` `sourceUrl`, `HelpPage` resources, `AboutPage` package/source/license URLs (D9/M26). Tests: `javascript:`, `data:`, `vbscript:`, ` javascript:` with leading space, `JAVASCRIPT:`, `smb://host/share`, `\\host\share`, `/mnt/x`, `https://x` (A/D).
4. Acceptance: tests green; `grep -rn "navigator.clipboard" src` only in `clipboard.ts`; `grep -rn "href={" src/lib/ItemDetail.svelte src/lib/Breadcrumbs.svelte src/lib/BrowsePage.svelte src/App.svelte src/lib/HelpPage.svelte src/lib/AboutPage.svelte` shows each dynamic href going through `safeHref`/`buildOpenUrl`.

---

## Out of scope for P3 (ruled)
Comma-safe multi-values (D11/A31), search hash reducer, DSL translation → P6. RBAC gating of admin UI → P8. CSP/cache headers → P10. The migration of error banners onto `Alert` placed next to the action → P5 (P3 only fixes the text).
