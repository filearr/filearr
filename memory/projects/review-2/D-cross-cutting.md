# Review D — API client, build/PWA, types, tests, integration

Reviewed at `main`, read-only. Verified by running `npm test` (236 pass), `npm run check:tests` (clean), `npm run check` (676 files, 0 errors), and by generating the live OpenAPI document from `filearr.main:app` (224 paths / 278 operations) into a scratch venv, then diffing it against `api.ts`. The one machine-checkable claim per finding is marked (verified) or (read-only inference).

## Summary
- The TypeScript is clean: 0 `any`, 0 `@ts-ignore`, 7 non-null assertions, 100 script-level `as` casts, 0 `console.*`, no `{@html}` or `innerHTML`. `check`, `check:tests`, `test` and `build` all run in CI on Node 24 (matching the Dockerfile).
- The weak spot is the request layer. `request()` assumes every success body is JSON, so a 204 is parsed and throws; this makes a successful password change display as a failure. It has no timeout, no 401 handling and no retry. 33 raw `fetch` copies diverge from it, and 13 of them throw plain `Error` instead of `ApiError`.
- Auth is fail-open and never re-checked. Any `/auth/me` failure drops the login wall, and a session that expires mid-use leaves pollers hammering 401s with no redirect.
- Integration is thin. The backend sends no cache headers or security headers, Caddy sends none either, the service worker calls `skipWaiting`/`clientsClaim` with no reload flow, and the image build uses `npm install` and never sets `FILEARR_VERSION`, so the footer says "dev".
- 117 of 250 body-returning operations have no typed response schema, so the hand-written types in `api.ts` are unverified. I found 5 real drifts. Only `bulkEdit` touches `api.ts` in tests, so the request layer, `pathlinks`, `schedule`, `searchparams` and `help` are untested.

## Findings

### Critical

**D1 · A successful password change is reported as a failure** · `frontend/src/lib/api.ts:104-115` (`request`), `:2397-2401` (`changeMyPassword`), `frontend/src/lib/AccountPage.svelte:106-116` · (verified)
- What happens: `POST /api/v1/auth/password` is declared `status_code=204` and returns `Response(204)` (`backend/filearr/api/auth.py:475-521`). `request()` always runs `res.json()`, which throws `SyntaxError: Unexpected end of JSON input` on an empty body. `AccountPage` catches it and shows that raw text as the error, never shows "Password changed", and never runs the 1.5 s reload. `errText` also never handles 403: the backend answers a wrong current password with 403, so the `e.status === 401` branch is dead.
- Why it matters: the password is changed and all sessions are revoked, but the user sees an error and stays on a now-dead page. They will retry with the old password, hit the lockout limiter, and may assume the change failed.
- Fix: make `request()` return `undefined as T` when `res.status === 204`, or when `content-length` is 0 or the content-type is not JSON. Then fold the 13 `status === 204` raw-fetch helpers into it (see D8).
- Test idea: stub `globalThis.fetch` to return `new Response(null, {status: 204})` and assert that `changeMyPassword` resolves. `bulkEdit.node.test.ts` already imports `api.ts` under Node, so this works today.

**D2 · No global auth-failure handling, and the auth probe fails open** · `frontend/src/App.svelte:99-117` (`probeAuth`), `frontend/src/lib/api.ts:2479-2484` (`authMe`), `:104-115` · (verified by reading; behaviour inferred)
- What happens, part 1: `probeAuth()` runs once at startup. If `authStatus()` returns `enabled` but `authMe()` then throws (a 5xx, a Caddy 502 during a restart, or a network blip), the catch block sets `authMode = "disabled"`. `gated` becomes false, so the full app renders with no principal, no login wall and no account link. Every request then 401s with `ApiError: 401: {"detail":...}` banners.
- What happens, part 2: nothing in the app watches for a mid-session 401. `request()` has no interceptor and `grep 401` finds only `AccountPage` and a comment. When the session TTL or inactivity timeout expires, every poller (Jobs 2 s, Libraries 30 s, Logs 10 s, Agents) keeps firing and showing red error banners. `me` stays stale and the user is never sent to the login page; they must reload by hand.
- Why it matters: this is the most visible failure on any long-lived admin tab. The backend still enforces auth, so it is a UX and robustness problem, not an authorisation bypass.
- Fix:
  1. Add a single `onUnauthorized` hook in `request()` and the raw-fetch helpers. On 401 from a non-`/auth/*` call, set a store that `App.svelte` observes and re-run `probeAuth()`.
  2. In the `probeAuth` catch, keep a "backend unreachable, retry" state rather than flipping `authMode` to `"disabled"`. Only fail open when `authStatus()` itself says `disabled`.
  3. Stop pollers while gated.
- Test idea: `request()` with a stubbed fetch returning 401 should invoke a registered callback exactly once. Unit-test `probeAuth`'s decision as a pure function of `(status, meResult)` after extracting it.

### Important

**D3 · The image build uses `npm install`, not `npm ci`** · `Dockerfile:2-7` vs `.github/workflows/ci.yml` (frontend job) · (verified)
- What happens: CI runs `npm ci` (lockfile-exact), but the image build runs `npm install` after `COPY frontend/package.json frontend/package-lock.json* ./`. `npm install` can re-resolve within the caret ranges (`^5.56.8`, `^8.2.1`, ...) and silently ship different versions from the ones CI tested. The `*` glob also lets the build proceed with no lockfile at all.
- Fix: use `npm ci` and drop the glob.

