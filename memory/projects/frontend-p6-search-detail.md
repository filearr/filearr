# Frontend P6 — Search and detail correctness (implementation plan)

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development. Steps use checkbox (`- [ ]`) syntax.

**Date:** 2026-11-01 · **Spec:** `memory/projects/frontend-review-2.md` §"P6" (the A-series) + the **P6 checklist** in its P5 status line · **Findings detail:** `memory/projects/review-2/A-search-browse-detail.md` (A2–A45; A1, A4, A10, A11, A18, A23, A28, A30, A33, A41 were fixed in P3/P5).
**Branch:** `claude/frontend-p6-search-detail` (off `main` at f0a7085, after PR 32).
**Goal:** the Search page, item detail, Browse, map and Retrieve behave correctly under real use: the query help matches what the server does, Back/Forward and deep links restore exactly what they show, paging never duplicates or mixes queries, bulk and single edits never lose data, every row and the detail pane show the basic file facts, thumbnails and downloads do not fail silently, and one tested `format.ts` renders every byte, date and duration.

**Architecture:** pure modules carry the logic (`format.ts`, `searchDsl.ts`, `searchHash.ts`, `searchState.ts`, `searchResults.ts`, `bulkOrchestrate.ts`, `cards/highlights.ts`, `thumbPolicy.ts`, `mapInteraction.ts`) with Node tests; the Svelte pages become thin callers. Frontend only: every backend gap becomes a ticket line in the ledger, and the UI is written forward-compatible where a field is missing (Browse `status`).

**Rulings (orchestrator):**
1. **DSL is translated client-side** in `dslToParams()` to the flat `/search` params the backend has: `kind:`→`file_category`, `ext:`→`extension` (single; a second value is reported as unsupported), `size:`→`size_gte/size_lte`, `modified:`→`mtime_gte/mtime_lte`, `tag:`→`tags`; `-word` and `"phrase"` stay in `q` (Meilisearch handles them). `meta.*`, `cf.*`, `path:`, `created:`, `-tag:`, `~fuzzy` and `A..B` on dates are **not** translatable → the Search-context help drops those sections and a typed one shows an inline "not supported in search" note; Reports keep the full help. (Backend ticket: server-side DSL.)
2. **History:** typing and slider drags use `history.replaceState`; chip toggles, applying a saved search, Enter in the query box, and view/sort changes push. One pure reducer owns the own-write token.
3. **Comma values:** the wire and saved-search formats stay comma-joined (backend contract: `tags` is split server-side), so a tag or principal containing a comma cannot be filtered today → backend ticket for repeatable `tags`/`principal`. The hash parser collects repeated keys (`a=1&a=2` → `"1,2"`) so API-style links work.
4. **Semantic probe:** no cheap flag endpoint exists; one module-level cached `/stats` probe (`semanticProbe.ts`) serves SearchPage and ItemDetail (backend ticket: `GET /system/features`).
5. **Cards** are keyed by backend category (`image, audio, video, document, three-d-cad, development, archive, system, other`); `SpreadsheetCard` is chosen for `document` + `file_group === "spreadsheet"`; `audiobook`/`model3d`/`spreadsheet` registry keys go.
6. **Thumb policy mirrors `backend/filearr/tasks/thumbs.py:is_thumbnailable`:** request a thumbnail only for `image`, `video`, `audio`, `three-d-cad`, and `document` when the filename ends in `.pdf`; otherwise render the placeholder with no request. One retry after 2 s on error (the backend returns 404 while a video poster is queued), then the placeholder; the placeholder has the same box so nothing shifts.
7. **Browse status badge needs `TreeItem.status`/`mtime` from the backend** → ticket; the frontend renders the badge and the date when the fields are present and nothing otherwise.
8. **Retrieve download streams through the browser:** a `HEAD` request on the download URL (cookie auth) decides between an anchor navigation (`<a href download>`, streamed by the browser, no memory) and an inline error; the blob path is removed.
9. **Share-path consistency (A24/A25)** is not in the P6 spec list and stays deferred (recorded for P9 with the path-format work). A35 grid-row arrow movement and A37 slider scales are P9 layout items.
10. The whole-branch review runs in two halves (search/state modules; detail/browse/map/retrieve/format).

