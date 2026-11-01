# P6 search and detail acceptance — flows (a)–(i)

- Repo `/home/user/repo`, branch `claude/frontend-p6-search-detail`. Code under test: Tasks 1–7 (up to 37a1983) plus the Task 8 commit, which carries extra 1 (Thumb `onFail`, detail preview collapse) and the two fixes listed below.
- Stack: Vite dev `http://127.0.0.1:5173` (hash routes), API :8000 with auth on, one seeded library ("Sample Media", 38 items), no agents, no semantic search. Signed in as `admin` (cookie from `POST /auth/login`, shared by every browser context).
- Script and outputs (scratchpad, not in the repo): `/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/p6-accept/` — `accept.mjs` (Playwright/Chromium, `serviceWorkers: 'block'`, one fresh context per flow so route mocks never leak, axe-core 4.10.3 from `frontend/node_modules`), `run4.log` (the run reported here; `run1.log`–`run3.log` are the earlier runs), `results.json`, `console.json`, `mocks.json`, `axe/*.json` (one file per scan, `axe/summary.json`), `b-history.json`, `shots/*.png`.
- Viewport 1440×900 unless stated; (i) also runs at 390×844. Dark theme through the header Theme control (with a dialog open, `html.dark` is toggled directly, as `applyTheme` does).
- Every number below comes from run 4, after all fixes. **Tally: 36 PASS, 0 FAIL, 0 NOT EXERCISED** (rows below; the NOT EXERCISED list at the end is coverage the brief's flows did not ask for, not rows of the tally).

## Flow table

View column: **L** = list view, **T** = tile (grid) view, **—** = not a results view (dialog, Browse, map).

| # | Check | View | Result | Evidence | Mocks |
|---|---|---|---|---|---|
| a1 | `kind:video size:>1M` narrows | L | PASS | `kind:video` → request `file_category=video`, total 1; `kind:video size:>1M` → `size_gte=1048577&file_category=video`, total 0, "No matches…" copy; control `size:>1M` → `size_gte=1048577`, total 1. Hash stays `#/search?q=kind:video+size:>1M` (DSL only on the request path). | none |
| a2 | `meta.x:1` shows the notice | L | PASS | warn Alert "Not supported in search: `meta.x:1` — see the query help." (screenshots light + dark). | none |
| b1 | typing `mov` makes one entry | L | PASS | `history.length` 3 → 3 (replace), hash `#/search?q=mov`. | none |
| b2 | chip, saved search push; Back ×2, Forward ×2 | L | PASS | chip Video → `file_category=video&q=mov` (hlen 4); saved "Review saved search" `{q:"report"}` → `q=report` (hlen 5); Back → `video&mov`, Back → `mov`, Forward → `video&mov`, Forward → `report`. | none (existing seeded saved search; nothing written) |
| b3 | box, chips, URL agree at every step | L | PASS | all 8 steps: box = `q` of the hash, one `category: Video & subtitle` chip exactly when the hash carries `file_category=video` (`b-history.json`). | none |
| c1 | deep link `size_gte=1000&file_category=image` | L | PASS | rows at 1070 ms, `/taxonomy` answered at 3543 ms; chips `category: image` → `category: Image` once taxonomy lands, `size ≥ 1000 B` throughout; one main request `/search?size_gte=1000&file_category=image`; every row ≥ 1000 B. | `/taxonomy` and `/stats` delayed 3 s (real bodies) |
| d1 | three pages, no duplicate ids | L | PASS | `q=e`: cursor requests at offsets 10 and 20; "Select all loaded (30)" despite each cursor page repeating page 1's last hit; no DOM snapshot with a repeated `row-<id>`. | results `/search` without `limit` → `limit=10`; every cursor page gets page 1's last hit prepended |
| d2 | typing a new query while scrolling | L | PASS | Ctrl+A, `f`, `i` with a scroll-to-end after each key: final rows = the 13 hits of `fi` from the API, "Select all loaded (13)", no foreign or repeated ids. The in-flight `q=e` page-4 cursor request (snapshot of the shown query) was superseded by the fresh `fi` search. | same as d1 |
| d3 | page-2 500 → error block, Retry keeps page 1 | L | PASS | `StateBlock` "Search backend error (mock)" + Retry under the list; 10 rows kept; 4 more scroll-to-end events sent no request (1 × 500 total); after unblocking, Retry loaded page 2, page 1's ids still present, block gone. Screenshots light + dark. | results `/search` → `limit=10`; any `cursor` request → 500 `{"detail":"Search backend error (mock)"}` until unblocked |
| e1 | detail edit → row updates without refetch | L | PASS | title "P6 Accept Title" + tag `p6accdetail` saved in the detail of `image_05.png`; row shows the title after Close; 0 `/search` requests between Save and Close. | none (real write, reverted — e3) |
| e2 | bulk add after a detail edit keeps both tags | L | PASS | row checkbox, "Tags to add" `p6accbulk`, Apply to 1 → server tags `["p6accdetail","p6accbulk"]`; toast "1 updated — search index updates shortly." | none (real write, reverted — e3) |
| e3 | revert | — | PASS | `PATCH /items/<image_05>` `{title:null, tags:[]}` → 200, server title null, tags `[]` (the originals). | — |
| e4 | chunk failure copy | L | PASS | Alert "500 updated before the request failed; 100 not attempted. / chunk 2 blocked by test"; batch sizes `[500,100]`; Apply to 600 still offered (selection kept). | `/search?q=mockbulk` → 600 cloned hits (fake ids, category `other`); `POST /items/batch` #2 → 500 `{"detail":"chunk 2 blocked by test"}`, others all-ok (nothing reaches the backend); `/items/copy-counts` → `{}` |
| f1 | `.txt` makes no request | L | PASS | 18 rows rendered, 5 `.txt` among them, 0 thumb requests for them; requested ids match the policy for every rendered row (0 mismatches). | none |
| f2 | mocked 404 → 200 retries once | L | PASS | `Some Movie (2020).srt`: `?tier=grid` → 404, `?tier=grid&r=1` 2011 ms later → 200, image rendered. | `GET /items/<srt>/thumb*`: first 404 `{"detail":"queued; retry shortly"}`, then 200 1×1 PNG |
| f3 | list row heights equal | L | PASS | all 18 rendered rows 44 px (with/without picture, with/without a snippet line). **Failed in run 1** (44/48 px) — fix 2. | none |
| f4 | tile heights equal | T | PASS | 38 tiles, all 275 × 215 px. | none |
| f5 | extra 1: preview collapse | — | PASS | detail of `01 - Tone 440Hz.wav`: preview box present, `?tier=preview` 404, `&r=1` 404, box gone; same for the `.srt`; `big panorama.jpg` keeps its preview (200). List rows of the three wav files stay 44 px with the "AUD" placeholder. | none (real 404s: the seeded wav/srt have no artwork) |
| g1 | facts header = `GET /items/{id}` | — | PASS | wav, notes.txt, big panorama.jpg, srt: filename, year, title, `fmtBytes(size)`, "Modified `fmtDateTime(mtime)`", category, group, status (when not active), tags all match (formatters imported from `/src/lib/format.ts` in the page). Screenshots light + dark. | none |
| g2 | `_extract_error` row | — | PASS | `file_1.log` detail: "Extraction failed: ffprobe exited 1: invalid data". | `GET /items/<file_1.log>` → real body + `metadata._extract_error`, `_extract_error_kind="error"` |
| h1 | Browse: non-admin hides Rescan | — | PASS | `Documents`: admin Targeted scan 1, Rescan 4, folder menu "Copy path / Scan folder + subfolders / Scan this folder only"; viewer 0 / 0 / "Copy path". | `GET /auth/me` → `global_role="viewer"`, `scopes=["read"]` |
| h2 | map marker click keeps the area | — | PASS | drawn area → chip `area 34.53°N 32.70°W – 51.81°N 25.92°E`; single-marker click opens Item details, 0 new geo requests, dashed rect still drawn while open and after Escape, chip unchanged. | `GET /libraries` → `expose_gps=true`; `/search` with `geo_top_lat` → 4 real hits with synthetic `_geo` (Lisbon, Madrid, 2× Paris) filtered by the box |
| h3 | Retrieve probe error inline | — | PASS | one `GET /transfers/T/download` with `Range: bytes=0-0` → 409; dialog Alert "Transfer has expired"; no download event. (The brief's "HEAD" is the ranged-GET probe since the Task 7 ruling: the backend answers HEAD with 405.) | `GET /items/<data.xml>` + `source_agent_id`; `agent-status` online; `POST transfer` → pending; `events-token`; SSE one `done` (staged, verified); `/transfers/T` → expired; download probe → 409 `{"detail":"Transfer has expired"}` |
| i1–i12 | axe, 0 new violations vs P5 | L, T, — | PASS ×12 | see the axe table. | none |
| i13 | combobox arrows from the query box | L | PASS | `aria-expanded="true"`; `aria-activedescendant` row 1 → ArrowDown row 2 → ArrowDown row 3 → ArrowUp row 2; focus stays on "Search your library"; Enter in the box commits the query (no modal, text kept — the Task 3 design). | none |

## axe — P5 → P6 (same query `image`, same item: first result opened with Enter from the grid)

| Scan | P5 | P6 | Delta |
|---|---|---|---|
| search-results-list desktop light / phone light | clean / clean | clean / clean | 0 |
| search-results-list desktop dark / phone dark | color-contrast 1 / 1 | color-contrast 1 / 1 | 0 (same node: the intro line `.text-slate-500 > span`, 4.23:1) |
| search-results-grid desktop light / phone light | clean / clean | clean / clean | 0 |
| search-results-grid desktop dark / phone dark | color-contrast 1 / 1 | color-contrast 1 / 1 | 0 (same node) |
| dlg-item-detail desktop light / phone light | color-contrast 3 / 3 | color-contrast 3 / 3 | 0 (same nodes: the 10 px chip labels in `cards/Highlights.svelte` — Dimensions, Format, Colour mode — 4.34:1) |
| dlg-item-detail desktop dark / phone dark | color-contrast 4 / 4 | color-contrast 4 / 4 | 0 (same nodes: intro line + the 3 `Highlights` labels, 3.06:1) |

The list rows now carry the two new gridcells (size, date): 6 cells per row, axe clean on them in both themes and both widths. **12 scans, 0 new violations, 0 fixed.** The pre-existing contrast nodes are the P9 items already recorded in P5.

## Fixes made in this task

1. **`Thumb.svelte` — `derived_inert` warnings.** Run 1 logged 4 `[svelte] derived_inert` warnings in flow (b) (intermittent: 2 of 4 isolated re-runs). Stack: `HTMLImageElement.onError` in `Thumb.svelte`. A row's `<img>` delivers its error/load event after its Thumb was destroyed (rows replaced by the next query), and the handler read `base`/`phase`/`src`. The handlers now return when the component is destroyed (`destroyed` flag set in `onDestroy`). Six isolated re-runs of (b) and the full run 4: 0 warnings.
2. **`SearchPage.svelte` — list rows with a snippet were 48 px.** The label line inherited the 16 px font's 24 px line height, so label + snippet (24 + 16) plus padding exceeded `min-h-11`. The label line gets `leading-5`, so every list row is 44 px (f3). The snippet line itself predates P6.
3. **Extra 1** (binding ruling, not an acceptance failure): `Thumb` `onFail` fires once per item when the placeholder is final (retry failed, or not thumbable); `ItemDetail` drops the preview slot for that item (f5). Grid/list rows keep their placeholder.

Run 1's other red rows were script expectations, corrected and re-run: (a2) the notice locator; (b3) the chip label is the taxonomy label ("Video & subtitle"), not the key; (d1) DOM rows are virtualised, so the loaded count is read from "Select all loaded (N)", and the page-size mock is 10 (with 5 per page the list does not overflow, so it never scrolls — see notes); (f2) the srt row is beyond the rendered rows of the all-categories list, so the retry check runs on `q=movie`; (i13) the cursor starts on row 1, and Enter in the box commits rather than opens (Task 3).

## Console

Run 4, all flows: **0 page errors, 0 Svelte warnings.** `console.error` lines are all "Failed to load resource":
- 56 × 404 — `GET /items/<id>/thumb?tier=grid|preview[&r=1]` for the seeded wav/srt files without artwork (the policy requests them; one retry each), plus the first mocked 404 in f2;
- 2 × 500 — the mocked page-2 failure (d3) and the mocked batch chunk (e4);
- 1 × 409 — the mocked Retrieve probe (h3).

## Notes

- **`meta.x:1` alone shows the whole library.** The unsupported token is removed from `q` (Task 2 design), so the request is unfiltered (38 results) and the notice is the only hint. The zero-result copy that names unsupported terms appears only when the rest of the query matches nothing.
- **The detail preview collapses about 2 s after opening** for audio without artwork (the retry delay), so the tabs move up once. Expected from the ruling; noted for P9 layout if it reads as a jump.
- **A results page shorter than the list viewport never asks for the next page** (no scroll event). Not reachable with the backend's page size of 50 (2200 px of rows) on ordinary screens; seen only with a 5-per-page mock. P9 candidate (auto-fill).

## NOT EXERCISED (and why)

- **Tile view for flows (b)–(e), (g), (h):** run in list view only; tiles were exercised in (f4) heights and the four (i) grid scans. Tile-view paging (`onGridScroll`) and tile keyboard movement (P9 per ruling 9) were not driven.
- **Phone width** outside the (i) axe scans.
- **Both themes** are covered by axe for Search and ItemDetail and by screenshots for (a2), (d3), (f) list/tiles and (g1); the other flows ran in light only.
- **A ready transfer downloading end to end** (`<a download>` navigation): Playwright cannot route it; Task 7 showed it in two parts. Here only the probe-error path (h3).
- **Retrieve cancel / reconnect, map cluster click, bulk cancel and 422, write-scope non-admin (editor)**: covered by the Task 5/7 smokes, not repeated.
- **Real queued video posters** (the 404 → 200 case is mocked), **semantic search** (the stack has none), **auth-disabled mode**, **Browse status badge** (the backend sends no `TreeItem.status`/`mtime`, ruling 7).
- **Saved searches with ranges or sort, Enter push, slider drags**: Task 3 smokes; (b) used the one seeded saved search.
