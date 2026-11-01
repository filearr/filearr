# Review A — Search / Browse / Item detail / Map

Paths are relative to /home/user/repo. `SP` = frontend/src/lib/SearchPage.svelte, `ID` = ItemDetail.svelte, `BP` = BrowsePage.svelte.
Read-only review; nothing in the repo was modified. Where a claim depends on browser behaviour I could not run, it is marked (unverified).

## Summary
- Works well: the pure cores (bulkEdit.ts, map/geo.ts, osFormat.ts, routes.ts) are careful and tested; SP uses AbortController on search/tag/map fetches; snippets are rendered as text + `<mark>` (no `{@html}`); GPS gate and RBAC are enforced server-side; the item-agent-status / transfers / custom-fields / copies contracts match.
- Worst: (1) one window-level keydown handler in SP swallows Space/Enter/arrows for every input, select, slider and button on the page once results exist, so users cannot type spaces in the query/tag/bulk-tag boxes and a space typed in the bulk bar silently changes the selection; (2) the "Query syntax" help promises `kind:/ext:/size:/modified:/meta.*` filters that `GET /search` never parses (it hands `q` straight to Meilisearch); (3) bulk tag edits are computed from the (async, possibly stale) search-index copy of each item's tags and written back as a full replacement, silently clobbering fresher tags.
- Next tier: saved searches 422 in grid/map view; browser forward-navigation and Back-to-empty are broken; deep-link filters from the URL are invisible/unremovable; `copyText()` result is ignored (false "Copied!") at 6 call sites; card registry keys don't match the taxonomy so the 3D and spreadsheet cards are dead; curated-but-unrendered fields are silently dropped from every card; ItemDetail / BrowsePage have no stale-response guard; Browse rejects folders whose 2nd character is ":"; Rescan/Retrieve affordances are offered where the backend always refuses.
- Error handling is uniformly "String(e)" of an ApiError (`ApiError: 422: {"detail":…}` shown to the user); there is no global 401 handling.
- Test coverage is good for pure modules but nothing covers searchparams.ts, pathlinks.ts, cards/format.ts, cards/keyfacts.ts, clipboard.ts, `api.search()` param expansion, or the SearchPage param/hash/selection logic (which lives untestably inside the 2000-line component).

## Findings

### Critical (data loss, wrong data shown, broken core flow)

**A1 · Global keydown handler hijacks Space / Enter / arrows in every control** · SP:1148-1182, registered at SP:1285 (`<svelte:window onkeydown>`)
What happens: after the `/` and Ctrl+K branch, the handler only bails for `selected` (modal), `view==="map"` and `!hits.length`. It never checks `inEditable(document.activeElement)` or whether the target is a button/select/range. With results on screen: `e.key === " "` → `preventDefault()` + `toggleSelect(selectedIndex)`; `Enter` → `preventDefault()` + `openItem(hits[selectedIndex])` (Ctrl/Cmd+Enter copies path); ArrowUp/Down → `moveSel` + `preventDefault()`.
Consequences:
- A space can't be typed in the query box (SP:1288), the extension filter (1511), the principal box (1552; only Arrow/Enter/Escape are stopped), the tag box (1612) or the **bulk-edit tag inputs** (BulkEditBar.svelte:~186/~204, whose `tagKey` calls `preventDefault` but not `stopPropagation`). Multi-word queries/tags such as `annual report`, `sci fi`, `Domain Users` are impossible once a first result set exists.
- Typing a space in the bulk bar toggles the selection state of the row at `selectedIndex` (silently changes which items the bulk edit will touch).
- Enter in the query box opens `hits[selectedIndex]` of the PREVIOUS query (debounce 150 ms has not fired yet): wrong item opened. Enter in the bulk tag input opens the detail modal instead of committing the chip.
- Enter on any focused filter chip / "Select all loaded" / "Apply" button is `preventDefault`ed on keydown, which cancels the click (keyboard users cannot activate them while results exist); Space on buttons likely the same (unverified in Chrome).
- ArrowUp/Down on the Sort `<select>`, the Match-in / Readable-by selects and all four range sliders + the semantic slider are swallowed (they move the results cursor instead), so those controls are mouse-only.
Why it matters: core flow (typing a refined query, tagging in bulk) is broken in the normal state of the page, and one variant corrupts the bulk-edit target set.
Fix: gate the list shortcuts on `e.target` being the results container (or body) and not INPUT/TEXTAREA/SELECT/BUTTON/contentEditable; never act on Space/Enter unless the listbox has focus.
Test idea (DOM-free): extract `listKeyAction(key, mods, targetTag, state) -> "move"|"open"|"toggle"|"copy"|null` and unit-test that INPUT/SELECT/BUTTON targets return null.