**D4 · The footer shows "Filearr dev" in every release image, and `/version.app_version` is ignored** · `frontend/vite.config.ts:117`, `frontend/src/App.svelte:346`, `:79-86` · (verified)
- What happens: `__APP_VERSION__` is `process.env.FILEARR_VERSION ?? "dev"`. `FILEARR_VERSION` is set nowhere: not in the Dockerfile, `build.yml`, or compose (repo-wide grep). The backend already returns `app_version` from `/api/v1/version` (`backend/filearr/api/system.py:36-55`), but the client reads only `build_stamp`, `source_url` and `agents_enabled`.
- Fix: show `v.app_version` from the runtime endpoint and fall back to the build constant. Alternatively pass `ARG FILEARR_VERSION` through the Dockerfile.

**D5 · No cache headers for the SPA, so stale `index.html` can point at chunks that no longer exist** · `backend/filearr/main.py:124-127` (`StaticFiles(directory="static", html=True)`), `docker/caddy/Caddyfile.*` · (read-only inference; cache behaviour needs a browser check)
- What happens: Starlette `StaticFiles` sends `ETag` and `Last-Modified` but no `Cache-Control`. Grep finds no cache or security header anywhere in the backend except the thumbnail route (`api/items.py:556`). Browsers apply heuristic freshness (about 10% of the file's age), so after a deploy `index.html`, `sw.js`, `registerSW.js` and `manifest.webmanifest` can be served from the HTTP cache. The new image has replaced `/assets/*` with new hashes, so the stale HTML 404s on its chunk URLs. Conversely, immutable hashed assets get revalidated on every load instead of being cached for a year.
- Why it matters: `App.svelte` has a `pageLoadFailed` fallback for exactly this ("Filearr may have been updated"), which shows how often it occurs.
- Fix: a small middleware or custom `StaticFiles` subclass setting `no-cache` for non-`/assets/` paths (`index.html`, `sw.js`, `registerSW.js`, `manifest.webmanifest`) and `public, max-age=31536000, immutable` for `/assets/*`. Doing it in the backend covers the plain `:8484` port as well as Caddy.
- Test idea: a pytest on the ASGI app asserting the headers per path.

**D6 · The service worker auto-updates with no reload flow, so old tabs break after a deploy** · `frontend/vite.config.ts:75-110`, `frontend/dist/sw.js`, `dist/registerSW.js` · (verified in the build output)
- What happens: `registerType: "autoUpdate"` generates `skipWaiting()` plus `clientsClaim()` and a bare `registerSW.js` that only calls `register()` on load. There is no `virtual:pwa-register` import, no `onNeedRefresh`, no `controllerchange` reload, and no `vite:preloadError` handler (grep: none). On deploy:
  1. The new SW takes over open tabs immediately.
  2. The old precache is cleaned.
  3. An old tab's next lazy `import("./lib/XPage.svelte")` requests the old hashed URL and gets a 404, so the page shows "failed to load, Reload".
  4. The first navigation after a deploy is served the old precached shell, so users see the old version for one extra visit.
- Fix: use `registerType: "prompt"` (or `virtual:pwa-register` with `onNeedRefresh`) and show a toast with a Reload button. Also add `window.addEventListener("vite:preloadError", () => location.reload())`, guarded once per session to avoid a reload loop.

**D7 · No security headers anywhere (CSP, HSTS, frame-ancestors, nosniff, Referrer-Policy)** · `docker/caddy/Caddyfile.internal`, `Caddyfile.acme`, `backend/filearr/main.py`, `frontend/index.html` · (verified by grep)
- What happens: there is no `header` directive in either Caddyfile, no response-header middleware in the backend, and no CSP `<meta>`. `ci.yml` and `index.html` contain none either.
- Why it matters: an admin console that can create users, mint API keys and trigger deletes is frameable (clickjacking), has no HSTS on the public ACME deployment, and has no CSP to limit any XSS (see D9).
- Feasibility: `index.html` has no inline script. `registerSW.js` is external, Svelte 5 emits no inline `<script>`, and Tailwind output is a stylesheet. A policy of `default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'` should work. `'unsafe-inline'` is needed for style because components use `style=` attributes.
- Exceptions: relax it for `/api/docs` (FastAPI's Swagger HTML has an inline script) and extend `img-src` with the opt-in tile host from `mapSettings.tileUrl`.
- Fix: set the headers in Caddy under `header { ... }` and also in a backend middleware, so that the plain port is covered.

**D8 · 33 hand-copied raw `fetch` calls duplicate auth and error handling and diverge from `request()`** · `frontend/src/lib/api.ts` (33 `await fetch(`; 33 `KEY()` sites) · (verified)
- What happens:
  - Each copy re-implements the `Authorization` header.
  - 13 throw `new Error(\`${status}: ${text}\`)` and 15 throw `ApiError`. Callers that test `e instanceof ApiError` or `e.status` (13 sites) silently miss the 13 plain-Error helpers.
  - `DeleteLibraryDialog.svelte:30-36` classifies errors by `msg.includes("409:")` and `startsWith("Error: 409")`, which also misfires when a response body contains "409:".
  - `credentials: "same-origin"` appears on some copies and not others (it is the fetch default, so redundant).
  - `saveBlob` (`:2172-2183`) throws a plain Error and has no `Content-Disposition` filename use.
  - `friendlyError` ends in `String(e)`, which renders `ApiError: 500: {"detail":"..."}` with the class name and raw JSON.
  - The `{detail}` extraction is copied six times as a local `errDetail`/`errText` (`TaxonomyPage.svelte:86`, `SearchPage.svelte:369`, `ItemDetail.svelte:208`, `AccountPage.svelte:30`, `AgentsPage.svelte:157`, `JobsPage.svelte:264`). The `friendlyError` the task asked about lives in `api.ts` and handles only 401/403/404.
  - 422 validation arrays are shown via `JSON.stringify(detail)`, which is unreadable. FastAPI's `detail: [{loc,msg,type}]` should be formatted as `field: message`.
- Fix: one `send(method, path, {body, expect: "json" | "none" | "blob"})` core that always throws `ApiError`, plus one exported `errorMessage(e)` that parses `detail` (string or array). Replace the six local copies and make `DeleteLibraryDialog` check `e.status`.
- Test idea: table-driven tests of `errorMessage` over `ApiError` bodies (string detail, array detail, non-JSON, empty body, 502 HTML).

**D9 · Agent-supplied `share_url` is rendered into `href` with no scheme check** · `frontend/src/lib/ItemDetail.svelte:83,495-503`, `frontend/src/lib/pathlinks.ts:14-20,54-57`, `backend/filearr/share_resolution.py:77-80` · (verified; exploitability needs a browser check)
- What happens: the backend returns the agent's `share_hint.share_url` verbatim when it is any non-empty string (`share_resolution.py:77-80`). `ItemDetail` then does `<a href={itemShareUrl} target="_blank">`. The validators in `agent_share_maps.py` apply only to admin-created share maps, not to hints from the replication stream. `classifyPrefix` accepts any `scheme://`, so `javascript://%0aalert(1)` classifies as a URL. `buildOpenUrl` is used by Breadcrumbs and BrowsePage for library `share_prefix` values, and the library `share_prefix` field has no scheme validation (`schemas.py:63,106`).
- Why it matters: a compromised or hostile agent, which runs on a separate file server, can plant a clickable `javascript:` link that an admin opens from the item page. `rel="noopener"` does not neutralise a `javascript:` URL.
- Contrast: `ItemDetail` already has a local `safeHttpUrl` allowlist for `origin_url` and `referrer_url`; the share link does not use it. `AboutPage` URLs are sanitised server-side (`about.py:295`).
- Fix: validate the scheme against `smb|sftp|ftp|nfs|webdav|file` (plus http/https) in `pathlinks.ts` and at agent-hint ingest. Hoist the allowlist to a shared module, and render the text without a link when the check fails.
- Test idea: `buildOpenUrl("javascript://x")` and `buildOpenUrl("data://x")` return `""`.

**D10 · Unguarded `localStorage` at module load and on every request can blank the whole app** · `frontend/src/lib/api.ts:59` (`KEY`), `theme.svelte.ts:10,16`, `osFormat.svelte.ts:6`, `map/mapSettings.svelte.ts:19-20`, `SearchPage.svelte:146,192,196,200`, `ItemDetail.svelte:355,359` · (read-only inference)
- What happens: only the writes inside `applyTheme` and `setShareFormat` are wrapped in `try/catch`. All reads are bare, and `theme.svelte.ts` is imported by `App.svelte` at the top level. In browsers where storage access throws (Chrome "block all cookies", some embedded or privacy configurations), the module throws at import and the page stays empty. `KEY()` runs on every API call, so even a rescued page cannot call the API.
- Fix: one `safeStorage` helper (`get(key)`, `set(key, v)`) with try/catch and an in-memory fallback, used by all of the above.
- Test idea: stub `globalThis.localStorage` with a throwing getter and import the module.

**D11 · Comma-joining repeatable search params corrupts values that contain commas** · `frontend/src/lib/api.ts:127-143` (`REPEATABLE_SEARCH_PARAMS`, `search`), `frontend/src/lib/SearchPage.svelte:510,588` · (verified by reading)
- What happens: multi-selects are joined with `,` (`selectedPrincipals.join(",")`) and `search()` splits on commas again. The `principal` facet is made of SIDs, `DOMAIN\name` strings and display names (`api.ts:241-246` says so). An AD display name like `Doe, John` becomes two principals `Doe` and `John`. The restore path (`p.principal.split(",")`) does the same, so a saved search with such a principal also reloads wrong.
- Fix: carry arrays through the params record (use `Record<string, string | string[]>`), or percent-encode commas inside values (`%2C`) in the join and decode on split.
- Test idea: `search()` with `principal: "Doe%2C John"` stub-fetch assertion on the final query string. Also unit-test the encode/decode pair.

**D12 · No timeouts, retries, or offline handling in the request layer** · `frontend/src/lib/api.ts:104-115`, `frontend/src/lib/polling.ts` · (read-only inference)
- What happens: `request()` takes an optional `signal` but never creates a timeout. `AbortSignal.timeout` is used nowhere, so a hung connection (a dead Caddy upstream, a stalled mobile link) leaves the spinner on indefinitely. There is no retry or backoff for idempotent GETs, and no `navigator.onLine` or "offline" handling beyond `RetrievePanel`. `usePolling` fires `fn` without awaiting the previous call, so a slow endpoint stacks overlapping requests.
- Fix: default `AbortSignal.any([init.signal, AbortSignal.timeout(30_000)])`, a longer ceiling for exports and downloads, and a skip-if-in-flight guard in `createPoller`.
- Test idea: `createPoller` with an `fn` that returns a pending promise should not call it again until it settles.

**D13 · API types are hand-maintained against a backend where 47% of operations have no response schema** · `frontend/src/lib/api.ts` (all), `backend/filearr/api/*.py` · (verified)
- What happens: of 250 body-returning operations in the generated OpenAPI, 117 return a plain `dict` or `list[dict]` and so expose no schema. That includes `/items/{id}/permissions`, `/items/{id}/agent-status`, the transfer status, `/taxonomy`, `/system/jobs/*`, `/system/disk`, `/libraries/{id}/diagnose`, `/libraries/{id}/errors`, `/libraries/hash-status`, `/fs/browse`, `/query/*`, exports and custom-report runs. The types for these in `api.ts` are therefore unchecked, and drift can only be found by reading Python. The other 133 operations expose real schemas that nothing diffs against `api.ts`.
- Result: five confirmed drifts (table below). They are the visible part of that class of error.
- Fix: give the dict-returning routes `response_model=` (the detail objects are small and already shaped). Then generate types with `openapi-typescript` from `/api/openapi.json`, or add a CI step that diffs field names between the generated schema and `api.ts` interfaces. This script (my `cmp.py`) already does the field-name half.

**D14 · The request layer, `pathlinks`, `schedule`, `searchparams`, `help` and others have no tests** · `frontend/tests/*` · (verified)
- What happens: 17 test files cover 17 modules (236 tests, all passing). Not covered: all of `api.ts` (request/ApiError/friendlyError/thumbUrl/search param expansion/`initiateTransfer` 409 parsing/`resolveShareHint`/`isAdminPrincipal`), `pathlinks.ts` (security-relevant: D9), `schedule.ts` (253 lines of cron building), `searchparams.ts`, `help.ts` (including the invariant that every `HELP_TOPICS` key exists in `HELP`), `dslHelp.ts`, `handoff.ts`, `cards/{format,keyfacts,registry}.ts`, and `theme.svelte.ts` (`applyServerPreferences` validation). `bulkEdit.node.test.ts` already imports `api.ts`, so Node can load it.
- Coverage map below. D1, D8 and D11 would each have been caught by a 10-line test.
- No component or e2e layer exists for 24k lines of Svelte, and no coverage reporting (`node --test --experimental-test-coverage` is available).

**D15 · SSE reconnect handling has leaks and an unbounded retry in `RetrievePanel`** · `frontend/src/lib/RetrievePanel.svelte:126-176,219-228`, `frontend/src/lib/LibrariesPage.svelte:319-375` · (read-only inference; races need a runtime check)
- `RetrievePanel`: on a dropped connection it retries with a fixed `setTimeout(..., 2000)` and no growth or cap, while `transferId === id`. The timer is not cleared by the effect cleanup (`closeStream()` only closes the `EventSource`). After unmount, or when `opening` is mid-mint, the callback can open a fresh `EventSource` for a destroyed component. A backend outage means a mint POST plus a connection attempt every 2 s per open panel.
- `LibrariesPage.openStream`: after `await mintScanEventsToken(id)` it creates an `EventSource` without re-checking that the scan is still running or the page is still mounted. `closeStream(id)` during the mint is lost, so a stream can leak after unmount. The backoff there is correct (1 s to 15 s).
- Stream tokens are correct everywhere: single-use, minted per connect, and never the API key. This design is good.
- Fix: share one `connectSse({mintToken, url, onFrame})` helper with a cancel flag checked after each await, capped exponential backoff and jitter, and a max-attempt give-up.

**D16 · Dependency updates are not automated for npm, and tooling is unpinned** · `.github/dependabot.yml`, `Dockerfile:57`, CI · (verified)
- What happens: Dependabot covers only `github-actions` (documented as deliberate for Python). The npm side gets no update PRs even for security fixes (`npm outdated`: bits-ui, svelte, svelte-check, swagger-ui-dist behind). There is no `npm audit` step. The Dockerfile uses `ghcr.io/astral-sh/uv:latest`, a mutable tag, while elsewhere the repo pins by digest and SHA.
- Fix: enable an npm ecosystem entry with grouped minor/patch updates, and add `npm audit --omit=dev --audit-level=high` to the frontend CI job. Pin the uv image.

**D17 · No error boundary, global error handler, or telemetry; bug reports have little to attach** · `frontend/src/main.ts`, `frontend/src/App.svelte` · (verified by grep)
- What happens: `main.ts` is three lines. There is no `<svelte:boundary>` (grep: none), no `window.onerror` or `unhandledrejection` listener, and no `console.*` at all. A thrown render error unmounts the tree to a blank page and an unhandled rejection is invisible. Network failures show `TypeError: Failed to fetch`. A user filing a bug can send only the About page's "Copy as Markdown" version table (a good asset) and whatever the red banner said.
- Fix: wrap the page slot in `<svelte:boundary>` with a "Something broke, copy diagnostics" fallback. Register `error` and `unhandledrejection` handlers that keep the last ~20 events in memory. Add them to the About page's copy-as-Markdown dump (route, build stamp, last failed API calls with status and path, no bodies).

**D18 · The `apiKey` localStorage path is dead and contradicts the stream-token policy** · `frontend/src/lib/api.ts:59-75,2479` · (verified)
- What happens: `KEY()` reads `localStorage["apiKey"]`, but no component writes it (grep across `src/`). `apiKey()` is exported and unused. `thumbUrl()` appends `?api_key=<key>` to every `<img>` URL when the key exists, which is the exact pattern the SSE code and the backend (`docs/future-roadmap.md:460`) call out as a credential-in-logs risk and removed. `authMe()` alone omits the Bearer header, so a browser holding only a valid API key is shown the login wall.
- Fix: delete the apiKey code path (session cookie is the only supported browser credential), or if it is a supported debugging aid, document it, route it through the same header helper, and remove `?api_key=` from thumbnail URLs. The backend still accepts it (`api/items.py:483-524`), so its removal there is a separate decision.

### Minor

**D19 · PWA precache is everything, including the dev UI-kit page; no offline story** · `frontend/vite.config.ts:75-110`, `dist/sw.js` · (verified)
- 43 files, 1.19 MB precached on first visit, all lazy page chunks included whether or not the user ever visits them. That includes `KitPage` (92 KB), a development gallery reachable at `#/kit` and shipped to production (`routes.ts` calls it "URL only: deliberately absent from the nav").
- `navigateFallbackDenylist` is correct: `^/api(/|$)` and `^/docs(/|$)` cover `/api/docs`, `/api/openapi.json`, `/api/llm/v1/*`, `/api/v1/agent-dist` and the mkdocs site. `/swagger-ui/*` is only fetched as subresources, which the SW does not route. The lazy chunk question is answered by D6, not by precache coverage.
- No runtime caching or offline page: offline the shell loads from precache but every call fails and `probeAuth` fails open (D2), so the app looks half-alive rather than saying "offline".
- Manifest (`dist/manifest.webmanifest`): `start_url`, `scope`, `background_color` and `lang` are generated, but there is no `id`, and `icon-512.png` is listed for both `any` and `maskable`. If the mark is not inside the maskable safe zone (80% circle) it will be cropped on Android; check the artwork. No `screenshots` or `categories`.
- Fix: exclude `KitPage` from production builds (dynamic import behind `import.meta.env.DEV`), and set `workbox.globIgnores` for rarely used chunks or lazy-cache them with a runtime route.

**D20 · OIDC login always returns to `/`, losing the deep link** · `frontend/src/lib/LoginPage.svelte:31-34`, `api.ts:2350-2353` · (verified)
- The backend sanitises `return_to` correctly (`api/oidc.py:59-100`: must start with `/`), so there is no open redirect. The client, however, always sends `"/"`. A user who followed `#/jobs` and was bounced to the login wall lands on `#/search` after SSO. Appending the hash (`"/" + location.hash`) is safe because the server only sees the path; but note `_fail` appends `?sso_error=` after `return_to`, which would then fall after the `#` and break `LoginPage`'s `location.search` read, so handle that together.
- `?sso_error=` also stays in the address bar forever after the failed attempt, because `App.svelte` only normalises the hash.

**D21 · No ESLint/Prettier; `vite.config.ts` and `scripts/*.mjs` are never type-checked; strictness flags are minimal** · `frontend/tsconfig*.json`, `package.json` · (verified)
- `strict: true` and `verbatimModuleSyntax` are on, but `noUncheckedIndexedAccess`, `noFallthroughCasesInSwitch`, `noUnusedLocals` and `exactOptionalPropertyTypes` are off. `tsconfig.json` includes only `src/**` and `tsconfig.test.json` only `tests/**` and `src/lib/**/*.ts`, so `vite.config.ts` (a 150-line build script with `as` casts) is checked by nothing.
- `allowImportingTsExtensions` is justified in the config comments and harmless here (`noEmit`), but the codebase mixes `./about.ts` and `./api` styles. This is by design (extension required where Node runs the module), but nothing enforces it.
- Engines: `package.json` says `>=24`; CI and the Dockerfile use Node 24, so they agree. There is no `.npmrc` `engine-strict`, so Node 22 (the machine I used) installs and runs everything, and `npm test` passes on 22.22. No mismatch to fix; just not enforced.
- Fix: add `vite.config.ts` to a `tsconfig.node.json`, enable `noFallthroughCasesInSwitch` and `noUnusedLocals`, and consider `noUncheckedIndexedAccess` module by module.

**D22 · Duplicated helpers and inconsistent formatting** · `frontend/src/lib/*` · (verified)
- `fmtBytes`/`formatBytes`/`fmtSize` is implemented about 12 times (SearchPage, LibrariesPage, FilterBuilderPage, CustomReportsPage, ReportsPage, ExportsPanel, BrowsePage, JobsPage, KitPage, `cards/format.ts`, `treemap.ts`, `about.ts`). Units disagree: most use base-1024 with `KB/MB` labels, `JobsPage` and `about.ts` use `KiB/MiB`, and the 0 case returns `"0"`, `"0 B"`, `"—"` or `null` depending on file. Fractional input (0 < b < 1) yields `u[-1]` → `"512.0 undefined"` in all the log-based copies.
- Dates: 79 `toLocale*String` calls, 77 with no explicit locale or options, mixed with `toISOString().slice(0,10)`. `SearchPage.fmtDate` shows the UTC date for an epoch, and `stampToday()` (`api.ts:2192`) stamps export filenames with the UTC date, so a user in UTC-8 gets tomorrow's date on an evening export while other panels show local time.
- `FolderTreemap.svelte` contains raw NUL bytes (line 129, used as a key separator). `grep` and `git diff` treat the file as binary, hiding it from review tooling. Use `"\u0000"` or `|`.
- Fix: one `lib/format.ts` (bytes, date, duration, relative time) with tests; replace NUL with an escape.

**D23 · i18n readiness: none, but cheap to start** · all `.svelte` · (verified by count)
- About 390 single-line capitalised text nodes plus about 320 `title`, `placeholder` and `aria-label` attributes are English literals (lower bounds). `index.html` hard-codes `lang="en"`. There are 58 `(s)` plurals and no `Intl.PluralRules`. All dates and numbers use the browser locale implicitly. `TimelinePage.svelte:46` correctly pins `timeZone: "UTC"` for bucket labels; other pages assume local time.
- A full i18n retrofit is large. Cheap first steps: route all user-visible dates and numbers through D22's `format.ts`, replace `(s)` with a `plural(n, one, other)` helper, and move shared strings (`HELP` is already centralised in `help.ts`) into a messages map.

**D24 · `index.html` is minimal: dark-mode flash, no `theme-color`, no `<noscript>`** · `frontend/index.html`, `frontend/src/App.svelte:196-199` · (verified)
- `applyTheme()` runs in `onMount`, after the first Svelte render, so users on dark mode see a white flash before `.dark` is toggled (the `color-scheme` fix in `app.css:77-86` helps native widgets, not the page background).
- There is no `<meta name="theme-color">` (the manifest has one but browsers that read the meta get nothing), no `<meta name="color-scheme">`, no `<noscript>` message, no description. A 6-line inline script in `<head>` reading `localStorage.theme` would fix the flash, but it conflicts with a strict `script-src 'self'`; use a hash-based or nonce-based CSP entry, or an external tiny `theme-boot.js`.

**D25 · Native `confirm()` is used 33 times though a themed confirm host exists** · e.g. `frontend/src/lib/AgentsPage.svelte:445,458,507,563`, `LibrariesPage.svelte` · (verified)
- `ui/confirm.svelte.ts` plus `ConfirmHost` is wired in `App.svelte` and only `KitPage` uses it. The native dialogs are blocking, unstyled and cannot show the destructive-action emphasis. This likely overlaps the earlier UI review; listed for completeness.

**D26 · Deploy shape assumptions: root-path only, Caddy specifics** · `frontend/vite.config.ts`, `docker/caddy/*` · (read-only inference)
- Everything assumes the app is served at `/`: `API_BASE = "/api/v1"`, manifest `start_url: "/"`, `/docs/`, `/api/docs`, `/swagger-ui/`. A sub-path reverse proxy (`/filearr/`) would break without a `base` and a runtime API prefix. State this in the docs, or support a `<base>`-derived prefix.
- `Caddyfile.internal` uses `tls internal { on_demand }` with no `ask`; this is justified in the file for LAN-only use, but it mints a cert for any SNI if the port is ever exposed more widely.
- On the plain-HTTP port (HTTP/1.1, `8484`), browsers cap six connections per origin. Each running scan holds a permanent SSE connection plus a Retrieve panel's; with the Jobs/Libraries pollers this can starve other requests. Caddy's HTTP/2 path is not affected.
- The dev server proxies only `/api`, so `/docs/` and `/swagger-ui/` 404 under `vite`.

## api.ts ↔ backend schema mismatches (table)

Method: for every `request<Interface>(path)` in `api.ts` whose operation has a typed response model (about 100 interfaces) I compared field names; for untyped routes (117 operations) I read the Python return statements for roughly 25 endpoints. Everything not listed below matched on field names. "Optional" differences are not listed where the backend simply has a default, because Pydantic's output schema marks defaulted fields as not-required while still always sending them.

| # | Endpoint | `api.ts` symbol | Mismatch | Impact |
|---|---|---|---|---|
| 1 | `POST /agents/{id}/reconcile` (and any command listing) | `AgentCommandKind` (`api.ts:3216`) | Backend `CommandEnqueueIn.kind` allows `reconcile`; the TS union omits it. `reconcileAgent` returns `AgentCommandOut` whose `kind` can be `"reconcile"`. | Exhaustive `switch`/label maps over `kind` miss a variant; a `Record<AgentCommandKind, …>` renders undefined for it. |
| 2 | `POST /libraries/{id}/scan` | `scanLibrary` (`api.ts:905`) typed `{ job_id: number }` | Backend returns `{"job_id": null, "already_running": true}` when a job is already queued or running (`libraries.py:814`). | `job_id` can be `null`, and `already_running` is undeclared; callers cannot tell "started" from "already running". |
| 3 | `GET /auth/status` | `AuthStatus` (`api.ts:2338`) | Backend adds `ldap_enabled: bool` (`auth.py:79,286`); TS omits it. | The login page cannot hint at directory login; the field is silently dropped. |
| 4 | `GET`/`POST /rbac/grants` | `RbacGrant` (`api.ts:2519`) | Backend `GrantOut.warnings: list[str]` (NFC/NFD sibling advisory, `rbac.py:97-99`); TS omits it. `RbacPanel` never reads `warnings`. | The advisory the backend deliberately computes is never shown. |
| 5 | `GET /libraries`, `POST`, `PATCH` | `Library` (`api.ts:~420`) | Backend sends `created_at`; TS omits it. | Cosmetic. |
| 6 | `GET /search` hits | `SearchHit = Record<string, unknown> & {…}` | Intentionally open; any key access is `unknown`. | Typos in field names are never caught (see type-safety table). |
| 7 | `GET /items/{id}` | `ItemRecord = Record<string, unknown> & {…}` | Same as 6; `share_url`/`share_source` are the only typed fields. | Same. |
| 8 | `POST /items/batch` | `BatchItemResult = "ok" \| string \| {error: string; detail: unknown[]}` | Overlapping union (`"ok"` is also a `string`); narrowing relies on string compare. | Works, but not a discriminated result. |
| 9 | `POST /scans/…` vs `ScanRun.id` | `TargetedScanResult.scan_id: number\|null` vs `ScanRun.id: string` | The "scan id" returned by the trigger endpoints is a Procrastinate job id (int), while `ScanRun.id` is a UUID; both are called `scan_id`/`id`. | Naming trap: a caller can pass the job id where a `ScanRun` id is expected. |
| 10 | various `Partial<…>` request bodies | e.g. `updateLibrary` | Request bodies are inline `Partial<{…}>` with `chunking_enabled` mis-indented (`api.ts:~890`), which hints at a copy-paste; no schema check at all on request types. | Request-side drift is entirely unchecked. |

Matched on field names (sampled, about 35 endpoint families): presets and extension groups, file groups, taxonomy categories/groups, `/stats/libraries`, `/stats/timeline`, saved searches (`owner_principal`), alert channels/rules/events, custom fields, report schedules, exports, roles and compare, session settings, `/auth/me/*`, users, sessions, audit, agents list and summary, config groups, rollouts, release rollouts, agent commands, share maps, service accounts, enrollment tokens, transfer status, item agent-status, item permissions, failed jobs, `/version`, `/auth/password`. String-literal unions checked against backend enums: `HashPolicy`, `ApiKeyScope`, `TransferState`, `AgentStatus`, `ExportStatus`, `ExportFormat`, `AuthMode`, `share_prefix_source` all agree; only `AgentCommandKind` differs (row 1).

## Dead api.ts endpoints / unused backend endpoints (two lists)

**Declared in `api.ts` but never called from any component** (grep over `src/**/*.svelte` and `*.ts`, excluding `api.ts` itself):
- `getTransfer` (`GET /transfers/{id}`): the page uses SSE only
- `maintenanceMode` (`GET /system/maintenance-mode`): `jobsSummary` carries it
- `systemDisk` (`GET /system/disk`): `jobsSummary.disk` carries it
- `runningJobs` (`GET /system/jobs/running`): `jobsSummary.running` carries it
- `enqueueCustomReportExport` (`POST /custom-reports/{id}/export`)
- `getExport` (`GET /exports/{id}`)
- `updateAgent` (`PATCH /agents/{id}`)
- `cancelAgentCommand` (`POST /agent-commands/{id}/cancel`)
- `listAgentShareMaps`, `createAgentShareMap`, `updateAgentShareMap`, `deleteAgentShareMap`: the whole agent share-map surface, so the OPS/P10 share-map feature has no UI
- `getConfigGroup` (`GET /agents/config-groups/{id}`)
- `apiKey()` helper (D18), `REDACTED` constant, `AGENT_COMMAND_TERMINAL`

**Backend operations with no `api.ts` function (feature gaps worth surfacing)**, excluding agent-plane routes called only by the Go agent (`/agents/register`, `/commands/poll`, `/replication-batch`, `/thumbs`, `/staging`, `/reconcile/*`, `/inventory-results`, `/ca-ott`, `/certificate`, `/rebind`, `/update-manifest`, `/policy`, `/taxonomy` under an agent, `agent-releases` PUT/POST) and installer downloads:
- `GET/POST/PATCH/DELETE /libraries/{id}/scan-paths[...]` (per-library scan scopes): no UI at all
- `GET/PUT/DELETE /principal-aliases` (SID → name mapping, referenced in the permission view's `aliases`): no editor
- `GET /directory/status`, `GET /directory/objects`, `POST /directory/sync` (AD/LDAP directory sync): configured via `auth-config` but with no sync trigger or browser
- `GET /permissions/effective-access`: "who can access this path" has no screen
- `GET /search/federated`: semantic and passage search across indexes
- `POST /items/{id}/digests`: on-demand hashing
- `POST /system/rebuild-index`, `POST /system/embed-backfill`, `POST /system/rbac-backfill`: admin repair actions reachable only via curl
- `GET /system/backups` (list), while `runBackup` and `downloadBackup` are wired
- `PATCH /rbac/groups/{id}`: groups cannot be renamed in the UI
- `GET /agent-commands/{id}`, `GET /alert-rules/{id}`, `GET /alert-channels/{id}`, `GET /custom-reports/{id}`, `GET /saved-searches/{id}`, `GET /presets/{name}`, `GET /agent-share-maps/{id}`, `GET /metadata-profiles` (list; only the by-category get is used): detail GETs, low value
- `GET /health`: used by Docker/monitoring rather than the UI
- Note: three routes my path matcher flagged but are in fact used (`enrollment-tokens` mint/revoke, `sync-seed`, `logs`, `folder-tree`, `alert-events/summary`).

## Test coverage map (file → covered? → candidate tests)

`src/lib` TypeScript modules (17 test files / 236 tests, all green):

| Module | Covered? | Candidate tests |
|---|---|---|
| `api.ts` (4,027 lines) | No (only imported for a type by `bulkEdit` test) | stub-fetch tests: 204 handling (D1), `ApiError`/`errorMessage`, `friendlyError`, `thumbUrl`, `search()` repeatable-param expansion and comma values (D11), `initiateTransfer` 409 id parse, `resolveShareHint` longest-prefix and subfolder remainder, `isAdminPrincipal` with custom roles, `scanEventsUrl`/`transferEventsUrl` token encoding |
| `about.ts`, `agentAbout.ts`, `agentPolicyDoc.ts`, `bulkEdit.ts`, `color.ts`, `configGroups.ts`, `filterBuilder.ts`, `hostTools.ts`, `inventoryCollectors.ts`, `map/geo.ts`, `osFormat.ts`, `polling.ts`, `routes.ts`, `treemap.ts`, `ui/control.ts`, `ui/sort.ts`, `ui/tone.ts` | Yes | `routes`: only 4 tests and the page list is a hand-copied array, so adding a `Page` does not fail it; assert `Object.keys(PAGE_LABELS)` is covered |
| `pathlinks.ts` | No | scheme allowlist (D9), UNC/URL/posix encoding, trailing slashes, `#` and `%` in names |
| `schedule.ts` (253 lines) | No | cron ↔ friendly-schedule round trip, DST-free hour edges, `fmtTime` |
| `searchparams.ts` | No | `encodeSearchHash`/`parseSearchHash` round trip, `isSearchHash` edge cases (`#`, `#/`, `?` only) |
| `help.ts`, `dslHelp.ts` | No | every `HELP_TOPICS` item key exists in `HELP`; no HTML in help strings |
| `handoff.ts` | No | take-once semantics |
| `cards/format.ts`, `cards/keyfacts.ts`, `cards/registry.ts` | No | `fmtBytes` fractional/NaN, registry covers every card type |
| `clipboard.ts` | No (needs DOM) | inject a fake `document`/`navigator` |
| `announce.svelte.ts`, `theme.svelte.ts`, `osFormat.svelte.ts`, `map/mapSettings.svelte.ts`, `ui/confirm.svelte.ts`, `ui/toast.svelte.ts` | No (runes; Node cannot load `$state` without compiling) | move validation (`applyServerPreferences`, storage parsing) into rune-free modules like `color.ts`, then test those |
| `map/basemap.ts`, `map/basemapData.ts` | No | smoke: decoded table round trip |
| `App.svelte`, all pages and `ui/*.svelte` | No | needs a component or e2e layer; consider `@testing-library/svelte` with `jsdom`, or Playwright smoke for login gate + lazy page chunks |

Test quality smells: assertion density is fine (about 3 per test). `tests/routes.node.test.ts` and `tests/bulkEdit.node.test.ts` import with explicit `.ts` extensions as required. No tests are skipped. No snapshot or timing-sensitive tests. The runner has no coverage output and no `--test-reporter` config, so there is nothing to ratchet.

CI (`.github/workflows/ci.yml`, frontend job): `npm ci` → `check` → `check:tests` → `test` → `build`, on Node 24, with `cache: npm`. All four gates are present and `build.yml` re-uses this workflow before pushing images. Missing: `npm audit`, bundle-size budget, type-check of `vite.config.ts`, any lint step.

## Type-safety hot spots (table: file, any/as/! counts)

Counts come from the TypeScript AST over `.ts` files and `<script>` blocks of `.svelte` files (template expressions such as `(e.currentTarget as HTMLInputElement)` are not included; about 13 more by grep). Totals: `any` 0, `as` 100, `!` 7, `as unknown as` 1 (`AlertsPage.svelte:238`), `Record<string, unknown>` 108, `@ts-ignore`/`@ts-expect-error` 0.

| File | `as` | `!` | `any` | `Record<string,unknown>` | Notes |
|---|---|---|---|---|---|
| `lib/AgentsPage.svelte` | 10 | 0 | 0 | 10 | `h.scan as Record<string, unknown>` blocks reading agent health JSON, which is untyped on the server |
| `lib/SearchPage.svelte` | 9 | 0 | 0 | 1 | casts on `SearchHit` fields |
| `lib/filterBuilder.ts` | 6 | 3 | 0 | 0 | non-null on parsed rows |
| `lib/cards/registry.ts` | 8 | 0 | 0 | 1 | |
| `lib/theme.svelte.ts` | 8 | 0 | 0 | 2 | `as Mode`/`as Record` after `includes` checks; correct but repetitive |
| `lib/LibraryEditModal.svelte` | 6 | 0 | 0 | 0 | |
| `lib/ItemDetail.svelte` | 5 | 0 | 0 | 8 | `extractedMeta["origin_url"]` etc. |
| `lib/LibrariesPage.svelte` | 5 | 0 | 0 | 0 | `st.extract_errors as Record<…>` (stats is untyped, D13) |
| `lib/api.ts` | 5 | 0 | 0 | 37 | 37 `Record<string, unknown>` escape hatches (permissions posture, `external_profile`, `preferences`, `args`, ...) |
| `lib/AlertsPage.svelte` | 3 | 0 | 0 | 3 | one `as unknown as TestFireResult` fabricates a fake result object |
| `lib/ArchiveSection.svelte`, `PresetsPanel.svelte` | 2 + 0 | 1 + 2 | 0 | 1 + 0 | non-null assertions on API data |
| `lib/configGroups.ts` | 3 | 0 | 0 | 9 | |
| everything else | ≤ 2 each | | | | |

String-enum versus discriminated-union state: `TransferEvent extends Partial<TransferStatus>` with optional `reason`/`detail`, `BatchItemResult` (overlapping union), `TaxonomyCategory.extractor: string` (comment says deliberately open), and `Library.share_prefix_source` (a proper literal union) are the main sites. Most "state" types are flat interfaces with optional members rather than discriminated unions (for example `ItemAgentStatus`, `ItemPermissions.available`), which pushes the `available === false` / `agent_hosted === false` checks onto every caller.

## Questions I could not resolve from code alone

1. Does Caddy's `encode zstd gzip` buffer or compress `text/event-stream` responses such that scan and transfer progress is delayed? FastAPI's `EventSourceResponse` sends `: ping` keepalives, which helps, but I could not run Caddy here. Confirm with `curl -N` through the TLS port; if delayed, exclude `/api/v1/*/events` from `encode` or add `Cache-Control: no-transform` on those responses.
2. Does Chrome/Safari run a `javascript:` link opened with `target="_blank" rel="noopener noreferrer"` in the app origin (D9)? It depends on the browser version; the fix is cheap either way.
3. Is `icon-512.png` safe-zone padded for `purpose: "maskable"` (D19)? Needs a visual check, for example in Chrome DevTools → Application → Manifest.
4. Which heuristic freshness do browsers actually apply to `index.html` from the image (D5)? It depends on the file mtimes inside the image layer (`COPY --from=frontend` preserves them). A real `curl -I` against a deployed instance would show whether `Last-Modified` is the build date.
5. Is the plain-HTTP `8484` port intended to stay supported long term? It affects D5 (backend vs Caddy cache headers), D7 and the six-connection SSE limit in D26.
6. CSRF: state-changing calls rely on `SameSite=Lax` cookies (`config.py:95`, default) plus JSON bodies; there is no Origin check or token. Acceptable for Lax, but with `session_cookie_samesite = "none"` (offered by the setting) cross-site POSTs would carry the cookie. Is `none` meant to be reachable?
7. Should the UI-kit page (`#/kit`) ship in production builds at all (D19)? It is documented as an acceptance surface, but nothing gates it.