## Global constraints
- Svelte 5 runes, Tailwind 4, bits-ui 2.18, virtua. No new npm dependencies. **Never add `svelte-ignore`.** Tokens only; kit components for every control/state (P5 rules stand: `Field`, `Alert` beside the action, `StateBlock` for load/empty states, `Badge`/`toneForStatus`).
- Every task leaves all four green from `frontend/`: `npm run check` (0/0) · `npm run check:tests` · `npm test` · `npm run build`; `grep -c "bits-ui\|AlertDialog" dist/assets/index-*.js` → 0; `ls dist/assets | grep -c KitPage` → 0.
- Pure logic in DOM-free `.ts` modules with Node tests written **first** (RED → GREEN evidence in the report). No `{@html}` anywhere: highlight markers are parsed into segments.
- Behaviour-preserving outside the findings: same API calls, same copy unless a finding names the change; P3 typing (`numOrNull`/`intOrNull`), the P3 keyboard gate (`searchKeys.ts`) and the P5 kit usage stay.
- Each task: one commit, `frontend:` prefix, new commit (never amend) once a docs commit sits above. Per-task verification block (run from `frontend/`; paste the numbers): the four commands, the two dist greps, and `grep -rn "fmtBytes\|fmtSize\|formatBytes\|relTime\|fmtDuration\|fmtDate" src --include=*.svelte --include=*.ts | grep -v "lib/format.ts\|tests/" | grep "function "` → 0 after Task 1. Browser smoke on the live stack (`/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/review2/live/start.sh`, admin / ReviewPass123!) of every flow the task touches.

## Review focus
1. Back/Forward across a typing session, a chip toggle and a saved search must land on exactly the shown state each time, including Forward to the last own write. (Task 3 reducer tests; Task 8 flow.)
2. A bulk tag add on an item whose tags were edited moments ago in the detail pane must keep the new tags. (Task 5 orchestration test with a stale hit.)
3. Paging while the user types or drags a slider must never mix queries or duplicate rows. (Task 4 tests; Task 8 flow.)
4. A `size:>1G` or `modified:>7d` chip must narrow results; `meta.height:>=1080` must not silently return everything. (Task 2 tests; Task 8 flow.)
5. A video whose poster is still queued must show the placeholder, retry once, and never shift the grid. (Task 6; Task 8 flow with a mocked 404-then-200.)

---

## Task 1: One `format.ts`

**Files:** `src/lib/format.ts` (new), `tests/format.node.test.ts` (new); replace the local copies in `SearchPage.svelte` (`fmtBytes`, `fmtDate`), `LibrariesPage.svelte` (`fmtBytes`, `relTime`), `FilterBuilderPage.svelte`, `AgentStatusPanel.svelte` (`relTime`), `CustomReportsPage.svelte`, `ReportsPage.svelte`, `ExportsPanel.svelte`, `KitPage.svelte` (`formatBytes`), `RawView.svelte` (`fmtSize`), `BrowsePage.svelte` (`fmtSize`), `AgentsPage.svelte` (`relTime`), `JobsPage.svelte` (`relTime`, `fmtBytes`, `fmtDuration`), `ItemDetail.svelte` (`simSize`/`simWhen`), `about.ts` (`formatBytes` → re-export), `treemap.ts` (`fmtBytes` → import), `cards/format.ts` (`fmtBytes`/`fmtDuration` → re-export from `../format`, keep `asStr`/`asRows`; `asStr` of an object/array → `JSON.stringify`).

**Interfaces produced:**
- `fmtBytes(n: number | null | undefined, opts?: { zero?: string }): string` — base 1024, units `B KB MB GB TB PB`, one decimal above B (`1.0 KB`? no: `1 KB` for exact, else one decimal: `1.5 KB`), `0` → `opts.zero ?? "0 B"`, non-finite/null/negative → `"—"`.
- `fmtDate(v: number | string | Date | null | undefined): string` — local date `YYYY-MM-DD`; numbers are epoch seconds when `< 1e12`, else ms; ISO strings parsed; EXIF `YYYY:MM:DD HH:MM:SS` accepted; invalid → `"—"`.
- `fmtDateTime(v): string` — local `YYYY-MM-DD HH:mm`; `fmtUtc(v): string` — `YYYY-MM-DDTHH:mm:ssZ` for `title` tooltips.
- `fmtDuration(sec: number | null | undefined): string` — `0s`, `59s`, `1m 05s`→`1m 5s`, `1h 02m`→`1h 2m`, `25h 0m` (no days), negative/NaN → `"—"`.
- `relTime(v, now = Date.now()): string` — `just now` (<45 s), `N min ago`, `N h ago`, `yesterday`, `N days ago`, then `fmtDate`; future → `in N min` etc.

- [ ] **Step 1: Tests** (table-driven) for each function including: bytes 0/1023/1024/1536/2^40/NaN/null/−1; dates epoch-seconds vs ms, ISO with offset, EXIF, garbage; duration 0/0.4/59.6/3600/90000; relTime boundaries with a fixed `now`. Run → fail (module missing).
- [ ] **Step 2: Implement `format.ts`**; tests green.
- [ ] **Step 3: Replace every local copy** listed above with imports; delete the local functions; the grep in the verification block is 0. Keep each call site's output identical except where the copies disagreed (document the two or three changes in the report: e.g. ItemDetail "GiB" → "GB", SearchPage UTC date → local).
- [ ] **Step 4:** Verification block; browser spot-check Search row/slider labels, Libraries sizes, Jobs durations.
- [ ] **Step 5: Commit** `frontend: one format.ts for bytes, dates, durations and relative times`.

---

## Task 2: Query DSL translated to search params