**A2 · "Query syntax" help documents a DSL that `/search` does not implement** · frontend/src/lib/dslHelp.ts (whole file), DslHelp.svelte, SP:1390 `<DslHelp onInsert={insertDsl} context="search"/>`, SP:915-919; backend/filearr/api/search.py:465-791 (`index.search(q, …)` with `q` untouched), backend/filearr/querydsl.py:1-40 (header: reference parser for the agent's local `filearr query` CLI/UI and reports)
What happens: chips such as `kind:video`, `ext:mp4;mkv`, `size:>1G`, `modified:>7d`, `path:*/backups/*`, `tag:archived`, `meta.height:>=1080`, `cf.rating:>=4` are inserted into the box and executed. `search()` passes the string verbatim to Meilisearch, which tokenises `size:>1G` into the words "size" and "1G". Only `-word` and `"phrase"` happen to work natively. `~fuzzy` is meaningless (Meili is already typo-tolerant). `grep querydsl backend/filearr/api/search.py backend/filearr/search.py` → no hits; test_dsl_help_examples.py only asserts the examples parse with `querydsl.parse()`, not that search honours them.
Why it matters: the help text and the click-to-run chips produce zero or irrelevant results for the exact examples shown; the UI claims "everything is ANDed" filtering that doesn't exist.
Fix: either translate the DSL to the flat `/search` params (kind→file_category, ext→extension, size/modified→size_/mtime_ ranges, tag→tags) server-side or in a shared client module, or remove the help from the Search page (keep it on Reports).
Test idea: backend integration test that `GET /search?q=kind:video` narrows results; frontend: a pure `dslToParams()` if the client route is chosen.

**A3 · Bulk tag edit is computed from the search-index snapshot and written back as a full replacement** · SP:390-429 (`resolveTargets`, uses `h.tags` at 395-405), SP:439-450, bulkEdit.ts:`computeTagPatch`, api.ts:186 / items.py:696-779 (`tags` replaces)
What happens: tags on the wire REPLACE the list, so the client reads each hit's `tags` from Meilisearch and sends `current ± ops`. The index is updated asynchronously (`defer_index_sync`, which SP's own comment at 431-438 acknowledges). ItemDetail edits (ID:218-287, `patchItem`) don't notify SearchPage, so the row in `hits` keeps the pre-edit tags. Edit tags on item X in the detail pane, close it, tick X, bulk "add tag Y" within the sync window (or any time the index lags, e.g. during a scan/backlog): X's new tags are overwritten by (old tags + Y). Same for two operators. Only items whose tags are `undefined` on the hit are re-fetched.
Why it matters: silent data loss with a success toast.
Fix: always resolve current tags from `GET /items/{id}` (or add a server-side add/remove op to `/items/batch`), or at least refetch the tag list of every item that will receive a tag patch; also have ItemDetail emit `onChanged(item)` so the row is patched locally.
Test idea: extract the resolve/patch orchestration (inject `getItem`/`batchPatchItems`) and test "hit says [a], DB says [a,b], op add c ⇒ patch is [a,b,c]".

### Important (incorrect behaviour a user will hit, silent failures, contract mismatches)

**A4 · Saved search cannot be saved from Grid or Map view (422)** · SP:528 (`p.view = view`), SP:543-547 (`currentParams()` feeds `createSavedSearch`), SP:652-662; backend/filearr/api/search.py:1085-1091 (`SEARCH_PARAM_NAMES` = `/search` signature names), api/saved_searches.py:44-56 (`_validate_params`: unknown key → 422)
What happens: `view` is a UI-only param (SP:150-155 says "backend ignores the extra param", true for `/search`, false for saved-searches). Saving while `view=grid|map` returns 422 `unknown search param(s) ['view']…`; SP shows `savedError = String(e)` → `ApiError: 422: {"detail":"unknown search param(s) ['view']; allowed: […]"}`. List view saves fine, so it looks intermittent.
Fix: strip `view` (and other UI-only keys) in `saveCurrent`, or add `view` to the backend allow-list; show `errDetail(e)`.
Test idea: `currentParams()` extracted to a pure `buildSearchParams(state)`; assert no key outside the backend vocabulary (shared JSON list).

**A5 · Browser Forward (and re-visiting the last-written hash) is ignored** · SP:213 `lastWrittenHash`, SP:554-560 `reflectHash`, SP:627-635 `onHashChange`
Trace: A written (lw=A) → B written (lw=B) → Back to A: `onHashChange` applies A, `runFresh→reflectHash` computes h===location.hash and returns **without updating lw** (lw stays B) → Forward to B: `location.hash === lw` → `return`. UI keeps showing A while the URL says B. Same for a pasted/clicked link whose hash equals the last written one (e.g. ID's `searchByPrincipal` at ID:329-334 after having searched that principal before).
Fix: set `lastWrittenHash` to the hash being applied in `onHashChange`, or use a "pending own write" token cleared on the matching hashchange event.
Test idea: model `reflect`/`onHashChange` as a pure reducer on `{lw, hash}` and replay A→B→back→forward.

**A6 · Back to a hash without filters leaves stale results; display-only/empty saved searches don't clear** · SP:576-625 (`applyParams`, `searchable` at 620-624), SP:627-635
`applyParams({})` resets q/filters/sliders but only calls `runFresh()` when some non-display key has a value. Going Back to `#/search` (or applying a saved search holding only sort/view) leaves the previous `hits`, `total`, `facets`, `searched=true`, and selection on screen with an empty query box and no chips. `reflectHash` isn't run either, so the URL/state disagree.
Fix: in the non-searchable branch reset `hits/total/facets/nextCursor/searched` (return to the blank slate).

**A7 · Filters restored from a URL / saved search are invisible and silently dropped** · SP:211, 519-526 (`pendingRange` feeds the request), SP:1072-1090 (`activeFilters` has no chip for `pendingRange`), SP:638-641 (`onRangeChange` wipes ALL pending ranges)
A deep link with `size_gte=…` or `mtime_lte=…` filters the results but: sliders show the full range (applyParams resets them to bounds at 612-613), `sizeActive/mtimeActive` are false so no chip, and there is no way to see or remove the filter except by moving a slider — which then clears *both* the size and the date pending filters even though only one slider moved. Also `applyStats` replaces bounds with the facetStats of the already-filtered result.
Fix: render chips from `pendingRange`, clear per-pair; keep the original bounds when pendingRange is active.

**A8 · Deep-link / refresh search is delayed behind sequential fetches including the heavy `/stats`, then overwrites what the user typed** · SP:1214-1270; api.ts:398-405, 935 (`semanticStats()` = `GET /stats`), backend api/system.py:238-300 (grouped passes over items, queue snapshot, Meili, statvfs; up to `STATS_TOTAL_BUDGET_S=10s`)
`onMount` awaits listLibraries → getTaxonomy (→ fileGroups) → `semanticStats()` before `applyParams(initial)`. A bookmarked search shows the blank slate until all finish (up to ~10 s when /stats is slow), and if the user starts typing meanwhile, `applyParams` then replaces `q`/filters with the URL's. `/stats` is fetched on every SearchPage mount (ID caches per page load; SP doesn't) just to read `semantic.enabled`.
Fix: run `applyParams(initial)` first (or `Promise.all` the vocab fetches), skip it if the user already interacted, and cache/replace the probe with a cheap endpoint.

