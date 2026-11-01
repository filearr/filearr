# Review C — Agents / Admin / Auth / Account / Taxonomy / Filter builder / Help / About

Scope: functionality + client-side security. Read-only review; nothing in the repo was modified.
All paths are under `/home/user/repo`. `FE` = `frontend/src/lib`, `BE` = `backend/filearr`.
Evidence labels: **[verified]** = reproduced or proven from code on both sides (the 204 behaviour was run in Node); **[high]** = follows directly from Svelte/FastAPI semantics; **[verify]** = needs a browser/DB run to be certain.

## Summary
- The UI kit work left the *behaviour* layer untouched: there is **no global 401 handling**, and `request()` cannot parse a 204, so the self-service **password change always reports failure** (and has already killed every session).
- Three separate forms bind `<input type="number">` to string-typed state and then call `.trim()`: per-user session timeouts (silent no-op), taxonomy sort order, and the agent config-group **Save** whenever "Max entries per run" is touched.
- Admin-only surfaces (Agents tab, RBAC/LLM-key panels, custom fields, Updates/Logs cards) are shown to every role and just 403 with raw JSON; conversely several **read/write-scope backend routes expose admin-grade data** (logs, agent commands/results, fleet commands).
- Config-group and auth-provider editors have silent data-loss / lost-update traps (directory bind passwords wiped on save, "pin every field" saves, description can't be cleared, inventory block silently dropped, no version precondition).
- AgentsPage (4 024 lines) polls only the summary tiles, so the agent table, its online dots and the "applied truth" badges never refresh; errors are written to a banner at the top of a page whose action buttons are at the bottom.

Counts: **Critical 4 · Important 28 · Minor 30**.

---

## Findings

### Critical

**C1 · Password change always shows an error (and signs everyone out) · `FE/api.ts:104-115,2397-2401`, `FE/AccountPage.svelte:101-117`, `BE/api/auth.py:475-521` [verified]**
What happens: `POST /auth/password` returns `204 No Content`. `changeMyPassword` goes through `request()`, which does `return res.json()` on every OK response. `new Response(null,{status:204}).json()` rejects with `SyntaxError: Unexpected end of JSON input` (reproduced in Node 22). So the `catch` branch runs: the user sees "SyntaxError: Unexpected end of JSON input", `pwBusy` is reset, the "all sessions were signed out" message and the reload timer never fire — yet the server *did* change the password and `revoke_all_for_principal` deleted every session including the current one. The next click anywhere yields 401s (see C2). The user will usually retry with the old password and get a 403 "Current password is incorrect".
Why it matters: the only self-service credential flow is broken and leaves the user in a half-dead session.
Fix: make `request()` return `undefined` for `res.status === 204` (or `content-length: 0`), and have the Account page treat success as "sign out now" (call the logout/probe path rather than `location.reload()` after 1.5 s).
Test idea (DOM-free): extract `request` body handling into `parseResponse(res)`; test with `new Response(null,{status:204})`, `new Response("{}")`, `new Response("",{status:200})`.

**C2 · A 401 anywhere never returns the user to the login wall · `FE/api.ts:104-115`, `FE/App.svelte:99-127,249-257`, `FE/AgentsPage.svelte:167-173,385-398,608-621`, `FE/LogsPanel.svelte:39-43` [verified by grep: the only `401` handling in `src/` is `friendlyError`'s text and `AccountPage.errText`]**
What happens: `me` is set once in `probeAuth()` and never re-validated. When the idle window (default 7 d), absolute TTL, an admin "force sign-out", a role change/disable (`patch_user` → `revoke_all_for_principal`), "log out everywhere" or C1 kills the session, every call throws `ApiError(401, '{"detail":"Missing bearer token"}')`. Panels print that raw string (or, for the pollers on Agents/Logs/Jobs, swallow it in empty `catch {}` and show stale data forever). The header still says "Sign out" and the user's name. `friendlyError` even returns "Please sign in to continue." but nothing offers a way to do it.
Why it matters: expiry mid-use is certain in any deployment with auth on; today the user must guess to reload.
Fix: add `onUnauthorized` hook in `request()` and the other hand-rolled `fetch` helpers (there are ~30 of them — see M20): on 401 while `authMode !== "disabled"`, call a registered callback that sets `me = null` and runs `probeAuth()`. Pollers should stop on 401.
Test idea: unit-test a `createAuthGuard()` that, given a sequence of statuses, fires the callback once.

**C3 · `<input type="number">` bound to a `string` state, then `.trim()` → TypeError · [high — Svelte 5 `bind:value` on a number input yields `number | null`; the codebase itself documents this in `FE/filterBuilder.ts:133-142`]**
- `FE/UsersPanel.svelte:62-75` (`hoursOrClear(v: string)` → `v.trim()`), bound at `:263,268`. `saveDetails` calls it *outside* a `try`, so as soon as an admin types into "Idle timeout"/"Absolute lifetime" (value becomes a number; clearing gives `null`) the submit handler throws and nothing is saved, no message shown. Per-user session overrides can therefore never be set or cleared from the UI (the form only works if those two boxes are left untouched).
- `FE/TaxonomyPage.svelte:149-154` (`sortNum(s: string)` → `s.trim()`), bound at `:443`. Any category/group save where the Sort-order box was touched fails with the dialog error "TypeError: s.trim is not a function".
- `FE/AgentsPage.svelte:1654` (`f.invMaxEntries.trim()`), bound at `:3507`. In `saveGroup` → `buildSettings`; touching "Max entries per run" makes **Save & apply now / Publish with rollout** fail with a TypeError in `dialogError` for any group whose inventory block is emitted.
Fix: coerce through a shared `numStr(v: unknown)` (the existing `str()` in `filterBuilder.ts` is exactly that) or keep the state as `number | null`.
Test idea: move `hoursOrClear`, `sortNum`, `buildSettings` into `.ts` and unit-test them with `number`, `null`, `""` inputs.

**C4 · Saving the "AD directory sync" panel erases every cross-forest endpoint's stored bind password; the Test button then fails · `FE/AuthProvidersPanel.svelte:79,139-143,178-181,186-199`, `BE/authconfig.py:167-177,227-250,307-328` [verified from both sides]**
What happens: `GET /auth-config/directory` returns each `ldap_directories` entry *redacted*: `bind_password` removed, `has_bind_password: true` added. The panel renders that array as JSON in a textarea and `PUT`s it back verbatim. `_merge_endpoints` treats an entry with no `bind_password` (and not the `"__unchanged__"` sentinel) as "no password" and calls `e.pop("bind_password")`; the stored encrypted secret is dropped. `has_bind_password` is persisted as junk. `_overlay_endpoints` (used by "Test enumeration") does the same, so the test reports a bind failure even though a password was stored. Other secret fields use the sentinel correctly (`SECRET_UNCHANGED`), which makes this easy to miss.
Why it matters: silent loss of directory credentials; sync for secondary forests stops with no UI hint.
Fix: when loading, replace `has_bind_password:true` with `bind_password:"__unchanged__"` in the JSON the operator edits (and strip `has_bind_password` on save); or make the backend treat an absent key as "keep" when `has_bind_password` is true.
Test idea: DOM-free `directoriesToEditorJson()` / `editorJsonToPayload()` pair, round-trip tested.

### Important

**I1 · Auth bootstrap flow is fail-open and flashes the app · `FE/App.svelte:99-121,123-127,211-214`**
(a) `gated = authReady && …` ⇒ before the probe returns, the full shell *and the current page* render (and fire API calls that 401) for an unauthenticated user in `enabled` mode; then the login wall replaces them. (b) The `catch` in `probeAuth` sets `authMode = "disabled"` for **any** error — a transient 5xx from `/auth/me` (which throws a plain `Error`, `api.ts:2479-2484`) or a network blip drops the wall and shows an app that 401s everywhere, with no "Sign out". (c) `doLogout` has no try/catch: a failed `POST /auth/logout` leaves `me` set and surfaces as an unhandled rejection; `authLogout` ignores `res.ok` (`api.ts:2500-2502`). Fix: render a neutral splash until `authReady`; on probe failure show a "can't reach server — Retry" screen instead of assuming `disabled`; guard logout.

**I2 · Admin-only UI is shown to roles that can only 403 · `FE/AdminPage.svelte:35-47`, `FE/App.svelte:235-245,310`, `FE/LibrariesPage.svelte:1147`, `FE/JobsPage.svelte:1663-1671`, `FE/AgentsPage.svelte` (no `me` prop)**
- AdminPage tells non-admins "Your role can see access-control assignments here" and renders `RbacPanel` and `LlmKeysPanel` unconditionally, but every `/rbac/*` (`BE/api/rbac.py:242-615`), `/llm-keys/*` (`BE/api/llm_keys.py:66-166`) route is `require_scope("admin")`. A viewer/user sees red `ApiError: 403: {"detail":"Scope 'admin' required"}` blocks.
- `Agents` nav entry and page are shown to everyone when `agents_enabled`; `GET /agents`, tokens, config groups, `/about`, etc. are admin-only (`BE/api/agents.py:481-1077`), so non-admins get an empty page with a raw 403 banner and a full set of mint/revoke/delete buttons.
- `CustomFieldsPanel` (create/edit/delete) is ungated on Libraries (admin-only writes, `BE/api/custom_fields.py:81-171`); `UpdatesPanel` ("Check now", admin-only `BE/api/system.py:567-600`) is shown to every reader of the Jobs page.
- Admin nav tab itself is visible to all roles.
Fix: pass `me` to AgentsPage and gate; gate RbacPanel/LlmKeysPanel with `isAdmin`; add `isAdmin` to CustomFieldsPanel/UpdatesPanel; hide the nav entries by `isAdminPrincipal(me)`.

**I3 · Errors are rendered as raw JSON and mis-worded · `FE/api.ts:95-102`, `FE/RbacPanel.svelte:57,69,74,79…`, `FE/CustomFieldsPanel.svelte:59,98,132,147`, `FE/LoginPage.svelte:54-60`, `FE/AccountPage.svelte:25-36`, `FE/TaxonomyPage.svelte:86-97`, `FE/AgentsPage.svelte:154-165`**
`friendlyError` falls back to `String(e)` = `409: {"detail":"…"}`; pydantic 422s have `detail` as an *array of objects*, which `AccountPage.errText` (string-only) and the two `errDetail` copies (`JSON.stringify`) dump as JSON. Three copies of the same helper exist. The 403 text is `You don't have permission to ${verb} this item.` so callers produce "…to mint an LLM key this item." and "…to load logs this item." RbacPanel/CustomFieldsPanel use bare `String(e)` ⇒ `ApiError: 403: {…}`. Fix: one `apiMessage(e)` in `api.ts` that parses `detail` (string | `[{loc,msg}]` | `{validation:[…]}`), maps 401/403/404/409/429/5xx/network to sentences, and is used everywhere.

**I4 · Login/bootstrap error handling · `FE/LoginPage.svelte:38-67,124-126`**
(a) Bootstrap = `authBootstrap` then `authLogin`; if the second call fails (429 lockout, network), the catch prints "Could not create the first admin: …" although the admin exists; the page stays in `bootstrap` mode so every retry hits 409 "bootstrap is closed". (b) Every non-429 failure (500, offline, 403 disabled) is reported as "Invalid username or password." (c) "Minimum 8 characters" is a hint only (no `minlength`, no check); a short password yields `Could not create the first admin: 422: {"detail":[{"type":"string_too_short",…}]}`. (d) No confirm-password field when creating the *only* admin — a typo is unrecoverable without DB access. Fix: re-probe `/auth/status` after a bootstrap success/409, distinguish 401/429/5xx/network, add confirm field + `minlength`.

**I5 · Account page overwrites in-progress edits · `FE/AccountPage.svelte:52,134-150`, `FE/App.svelte:323`**
`$effect(() => { loadProfile(); })` reads `me.username/display_name/email/phone`, so any `onUpdated(p)` (e.g. "Save preferences" calls `onUpdated`) replaces `me` and re-runs the effect, wiping unsaved profile fields. Fix: seed once (`onMount`/`$effect.pre` with `untrack`) and only re-seed when `me.id` changes or after a successful profile save.

**I6 · Header Theme / Paths controls don't survive a reload for users with saved preferences · `FE/App.svelte:105-107,155-158,258-272`, `FE/theme.svelte.ts:18-31,37-61`**
`probeAuth()` runs on every page load and `applyServerPreferences()` makes **server values win** over localStorage. `toggleDark()` and the header "Paths" select write only localStorage, so a user who has ever pressed "Save preferences" sees their header choice revert on the next load. Fix: header controls should save to the account (debounced `putMyPreferences`) when `me` is set, or the Account page should say the header is "this browser only".

**I7 · User administration can lock the instance out; role/disable edits don't revert on failure · `BE/api/auth.py:787-806,850-885`, `FE/UsersPanel.svelte:107-146,199-209`**
Backend: `patch_user` has no guard against demoting/disabling the last admin or yourself; `delete_user` counts *disabled* admins as "remaining" and only counts the literal role `admin` (a custom role with the admin scope doesn't count either). Frontend: `UsersPanel` gets no `me`, so nothing marks "(you)" or disables self-actions; `setRole` leaves the `<select>` on the rejected value after an error (no refresh in the `catch`). Role change/disable also silently sign the target out everywhere. Fix: server-side "at least one enabled admin-scope principal must remain" check; UI confirms destructive self-edits and calls `refresh()` in `catch`.

**I8 · New-role form discards manual permission edits after "clone" and defaults to an empty ceiling · `FE/RoleForm.svelte:33-36,64-70,77-84`, `BE/api/roles.py:186-194`**
`submit` sends both the edited `scopes/ceiling_actions` *and* `clone_from`; the server's `if payload.clone_from:` branch overwrites them with the source role's values. So "clone viewer, then tick download" silently creates a clone without download. Also the create form starts with `actions = []` while the API default is `["search_metadata","search_content"]`; a custom role saved with the defaults has an empty ceiling and (for a path-scoped user) sees nothing. Fix: drop `clone_from` from the body (the client already copies), or let the server only clone when scopes/actions are omitted; default the checkboxes to the API defaults and warn when the ceiling is empty.

**I9 · API-key scope picker can create keys that can't read · `FE/ApiKeysPanel.svelte:52-62`, `BE/api_keys.py:50-61`, `BE/security.py:88`**
UI help says "write: Everything in read, plus …" and the role form normalises write⇒read, but the key picker only adds `admin ⇒ read+write`; ticking just *write* (or unticking *read*) mints `["write"]`. `_verify_credentials` allows only `scope in api_key.scopes or "admin" in scopes`, so that key gets 403 on every GET. `MintRequest._valid_scopes` does not expand either. Fix: expand on both sides (`expand_scopes`) and disable the implied checkbox like `RoleForm` does.

**I10 · "Copy" for show-once secrets breaks on plain http · `FE/ApiKeysPanel.svelte:112-117`, `FE/LlmKeysPanel.svelte:102-107`**
Both call `navigator.clipboard.writeText` directly; on `http://<lan-ip>` (the documented default deployment, see `FE/clipboard.ts:1-9`) `navigator.clipboard` is `undefined` → unhandled `TypeError`, no "Copied", while the banner says "copy it now; it is never shown again". Every other page uses `copyText()`. Also the secret stays in `minted` state until the mint form is toggled (no dismiss, no auto-clear on blur/route). Fix: use `copyText`, add an explicit "I have saved it" dismiss that nulls `minted`.

**I11 · Inline "new service account" in the key forms leaves orphans and stale lists · `FE/ApiKeysPanel.svelte:41,64-100`, `FE/LlmKeysPanel.svelte:45,58-90`**
`createServiceAccount` succeeds, then `mintApiKey`/`mintLlmKey` fails (409 disabled, 403 ceiling, 422): `newAccountName` was already cleared while `mintAccount` is still `"__new__"`, so a retry says "Name the new service account"; the created account isn't in the dropdown (no reload on failure) and `ServiceAccountsPanel` (separate component, no shared store) never learns about it. LLM panel defaults to `accounts[0]` even if disabled (API-keys panel picks the first non-disabled) → 409 "service account is disabled". Fix: set `mintAccount = owner` immediately after creating, `await load()` in a `finally`, share one accounts store.

**I12 · Auth-provider editor pins every field and has overlapping keys · `FE/AuthProvidersPanel.svelte:46-47,74-75,165-199`, `BE/authconfig.py:195-216,257-280`**
(a) `buildBody` sends *all* ~30 fields; `set_config` stores each non-null one, so a single save flips every field's badge from "env" to "gui" and freezes values the operator never touched (later `FILEARR_*` changes are ignored). Send only fields that differ from the loaded value. (b) `ldap_attr_display_name` and `ldap_attr_upn` exist in both the LDAP and the Directory form with separate stored blobs; `effective_settings` overlays ldap → directory → oidc, so once the Directory panel has been saved, editing those two fields in the LDAP panel has no effect on logins. (c) A cleared number input is `null`, then `Number(null) === 0` (`:177`) is saved as `0` (timeouts/page-size). (d) A stored secret can't be cleared from the UI (empty ⇒ sentinel). (e) `oidc_link_by_email` ("account-takeover risk") is a plain checkbox with no confirmation.

**I13 · Audit filter lists 21 of ~75 event types · `FE/AuditPanel.svelte:16-22` vs `BE/audit.py:31-172`**
Missing e.g. `profile_updated`, `role_created/updated/deleted`, `session_settings_changed`, `auth_config_changed`, all `agent_*`, `taxonomy_*`, `preset_*`, API/LLM-key and service-account events, `LLM_TOOL_CALL`. An admin can't filter to "who changed auth config". Also "Who" shows `username_attempted` or an 8-char UUID prefix for actor-only events; changing Since/Until without pressing Apply then "Load more" mixes new filters with the old cursor (`:38`). Fix: serve the vocabulary from the backend (or free-text) and key the cursor to the filters.

**I14 · RbacPanel defects · `FE/RbacPanel.svelte:20,55,92-112,196-203`**
(a) `createGrant` response (`GrantOut.warnings`: the NFC/NFD-sibling advisory, `BE/api/rbac.py:88-100,540-582`) is discarded, as is the 404/422 detail; (b) `onPickPath` uses `abs.startsWith(lib.root_path)`, so `/data/media2/x` is treated as inside `/data/media` and yields rel path `2/x` — a *wrong ACL scope*; (c) when "subject kind" flips group⇄user `gSubjectId` keeps the other kind's UUID [verify in browser] and the POST 404s; (d) `meta.role_ceilings` is fetched but never used, so a user-grant for an action over the role ceiling (422) is offered; (e) group members can be added to IdP-sourced groups the next sync will overwrite; (f) grant list shows raw ltree `scope` (`lib_<uuid>.…`) not a path. Fix each; add a boundary-aware `relPathUnder(root, abs)` helper with tests.

**I15 · Read-scope backend endpoints leak admin-grade/other-scope data to every logged-in reader (incl. path-scoped users) · `BE/api/system.py:603-652`, `BE/api/agent_commands.py:1103-1131`, `BE/api/system.py:879-935`**
`GET /system/logs` (read) returns the full app/worker log stream: tracebacks, file paths, and `login failed from <ip> (methods tried…)` (`BE/api/auth.py:395-400`) — paths are not RBAC-filtered. `GET /agent-commands` + `/agent-commands/{id}` (read) return every agent's command `payload` and `result` (stat/verify/inventory/retrieve results) with no principal scoping. `/system/about` (read) includes secret-key / CA-root fingerprints. Fix: raise these to `admin` (or filter by `ctx.sql_clause`), and gate the Logs card client-side.

**I16 · Fleet commands need only `write` scope · `BE/api/agent_commands.py:349-352,472-475,527-530,606-609,791-794,919-922,1147-1150`, `BE/api/agent_updates.py:676-680` vs `:977-980`, `agents.py:986`**
A role with `write` can suspend an agent, queue maintenance/re-extract/inventory/reconcile/self-update and cancel commands — but cannot list agents, and the UI treats the whole page as admin. `rehash-sweep` alone is `admin`. Pick one policy (admin for fleet-wide commands) and make the UI consistent (I2).

**I17 · AgentsPage never refreshes the agent table; errors appear off-screen; success is silent · `FE/AgentsPage.svelte:175-216,558-590,1903,2431-2638`**
The 15 s poller calls `refreshSummary()`, `refreshSweeps()`, `reloadRollouts()` only — **not** `listAgents`. So `last_seen_at`, `status` (pending→active), `update_available/pending`, `health.suspended`, "backing off", `last_auth_mode` and the "Online" dot (computed from `Date.now()` in a non-reactive helper) are frozen until the operator presses Refresh. `toggleSuspend` documents "the applied truth shows up in `health.suspended` after ~1 min", but nothing re-fetches, so the badge never appears and the button still says "suspend" (a second click queues a duplicate). All actions share one `error` banner rendered at the *top* (`:1903`) while the Registered-agents table is rendered last (`:3068`) — a failed row action is invisible without scrolling. Maintain/reconcile/inventory/update/suspend give no confirmation that a command was queued. Overlapping `refresh()` calls (several actions) have no sequence guard. Also `isOnline` hard-codes 5 min while the tiles use `FILEARR_AGENT_ONLINE_THRESHOLD_SECONDS`. Fix: poll `listAgents`, drive "now" from a ticking `$state`, show per-row toasts, add a sequence token; use the server's online flag.

**I18 · Effective-config viewer is cached for the page's lifetime · `FE/AgentsPage.svelte:798-809,523-527,1750-1751,1360-1363,1835-1855`**
`loadEffective` returns early if `effective[id]` exists; it is invalidated only by `saveMembership`. Saving/restoring a group, reordering, promoting/cancelling a rollout, or the agent confirming a generation leaves the "Effective configuration", "generation / confirmed" chip and "This agent will ignore:" list stale. Because `confirmed_generation` is what the operator uses to tell whether the agent has applied the change, the cached chip is actively misleading. Fix: clear `effective` (and `about` on demand) in `saveGroup`, `restoreVersion`, `reorderGroup`, rollout actions and on each poll for expanded rows.

**I19 · Config-group editor: description can't be cleared, no optimistic concurrency, non-atomic reordering · `FE/AgentsPage.svelte:1721-1735,1790-1830`, `BE/api/agent_config_groups.py:844-847,791-880`, `FE/api.ts:3540-3550`**
(a) UI sends `description: null` to clear; backend does `if body.description is not None:` so it is ignored — the old text comes back after refresh. (b) `ConfigGroupUpdateIn` carries no expected version; two admins editing the same group = last write wins and the loser's changes silently vanish (the dialog even stores `currentVersion` but doesn't send it). (c) `reorderGroup` issues two PATCHes (priority-only; no version, no confirm, no rollout, takes effect for all members on their next poll); a failure between them leaves two groups tied; swapping numbers can create a tie with a *third* group so the displayed order is decided by name, not by the arrow. Fix: use `model_fields_set` for description server-side, add `if_version`, add a single `POST …/reorder` endpoint, and confirm.

**I20 · Inventory settings silently dropped / can't be explicitly disabled · `FE/AgentsPage.svelte:1608-1656`**
The `inventory` block is emitted only if `inventoryEnabled || collectors.length || permsConfigured || invScheduleCron`. Unticking the master switch *and* all collectors (the UI text says "authored but inert") drops `paths`, `preset`, `max_entries`, `inherit_scan_paths` on save. It also makes it impossible for a higher-priority group to write `enabled:false` to switch off inventory enabled by a lower-priority group (the merge is per top-level key, `configGroups.ts:84-114`). Fix: emit the block whenever any inventory field is non-default or the stored doc had one.

**I21 · UI promises features that don't exist and ships backend features with no UI · `FE/AgentsPage.svelte:2579,2589`, `FE/api.ts:3048-3053,3261-3283,3313-3340`, `BE/api/agent_share_maps.py`, `BE/api/principal_aliases.py`, `BE/api/agents.py:762`**
Tooltips say "the result appears in the command history" — there is no command-history view; `cancelAgentCommand`, `listAgentShareMaps/create/update/delete`, `updateAgent` (rename), `getConfigGroup` are exported but never called; `POST /agents/{id}/ca-ott` and `/principal-aliases` have no client wrapper at all. Operators can't see why a queued command failed, rename an agent, map shares, or re-issue a CA one-time token from the console. Either build the screens or remove the promises (and dead exports).

**I22 · Filter builder "Open in search" is partly dead and silently lossy · `FE/filterBuilder.ts:660`, `FE/FilterBuilderPage.svelte:~240-250 (openInSearch/unmappedWarn)`, `BE/api/search.py:468-543`**
`kind:video` is mapped to a `type` param; the search endpoint has no `type` (W8 replaced it with repeatable `file_category`, `SearchPage.svelte:505-507`), so the kind filter is dropped without notice. The "conditions dropped" warning is set and then `location.hash = …` immediately unmounts the page, so the warning is never seen (the HELP text promises it is). Fix: emit `file_category`, and pass the unmapped list through the handoff module / show a toast on the search page.

**I23 · Filter-builder DSL emitter produces broken syntax for common values · `FE/filterBuilder.ts:118-127,290-299`, `BE/querydsl.py:322-327`**
`quoteIfNeeded` wraps in `"…"` without escaping; the grammar has no escape, so any value containing `"` yields an unterminated quote (`bad`/`unterminated_quote`) and, for a value like `a"b` with no whitespace, isn't quoted at all. `meta.`/`cf.` values are never quoted, so `cf.shelf:Top Floor` splits into a filter plus a stray free-text term (silently changing the query). The `key` box isn't lower-cased/validated (`meta.Height` ⇒ `bad_meta_key`). Fix: reject (`invalid`) or strip embedded quotes, quote meta/cf values containing whitespace, lower-case + regex-check the key in `validateCondition`; add vectors to `CODEC_VECTORS`.

**I24 · Preset fork is a two-step, non-atomic action · `FE/PresetsPanel.svelte:61-66`**
Fork (POST) then patch (PATCH) when patterns/caveat were edited. If the PATCH fails (e.g. invalid gitignore pattern → 422) the fork already exists but the dialog stays open in "fork" mode; retry → 409 "already exists". Fix: send patterns/caveat in the fork body (backend `ForkIn`) or switch the editor to "edit" mode after the first step.

**I25 · AgentsPage group dialog can't be dismissed safely and mixes concerns · `FE/AgentsPage.svelte:3085-3106,3994-4019`**
Plain `fixed` div (not the P2 `Modal`): no Escape/backdrop handling, no dirty-check — a mis-click on "✕"/"Cancel" discards a 40-field edit; "Save & apply now" publishes fleet-wide immediately without a confirm (rollout has one on promote/cancel). The raw-JSON pane says "The exact policy section that will be sent" but is a snapshot taken when opened (`openRaw`, `:1266-1272`), so later form edits aren't reflected and edits made in it are *not* sent unless "Apply" is pressed.

**I26 · Config-group "publish now" while a rollout is live is neither blocked nor warned · `BE/api/agent_config_groups.py:857-881`, `FE/AgentsPage.svelte:1680-1760`**
The 409 for an existing live rollout is only enforced when the new publish is itself a rollout. A plain publish sets `current_version` to the new snapshot while the old rollout keeps delivering its older `target_version` to covered agents. Confirm intended semantics; at minimum the dialog should warn when `g.active_rollout` is set.

**I27 · Policy form: `scan_cron`/`update_not_before` validators differ from server · `FE/agentPolicyDoc.ts:660-690,716-738`, `BE/policy.py`, `BE/update_gate.py:140-152`**
`textShapeError` uses `Date.parse` (accepts "Jan 5 2026", locale forms) where the server needs ISO-8601; the cron checker allows `?`. A client "valid" can be a server 422 shown as raw JSON. Also `update_not_before` (and `update_window` without zone) are interpreted in *central's* local zone (`parse_not_before`, `_localize`) but the field has no zone hint or picker, so an operator in another zone silently holds updates for the wrong interval.

**I28 · Stream tokens and `thumbUrl` rely on assumptions that aren't enforced · `FE/api.ts:59-75,356-370,963-975`, `BE/streamtokens.py:1-75`**
Stream tokens live in a per-process dict — correct only while a single uvicorn process serves the API (docs say so, nothing enforces it); with `--workers>1` mint and connect land on different workers and the SSE silently reconnect-loops. `thumbUrl` puts the real API key in the query string (`?api_key=`) from `localStorage.apiKey` — the same pattern the stream-token work removed — although no UI ever sets `apiKey` (see Questions).

### Minor

**M1 · LoginPage dead `warning` and stale `?sso_error` · `FE/LoginPage.svelte:16,49-52,131-133,25-28`** — the http-credentials warning is stored then `onAuthed()` unmounts the page (never shown). `?sso_error=` stays in the URL forever (re-appears after any later logout); the reason is ignored so every IdP failure reads the same.

**M2 · OIDC round-trip loses the deep link · `FE/LoginPage.svelte:30-34`, `BE/api/oidc.py:79-118`** — `oidcLoginUrl("/")` is hard-coded; hash routes can't be sent to the server, so after SSO the user lands on `#/search` instead of where they were. Stash `location.hash` in `sessionStorage` before leaving and restore after `probeAuth`.

**M3 · `AuthStatus` type lacks `ldap_enabled` · `FE/api.ts:2338-2346` vs `BE/api/auth.py:68-79`** — the "directory sign-in supported" hint can't be shown; the `DOMAIN\user` tip is a `title=` tooltip only (invisible on touch) at `LoginPage.svelte:111`.

**M4 · No remember-me / session-cookie choice** — cookie is always persistent (`BE/security.py:107-123`, `max_age=session_ttl_hours*3600` from the *env* TTL even when a per-user TTL differs, so the cookie can outlive or under-live the DB row). `BE/authx.py:~163` docstring says SameSite=Strict while code uses `lax`.

**M5 · CSRF relies solely on SameSite=lax + JSON content-type** — no CSRF token/Origin check (`BE/security.py:107-123`); if an operator sets `FILEARR_SESSION_COOKIE_SAMESITE=none` for an embedded deployment, every mutating endpoint is cross-site-forgeable. Worth an Origin check.

**M6 · Account page minor** — 401 text "the current password may be wrong" (`AccountPage.svelte:28`) is wrong: wrong password is 403, 401 means expired; `previewTheme()` persists unsaved previews to localStorage (`theme.svelte.ts:25-28`); after success `setTimeout` reload can fire after the user navigated away (`:109-112`).

**M7 · UsersPanel gaps · `FE/UsersPanel.svelte:89-105,118-125`** — admins can't edit a user's email (api `updateUser` supports it), can't see the "(you)" row, `confirm()` native dialogs, no password confirmation on reset, `dSaved` stays "saved" after reopening.

**M8 · SessionsPanel · `FE/SessionsPanel.svelte:54-68,165,177,202-212`** — `min={ss.min_hours}` blocks entering `0` though the text says "0 clears"; an emptied input is `null` → `Number(null)=0` → silently clears the override; admin "force sign-out" on yourself isn't followed by a re-probe.

**M9 · ServiceAccountsPanel / ApiKeysPanel duplicate loads and have no shared store** (`$effect(load)` ×3 for the same `GET /service-accounts`); `revoke` has no 404 handling.

**M10 · LlmKeysPanel can't set `libraries`, `content_access`, `reveal_paths`, `rate_limit`** though `MintRequest` supports them (`BE/api/llm_keys.py:26-37`); `path_scope` is a free-text ltree with no validation or picker.

**M11 · RolesPanel/RoleForm** — delete of a role in use is only prevented client-side by `users` count (stale after a user edit); builtin role edits have no warning; compare matrix omits scopes column help on mobile.

**M12 · PresetsPanel** — no client check of the 500-pattern / 512-char limits; edits are last-write-wins (the backend bumps `version` but PATCH takes no precondition).

**M13 · CustomFieldsPanel** — `applies_to` (media types) is supported by API but not exposed; label optional falls back to name; raw `String(err)`.

**M14 · LogsPanel · `FE/LogsPanel.svelte:24-66`** — auto-refresh replaces the list with page 1, discarding "Load older" pages; no request sequencing (slow older response can overwrite a newer filter); "critical" not selectable.

**M15 · AuditPanel race** — `load(true)` has no sequence guard; fast filter flips can show results for the previous filter.

**M16 · TaxonomyPage** — back button reads "← Admin" but goes to `#/libraries` (`:271-272`); "Sync built-in seed" never uses the existing `dryRun` mode (`api.ts:703`); "Reclassify items" (whole catalogue) has no confirm and no banner telling the operator that taxonomy edits need a reclassify; `errDetail` JSON-stringifies 422 arrays.

**M17 · FilterBuilderPage** — debounce `$effect` has no cleanup (`:149-155`), a pending preview fires after unmount; tag type-ahead has no debounce/sequence; preview shows only the first 25 rows (backend supports `offset`); "Save as custom report" fixes columns to `rel_path,library,size` regardless of what the preview showed.

**M18 · AgentStatusPanel · `FE/AgentStatusPanel.svelte:60-77`** — verify-poll loop (60 s, 3 s steps) keeps running after the panel unmounts (token only changes with `itemId`), and `refresh()` swallows 401/404.

**M19 · Secrets in component state longer than needed** — `minted` (API/LLM keys), `minted.token` + `installer.sidecar.enrollment_token` (`AgentsPage.svelte:964,1011`), and typed bind/client secrets in `AuthProvidersPanel.form` after a failed save/test stay in memory until the component unmounts. No `console.*` logging anywhere in scope (checked).

**M20 · Inconsistent fetch helpers · `FE/api.ts` (≈30 hand-rolled `fetch`)** — some send `credentials`, some don't, some throw `Error`, some `ApiError`, `authMe` omits the Bearer header (`:2479-2484`), `deleteGroup/addMember/removeMember/deleteGrant` throw plain `Error` (so `e.status` is undefined for callers), ids are interpolated without `encodeURIComponent` in most (`/rbac/groups/${id}`, `/auth/users/${id}`, `thumbUrl`) — safe only because ids are UUIDs.

**M21 · AgentsPage actions on `pending` agents** — update/suspend/maintain/reconcile/inventory/re-extract/re-hash buttons are offered for agents that haven't bound a cert (`:2561-2608`); commands will sit queued. `suspend` has no "pending" state so it can be double-queued.

**M22 · AgentsPage purge flow depends on error text** — `detail.includes("replicated data")` (`:930`) couples to `BE/api/agents.py:1166` wording; use a structured error code.

**M23 · Windows lifecycle one-liner suggests `irm http://<origin>/…ps1` run elevated · `FE/AgentsPage.svelte:2648-2653`** — over a plain-http origin this is unauthenticated code fetched into an elevated shell, and tells operators to pass an *admin* key on the command line. Offer an https URL, a hash, and a narrow key.

**M24 · Registered-agents list is N+1 on the server · `BE/api/agents.py:1049-1068`** — `resolve_update_target` and `resolve_effective_config` per row (up to 200) per page load and per Refresh.

**M25 · Dead code / drift** — `shadowedBy`, `sortLayers`, `formatProvenance` exported but only used by tests; `AuthProvidersPanel` empty `onMount` (`:212`); `RbacActions.role_ceilings`, `EffectiveConfigOut.last_seen_at` unused; `AGENT_COMMAND_TERMINAL` unused; `bits-ui`-era comments.

**M26 · Source/doc links not scheme-checked** — `href={sourceUrl}` (`App.svelte:373`), HelpPage `RESOURCES[].href`, AboutPage `app.source_url/license_url/p.url` come from env/server; an operator-set `javascript:` URL would execute on click. `ItemDetail` already has a local `safeHttpUrl` — hoist it. (Agent-reported module/tool links are safe: `agent_about.py:214` builds `https://pkg.go.dev/<path>` server-side. `{@html}` is not used anywhere in scope; all `target=_blank` have `rel`.)

**M27 · Help/About** — `Help.svelte` renders the same text in `title` and the popover (double speech); `HELP.filter_builder` promises "other conditions are listed and dropped" (see I22); About copy button has no failure retry state beyond text.

**M28 · Dialog accessibility regressions not covered by P2** — AgentsPage, TaxonomyPage, CustomFieldsPanel dialogs are hand-rolled `fixed inset-0` divs (no focus trap/Escape); listed here only because the P2 `Modal` exists but these were not migrated.

**M29 · Pagination/stale offsets** — `agentsPage` clamps to `total-1` rather than the last page start (`:143-147`); unreachable today because Next is disabled at the end, but fragile.

**M30 · `routeFromHash` ignores a query on non-search routes** — `#/agents?…`, `#/admin?x` fall back to Search (`routes.ts:98-103`); fine today, but any future deep-link parameter on those pages (e.g. `#/agents?agent=<id>`) will silently route to Search.

---

## API contract table

| UI call | Backend route | Match? | Notes |
|---|---|---|---|
| `authStatus` GET `/auth/status` | `auth.py:266` | Yes | TS type lacks `ldap_enabled` (M3) |
| `authMe` GET `/auth/me` | `auth.py:464` | Yes | 401→null; other errors plain `Error` → fail-open (I1); no Bearer header |
| `authLogin` POST `/auth/login` | `auth.py:332` | Yes | `warning` never shown (M1) |
| `authBootstrap` POST `/auth/bootstrap` | `auth.py:293` | Yes | 201, **no cookie** — separate login required (I4a) |
| `authLogout` POST `/auth/logout` | `auth.py:437` | Yes | 204; result unchecked |
| `patchMyProfile` PATCH `/auth/me` | `auth.py:541` | Yes | 409/422 shown raw (I3) |
| `putMyPreferences` PUT `/auth/me/preferences` | `auth.py:595` | Yes | 8 KiB cap not mirrored |
| `changeMyPassword` POST `/auth/password` | `auth.py:475` | **No (C1)** | 204 parsed as JSON |
| `mySessionTimeouts` / `getSessionSettings` / `patchSessionSettings` | `auth.py:617,659,670` | Yes | `0` blocked by `min` (M8) |
| `listUsers/createUser/updateUser/deleteUser` | `auth.py:710-885` | Yes | No email edit (M7); C3 on timeouts; no last-admin guard (I7) |
| `listMySessions/revokeMySession/revokeAllMySessions` | `auth.py:922-975` | Yes | |
| `listUserSessions/revokeUserSessions` | `auth.py:978-1012` | Yes | |
| `listRoles/compareRoles/createRole/patchRole/deleteRole` | `roles.py` | Mostly | `clone_from` overrides body perms (I8); default ceiling mismatch |
| `listGroups/createGroup/deleteGroup/members` | `rbac.py:255-462` | Yes | `PATCH /rbac/groups/{id}` never used; IdP groups editable (I14e) |
| `listGrants/createGrant/deleteGrant` | `rbac.py:468-606` | Mostly | `warnings` dropped (I14a); `GrantOut.library_id` ok |
| `rbacPreview` GET `/rbac/preview` | `rbac.py:612` | Yes | |
| `listRbacActions` | `rbac.py:242` | Yes | `role_ceilings` unused |
| `listAudit` GET `/audit` | `audit.py:69` | Yes | event-type list stale (I13) |
| `listApiKeys/mintApiKey/revokeApiKey/listApiKeyScopes` | `api_keys.py` | Yes | write w/o read (I9) |
| `listServiceAccounts/create/patch/delete` | `service_accounts.py` | Yes | |
| `listLlmKeys/mintLlmKey/revokeLlmKey/listLlmRoles` | `llm_keys.py` | Yes | 4 optional fields never sent (M10) |
| `getAuthConfig/saveAuthConfig/testAuthProvider/fetchLdapCert` | `authconfig.py` | **Partly (C4)** | `ldap_directories` sentinel, pin-all (I12) |
| `listPresets/create/fork/patch/delete` | `presets.py` | Yes | fork two-step (I24) |
| `listCustomFields/create/update/delete` | `custom_fields.py` | Yes | UI ungated (I2) |
| `systemFeatures`, `getUpdateCheck`, `runUpdateCheck`, `fetchLogs`, `systemAbout` | `system.py` | Yes | scope mismatch: update-check admin, logs/about read (I2, I15) |
| `getTaxonomy` + CRUD, `syncTaxonomySeed`, `reclassifyExtensions` | `taxonomy.py`, `system.py:1214` | Not re-verified in depth | C3 sort order; shapes look consistent |
| `previewQuery` POST `/query/preview`, `queryKeys`, `assistQuery` | `query.py:77,135,186` | Yes | preview limit ≤50 ok; rows ≤25 |
| `search` (via filter-builder handoff) | `search.py` | **No for `type`** | I22 |
| `listAgents` GET `/agents?limit&offset` | `agents.py:982` | Yes | admin only |
| `getAgentSummary` | `agents.py:815` | Yes | read scope |
| `list/mintEnrollmentToken`, `revokeEnrollmentToken(force)` | `agents.py:477-566` | Yes | `ttl_minutes` 1..1440 not mirrored with `max` |
| `revokeAgent`, `deleteAgent(purge,delete_libraries)` | `agents.py:1072-1195` | Yes | string-coupled 409 (M22) |
| `suspendAgent/runAgentMaintenance/reextract/reconcile/inventory/rehashSweep/triggerAgentUpdate` | `agent_commands.py`, `agent_updates.py` | Yes | scopes inconsistent (I16) |
| `listAgentCommands(kind,state)` | `agent_commands.py:1103` | Yes | read scope leak (I15); `cancelAgentCommand` unused |
| `list/create/update/deleteConfigGroup`, `history`, `rollback` | `agent_config_groups.py` | **Partly** | description null ignored (I19a); no version precondition |
| `listConfigRollouts` (`?status`), `listReleaseRollouts` (`?status_filter`) | `agent_config_groups.py:532`, `agent_updates.py:858` | Yes | param names differ by design (alias) |
| `create/cancel/promoteReleaseRollout`, config rollouts | `agent_updates.py:805-950`, `agent_config_groups.py:570-660` | Yes | |
| `setAgentConfigGroups` PUT | `agent_config_groups.py:1078` | Yes | |
| `getEffectiveConfig`, `agentAbout`, `listInventoryCollectors`, `listHostToolMinimums` | same | Yes | cache staleness (I18) |
| `issueInstallerConfig` | `agent_config_groups.py:1234` | Not re-verified | body fields `central_url_override`, `ttl_seconds` unused by UI |
| `itemAgentStatus`, `verifyItem` | `item_agent_status.py`, `items.py` | Yes | |
| `listAgentShareMaps` etc. | `agent_share_maps.py` | n/a | UI-less (I21) |

---

## AgentsPage split proposal (sub-tabs → components, shared state)

Today: one 4 024-line component with ~120 top-level `$state` bindings and a ~700-line template for the dialog. Suggested hash sub-routes `#/agents/fleet | enroll | groups | rollouts` (`routes.ts` change needed, M30) with these components:

| Component | Owns | Reads from shared store |
|---|---|---|
| `FleetSummaryTiles.svelte` | `summary`, poller | – |
| `AgentsTable.svelte` + `AgentRow.svelte` + `AgentDetail.svelte` (capabilities, membership editor, effective config, re-hash band, About) | `agentsOffset/Total`, `expanded`, per-row busy maps, `memberDraft`, `rehashBand`, `effective/about` caches | `agents`, `groups`, `sweeping/rehashing`, `toolMinimums` |
| `EnrollmentCard.svelte` (mint, installer sidecar, tokens table) | `minted`, `installer`, copy flags | `groups` (names) |
| `RolloutsCard.svelte` + `ReleaseRolloutForm.svelte` | `relRollouts`, `rolloutBusy`, form tiers | `agents` (`offerableVersions`) |
| `ConfigGroupsTable.svelte` | `reordering` | `groups` |
| `ConfigGroupDialog.svelte` (+ `PolicySection.svelte`, `InventorySection.svelte`, `ScanSelections.svelte`, `RawPolicyJson.svelte`, `GroupHistory.svelte`, `RolloutTiers.svelte`) | everything under `dialog`, `collectorEditor`, `rawText`, `tiers`, `historyRows` | `groups`, `presetNames` |

Pure code to extract first (testable): `buildSettings`/`settingsToForm`/`formToSettings` (currently in the component, untested, and the site of C3/I20/I19), `healthTitle`, `errDetail`/`apiMessage`, `untilTime/relTime`, `reorderPriorities(groups, id, dir)`, `shadowedKeys`.

Shared state that **breaks if naively split**:
1. `groups` must be a single store: the dialog's `shadowedKeys`, the table, the enrol/installer checkboxes, per-agent membership chips and `groupsById` all read it, and `reloadGroups()` is called from five places.
2. `effective[agentId]` cache must be invalidated by group saves/rollbacks/rollout actions that live in *other* components (I18) — put it in the store with an `invalidate(agentId?)`.
3. `error` is one banner today; after the split each card needs its own error slot or a toast (I17); otherwise errors still surface in the wrong component.
4. `agents` rows are mutated in place (`a.config_group_ids = res.group_ids; agents = agents`, `:521-522`) — with props this must become an event/store update.
5. `sweeping`/`rehashing` sets (one request each tick) are read by both the table badges and the row action buttons.
6. `refresh()` is the universal "reload everything" callback (≈25 call sites) — turn it into store `reload({agents, groups, tokens, rollouts})` with a sequence token (currently racy).
7. The 15 s poller must live once (store/root), not per component, or each card will multiply the 8 requests per tick.
8. Component-local `confirm()` natives should be replaced with the P2 `confirm()` so the dialog/table split doesn't each re-invent it.

---

## Missing test coverage (DOM-free candidates)

Existing Node tests cover: `about`, `agentAbout`, `agentPolicyDoc`, `configGroups`, `filterBuilder` (10 tests), `hostTools`, `inventoryCollectors`, `color`, `polling`, `routes`, `osFormat`, `sort`, `tone`, `treemap`, `mapGeo`, `bulkEdit`, `control`.

Not covered but extractable (and tied to defects above):
- `api.ts` `request()` / `ApiError` / `friendlyError` (C1, C2, I3): 204, empty body, non-JSON error body, 401 hook, FastAPI 422 array.
- `buildSettings` + `formToSettings`/`settingsToForm` from AgentsPage (C3, I19, I20): number/null inputs, inventory-disabled round trip, description clearing.
- `hoursOrClear` (UsersPanel), `sortNum` (Taxonomy), `ssIdle` conversion (SessionsPanel) (C3, M8).
- `AuthProvidersPanel.buildBody` (C4, I12): `ldap_directories` sentinel round trip, number `null`, only-changed-fields payload.
- `RoleForm.normalise/toggleScope` + payload builder (I8) and `ApiKeysPanel.toggleScope` (I9).
- `onPickPath` / `relPathUnder` (I14b).
- `AccountPage.errText`, `LoginPage` error mapper (I3, I4).
- `conditionsToSearchParams` mapping vs the real `/search` param vocabulary (I22) — add a test that every emitted key ∈ `SEARCH_PARAM_NAMES` (export that list from the backend or snapshot it); quote/meta-value vectors (I23).
- `reorderGroup` priority math incl. ties and third-group collisions (I19c).
- `applyServerPreferences` + header toggle persistence (I6) — `theme.svelte.ts` only partially tested via `color`.
- `AuditPanel.isoOrEmpty`, cursor/filter coupling (I13).
- `usePolling` is covered, but the Agents/Logs pollers' stop-on-401 behaviour isn't (C2).
- `clipboard.ts` fallback ordering (I10).
- `TaxonomyPage.errDetail`, `AgentsPage.errDetail` (duplicate helpers) — collapse then test once.

---

## Questions I could not resolve from code alone

1. **Who is supposed to set `localStorage.apiKey`?** Nothing in `src/` writes it (`grep`), yet `KEY()` is attached to ~33 requests and to `thumbUrl`. If it is only a developer escape hatch, a stale key makes the Bearer path win over the session cookie (`security.py:233-235`) and every call 401s "Invalid or expired API key" after a successful login. Should login clear it?
2. Is publishing a group **immediately while a rollout is live** intended to leave covered agents on the older rollout version (I26)?
3. Should path-scoped (RBAC-restricted) users be able to read `/system/logs`, `/agent-commands`, `/system/about` (I15)? The docstring says "same read scope as failed jobs", but failed jobs expose paths too — is *that* scoped?
4. Is SameSite=none a supported deployment? (M5.)
5. Is the API ever run with more than one uvicorn worker? (I28 — stream tokens are in-process.)
6. Does `POST /agents/installer-config` honour `ttl_seconds`/`central_url_override`, and which URL does it embed when Filearr sits behind a proxy? (Not re-verified.)
7. Taxonomy router (`api/taxonomy.py`) was outside the listed backend files; shapes in `api.ts:600-722` look consistent but weren't diffed against it.
8. For C4(b)/I12(b), is the ldap→directory overlay order intentional (directory wins for shared attribute keys)? If so the LDAP form should hide those two fields.