**Files:** `src/lib/searchDsl.ts` (new), `tests/searchDsl.node.test.ts` (new), `src/lib/dslHelp.ts` (context-aware sections), `src/lib/DslHelp.svelte`, `src/lib/SearchPage.svelte` (`buildParams` merge + unsupported-term notice).

**Interfaces produced:** `dslToParams(q: string, now = Date.now()): { q: string; params: Record<string, string>; unsupported: string[] }` — `params` keys ⊂ `{file_category, extension, size_gte, size_lte, mtime_gte, mtime_lte, tags}`; `q` is the input with translated and unsupported tokens removed (whitespace collapsed); `unsupported` lists the raw tokens that could not be translated (for the notice). Also `SEARCH_DSL_SECTIONS` filter: `dslSectionsFor(context: "search" | "reports")`.

- [ ] **Step 1: Tests:** `kind:video` → `{file_category:"video"}`; `kind:video kind:audio` → `"video,audio"`; `ext:pdf` → `extension:"pdf"`; `ext:mp4;mkv` → `extension:"mp4"` + unsupported `["ext:mp4;mkv"]`? No — ruling 1: a second value is unsupported: `extension:"mp4"`, `unsupported:["ext:mkv"]`; `size:>1G` → `size_gte:"1073741825"`? Use inclusive integers: `>1G` → `size_gte = 1G+1`, `>=1G` → `1G`, `<500K` → `size_lte = 500K−1`, `=10M` → both, `100M..4G` → both bounds; units K/M/G/T (1024) and plain bytes; `modified:>7d` (changed in the last 7 days) → `mtime_gte = now−7d` (seconds, floored), `modified:<30d` → `mtime_lte = now−30d`, `modified:>=2025-01-01` → `mtime_gte` of that local midnight, `modified:2024-01-01..2024-12-31` → both; `tag:archived tag:x` → `tags:"archived,x"`; `-word "a phrase" kind:video report` → `q:'-word "a phrase" report'`; `meta.height:>=1080`, `cf.rating:>=4`, `path:*/x/*`, `created:>7d`, `-tag:draft`, `~fuzzy` → in `unsupported`, removed from `q`; a token with a colon inside quotes stays in `q`; `size:abc` → unsupported; empty/whitespace → `{q:"", params:{}, unsupported:[]}`.
- [ ] **Step 2: Implement** (tokeniser that respects double quotes; one regex per operator; all ints as strings). Tests green.
- [ ] **Step 3: `dslHelp.ts`:** sections gain `contexts: ("search"|"reports")[]`; `dslSectionsFor("search")` returns only `kind`, `ext` (single value), `size`, `modified` (comparators and dates, no `created`), `tag` (no negation), `-word`, `"phrase"`; `DslHelp.svelte` uses it. Examples in the search context must all translate (test: every example of a search-context section yields `unsupported.length === 0`).
- [ ] **Step 4: `SearchPage.buildParams`:** run `dslToParams(q)`; `p.q = result.q`; merge `result.params` with the chip state: list params union (`file_category`, `tags`), scalar `extension` from the DSL wins over the chip; range params from the DSL win over sliders/pendingRange. Show `unsupported` as a dismissable `Alert tone="warn"` under the query box: "Not supported in search: `meta.height:>=1080` — see the query help." `activeFilters` gets chips for DSL-derived filters labelled `from query: kind video` with no clear button (the clear is editing the query).
- [ ] **Step 5:** Verification block; browser: `kind:video size:>1M` narrows; `meta.x:1` shows the notice and returns the unfiltered query; the help's Search tab shows only translatable sections.
- [ ] **Step 6: Commit** `frontend: translate the search DSL to flat params; help matches what search does`.

---

## Task 3: Search hash and state

**Files:** `src/lib/searchHash.ts` (new), `src/lib/searchState.ts` (new), `tests/searchHash.node.test.ts`, `tests/searchState.node.test.ts`, `src/lib/searchparams.ts` (+ `tests/searchparams.node.test.ts`), `src/lib/SearchPage.svelte`.