**A9 · Every debounced search pushes a history entry** · SP:554-560 (`location.hash = h`), SP:908-911
Typing a query produces several history entries; Back walks through each partial state and re-runs a search each time (and A5/A6 make that walk unreliable). Use `history.replaceState` for in-flight edits, `pushState` only for committed changes (chip toggles/saved search).

**A10 · Error handling: raw `ApiError` strings, no 401 handling, stale results shown under an error, no retry** · SP:738-740, 759-761, 660/676/687 (`String(e)`); api.ts:104-115 (`request`), 713-… `friendlyError` only used in ID/panels; App.svelte (no global 401)
- `error = String(e)` renders `ApiError: 422: {"detail":[{"type":…}]}` / `ApiError: 401: …`. Session expiry (401) shows that text on the Search page with no redirect to login (App only re-probes on its own triggers). 422 (e.g. stale saved `sort`/`search_in`, bad geo box) shows raw JSON. There is an `errDetail` helper in SP:369 and a copy in ID:205 but search/browse don't use either.
- `runFresh` failure leaves the previous `hits/total/nextCursor` in memory (only hidden by the template); `loadMore` failure sets the global `error`, which then **replaces the whole results list** (`{:else if error}` at SP:1743) even though earlier pages were fine, and the next scroll event re-fires `loadMore` (no backoff).
- `loadSaved`, `refreshCopyCounts`, `fetchTagSuggestions`, `fetchPrincipalSuggestions`, `listLibraries`, `getTaxonomy`, Map points (SP:815-819 → silently empty map) all swallow errors with `catch {}`; a 5xx on the map looks like "None of the N results carry coordinates".
- `request()` assumes JSON on 2xx (`res.json()`); a 204/empty body or HTML error page from a proxy throws `SyntaxError`. `deleteSavedSearch`/`touchItem` bypass `request` and throw plain `Error` (not `ApiError`).
Fix: one `describeError(e)` in api.ts (parse FastAPI `detail` string|list, map 401→"session expired, sign in", 403/404 friendly), a retry button, keep prior results on `loadMore` errors.
Test idea: `describeError` table test for 401/403/404/409/413/422(list & string)/network TypeError/non-JSON body.

**A11 · `copyText()` returns false instead of throwing, six callers treat that as success** · clipboard.ts:10-34; SP:1131-1140, ID:104-111 & 401-412, Breadcrumbs.svelte:76-100, BrowsePage.svelte:112-133, RawView.svelte:37-45
`await copyText(p); flash("Copied …")` — the `catch` fallbacks ("Copy failed (clipboard blocked)", the textarea fallback in Breadcrumbs/BrowsePage, "(clipboard blocked)" in ID) are dead code. On plain-http origins where both `navigator.clipboard` and `execCommand` fail (or the user gesture is gone) the UI says "Copied!" while nothing was copied. (AboutPage, LibraryDiagnoseModal and AgentsPage do use the boolean correctly.) ApiKeysPanel.svelte:114 and LlmKeysPanel.svelte:104 call `navigator.clipboard.writeText` directly and fail outright over http (out of scope but same class). The fallback also steals focus into a detached textarea and doesn't restore it.
Fix: `if (!(await copyText(x))) fail()`; restore `document.activeElement` after the fallback.
Test idea: stub `document`/`navigator` and assert `copyText` returns false when `execCommand` returns false / throws, and true on the modern path.

**A12 · Pagination integrity: cursor request uses live UI state; duplicate ids not de-duplicated** · SP:746-764 (`buildParams()` at 751), SP:755 (`hits = [...hits, ...r.hits]`), 1854 & 1905 (keyed each / VList `getKey` by `hitId`), 800-813 (map `acc`, cluster list keyed by `p.id` in MapPanel.svelte:700); backend search.py:81-91, 744 (offset cursor)
- `loadMore` re-reads the form (`q`, chips, sliders mid-drag, `semantic`) instead of the params that produced `nextCursor`; a keystroke inside the 150 ms debounce or a slider drag while scrolling pages the OLD offset of a DIFFERENT query. Store the params used for the last fetch.
- The cursor is just `{"o": offset}`; if documents are added/removed between pages (scans, index sync) rows shift and duplicates appear. Duplicate ids crash Svelte's keyed `{#each}` in dev and corrupt reconciliation in prod; also in the map point list. De-dupe by id when appending.
- A 400-char `Set` of ids doesn't survive: `selectedIds` keeps ids but `hits` replaced on `runFresh`; fine, but `selectedIndex` is not adjusted by `loadMore` shifts.

**A13 · Card registry keyed by retired media types; Model3dCard and SpreadsheetCard are unreachable** · cards/registry.ts:25-33 & 36-44; backend/filearr/file_groups.py:381-437 (category keys: image, audio, video, document, three-d-cad, development, archive, system, other); ID:117-119 (`cardFor(item.file_category)`)
`model3d`, `spreadsheet` and `audiobook` are not categories (3D is `three-d-cad`; spreadsheets/audiobooks are *groups* inside document/audio). Result: 3D models and spreadsheets render through DocumentCard/generic KeyFactsCard — no unsupported-geometry banner, no triangles/vertices/bbox, no sheet names; the audiobook chapter table only appears because AudioCard handles `chapters` for the whole audio category. development/archive/system/other all fall back to "Details". (The metadata *profiles* are correctly keyed `three-d-cad`, so labels work but cards don't.)
Fix: key the registry by category (`three-d-cad`) and let DocumentCard render the spreadsheet block when `m.sheets` exists; delete dead entries.
Test idea: DOM-free — move the key lists to a `.ts` constant and assert every CARD_REGISTRY key ∈ backend category list (export the list via a shared JSON, like querydsl-vectors.json).

**A14 · "Curated" exclude lists hide fields that no card actually renders** · cards/VideoCard.svelte:11-17, AudioCard.svelte:12-16, DocumentCard.svelte:10-16, SpreadsheetCard.svelte:10-14; cards/keyfacts.ts:81-84
`exclude={CURATED}` removes those keys from "More details", but the hand-written highlights omit several: Video — `bitrate`, `title`, `year`, `season`, `episode`, `color_primaries`, `color_transfer`; Audio — `title`, `chapter_count`; Document — `producer`, `creator`, `subject`, `keywords`, `revision`, `title`; Spreadsheet — `subject`, `title`. Those extracted facts are visible nowhere except the Raw tab. Also `leftover` filters `k.startsWith("_")`, so `_extract_error` (set when extraction fails, backend extract.py / libraries.py errors endpoint) is never shown: a failed extraction reads "No additional details extracted." with no reason.
Fix: build CURATED from the keys the highlights actually read; surface `_extract_error` as a warning row.
Test idea: unit test that for each card the set (keys shown in highlights ∪ rendered extras) ⊇ extracted key set; pure `keyfacts.ts` test for the `_` rule.

**A15 · Detail pane and result rows omit basic file facts users expect** · ID:479-664 (header = breadcrumbs + thumb only), SP:1923-1978 (row = category, title, year, snippet, path), BP:373-395, backend TreeItem lacks mtime/status
ItemDetail never shows size, modified/created date, tags, title (as distinct from filename), year, status, hashes (only in Raw). Result rows show neither size nor date although "Largest" and "Newest" sorts exist, so the sort order can't be verified by eye. Backend sends `highlight` (title/filename `<em>` markers, search.py:455-461) which SP never renders (only `snippet`), so filename matches aren't highlighted. After Edit → Save the card tab shows no title/tags, so the saved change isn't visible.
Fix: add a facts header (size, mtime in local time with UTC tooltip, tags chips, title/year, status badge) and size/date columns; render `highlight`.

**A16 · ItemDetail has no stale-response guard; edit save can pair id B with item A's data** · ID:414-449 (`$effect` fires 6 unguarded fetches), 218-287, 280 (`patchItem(id, patch)` where `patch` was diffed against `item`)
Opening another item quickly (similar tile → `onOpen`, or fast successive opens) can let the earlier `getItem/itemCopies/getItemPermissions/listCustomFields/similarItems` resolve last and overwrite state; `saveEdit` then diffs against item A's fields but PATCHes id B (e.g. "tags unchanged" decisions made against the wrong item). Use a per-effect token/AbortController (AgentStatusPanel.svelte already does this with `token`).
Test idea: extract `diffPatch(item, form)`; assert it is only applied when `item.id === id`.

**A17 · Unsaved edits are lost on Esc / backdrop / navigation; the list is not updated after a save** · ID:451-453 (window Escape → `onClose`), ID:466-471, ID:300 (`navigate`), SP:1993-1995, BP:419
Escape (also fired when typing in the Edit form) closes the modal with no dirty check; the same for backdrop click and `onOpen` navigation to another item. `onClose` is the only callback, so SP/BP rows keep old title/tags/year after a successful save (SP's `applyLocally` only runs for bulk edits) — which also feeds A3. Add `isDirty` confirm and an `onChanged(item)` prop.

**A18 · BrowsePage: concurrent loads corrupt the listing** · BP:74-97, 102-110
`load()` captures `libraryId/path/offset` live: after the first `await`, the follow-up folder pages (BP:84-86) call `libraryTree(libraryId, path, LIMIT, offset, res.folders.length)` with the CURRENT props, so navigating A→B mid-load appends B's folders to A's `res` and then `tree = res` is assigned for a stale route; there is no AbortController/sequence guard, so a slow earlier response also overwrites a newer one (folder clicks, Prev/Next). Navigating to another folder while on page ≥2 sets `offset = 0` inside the same `$effect` that reads it → a second redundant load. Folder pages (≤500 each, guard<20 ⇒ ≤10 500 folders) are silently truncated beyond that, and re-fetched on every Prev/Next.
Fix: snapshot args at the top of `load`, use a sequence token, only refetch items on offset change.
Test idea: factor "assemble folder pages" into a pure function taking a fetcher; test truncation and stale discard.

**A19 · Browse (and targeted scan) reject folders whose second character is ":" and mangle backslashes** · backend/filearr/api/libraries.py:87-110 (`raw[1] == ":"` ⇒ 422 "not a drive path"; `replace("\\","/")`); same logic in api/scan_paths.py:40-60 used by `scan/targeted`; BP:93-96 shows `422: {"detail":…}` raw
A folder named `A:Side`, `1:1 Ratio`, `T:Test` (legal on Linux/SMB-mounted music/video trees) can't be browsed or rescanned; folders containing a literal `\` are mangled to a different path. `.strip()` also changes names with leading/trailing spaces. Only reject a drive prefix when it matches `^[A-Za-z]:[\\/]`, and don't treat `\` as a separator for POSIX libraries.

**A20 · Browse lists tombstoned (`missing`) items with no marker; sidecars can't be browsed** · backend libraries.py:520,536,555 (`status <> 'trashed'` ⇒ includes `missing`), TreeItem schema has no `status`; BP:373-395; ID shows no status
Search shows only `active`; Browse shows items missing since the last scan (deleted on disk) as normal files, with a thumbnail attempt and an Open/Retrieve path. Add `status` to TreeItem and badge/filter; document that sidecars (`sidecar_of IS NULL`) are intentionally hidden.

**A21 · Rescan/Targeted-scan UI offered where the backend always refuses** · BP:221-225, 288-329, 387-392; backend libraries.py:872-916 (write scope; agent-owned library ⇒ 422)
Every folder menu and file row has Scan/Rescan; for agent-owned libraries (`Library.source_agent_id`, already loaded in `resolveLibrary`) and for read-only users the call always fails with `Could not scan X: ApiError: 422: {…}`. Hide/disable based on `lib.source_agent_id` and role, and show `errDetail`.

**A22 · Thumb: failure is permanent, no placeholder, layout shifts** · Thumb.svelte:16-25; backend items.py:610-630 (video miss ⇒ 404 "queued; retry shortly")
`failed` latches true on the first `onerror` and the `<img>` is removed (`{#if !failed}` renders nothing, despite the comment promising a badge/icon underneath). Not-yet-generated video thumbs (backend returns 404 and enqueues) never retry in grid/Browse views; rows with and without thumbs misalign and grid tiles (`aspect-square`) collapse to uneven heights. Render a fixed-size placeholder and retry once/with backoff.

**A23 · API key is placed in every thumbnail URL** · api.ts:71-75 (`?api_key=`), backend items.py:479-525
Each `<img src>` carries the long-lived key (access logs, proxies, devtools, screenshots), inconsistent with the SSE design that mints single-use stream tokens "so the real API key never rides the URL" (api.ts:~340). Session-cookie logins are unaffected; key-based UI use is. Use the cookie, or mint short-lived thumb tokens.

**A24 · ItemDetail's resolved share link ignores the OS path-format preference and hides the other open/copy actions** · ID:495-514, ID:489 (`hideFileActions={!!itemShareUrl}`), Breadcrumbs.svelte:117-190
UI-T15 added `shareFormat`/`detectedPlatform` so Windows users get UNC; the item-level `share_url` (agent hint/mapping/library) is shown and copied verbatim (`smb://…`, which Explorer cannot open) and, because it suppresses Breadcrumbs' file row, the native-path "Copy path" and "Open folder" disappear whenever `share_url` resolves. Pass `share_url` through `formatShare/shareLocation` (the backend can send the UNC counterpart) and keep Copy-native-path.

**A25 · "Copy path" differs between Search rows, the Copies list and the detail pane** · SP:1112-1129 (library share → native_prefix → container path), ID:800-809 (`c.native_path ?? c.path`), ID:495 (item `share_url`)
Search rows ignore per-item agent hints/mappings (only the library share), the Copies list ignores shares and OS format, so the same file yields three different strings. For agent-hosted items the fallback `path` is the agent-side absolute path. Centralise in one `resolveItemPath(item, lib)` in pathlinks.ts.

**A26 · Bulk edit orchestration gaps** · SP:452-494, BulkEditBar.svelte:~100-160, ~215-222
- Multi-chunk apply (>500): a request-level failure on chunk k (network/5xx/401) lands in `bulkError`, but chunks 1..k-1 were already committed; no summary, no `applyLocally`, selection untouched ⇒ the user retries blindly and can't tell what changed.
- `clearSelection()` after success and `bulkSummary` assignment run even if the user ran a new query or changed the selection during the request (no run token).
- Typing a tag without pressing Enter leaves `tagsAdd` empty ⇒ `nothingToDo` ⇒ the Apply button is `disabled` at mousedown; the "fold in a half-typed chip" code in `apply()` can't run on the first click (the blur-commit enables the button after the click was swallowed). Likely requires two clicks (unverified).
- Switching `fieldName` keeps the old `fieldRaw`/`fieldMode` (a boolean `true` pre-fills a text field as "true"; a text value fails integer coercion) — reset on change.
- `errDetail(e)` JSON-stringifies FastAPI 422 `detail` arrays (batch body validation) into one line.
Test idea: orchestrate with injected `batchPatchItems` and assert partial-progress reporting.

**A27 · MapPanel: marker presses start a select-drag and clear the active area** · map/MapPanel.svelte:175-185, 206-220, 641-652
`pointerdown/up` handlers are on the `<svg>` and marker `<g>`s are children, so pressing a marker bubbles to `onPointerDown` (sets pointer capture, `dragging = select`) and `onPointerUp` treats it as a click (<4 px) → `if (box) onBoxChange(null)`: clicking a marker while an area filter is active removes the area and re-runs two searches. Because the svg owns pointer capture, the `click` that follows is (per Pointer Events) retargeted to the svg in current browsers, so the marker's `onclick` (open item / expand cluster) may never fire for mouse users (unverified; Enter/Space on the marker works). Ignore pointerdown whose `target` is inside a marker, or capture only after movement > 4 px.

**A28 · MapPanel numeric form turns a blank field into 0** · map/MapPanel.svelte:439-454, geo.ts boxError
`Number("")` is `0` (finite), so clearing the North field and pressing Apply silently searches `top=0`; `boxError`'s "Every edge needs a number" message is unreachable for blanks (only `"-"`/text give NaN). Treat empty/whitespace as NaN. Also accepts only `.` decimals (a locale comma yields the NaN message).
Test idea: extract `parseDraft(draft) -> GeoBox | error` and test "", " ", "37,5", "-".

**A29 · RetrievePanel: silent download failure, whole file buffered in memory, leaked reconnect, no-op Cancel** · RetrievePanel.svelte:163-176, 193-203, 205-217, 275-282; api.ts:~365-377 & 2172-2183 (`saveBlob`)
- `download()` sets `message` on error but the template only renders `message` in the `failedPhase` branch (L279); in the `canDownload` branch a 409/401/expired download shows nothing.
- `downloadTransfer` does `res.blob()` then an object URL: the entire staged file (backend serves a Range-capable FileResponse precisely so it can stream) is held in browser memory; multi-GB videos will fail or crash the tab. `URL.revokeObjectURL` is called synchronously after `a.click()` (can cancel the download in Safari/Firefox). Use a tokenised direct `<a href>` or `showSaveFilePicker` streaming.
- The 2 s reconnect `setTimeout` (L170) is never cleared and its guard `transferId === id` stays true after unmount (the effect cleanup only closes the stream), so a pending timer re-opens an `EventSource` on a destroyed component and may loop on errors.
- Cancel during `starting` (no `transferId` yet) does nothing.

**A30 · AgentStatusPanel keeps polling after unmount** · AgentStatusPanel.svelte:60-76, 95-102
`pollUntilVerified` loops up to 60 s (every 3 s, ≈20 `/agent-status` calls) and only stops on `token` change; closing the modal doesn't bump `token` (no destroy hook). Also an already-`verify_in_flight` item never re-polls, so the Verify buttons stay disabled until reopened.

**A31 · Tag/principal suggestion contract & delimiter issues** · api.ts:2004-2014 vs backend search.py:1001-1041; SP:874; SP:507-514, 580-595, api.ts:127-141; search.py:228
- `searchTags()` sends `type=<category>` but `/search/tags` accepts `file_category` (list), `library`, `status` — `type` is ignored, so the "scope suggestions to the first selected category" feature (SP:872-875) never works; the other active filters (library, extension, etc.) aren't passed either.
- Multi-values travel as a comma-joined string (hash, saved search, `tags` param split by `tags.split(",")` on the server): a tag or principal containing a comma (`Doe, John`, `Smith, J`) is split into two filters on restore and in `api.search` (principal/library/…); `tags` isn't in `REPEATABLE_SEARCH_PARAMS` so it relies on the server split.
- `parseSearchHash` keeps only the last value of a repeated key, so hand-built/API-style links (`file_category=a&file_category=b`) lose values.

**A32 · Detail/list data-format and time-zone inconsistencies** · SP:1199-1208, ID:363-374 & 717, BP:195-205, RawView.svelte:21-23, cards/format.ts:14-23, DocumentCard/ImageCard (`asStr(m.created|modified|taken_at)`)
Five separate byte formatters (SP `fmtBytes` 1024/"KB", BP `fmtSize`, ID `simSize` "GiB/MiB/KiB", cards `fmtBytes` returns null for 0, RawView `fmtSize`) disagree on units/zero; `fmtDate` in SP is UTC date (`toISOString`) while ID/`simWhen` and `perms.collected_at` use local; EXIF `taken_at` shows as `2021:05:03 14:22:11`, docs' `created/modified` as raw ISO strings, with no timezone note. `asStr(obj)` → `"[object Object]"` (keyfacts' `factValue` JSON-encodes; `asStr` doesn't). AudioCard hard-codes "kbps" but `tinytag` yields kbit/s while the mutagen fallback (tasks/extract.py:277-280) and ffprobe (ffprobe.py:353) store bit/s, so APE/FLAC-fallback files read "1411200 kbps". Consolidate into one `format.ts`.
Test idea: table tests for 0, 1023, 1024, 2^40, NaN, null, negative; ISO vs EXIF dates.

### Minor (polish, dead code, small traps)

**A33 · Unguarded `localStorage` at init/use** · SP:146,192,196,200; ID:355-359; osFormat.svelte.ts:6; map/mapSettings.svelte.ts:15-16,25-26; api.ts:59 (`KEY()` on every request)
In private/blocked-storage modes the accessor throws: SearchPage/ItemDetail fail to mount, every `request()` throws. Wrap in try/catch (osFormat's setter already does for writes only).

**A34 · Resource hygiene leftovers** · SP:1273-1282 (`onDestroy` misses `principalController.abort()` and `clearTimeout(principalDebounce)`), ID:103,336 (`shareCopiedTimer`,`copiedTimer`), Breadcrumbs.svelte:70 (`copyTimer`), BP:49,142 (`copyTimer`,`toastTimer`), RawView.svelte:41 (`setTimeout` without handle). Harmless writes to destroyed state, but easy to fix. MapPanel's listeners (online/offline, wheel) are cleaned correctly; map tiles are plain `<image>` nodes keyed by tile (no leak); the basemap decode is cached once.

**A35 · Grid view keyboard navigation doesn't follow the cursor** · SP:1099-1106 (`vlist?.scrollToIndex` only exists in list view), 1854-1897
In grid view arrows move `selectedIndex` by 1 (not by row), nothing scrolls the active tile into view. Hover-only controls (`opacity-0 group-hover:opacity-100` on row checkbox/"Copy path", SP:1940/1975, grid tile 1873) are invisible on touch devices, so bulk select and copy are undiscoverable there.

**A36 · `applyParams` accepts unvalidated values** · SP:599-606
`semantic=abc` → `NaN` (slider label "NaN"); unknown `search_in`/`sort` from an old saved search/URL are sent to the server (`pattern` ⇒ 422 raw) and leave `<select bind:value>` blank; an inverted geo box in the URL is kept in state so the "area" chip renders although `boxParams` drops it (no filter applied).

**A37 · Range sliders are linear and include bogus mtimes** · SP:1655-1696, 691-706
A linear 0..max bytes slider is useless with a long-tail size distribution; `mtime` facetStats max includes the future-dated rows the Timeline page labels "invalid" (FIX-3), stretching the slider. `fmtDate` labels are UTC dates.

**A38 · routes.ts edge cases** · routes.ts:11-31, 76-87
`#/browse/` yields `{page:"browse", browseLib:""}` (BrowsePage then calls `/libraries//tree`), contradicting the doc comment that a library-less browse is not a route; a malformed `%` escape in the path silently falls back to the library root while the library id falls back to the raw string. All producers I found use `gotoBrowse/browseHash` (LibrariesPage:944,1103; ReportsPage:133; ID:300; BP:218,283); no hand-built `#/browse` strings. Paths with `#`, `?`, `%` round-trip correctly.

**A39 · osFormat platform detection** · osFormat.ts:28-35
`p.includes("win")` also matches "Darwin"; harmless today (`navigator.platform`/UA-CH never returns it) but fragile. Use `startsWith`.

**A40 · BrowsePage polish** · BP:16-35, 47, 63-71, 363/382, 398-409
`it.title ?? it.filename` shows blank for `""` titles (SP uses `||`); `libName/shareUrlRaw/shareUncRaw` keep the previous library's values if `listLibraries()` fails on a library switch (wrong open/copy prefixes); folder/⋯ menus have no outside-click/Escape dismissal (Breadcrumbs too); Prev/Next doesn't scroll to top; if the current page empties (items removed) it renders "This folder is empty." and hides Prev (pagination controls are inside `tree.items.length > 0`).

**A41 · Saved-search UX** · SP:652-689
Uses blocking `window.prompt/confirm`; errors are `String(e)`; no "update this saved search with current params" (PATCH `params` exists in api.ts:1709 but is unused by the UI); no indication which saved search is active.

**A42 · Dead / unused code** · cards/format.ts:`fmtBytes` (no importer), registry entries `model3d/spreadsheet/audiobook` (A13), `initiateTransfer().existing` never read, `highlight` field on `SearchHit` (A15), `TaxonomySelector` only used by LibraryEditModal (fine) but silently keeps orphan keys: `categories`/`groups` that no longer exist in the tree can't be removed in the UI, and unchecking a category after group picks loses those picks (TaxonomySelector.svelte:21-33). ItemDetail's `onClose?.()` (ID:333) optional-chain on a required prop. `libs` only used for labels/copy paths.

**A43 · IME / partial input triggers searches** · SP:1294, 908-911
`oninput` fires during IME composition; the 150 ms debounce still issues queries for half-composed text. Skip while `e.isComposing`.

**A44 · `/stats`-based probe duplication and repeated config fetches** · SP:1256-1261, ID:1-14 (cached), cards/KeyFactsCard.svelte:38-59, ID:428-430, BulkEditBar.svelte:96-103
Each KeyFactsCard mount refetches `getMetadataProfile` and `listCustomFields`; ItemDetail and BulkEditBar fetch `listCustomFields` again — 3 identical GETs per detail open. A tiny module-level cache (like ID's `semanticProbe`) would remove them.

**A45 · Total shown as exact** · SP:1747-1749
`{total} results` uses Meilisearch `estimated_total_hits` (search.py:743); label as "about N" when large.

## API contract table
| UI call (api.ts fn) | Backend route | Match? | Notes |
|---|---|---|---|
| `search(params, signal)` api.ts:129 | GET /api/v1/search (search.py:465) | Mostly | repeatables expanded OK (`file_category,file_group,library,principal,exposure`); `tags` comma string split server-side; `view` ignored here but rejected by saved-searches (A4); 422 bodies shown raw (A10); `highlight` returned but unused (A15) |
| `searchTags(q,{type,library})` api.ts:2004 | GET /search/tags (search.py:1001) | **No** | UI sends `type`; backend reads `file_category` (A31) |
| `searchPrincipals(q)` api.ts:244 | GET /search/principals | Yes | RBAC-scoped; no other filters passed |
| `listSavedSearches/createSavedSearch/updateSavedSearch/deleteSavedSearch` api.ts:1699-1720 | /saved-searches (saved_searches.py) | Partly | create/patch 422 for any key ∉ `/search` signature (`view`) (A4); 409 on duplicate name shown raw; `deleteSavedSearch` throws plain `Error` |
| `getItem(id)` api.ts:204 | GET /items/{id} (items.py:198) | Yes | `library_share_prefix`, `library_share_unc`, `native_path`, `share_url/source`, `library_name` all sent; `status`/`deleted_at` sent but never shown |
| `patchItem(id, patch)` api.ts:149 | PATCH /items/{id} (items.py:637) | Yes | explicit null clears; schema has no year range, UI clamps 1..9999; tags `null` would NULL the column (backend, see questions) |
| `batchPatchItems(map)` api.ts:186 | POST /items/batch (items.py:696) | Yes | 500 cap honoured by `chunk`; per-item `"ok"/"error: …"/{error,detail}` parsed by `summarizeResults`; single transaction, request-level failure loses chunk progress info (A26) |
| `copyCounts(ids)` api.ts:~1990 | POST /items/copy-counts (items.py:965) | Yes | cap 200 respected; ids not scope-checked server-side (counts are) |
| `itemCopies(id)` api.ts:1984 | GET /items/{id}/copies | Yes | `count` excludes self if self not active (off-by-one for tombstoned items) |
| `similarItems(id,10)` api.ts:386 | GET /items/{id}/similar | Yes | 409/422/404 all collapse to "unavailable" text |
| `getItemPermissions(id)` api.ts:239 | GET /items/{id}/permissions | Yes | read-only; failures hide section |
| `touchItem(id)` api.ts:~252 | POST /items/{id}/touch | Yes | 204; fire-and-forget |
| `itemAgentStatus(id)` api.ts:285 | GET /items/{id}/agent-status (item_agent_status.py) | Yes | fields match; online flag computed server-side |
| `verifyItem(id,mode)` | POST /items/{id}/verify | Yes (not deep-checked) | |
| `initiateTransfer / mintTransferEventsToken / transferEventsUrl / cancelTransfer / downloadTransfer` api.ts:318-377 | transfers.py:166,353,400,471,533 | Yes | 409 existing-id parsing OK; requires `download` + write scope (UI shows button to all); download buffered in memory (A29) |
| `thumbUrl(id,tier)` api.ts:71 | GET /items/{id}/thumb (items.py:562) | Yes | key in query string (A23); 404 for queued video thumbs not retried (A22) |
| `libraryTree(id,path,limit,offset,foldersOffset)` api.ts:1676 | GET /libraries/{id}/tree (libraries.py:469) | Yes (types) | `TreeItem` has no status/mtime; 422 for `x:` folder names (A19); includes `missing` items (A20) |
| `targetedScan(id,{path,recursive})` api.ts:924 | POST /libraries/{id}/scan/targeted (libraries.py:872) | Yes | write scope; 422 for agent-owned library, 404 if not on disk (A21) |
| `listLibraries()` api.ts:724 | GET /libraries | Yes | `share_prefix_effective`, `share_unc_effective`, `agent_name`, `expose_gps`, `source_agent_id` present |
| `getTaxonomy()` api.ts:651 / `fileGroups()` api.ts:589 | GET /taxonomy, GET /system/file-groups | Yes | cards registry not aligned to the category keys returned (A13) |
| `semanticStats()` api.ts:398 | GET /stats (system.py:238) | Yes but heavy | full dashboard payload (≤10 s budget) used for one boolean (A8) |
| `listCustomFields()` api.ts:866 | GET /custom-fields | Yes | `library_ids` UUID strings; scope read |
| `getMetadataProfile(cat)` api.ts:1744 | GET /metadata-profiles/{file_category} | Yes | keys are the new category keys (incl. `three-d-cad`) |
| Map: `search()` with `geo_*` + `limit=200` | GET /search | Yes | `le=200` honoured; whole-world box when none drawn; invalid box dropped client-side |
| `fs`, `query`, `agent_thumbs`, `custom_reports` backends | not called by in-scope components | n/a | fs.py (`/fs/browse`, admin) is FolderPicker; query.py is the Reports builder; agent_thumbs.py is agent-only |

## Missing test coverage (DOM-free candidates)
Existing node tests: bulkEdit, mapGeo, osFormat, routes, polling, sort, treemap, etc.
No coverage for:
1. `searchparams.ts` (encode/parse round-trip, repeated keys, `isSearchHash`, `#/search?`), plus the SP hash reducer (`lastWrittenHash`, A5/A6) once extracted.
2. `buildParams/currentParams/applyParams` (SP:497-625): extract to `searchState.ts` (pure): range/pendingRange merging (A7), `view` stripping for saves (A4), hashMode, NaN semantic, unknown enum values, comma-in-value cases.
3. `api.search()` repeatable expansion and `REPEATABLE_SEARCH_PARAMS` (needs a `localStorage`/`fetch` stub); `request()` error paths; a `describeError` helper (A10).
4. `pathlinks.ts` (`buildOpenUrl`, `buildDisplayPath`, `classifyPrefix`): unicode, `#`, `%`, `?`, spaces, UNC ↔ `file://`, IPv6-literal hosts, trailing slashes, empty rel.
5. `clipboard.copyText` with stubbed `document/navigator` (secure vs insecure, execCommand false/throws) — and a lint/test that callers check the boolean (A11).
6. `cards/format.ts` & `cards/keyfacts.ts`: `asStr` with objects/arrays, `fmtDuration` edges (0.4 s, 59.6 s, ≥24 h), `fmtBytes`, `orderKeyFacts` precedence/`_` keys/exclude; registry↔category-key consistency (A13) and highlights↔CURATED consistency (A14).
7. Result de-dupe/merge for pagination (A12) and the Browse folder-page assembly (A18) as pure functions.
8. Bulk apply orchestration with injected API (partial chunk failure, stale-tag re-resolve) (A3, A26).
9. Keyboard-shortcut predicate for the results list (A1).
10. Map: `parseDraft` (A28), `clusterPoints`/`boxParams` are already covered by mapGeo tests; add "marker pointerdown must not start a drag" as a pure `shouldStartDrag(targetKind)`.
11. `routes.ts`: `#/browse/`, malformed `%` paths, `#/search?x` fallbacks (A38).
12. `osFormat.detectPlatform("Darwin")` (A39).
13. Backend: `/search` honours (or rejects) DSL strings (A2); saved-search accepts every key SP can emit (A4); browse path `x:` folder (A19).

## Questions I could not resolve from code alone
1. Is the Search-page DSL (A2) intended to be served by a not-yet-merged translation layer, or is the help block simply mis-scoped? I found no code path that parses `q` for `/search`.
2. Does Meilisearch's default `pagination.maxTotalHits = 1000` apply here? `ensure_index` (search.py:126-162) never sets `pagination`; with offset/limit that default can cap both `estimatedTotalHits` and the reachable offset (`offset+limit ≤ 1000`), which would make "N results" and infinite scroll stop at 1000 on large catalogs (SP:226-230 mentions 1.09 M results, suggesting totals above the cap were seen — needs a live check against the pinned Meili version).
3. Does Chromium retarget `click` to the pointer-captured `<svg>` for marker clicks (A27), and does `preventDefault()` on `keydown` Space cancel button activation (A1)? Both need a browser run.
4. Is `mutagen`'s `info.bitrate` (bit/s) vs `tinytag`'s (kbit/s) mismatch (A32) intentional given `profiles.py:117` documents audio bitrate as kbps while `profiles.py:148` documents video as bit/s?
5. `ItemPatch.tags: list[str] | None` — an explicit `tags: null` is applied via `setattr(item,"tags",None)` (items.py:652-656, 727-729); is the column nullable? (UI never sends it, API clients might.)
6. How is a 401 meant to surface for an expired session (App's `probeAuth` runs on login/logout only)? If a periodic probe exists elsewhere, A10's "no redirect" may be softer than stated.
7. Should Browse show `missing` (tombstoned) items (A20) — the backend comment says only "trashed tombstones are excluded".
8. `copy_counts` does not authorize the requested ids (only scopes the counts); acceptable by design?