**Interfaces produced:**
- `searchHash.ts`: `createHashSync()` → `{ reflect(current: string, encoded: string, mode: "push" | "replace"): { write: "push" | "replace" | "none" }; incoming(hash: string): "apply" | "ignore" }` — pure state machine over the own-write token: `reflect` records the hash it is about to write; `incoming` returns `ignore` once for that exact hash then clears the token (so Forward to the same hash later applies); any other hash applies and clears the token.
- `searchState.ts`: `applyParamsPure(p: Record<string,string>, bounds): { state: Partial<SearchState>; searchable: boolean; invalid: string[] }` — validates enums (`search_in` ∈ all|content|names, `sort` ∈ the page's option keys, `view` ∈ list|grid|map, `exposure`, `world_readable`), `semantic` numeric in [0,1] else dropped, geo box all-or-nothing and not inverted, lists split on commas; `invalid` names dropped keys. `pendingRangeChips(pending): Chip[]` — one chip per pair (`size`, `date`) with labels via `format.ts`. `UI_ONLY_PARAMS`.
- `searchparams.ts`: `parseSearchHash` joins repeated keys with commas.

- [ ] **Step 1: Tests** — hash reducer replay A→B→Back(A)→Forward(B): every step `apply`; own write then matching hashchange → `ignore`, a second identical hashchange → `apply`; `parseSearchHash("#/search?file_category=a&file_category=b")` → `"a,b"`; `applyParamsPure` enum/NaN/geo cases; `searchable` false for display-only params; `pendingRangeChips`.
- [ ] **Step 2: Implement** the three modules; tests green.
- [ ] **Step 3: SearchPage:** `reflectHash(mode)` — `"replace"` from the debounced `onInput` and slider `oninput`; `"push"` everywhere else (chip toggles, `applySaved`, Enter in the query box, `setView`, sort change). Use `history.replaceState(null, "", h)` for replace, `location.hash = h` for push. `onHashChange` → `sync.incoming(location.hash)`. `applyParams({})` and any non-searchable apply reset `hits/total/facets/nextCursor/searched/selection` to the blank slate (A6). Pending-range chips render from `pendingRange` with per-pair clear; `onRangeChange` clears only the moved pair; `applyStats` keeps the original bounds while that pair is pending (A7). `applyParams` reports `invalid` keys as one `toast(…, {tone:"warn"})` "Ignored unknown search options: sort" (A36). `onInput` skips while `e.isComposing` (A43). The query `Input` becomes the combobox for the results: `role="combobox" aria-controls="search-results" aria-expanded={hits.length>0} aria-activedescendant` mirrors the grid's; ArrowDown/ArrowUp from the query box move the results cursor (`searchKeys.ts`: when `target.tag === "INPUT"` and the input has `data-results-combobox`, arrows return `move:±1`; add tests). Total renders as `about N results` when `total >= 1000` (A45).
- [ ] **Step 4:** Verification block; browser: type three letters (one history entry), toggle a chip (push), Back/Forward twice, deep link with `size_gte` shows a chip, removing it keeps the date chip; combobox arrows.
- [ ] **Step 5: Commit** `frontend: search history reducer, validated params, pending-range chips, combobox arrows`.

---

## Task 4: Fetching: mount order, paging integrity, probe cache

**Files:** `src/lib/searchResults.ts` (new), `tests/searchResults.node.test.ts`, `src/lib/semanticProbe.ts` (new), `src/lib/SearchPage.svelte`, `src/lib/ItemDetail.svelte` (use the shared probe), `src/lib/api.ts` (`searchTags` sends `file_category`/`library`), `src/lib/configCache.ts` (new: memoised `listCustomFields`/`getMetadataProfile` for one page load, A44), `src/lib/cards/KeyFactsCard.svelte`, `src/lib/BulkEditBar.svelte` (use the cache).

**Interfaces produced:** `mergeHits<T>(existing: T[], incoming: T[], idOf: (t: T) => string): { hits: T[]; dropped: number }` (de-dupe by id, keeps order); `semanticEnabled(): Promise<boolean>` cached per page load; `cachedCustomFields(signal?)`, `cachedProfile(category, signal?)`.

- [ ] **Step 1: Tests** for `mergeHits` (no dupes, overlapping page, empty incoming) and a `FetchSession` helper if extracted (params snapshot: `loadMore` uses the params of the last `runFresh`, never live state).
- [ ] **Step 2: SearchPage:** `onMount` registers the listener, then `applyParams(initial)` **synchronously** (if the hash has params and the user has not typed yet — track `userTyped` set by `onInput`), then `Promise.all([listLibraries, getTaxonomy→groups, semanticEnabled()])` for vocabularies (A8). `runFresh` snapshots `lastParams = buildParams()`; `loadMore` sends `{...lastParams, cursor}`; appends via `mergeHits`; a `loadMore` failure keeps the pages shown and renders a compact `StateBlock state="error" onRetry` under the list, and scroll-triggered `loadMore` is suppressed until Retry (A12). `refreshCopyCounts` and `loadMapPoints` failures surface: map → `Alert` in the map panel "Could not load map points" with Retry (not an empty map).
- [ ] **Step 3:** `searchTags(q, { file_category, library })` matches the backend (`/search/tags` reads `file_category` list + `library`); SearchPage passes the selected categories and libraries (A31). `semanticProbe.ts` replaces both local probes. `configCache.ts` replaces the three repeated GETs per detail open (A44).
- [ ] **Step 4:** Verification block; browser: deep link renders results before the taxonomy chips fill; scroll paging while typing never shows a row twice (check `hitId` uniqueness in devtools); block `/search` for page 2 → error block + Retry.
- [ ] **Step 5: Commit** `frontend: search fetching — deep link first, paging snapshot and de-dupe, shared probes`.

---

## Task 5: Bulk tags and single edits never lose data

**Files:** `src/lib/bulkOrchestrate.ts` (new), `tests/bulkOrchestrate.node.test.ts`, `src/lib/SearchPage.svelte` (`resolveTargets`/`applyBulk` → the orchestrator; `onChanged` row patch), `src/lib/BulkEditBar.svelte`, `src/lib/ItemDetail.svelte` (`onChanged` prop), `src/lib/BrowsePage.svelte` (`onChanged` row patch).

**Interfaces produced:**
- `runBulk(selected: BulkTarget[], ops: BulkOps, api: { getItem(id, signal?): Promise<{tags?: unknown}>; batchPatch(body): Promise<BatchPatchResponse> }, opts: { signal?: AbortSignal; onChunk?: (done: number, total: number) => void }): Promise<BulkOutcome>` — when `ops` touch tags, **every** target's current tags come from `getItem` (never from the hit), 20 at a time; patches via `buildPatches`; chunks of 500; a request-level failure on chunk k stops and returns `{ summary (chunks 1..k−1 merged), failed: { chunkIndex, error }, patchesApplied }` so the caller can `applyLocally` what did commit and report "N updated before the failure; M not attempted".
- `ItemDetail` prop `onChanged?: (item: ItemRecord) => void`, called after a successful `saveEdit` with the PATCH response.

- [ ] **Step 1: Tests** with an injected api: hit says `[a]`, `getItem` says `[a,b]`, op add `c` → patch `[a,b,c]`; a `getItem` failure → listed under `unresolved`, not patched; chunk 2 of 3 throws → `summary.ok` covers chunk 1, `failed.chunkIndex === 1`, chunk 3 not attempted; a run token (`signal` aborted between chunks) → stops without applying.
- [ ] **Step 2: Implement**; SearchPage `applyBulk` uses it with an `AbortController` aborted by `runFresh` and by selection changes (the P3 `latest()` pattern); `applyLocally` runs for committed chunks on failure too; the summary copy names both numbers (A26).
- [ ] **Step 3: BulkEditBar:** Apply is enabled when a tag draft is non-empty and folds it in on click (one click); `fieldName` change resets `fieldRaw`/`fieldMode`; the 422 `detail` array renders through `errorMessage` (already pydantic-aware) one line per field.
- [ ] **Step 4: `onChanged`:** ItemDetail calls it after save; SearchPage patches the row (`title`, `year`, `tags`) and BrowsePage patches its `tree.items` entry; the gallery is unaffected.
- [ ] **Step 5:** Verification block; browser: edit tags on an item in the detail pane, close, select it, bulk-add a tag → both tags survive; block chunk 2 (mock) on a 600-item mock selection → copy shows 500 updated / 100 not attempted.
- [ ] **Step 6: Commit** `frontend: bulk edits resolve live tags and report partial progress; detail saves update the rows`.

---

## Task 6: Detail facts, cards, thumbnails

**Files:** `src/lib/ItemDetail.svelte`, `src/lib/SearchPage.svelte` (row size/date + `highlight`), `src/lib/cards/registry.ts`, `src/lib/cards/highlights.ts` (new), `tests/cards.node.test.ts` (new), `src/lib/cards/*Card.svelte`, `src/lib/cards/KeyFactsCard.svelte`, `src/lib/cards/keyfacts.ts`, `src/lib/Thumb.svelte`, `src/lib/thumbPolicy.ts` (new), `tests/thumbPolicy.node.test.ts`, Thumb callers (SearchPage ×2, BrowsePage ×2, ItemDetail ×2).

**Interfaces produced:**
- `cards/highlights.ts`: `export const HIGHLIGHT_KEYS: Record<"video"|"audio"|"image"|"document"|"spreadsheet"|"three-d-cad", readonly string[]>` — the keys each card renders; each card imports its list and uses it both to render and as `exclude`. `BACKEND_CATEGORIES = ["image","audio","video","document","three-d-cad","development","archive","system","other"] as const`. `cardFor(category, fileGroup?)`, `cardLabel(category, fileGroup?)`.
- `thumbPolicy.ts`: `thumbable(category: string | null | undefined, filename?: string | null): boolean` (ruling 6); `Thumb` props add `category?: string | null`, `filename?: string | null`.
- `parseHighlight(s: string): Seg[]` reusing `parseSnippet`'s marker parsing (move it to `searchResults.ts` as `parseMarked(s, pre, post)` with tests).

- [ ] **Step 1: Tests:** every `CARD_REGISTRY` key ∈ `BACKEND_CATEGORIES`; `cardFor("three-d-cad")` is Model3dCard, `cardFor("document","spreadsheet")` is SpreadsheetCard, `cardFor("archive")` is KeyFactsCard; for each card `HIGHLIGHT_KEYS` ⊇ the keys the old CURATED lists named that the card renders (list them from the current cards) and includes the previously missing `bitrate, title, year, season, episode, color_primaries, color_transfer` (video), `title, chapter_count` (audio), `producer, creator, subject, keywords, revision, title` (document), `subject, title` (spreadsheet); `orderKeyFacts` keeps `_extract_error` out of leftovers but a new `extractError(meta)` returns it; `thumbable` table (image ✓, video ✓, audio ✓, three-d-cad ✓, document+`a.pdf` ✓, document+`a.docx` ✗, archive ✗, null ✗); `parseMarked` with nested/unbalanced markers.
- [ ] **Step 2: Cards:** registry per ruling 5; each card renders the facts its `HIGHLIGHT_KEYS` name (adding the missing ones as plain rows) and passes the same list as `exclude`; `KeyFactsCard` renders `_extract_error` as `<Alert tone="warn" live={false}>Extraction failed: {reason}</Alert>` above the facts; `asStr` objects → JSON. AudioCard bitrate: values `> 10000` are bit/s → show `Math.round(v/1000) kbps`, else kbps as-is (A32 note), with a code comment naming the two extractors.
- [ ] **Step 3: ItemDetail facts header** under the breadcrumbs: filename (and `title` when it differs), `year`, `fmtBytes(size)`, modified `fmtDateTime(mtime)` with `title={fmtUtc(mtime)}`, tags as `Badge`s, `status` as `Badge tone={toneForStatus(status)}` when not `active`, `file_category`/`file_group` chips. After Save the header reflects the response (it reads `item`, which `saveEdit` replaces).
- [ ] **Step 4: Search rows:** list rows gain right-aligned `fmtBytes(size)` and `fmtDate(mtime)` columns (hidden below `sm`), tiles show size in the caption; filename/title render `highlight.filename`/`highlight.title` through `parseMarked` with `<mark>` when present, else the plain string. The P5 `nested-interactive`-free grid structure stays (new cells are `role="gridcell"`).
- [ ] **Step 5: Thumb:** always render a fixed-box placeholder (`bg-surface-3` with the category glyph/text, same `size`/`rounded`); when `thumbable(category, filename)` render the `<img>` over it; `onerror` → one retry after 2 s (new `src` with `&r=1`) then stay on the placeholder; `decoding="async"`; callers pass `category`/`filename`.
- [ ] **Step 6:** Verification block; browser: detail header on a seeded item, a `.txt` item makes no thumb request (network tab), a mocked 404-then-200 video thumb retries once, highlight marks on a filename match.
- [ ] **Step 7: Commit** `frontend: detail facts header, category-keyed cards, extraction errors shown, thumb policy and retry`.

---

## Task 7: Browse, map and retrieve

**Files:** `src/lib/BrowsePage.svelte`, `src/App.svelte` (pass `me` to the browse route), `src/lib/map/MapPanel.svelte`, `src/lib/mapInteraction.ts` (new) + `tests/mapInteraction.node.test.ts`, `src/lib/RetrievePanel.svelte`, `src/lib/api.ts` (`transferDownloadUrl(id)`, `probeDownload(id): Promise<void>` via `send("HEAD", …, { expect: "none" })`).

**Interfaces produced:** `shouldStartDrag(target: { inMarker: boolean; button: number }): boolean`; `transferDownloadUrl(id: string): string`.

- [ ] **Step 1: Browse (A20/A21/A40):** `me` prop; Scan/Rescan controls hidden when `lib.source_agent_id` is set or `!isAdminPrincipal(me)` (write scope) with a one-line note in the folder menu "Scans run from the Agents page for agent-hosted libraries"; render `Badge tone={toneForStatus(it.status)}` and `fmtDate(it.mtime)` when `status`/`mtime` are present on a tree item (forward-compatible, ruling 7; add the optional fields to the TS type); `it.title || it.filename`; reset `libName/shareUrlRaw/shareUncRaw` before resolving a new library; folder and ⋯ menus close on Escape and outside click; Prev/Next scrolls the list to top.
- [ ] **Step 2: Map (A27):** test `shouldStartDrag` (inMarker → false; button≠0 → false); `onPointerDown` returns early when `e.target.closest("[data-marker]")` (markers get `data-marker`), so marker clicks never clear the area or start a drag; verify in the browser that a marker click opens/expands with an area filter active.
- [ ] **Step 3: Retrieve (A29):** `download()` → `await probeDownload(id)` (HEAD; errors through `errorMessage` into an `Alert` under the action row in **every** phase branch) then an anchor navigation to `transferDownloadUrl(id)` with `download={filename}`; remove the blob path from `downloadTransfer` (keep `saveBlob` for the other callers); the 2 s reconnect timer is stored and cleared on unmount/item change; Cancel during `starting` sets `cancelRequested` so the transfer is cancelled as soon as `initiateTransfer` resolves.
- [ ] **Step 4:** Verification block; browser: Browse as admin vs a mocked non-admin `me` (controls hidden), map marker click with an area, retrieve → download of a mocked ready transfer streams (no blob in memory: check no `blob:` URL is created), HEAD 409 shows the error inline.
- [ ] **Step 5: Commit** `frontend: browse gating and status, map marker presses, streamed retrieve download`.

---

## Task 8: Browser acceptance and docs

**Files:** `memory/projects/review-2/p6-acceptance.md` (new), fixes in the owning files.

- [ ] **Step 1: Flows (Playwright on the live stack, both themes where visual):** (a) DSL: `kind:video size:>1M` narrows (compare totals), `meta.x:1` shows the notice; (b) history: type `mov` (one entry), toggle a chip, apply a saved search, Back ×2, Forward ×2 — the query box, chips and URL agree at every step; (c) deep link with `size_gte` → chip present, results before chips load; (d) paging: scroll three pages while typing a new query → no duplicate ids, rows belong to the latest query; mock page-2 500 → error block + Retry keeps page 1; (e) detail edit → row updates without refetch; bulk add after a detail edit keeps both tags; chunk failure copy; (f) thumbs: `.txt` makes no request; mocked 404→200 retries once; grid heights equal; (g) facts header values match `GET /items/{id}`; `_extract_error` row on a mocked item; (h) Browse: non-admin hides Rescan; map marker click keeps the area; Retrieve HEAD error inline; (i) axe on Search (results, list and tiles, with the new columns) and ItemDetail → 0 new violations vs P5; combobox arrows from the query box.
- [ ] **Step 2:** Fix failures in the owning files; write the report (flow table, console errors, mocks used, axe deltas).
- [ ] **Step 3: Commit** `frontend: P6 browser acceptance`.

---

## Self-review notes
- Spec coverage: DSL (T2), hash reducer + replaceState (T3), pending-range chips (T3), `applyParams(initial)` first + probe (T4), loadMore snapshot + de-dupe (T4), comma-safe (ruling 3 + T3 parser + T4 `searchTags`), ItemDetail token guard (P5) + `onChanged` (T5) + facts header (T6), bulk tags (T5), card registry/CURATED/`_extract_error` (T6), Thumb (T6), Browse stale guard (P3) + status + Rescan gating (T7), map pointerdown (T7), blank-field NaN (P3), Retrieve streaming/error/cancel (T7), `format.ts` (T1). P6 checklist: zero-result copy becomes filter-aware in T2 (mention unsupported terms) — add to T2 Step 4; combobox arrows (T3); tile grid Up/Down and `aria-activedescendant` on virtualised rows stay P9 (ruling 9).
- Types: `dslToParams` returns strings only; `mergeHits` generic; `runBulk` takes the existing `BulkTarget`/`BulkOps`/`BatchPatchResponse` types from `bulkEdit.ts`/`api.ts`; `thumbable` takes `(category, filename)`.


## Amendments and hand-offs (from the SDD run — these supersede the text above where they conflict)

**Rulings amended during execution**
- **Ruling 1 / Task 2:** `A..B` date ranges on `modified:` ARE translated (both bounds); `group:` → `file_group` is translatable; `hash:` stays unsupported (whole-query hash mode exists). The hash and saved searches store the typed query only; DSL-derived params exist on the request path alone. `FilterBuilderPage` uses the reports help context.
- **Ruling 6 / Task 6:** `three-d-cad` thumbnails ARE requested (the serve path renders 3D on demand; `is_thumbnailable` only gates the extract-time ride-along). Retry applies to `video` only (fix wave).
- **Ruling 8 / Task 7:** the HEAD probe is replaced by a one-byte ranged GET (`Range: bytes=0-0`, 200/206 ok) because FastAPI GET routes answer 405 to HEAD; one code path, then the anchor navigation. Task 7 Step 1's `isAdminPrincipal(me)` gate became `canWrite(me)` (`me.scopes.includes("write")`, falling back to the admin role when `scopes` is absent) to match the backend's `require_scope("write")`.
- **Task 3:** the editing-session history rule (fix wave): the first replace after a push or an applied incoming hash is upgraded to a push, so each typing or drag session gets exactly one entry.
- **Task 4:** a bounds probe (same params minus the range keys, `limit=1`) runs when a deep link carries a pending range and no bounds exist; `configCache` is invalidated by `CustomFieldsPanel` on create/update/delete.
- **Task 5:** a cancelled bulk run keeps Apply disabled until its in-flight chunk settles; a superseded run writes no error or summary state.
- **Task 8 extra:** `Thumb.onFail` collapses the detail preview slot.

**Backend tickets raised**
- Server-side DSL for `/search` (`meta.*`, `cf.*`, `path:`, `created:`, `-tag:`), or an official statement that the help is reports-only.
- Repeatable `tags` / `principal` params so values containing commas can be filtered.
- A cheap `GET /system/features` (semantic flag) in place of the `/stats` probe.
- `TreeItem.status` and `TreeItem.mtime` for Browse (the UI already renders them when present).
- HEAD on `/transfers/{id}/download` (removes the one-byte probe's audit line and the stripped-`Range` buffering case).
- `/search/tags` accepting several `library` values.
- Exclude subtitles (`file_group === "subtitle"`) from `is_thumbnailable` and the serve path; stop enqueuing a doomed `thumb_item` job on every miss.
- Taxonomy extractor contract (`"none"` vs `null`) from P5.

**Deferred to later phases**
- P9: A24/A25 share-path consistency (`resolveItemPath`), A35 tile-grid row movement and scroll-into-view, A37 slider scales, `aria-activedescendant` on virtualised rows, Breadcrumb ⋯ menu dismissal (A40), `Highlights.svelte` contrast, a results page shorter than the list never triggering `loadMore`, `errorMessage` "; " split moved into `apiErrors.ts`, the kind/group chip-vs-DSL union reading as OR.
- P10: dead `semanticStats()` export in `api.ts`; `invalidateProfiles()` has no caller until a profile editor exists.

## Execution ledger (from the SDD run)

**Outcome:** 15 commits over main f0a7085; 8 tasks, 5 with one fix round; whole-branch review in two parallel halves → 2 Important code findings + 1 docs finding + 16 minors fixed in one wave (this section is the docs fix), re-reviewed clean. Acceptance: `review-2/p6-acceptance.md` (36/36 flows).

**Rulings made during execution:**
- Ruling: T2 Step 1's `ext:mp4;mkv` expectation is `extension:"mp4"`, `unsupported:["ext:mkv"]` (ruling 1); the crossed-out alternative in the plan text is not the spec — cost if wrong: none.
- Ruling: FilterBuilderPage help context → "reports" (one line, outside the task's list) — it accepts the full syntax — cost if wrong: none. Ruling: date ranges `A..B` on modified ARE translated (ruling 1 text withdrawn on that point; brief test wins). Ruling: hash and saved searches store the typed query, never DSL-derived params; only the request path merges the translation — relative dates stay relative — cost if wrong: a saved search re-translates on apply (intended). Ruling: `group:` → file_group translatable; `hash:` stays unsupported.
- Ruling: slider bounds on a filtered deep link — Task 4 runs one extra bounds request (same params minus range keys, smallest limit the backend allows) when pendingRange is active and no bounds exist, so the sliders show the unfiltered range — cost if wrong: one extra request on range deep links.
- Ruling: CustomFieldsPanel.svelte may receive the one-line `invalidateCustomFields()` calls (outside the task's list) — cost if wrong: none.
- Ruling (Task 8 extra): Thumb gains `onFail?: () => void` fired when the placeholder is final; ItemDetail collapses the 256 px preview slot on it so audio-without-artwork and .srt items show no dead box — cost if wrong: one prop.
- Ruling: probe = GET with `Range: bytes=0-0` (200/206 = ok), one code path, then anchor navigation — cost if wrong: one extra download audit line per click. Backend ticket: add HEAD to /transfers/{id}/download.

**Deferred minors (non-blocking per the final reviewers):**
- Task 1: minor (deferred): fmtDate(0) renders 1970-01-01 (old SearchPage copy returned —; reachable only with zero bounds); no test for the 1e12 s/ms boundary or negative epochs.
- Task 2: minor (deferred): size chip label rounds (`size_lte=1048575` → "≤ 1 MB"); an extension chip and a "from query: ext" chip can both show when the DSL overrides; backticks render literally in the zero-result StateBlock copy; modified range form is prose-only in the help (backend pins 32 chips).
- Task 3: minor (deferred): Enter after the debounce pushes nothing (duplicate entry impossible; acceptable); single-result deep link leaves the size slider "range unavailable" (bounds need an unfiltered stats request → Task 4 note / P9 sliders). Unbriefed calls 1–7 accepted by the reviewer and the controller.
- Task 4: minor (deferred): semantic deep-link re-run calls runFresh (clears selection; rare, untested live); map error Alert replaces MapPanel so a drawn box is clearable only via chips until Retry; semanticProbe.ts has no direct test; `semanticStats()` in api.ts is now a dead export (P10 cleanup); /search/tags accepts one `library` (backend ticket).
- Task 5: minor (deferred): errorMessage "; " split in SearchPage is a stopgap (move to apiErrors.ts later); `bulkErrorLead` left set after dismiss (harmless); full-success clearSelection aborts a finished controller (harmless).
- Task 6: minor (deferred): cards source-level test is weak (regex `m.key` match; no assertion on lead rows / live=false Alert); Thumb A→B→A reuse retries once more (harmless); Highlights.svelte 4.34 contrast pre-existing (P9).
- Task 7: minor (deferred): scan controls flash before the library lookup resolves for agent-hosted libraries; a proxy that strips Range makes the probe buffer the body (disclosed); window Escape handler preventDefaults while a folder menu is open.
- Task 8: review clean (0 Important). minor (deferred): most flows list-view only (disclosed); preview collapse persists per mounted ItemDetail; Thumb $effect tracks onFail (harmless).
