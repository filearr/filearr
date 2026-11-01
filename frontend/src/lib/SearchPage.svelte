<script module lang="ts">
  // Heavy panels shown only on demand are split into their own chunks so the
  // page chunk stays small. Promises are cached at module level so `{#await}`
  // sees the same settled promise on every reopen (no re-fetch, no flash).
  let itemDetailModule: Promise<typeof import("./ItemDetail.svelte")> | undefined;
  function loadItemDetail() {
    return (itemDetailModule ??= import("./ItemDetail.svelte"));
  }
  let mapPanelModule: Promise<typeof import("./map/MapPanel.svelte")> | undefined;
  function loadMapPanel() {
    return (mapPanelModule ??= import("./map/MapPanel.svelte"));
  }
</script>

<script lang="ts">
  import { fmtBytes, fmtDate } from "./format";
  import { COPY_FAILED, copyText } from "./clipboard";
  import { listKeyAction } from "./searchKeys";
  import * as safeStorage from "./safeStorage";
  import { onMount, onDestroy } from "svelte";
  import { VList, type VListHandle } from "virtua/svelte";
  import Thumb from "./Thumb.svelte";
  import DslHelp from "./DslHelp.svelte";
  import BulkEditBar from "./BulkEditBar.svelte";
  import { Alert, Button, Checkbox, Input, Select, StateBlock, confirm, promptText } from "./ui";
  import { toast } from "./ui/toast.svelte";
  import {
    batchPatchItems,
    getItem,
    search,
    listLibraries,
    listSavedSearches,
    createSavedSearch,
    updateSavedSearch,
    deleteSavedSearch,
    copyCounts as apiCopyCounts,
    searchTags,
    searchPrincipals,
    fileGroups as apiFileGroups,
    getTaxonomy,
    type SearchResponse,
    type Library,
    type SavedSearch,
    type TagSuggestion,
    type ItemPatchBody,
    errorMessage,
  } from "./api";
  import {
    buildPatches,
    chunk,
    mergeSummaries,
    rangeIndices,
    summarizeResults,
    type BulkFailure,
    type BulkOps,
    type BulkSummary,
    type BulkTarget,
    type FieldTarget,
  } from "./bulkEdit";
  import { buildDisplayPath } from "./pathlinks";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import { encodeSearchHash, isSearchHash, parseSearchHash } from "./searchparams";
  import { createHashSync } from "./searchHash";
  import {
    SORTS,
    UI_ONLY_PARAMS,
    applyParamsPure,
    clearRangePair,
    pairPending,
    pendingRangeChips,
    type RangePair,
  } from "./searchState";
  import { dslToParams, mergeDslParams, type DslResult } from "./searchDsl";
  import { boundsProbePairs, boundsProbeParams, createPageSession, mergeHits } from "./searchResults";
  import { semanticEnabled as probeSemantic } from "./semanticProbe";
  import {
    MAP_POINT_CAP,
    boxParams,
    formatBox,
    geoOf,
    type GeoBox,
    type MapPoint,
  } from "./map/geo";

  // ---------------------------------------------------------------------- //
  // P3-T3/T4 — virtualized, keyboard-first results with facet chips +       //
  // facetStats-derived range sliders. P3-T7 — Saved panel + deep-linkable   //
  // param hash (#/search?...). FIX-4 — full-width rows, filename priority.   //
  // ---------------------------------------------------------------------- //

  type Hit = Record<string, unknown>;
  // W8: type filtering is the two-level file taxonomy. ``file_category`` (coarse,
  // primary chip row) and ``file_group`` (granular, secondary multi-select) are
  // both MULTI-select + repeatable; the old single-select ``media_type`` is gone.
  // A normalized option shape shared by both rows (key sent, label shown).
  type TaxOption = { key: string; label: string; description: string };
  // Small static fallbacks so the filters still render if the taxonomy/file-group
  // services are offline — the live vocabulary is always preferred (see onMount).
  const CATEGORY_FALLBACK: TaxOption[] = [
    { key: "video", label: "Video", description: "" },
    { key: "audio", label: "Audio", description: "" },
    { key: "image", label: "Image", description: "" },
    { key: "document", label: "Document", description: "" },
    { key: "archive", label: "Archive", description: "" },
  ];
  const FILE_GROUP_FALLBACK: TaxOption[] = [
    { key: "archive", label: "Archive", description: "" },
    { key: "source-code", label: "Source code", description: "" },
    { key: "ebook", label: "E-book", description: "" },
    { key: "raw-photo", label: "Raw photo", description: "" },
    { key: "document", label: "Document", description: "" },
    { key: "subtitle", label: "Subtitle", description: "" },
  ];
  // Top-N extensions surfaced by the type-ahead-lite (P3-T4). The facet list can
  // be long; we rank by live count and show a bounded slice.
  const EXT_LIMIT = 10;
  // A bare lowercase-hex string (16..64 chars) is an exact-hash lookup (P3-T1).
  const HASH_RE = /^[0-9a-f]{16,64}$/;

  let q = $state("");
  // P6 (A2): /search passes q to Meilisearch verbatim, so the translatable DSL
  // operators (kind:/group:/ext:/size:/modified:/tag:) are lifted into flat params by
  // searchDsl.ts. This live reading drives the "from query" chips and the
  // unsupported-term notice; buildParams() re-translates at request time.
  const dsl = $derived(HASH_RE.test(q.trim()) ? dslToParams("") : dslToParams(q));
  // The notice is dismissable per SET of unsupported tokens: typing a different
  // unsupported term brings it back.
  const unsupportedKey = $derived([...new Set(dsl.unsupported)].sort().join("\n"));
  let dismissedUnsupported = $state("");
  // Zero results: name the unsupported terms first — they are the likely cause.
  const noMatchesMessage = $derived.by(() => {
    const u = dsl.unsupported;
    if (!u.length) return "No matches. Try fewer words, remove a filter chip, or set Match in to everything.";
    const one = u.length === 1;
    return `No matches. ${u.map((t) => `\`${t}\``).join(", ")} ${one ? "is" : "are"} not supported in search — remove ${one ? "it" : "them"} or see the query help.`;
  });
  // W8 file taxonomy filters. Both multi-select; each rides the deep-link hash as
  // one comma-joined value and api.search() expands it into a repeatable param.
  // Empty set = "all". Options populated from GET /taxonomy (see onMount).
  let categoryOptions = $state<TaxOption[]>([]);
  let selectedCategories = $state<string[]>([]);
  let fileGroupOptions = $state<TaxOption[]>([]);
  let selectedGroups = $state<string[]>([]);
  // Library filter (multi-select, OR). A library is also how a machine/agent is
  // addressed — every agent root is its own library — so the row groups agent
  // libraries under the agent's name and offers a one-click "whole machine".
  let selectedLibraries = $state<string[]>([]);
  // Permission filter (2026-08-23): principals that can read (OR) + world-readable.
  let selectedPrincipals = $state<string[]>([]);
  let worldReadable = $state<"" | "true" | "false">("");
  // 2026-08-26: effective read tier + share-vs-filesystem disagreement.
  let exposure = $state<"" | "anonymous" | "authenticated" | "restricted">("");
  let shareMismatch = $state(false);
  let principalQuery = $state("");
  let principalSuggestions = $state<{ value: string; count: number }[]>([]);
  let principalOpen = $state(false);
  let principalActiveIndex = $state(-1);
  let principalDebounce: ReturnType<typeof setTimeout>;
  let principalController: AbortController | null = null;
  let extension = $state(""); // "" = any extension
  let extQuery = $state("");  // type-ahead-lite filter text over the ext facet
  let includeSidecars = $state(false); // T3 sidecars hidden by default
  // 2026-08-20: which attributes the query text matches — "content" restricts
  // to indexed file content (body/OCR text, archive members) so a path or
  // filename hit alone stops returning the item; "names" is the inverse.
  let searchIn = $state<"all" | "content" | "names">("all");
  // Roadmap §20: the Filters/Saved open-closed choice persists across visits
  // (same safeStorage pattern as the theme choice) — collapsed by default.
  let filtersOpen = $state(safeStorage.get("searchFiltersOpen") === "1");
  let hashMode = $state(false);
  // Explicit ordering (P3-T7 deep-linkable): round-trips through save/apply + hash.
  let sortMode = $state("");
  // P12 slice 2 — list vs. responsive thumbnail GRID, plus the R8-UI ``map``
  // lens. Rides the search hash (``view=grid`` / ``view=map``) so a deep link /
  // saved search preserves it. Presentational only: never affects the /search
  // query (the backend ignores the extra param).
  let view = $state<"list" | "grid" | "map">("list");

  // R8-UI photo GPS map. The map is a LENS on the current query, so the area a
  // user draws on it is just another filter in the SAME flat param record
  // everything else uses (geo_top_lat/geo_right_lng/geo_bottom_lat/geo_left_lng
  // — see map/geo.ts). That means it composes with the text query and every
  // chip, rides the deep-link hash, and is captured by a saved search for free.
  let geoBox = $state<GeoBox | null>(null);
  let mapPoints = $state<MapPoint[]>([]);
  let geoTotal = $state(0);
  let mapLoading = $state(false);
  // A12/A10: a failed map fetch says so (with Retry) instead of an empty map
  // that reads as "no result carries coordinates".
  let mapError = $state(false);
  let mapController: AbortController | null = null;
  // One page of the map fetch. 200 is the API's own per-request ceiling.
  const MAP_PAGE = 200;
  // The bundled manual is served at /docs/ by this instance; the Vite dev server
  // has no such mount, so dev points at the public site (same rule as HelpPage).
  const DOCS_URL = import.meta.env.DEV ? "https://filearr.com/" : "/docs/";

  // P3-T8 hybrid semantic search. The slider is HIDDEN unless /stats reports the
  // feature enabled server-side; `semantic` is the 0..1 blend ratio (0 = keyword).
  let semanticEnabled = $state(false);
  let semantic = $state(0);

  // P3-T12 tag type-ahead: selected tags become the AND-semantics `tags` param.
  let selectedTags = $state<string[]>([]);
  let tagQuery = $state("");
  let tagSuggestions = $state<TagSuggestion[]>([]);
  let tagActiveIndex = $state(-1);
  let tagOpen = $state(false);
  let tagDebounce: ReturnType<typeof setTimeout>;
  let tagController: AbortController | null = null;

  // P3-T10 duplicate badges: id -> group size (>1 only). Batched after results
  // land in ONE /items/copy-counts call — never a per-row query.
  let copyCountMap = $state<Record<string, number>>({});
  // A failed copy-count batch: a warning with Retry above the list (badges are
  // decoration, so the results stay). Bumped per fresh search so a stale
  // batch's failure never lands on a newer result set.
  let copyCountsError = $state("");
  let copyCountsGen = 0;

  // P3-T7 saved searches (named, persisted param bundles).
  let saved = $state<SavedSearch[]>([]);
  let savedOpen = $state(safeStorage.get("searchSavedOpen") === "1");

  function toggleFiltersOpen() {
    filtersOpen = !filtersOpen;
    safeStorage.set("searchFiltersOpen", filtersOpen ? "1" : "0");
  }
  function toggleSavedOpen() {
    savedOpen = !savedOpen;
    safeStorage.set("searchSavedOpen", savedOpen ? "1" : "0");
  }

  // Roadmap §20: the page starts EMPTY — no match-all preload implying the
  // whole catalog is "a result". Flips true on the first query (typed, chip,
  // deep link, or saved search) and stays true for the session.
  let searched = $state(false);
  // Saved-search action errors, keyed by origin: "save" for the Save button,
  // else the saved search's id (its Rename/Delete row).
  let savedErrors = $state<Record<string, string>>({});
  function setSavedError(key: string, msg: string) {
    const next = { ...savedErrors };
    if (msg) next[key] = msg;
    else delete next[key];
    savedErrors = next;
  }
  // Range params carried through from an applied/deep-linked search until the user
  // next moves THAT pair's slider (the sliders are facetStats-derived, not
  // directly restorable — see applyParams). Shown as their own chips (A7).
  let pendingRange = $state<Record<string, string>>({});
  // The own-write token (A5): tells our own hash writes from Back/Forward.
  const sync = createHashSync();

  let hits = $state<Hit[]>([]);
  let total = $state(0);
  let facets = $state<Record<string, Record<string, number>>>({});
  let nextCursor = $state<string | null>(null);
  let error = $state("");
  let loading = $state(false);
  // A12: the params the last fresh search was sent with; loadMore pages with
  // these (plus the cursor), never the live form.
  const pages = createPageSession();
  // A12: a failed next page keeps the pages already shown and puts a compact
  // error + Retry under the list; scroll/keyboard paging stays off until a
  // Retry succeeds (no re-fire on every scroll event).
  let loadMoreError = $state("");
  let loadMoreBlocked = $state(false);
  // A8: set by the first keystroke in the query box, so a deep link applied on
  // mount never overwrites what the user already typed.
  let userTyped = false;

  // Roving LOGICAL selection (P3-T3): NOT DOM focus — rows unmount under
  // virtualization, so we track an index + drive aria-activedescendant instead.
  let selectedIndex = $state(0);
  let selected = $state<string | null>(null); // open ItemDetail id

  // library_id -> Library, for native_prefix-resolved copy-path (invariant 3).
  let libs = $state<Map<string, Library>>(new Map());

  // R8-UI privacy gate, read off the libraries we already load: when NO library
  // has ``expose_gps`` on, the search index carries no coordinates at all, so a
  // geo query returns nothing by design. The map must say THAT rather than look
  // broken. Undecidable until the libraries land, hence the size check.
  const gpsGated = $derived(
    libs.size > 0 && ![...libs.values()].some((l) => l.expose_gps),
  );

  // Range sliders (P3-T4). Bounds come from facetStats; positions are the live
  // slider values. A filter is "active" only while the slider is narrowed off a
  // bound — that keeps the bounds stable instead of collapsing onto the filter.
  type Bounds = { min: number; max: number };
  let sizeBounds = $state<Bounds | null>(null);
  let sizeLo = $state(0);
  let sizeHi = $state(0);
  let mtimeBounds = $state<Bounds | null>(null);
  let mtimeLo = $state(0);
  let mtimeHi = $state(0);

  const sizeActive = $derived(
    !!sizeBounds && (sizeLo > sizeBounds.min || sizeHi < sizeBounds.max),
  );
  const mtimeActive = $derived(
    !!mtimeBounds && (mtimeLo > mtimeBounds.min || mtimeHi < mtimeBounds.max),
  );

  let vlist = $state<VListHandle | undefined>();
  let searchInput = $state<HTMLInputElement | null>(null);
  let debounce: ReturnType<typeof setTimeout>;
  let controller: AbortController | null = null;

  // ---- accessors (hits are untyped Meili docs) ----------------------------
  const str = (h: Hit, k: string): string =>
    typeof h[k] === "string" ? (h[k] as string) : "";
  const num = (h: Hit, k: string): number | null =>
    typeof h[k] === "number" ? (h[k] as number) : null;
  const hitId = (h: Hit): string => str(h, "id");
  // A row checkbox's accessible name (its label is visually hidden).
  const selectText = (h: Hit): string =>
    `Select ${str(h, "title") || str(h, "filename") || hitId(h)}`;
  // Shared look of the toggle chips (category, library, group, extension,
  // Saved/Filters): on = accent fill, off = a plain outline.
  const CHIP_ON = "border-transparent bg-accent text-accent-fg";
  const CHIP_OFF = "border-border-strong";

  // P3-T5 snippet rendering. The backend returns ``snippet`` (cropped document
  // body) with matches wrapped in <em>…</em>. We NEVER {@html} it (untrusted
  // document content); instead we split on the marker tags and render plain text
  // nodes + <mark> elements, so any stray "<script>"/"<img>" in the body is shown
  // as literal text and can never execute.
  type Seg = { text: string; mark: boolean };
  const SNIPPET_RE = /<em>([\s\S]*?)<\/em>/g;
  function parseSnippet(s: string): Seg[] {
    const segs: Seg[] = [];
    let last = 0;
    let m: RegExpExecArray | null;
    SNIPPET_RE.lastIndex = 0;
    while ((m = SNIPPET_RE.exec(s)) !== null) {
      if (m.index > last) segs.push({ text: s.slice(last, m.index), mark: false });
      segs.push({ text: m[1], mark: true });
      last = m.index + m[0].length;
    }
    if (last < s.length) segs.push({ text: s.slice(last), mark: false });
    return segs;
  }

  // ------------------------------------------------------------------------ //
  // IN-T4 — multi-select + bulk edit.                                         //
  //                                                                           //
  // Selection is a Set of item IDs, NOT indices: rows unmount under            //
  // virtualization and `loadMore()` appends to `hits`, so an index-based        //
  // selection would drift the moment the user scrolls. IDs survive both, which  //
  // is why the same Set drives the list and the grid with no translation.       //
  //                                                                            //
  // Svelte 5 does not deep-proxy a Set, so every mutation REPLACES it —         //
  // reassignment is what publishes the change to the template.                 //
  // ------------------------------------------------------------------------ //
  let selectedIds = $state<Set<string>>(new Set());
  // Anchor for shift-click ranges, in the CURRENT result order.
  let lastToggledIndex = $state(-1);
  let bulkBusy = $state(false);
  let bulkError = $state("");
  // The last batch's per-item outcome. Kept until dismissed or a new query —
  // a failure list that vanishes with the toast would be worthless.
  let bulkSummary = $state<BulkSummary | null>(null);
  let bulkFailuresOpen = $state(false);

  // Only hits still on screen can be selected, so intersecting is safe and keeps
  // the bulk targets in the result's current order.
  const selectedHits = $derived(hits.filter((h) => selectedIds.has(hitId(h))));
  // Category + library per selected item, for the custom-field applicability
  // intersection in the bulk bar.
  const bulkTargets = $derived<FieldTarget[]>(
    selectedHits.map((h) => ({
      file_category: str(h, "file_category"),
      library_id: str(h, "library_id"),
    })),
  );

  function clearSelection() {
    selectedIds = new Set();
    lastToggledIndex = -1;
  }

  function selectAllLoaded() {
    selectedIds = new Set(hits.map((h) => hitId(h)).filter(Boolean));
    lastToggledIndex = hits.length - 1;
  }

  /** Toggle one row. With ``shift`` the whole range from the last-toggled row is
   *  forced to the NEW state of the clicked row (standard file-manager
   *  behaviour: shift-click extends a selection, it does not invert a range). */
  function toggleSelect(index: number, shift: boolean) {
    const h = hits[index];
    if (!h) return;
    const id = hitId(h);
    if (!id) return;
    const next = new Set(selectedIds);
    const turningOn = !next.has(id);
    if (shift && lastToggledIndex >= 0 && lastToggledIndex < hits.length) {
      for (const i of rangeIndices(lastToggledIndex, index)) {
        const rid = hitId(hits[i]);
        if (!rid) continue;
        if (turningOn) next.add(rid);
        else next.delete(rid);
      }
    } else if (turningOn) {
      next.add(id);
    } else {
      next.delete(id);
    }
    selectedIds = next;
    lastToggledIndex = index;
  }

  /** Narrow the selection to the ids the last batch REJECTED, so the operator can
   *  fix scope/permissions and re-apply the operations still sitting in the bar. */
  function selectFailedOnly(failures: BulkFailure[]) {
    selectedIds = new Set(failures.map((f) => f.id));
    lastToggledIndex = -1;
  }

  /** Resolve each selected item's CURRENT tag list — the input the per-item tag
   *  arithmetic needs, because `tags` replaces the whole list on the wire.
   *
   *  Search hits carry `tags` (it is projected into the Meili doc), so the common
   *  path costs nothing. A hit that somehow lacks the array is re-fetched rather
   *  than assumed empty: treating "unknown" as [] would DELETE every tag on that
   *  item the moment any tag operation ran. An item we cannot resolve is reported
   *  as a failure and excluded from the batch — never guessed at. */
  async function resolveTargets(): Promise<{ targets: BulkTarget[]; unresolved: BulkFailure[] }> {
    const targets: BulkTarget[] = [];
    const unresolved: BulkFailure[] = [];
    const needsFetch: Hit[] = [];
    for (const h of selectedHits) {
      const raw = h.tags;
      if (Array.isArray(raw)) {
        targets.push({
          id: hitId(h),
          tags: raw.filter((t): t is string => typeof t === "string"),
          file_category: str(h, "file_category"),
          library_id: str(h, "library_id"),
        });
      } else {
        needsFetch.push(h);
      }
    }
    // Bounded concurrency: the same chunking discipline as the submit path, so a
    // 2,000-item selection cannot open 2,000 sockets.
    for (const group of chunk(needsFetch, 20)) {
      const recs = await Promise.all(
        group.map((h) => getItem(hitId(h)).catch(() => null)),
      );
      recs.forEach((rec, i) => {
        const h = group[i];
        if (!rec) {
          unresolved.push({ id: hitId(h), reason: "could not read this item's current tags" });
          return;
        }
        const raw = rec.tags;
        targets.push({
          id: hitId(h),
          tags: Array.isArray(raw) ? raw.filter((t): t is string => typeof t === "string") : [],
          file_category: str(h, "file_category"),
          library_id: str(h, "library_id"),
        });
      });
    }
    return { targets, unresolved };
  }

  /** Reflect a successful patch in the rows already on screen.
   *
   *  Not a refetch: `defer_index_sync` is asynchronous, so re-running the query
   *  right now would very likely return the OLD document and look like the edit
   *  was lost. Patching the loaded hits shows the truth immediately, and the
   *  toast says the index catches up shortly rather than pretending it already
   *  has. (user_metadata is not projected into search hits, so only tags/year
   *  need mirroring here.) */
  function applyLocally(okIds: string[], patches: Record<string, ItemPatchBody>) {
    const ok = new Set(okIds);
    hits = hits.map((h) => {
      const id = hitId(h);
      if (!ok.has(id) || !patches[id]) return h;
      const p = patches[id];
      const next: Hit = { ...h };
      if (p.tags) next.tags = p.tags;
      if ("year" in p) next.year = p.year;
      return next;
    });
  }

  async function applyBulk(ops: BulkOps) {
    bulkBusy = true;
    bulkError = "";
    bulkSummary = null;
    bulkFailuresOpen = false;
    try {
      const { targets, unresolved } = await resolveTargets();
      const patches = buildPatches(targets, ops);
      const ids = Object.keys(patches);
      if (!ids.length && !unresolved.length) {
        toast("Nothing to change — every selected item already matches.");
        return;
      }
      // Chunked at exactly the server's cap: a request over 500 keys is rejected
      // with a 413, so chunking is the contract rather than a nicety.
      const parts: BulkSummary[] = [];
      for (const group of chunk(ids)) {
        const body: Record<string, ItemPatchBody> = {};
        for (const id of group) body[id] = patches[id];
        const res = await batchPatchItems(body);
        parts.push(summarizeResults(res.results));
      }
      const sum = mergeSummaries(parts);
      // Items we could not read are failures too — they never reached the batch,
      // and silently dropping them would misreport the count.
      sum.failures = [...sum.failures, ...unresolved];
      bulkSummary = sum;
      bulkFailuresOpen = sum.failures.length > 0;
      applyLocally(sum.ok, patches);
      if (!sum.failures.length) clearSelection();
      toast(
        sum.failures.length
          ? `${sum.ok.length} updated, ${sum.failures.length} failed — search index updates shortly.`
          : `${sum.ok.length} updated — search index updates shortly.`,
        { tone: sum.failures.length ? "warn" : "ok" },
      );
    } catch (e) {
      // A request-level failure (auth, 413, network). Per-ITEM failures never
      // land here — they come back inside a 200 and are shown in the list.
      bulkError = errorMessage(e, { verb: "edit these items" });
    } finally {
      bulkBusy = false;
    }
  }

  // ---- query params -------------------------------------------------------
  // The UI state as typed: q carries any DSL verbatim (see currentParams()).
  function uiParams(): Record<string, string> {
    const term = q.trim();
    hashMode = HASH_RE.test(term);
    const p: Record<string, string> = {};
    if (hashMode) p.hash = term;
    else p.q = q;
    if (extension) p.extension = extension;
    // Multi-select taxonomy filters ride as one comma-joined value each;
    // api.search() expands them into repeatable ``file_category`` /
    // ``file_group`` query params for the backend's List[str].
    if (selectedCategories.length) p.file_category = selectedCategories.join(",");
    if (selectedGroups.length) p.file_group = selectedGroups.join(",");
    if (selectedLibraries.length) p.library = selectedLibraries.join(",");
    if (selectedPrincipals.length) p.principal = selectedPrincipals.join(",");
    if (worldReadable) p.world_readable = worldReadable;
    if (exposure) p.exposure = exposure;
    if (shareMismatch) p.share_mismatch = "true";
    if (selectedTags.length) p.tags = selectedTags.join(",");
    if (includeSidecars) p.include_sidecars = "true";
    if (searchIn !== "all") p.search_in = searchIn;
    // Range filters: only send a bound when the slider is narrowed off it, so a
    // full-range slider is a no-op (and lets the bounds keep refreshing).
    if (sizeBounds && sizeLo > sizeBounds.min) p.size_gte = String(Math.round(sizeLo));
    if (sizeBounds && sizeHi < sizeBounds.max) p.size_lte = String(Math.round(sizeHi));
    if (mtimeBounds && mtimeLo > mtimeBounds.min) p.mtime_gte = String(Math.round(mtimeLo));
    if (mtimeBounds && mtimeHi < mtimeBounds.max) p.mtime_lte = String(Math.round(mtimeHi));
    // Range params from an applied saved search / deep link, until a slider moves.
    for (const k of ["size_gte", "size_lte", "mtime_gte", "mtime_lte"]) {
      if (!(k in p) && pendingRange[k]) p[k] = pendingRange[k];
    }
    if (sortMode) p.sort = sortMode;
    if (view !== "list") p.view = view;
    // R8-UI: the map's area selection is a FILTER like any other — four flat
    // params on the same query. boxParams() emits nothing for a box the API
    // would reject (inverted / antimeridian-crossing), so a bad box degrades to
    // "no area filter" instead of a 422.
    Object.assign(p, boxParams(geoBox));
    // Only send a semantic ratio when the feature is on AND the user dialed it
    // up — a 0 keeps the keyword path byte-identical (and deep links stay clean).
    if (semanticEnabled && semantic > 0) p.semantic = String(semantic);
    return p;
  }

  // What /search (results, paging, map) receives: the UI params with the query DSL translated
  // (searchDsl.ts). DSL list params union with the chips, the DSL extension and
  // ranges win over the chip and sliders, and q loses the translated and
  // unsupported tokens. A query without DSL tokens is returned untouched.
  function buildParams(): Record<string, string> {
    const p = uiParams();
    return hashMode ? p : mergeDslParams(p, dslToParams(q));
  }

  // The current, non-empty UI param bundle — what the URL hash reflects and a
  // saved search stores. It keeps the query AS TYPED and carries none of the
  // DSL-derived params (chips still contribute theirs), so a relative
  // `modified:>7d` is never frozen into a date: the DSL is re-translated by
  // buildParams() on every request, including after applying a saved search.
  function currentParams(): Record<string, string> {
    return Object.fromEntries(
      Object.entries(uiParams()).filter(([, v]) => v !== ""),
    );
  }

  // Reflect the current params into the location hash (#/search?...). Guarded
  // to SEARCH routes only: a late async continuation (or a hashchange handled
  // while unmount is pending) must never clobber a navigation to another tab —
  // writing "#/search" over "#/jobs" is exactly the snap-back bug this fixes
  // (live 2026-08-04). Ruling 2 (A9): typing and slider drags REPLACE the
  // current history entry; committed changes (chips, saved search, Enter,
  // view/sort) PUSH one. sync records the write either way (searchHash.ts).
  function reflectHash(mode: "push" | "replace") {
    if (!isSearchHash(location.hash)) return;
    const h = encodeSearchHash(currentParams());
    const { write } = sync.reflect(location.hash, h, mode);
    if (write === "replace") history.replaceState(null, "", h);
    else if (write === "push") location.hash = h;
  }

  // Toggle list/grid. A layout change needs no re-query -- just re-render the
  // already-loaded hits and reflect the new ``view`` into the URL hash so the
  // choice is deep-linkable / saved-search-persisted.
  function setView(v: "list" | "grid" | "map") {
    if (view === v) return;
    view = v;
    reflectHash("push");
    // The map needs its own bounded, geo-filtered fetch (the results page is
    // ordered by relevance and capped at a screenful) — pull it on first open.
    if (v === "map" && searched) loadMapPoints();
  }

  // Apply a flat param bundle (deep link, back/forward, or a saved search) into
  // the UI state and re-query. searchState.ts validates every value (A36) and
  // keeps range params in pendingRange until that pair's slider moves (A7).
  // `mode` is how a re-encoded hash is written: "replace" when the bundle came
  // from the URL (Back/Forward, deep link), "push" for a saved search.
  function applyParams(p: Record<string, string>, mode: "push" | "replace") {
    const { state: s, searchable, invalid } = applyParamsPure(p, { size: sizeBounds, mtime: mtimeBounds });
    q = s.q;
    extension = s.extension;
    selectedCategories = s.selectedCategories;
    selectedGroups = s.selectedGroups;
    selectedLibraries = s.selectedLibraries;
    selectedPrincipals = s.selectedPrincipals;
    worldReadable = s.worldReadable;
    exposure = s.exposure;
    shareMismatch = s.shareMismatch;
    selectedTags = s.selectedTags;
    tagQuery = "";
    tagOpen = false;
    includeSidecars = s.includeSidecars;
    searchIn = s.searchIn;
    sortMode = s.sortMode;
    view = s.view;
    geoBox = s.geoBox;
    semantic = s.semantic;
    pendingRange = s.pendingRange;
    if (s.sizeLo != null && s.sizeHi != null) { sizeLo = s.sizeLo; sizeHi = s.sizeHi; }
    if (s.mtimeLo != null && s.mtimeHi != null) { mtimeLo = s.mtimeLo; mtimeHi = s.mtimeHi; }
    if (invalid.length) toast(`Ignored unknown search options: ${invalid.join(", ")}`, { tone: "warn" });
    // Auto-run ONLY when the params actually FILTER something: display-only
    // leftovers (view / sort / semantic) restore the prefs on a blank slate
    // (roadmap §20). A6: the blank slate is reset, not left showing the
    // previous query's results, and the URL is brought in line.
    if (searchable) {
      runFresh(mode);
    } else {
      blankSlate();
      reflectHash(mode);
    }
  }

  // Roadmap §20 empty start, also what a non-searchable apply returns to (A6).
  function blankSlate() {
    clearTimeout(debounce);
    inputPending = false; // the next Enter must not re-run a cancelled edit
    controller?.abort(); // a late response must not repopulate the slate
    controller = null;
    loading = false;
    hits = [];
    total = 0;
    facets = {};
    nextCursor = null;
    pages.clear();
    loadMoreError = "";
    loadMoreBlocked = false;
    copyCountsGen++;
    copyCountsError = "";
    searched = false;
    clearSelection();
    error = "";
    bulkSummary = null;
    bulkError = "";
    mapController?.abort();
    mapController = null;
    mapLoading = false;
    mapError = false;
    mapPoints = [];
    geoTotal = 0;
  }

  function onHashChange() {
    // Every hashchange settles the own-write token, but only search-route
    // hashes are ours. On a back/forward to another tab this listener still
    // fires once before the unmount flush; treating "#/jobs" as empty params
    // re-ran a search and wrote "#/search" back — reverting the navigation.
    const act = sync.incoming(location.hash);
    if (!isSearchHash(location.hash) || act === "ignore") return;
    applyParams(parseSearchHash(location.hash), "replace");
  }

  // A slider drag takes over from that pair's applied/deep-linked range only;
  // the other pair's pending filter stays (A7). Drags replace history.
  function onSliderInput(pair: RangePair) {
    if (pairPending(pendingRange, pair)) pendingRange = clearRangePair(pendingRange, pair);
    reflectHash("replace");
  }
  function onRangeChange(pair: RangePair) {
    pendingRange = clearRangePair(pendingRange, pair);
    runFresh("replace");
  }

  // The × on a pending-range chip: drop that pair only and re-query.
  function clearPending(pair: RangePair) {
    pendingRange = clearRangePair(pendingRange, pair);
    runFresh("push");
  }

  // ---- saved searches (P3-T7) --------------------------------------------
  function savedParams(): Record<string, string> {
    const p = currentParams();
    for (const k of UI_ONLY_PARAMS) delete p[k];
    return p;
  }

  async function loadSaved() {
    try {
      saved = await listSavedSearches();
    } catch {
      // Non-fatal: the panel just shows nothing (e.g. read scope missing).
    }
  }

  // promptText validate: a name another saved search already has (case-
  // insensitive). `exceptId` lets a rename keep (or re-case) its own name.
  function duplicateName(name: string, exceptId?: string): string | undefined {
    const n = name.trim().toLowerCase();
    return saved.some((s) => s.id !== exceptId && s.name.toLowerCase() === n)
      ? "A saved search with that name already exists."
      : undefined;
  }

  async function saveCurrent() {
    const name = await promptText({
      title: "Name this search",
      label: "Name",
      confirmLabel: "Save",
      validate: (v) => duplicateName(v),
    });
    if (!name) return;
    setSavedError("save", "");
    try {
      await createSavedSearch({ name, params: savedParams() });
      await loadSaved();
    } catch (e) {
      setSavedError("save", errorMessage(e));
    }
  }

  function applySaved(ss: SavedSearch) {
    applyParams(ss.params ?? {}, "push");
  }

  async function renameSaved(ss: SavedSearch) {
    const name = await promptText({
      title: "Rename search",
      label: "Name",
      initial: ss.name,
      confirmLabel: "Rename",
      validate: (v) => duplicateName(v, ss.id),
    });
    if (!name || name === ss.name) return;
    setSavedError(ss.id, "");
    try {
      await updateSavedSearch(ss.id, { name });
      await loadSaved();
    } catch (e) {
      setSavedError(ss.id, errorMessage(e));
    }
  }

  async function removeSaved(ss: SavedSearch) {
    const ok = await confirm({
      title: "Delete saved search?",
      message: `"${ss.name}" will be removed from your saved searches.`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    setSavedError(ss.id, "");
    try {
      await deleteSavedSearch(ss.id);
      await loadSaved();
    } catch (e) {
      setSavedError(ss.id, errorMessage(e));
    }
  }

  const BOTH_PAIRS: readonly RangePair[] = ["size", "date"];

  function applyStats(r: SearchResponse, pairs: readonly RangePair[] = BOTH_PAIRS) {
    // Update slider bounds from facetStats ONLY while that facet isn't being
    // narrowed — otherwise an active filter would shrink its own track. A
    // pending (URL / saved-search) range narrows it too: keep the bounds we had.
    // With none yet, runFresh's bounds probe supplies the unfiltered stats
    // (`pairs` limits which pairs a response may set).
    const ss = pairs.includes("size") ? r.facet_stats?.size : undefined;
    if (ss && !sizeActive && !(sizeBounds && pairPending(pendingRange, "size"))) {
      sizeBounds = { min: ss.min, max: ss.max };
      sizeLo = ss.min;
      sizeHi = ss.max;
    }
    const ms = pairs.includes("date") ? r.facet_stats?.mtime : undefined;
    if (ms && !mtimeActive && !(mtimeBounds && pairPending(pendingRange, "date"))) {
      mtimeBounds = { min: ms.min, max: ms.max };
      mtimeLo = ms.min;
      mtimeHi = ms.max;
    }
  }

  // ---- fetching -----------------------------------------------------------
  async function runFresh(mode: "push" | "replace" = "push") {
    // Cancel any in-flight request so a stale response can never overwrite a
    // newer one (P3-T3 AbortController on the 150 ms debounce).
    controller?.abort();
    const ctrl = new AbortController();
    controller = ctrl;
    searched = true;
    error = "";
    loading = true;
    loadMoreError = "";
    loadMoreBlocked = false;
    copyCountsGen++;
    copyCountsError = "";
    // IN-T4: a NEW query means a new result set — carrying a selection across it
    // would let an invisible, unrelated item ride along into the next bulk edit.
    // (Paging the SAME query via loadMore() deliberately keeps the selection.)
    clearSelection();
    bulkSummary = null;
    bulkError = "";
    reflectHash(mode); // keep the URL in sync with the query being run (deep-linkable)
    const params = buildParams();
    pages.start(params); // A12: loadMore pages THIS query
    // A filtered deep link has no slider bounds yet: one extra, hit-less request
    // without the range keys reads the unfiltered facet_stats for those pairs,
    // so the sliders show the whole range (ruling, P6 Task 3). Same controller:
    // a newer search aborts it too. A failed probe falls back to the main stats.
    const probePairs = boundsProbePairs(pendingRange, { size: sizeBounds, mtime: mtimeBounds });
    const probe = probePairs.length
      ? search(boundsProbeParams(params), ctrl.signal).catch(() => null)
      : null;
    try {
      const r = await search(params, ctrl.signal);
      if (ctrl.signal.aborted) return;
      hits = mergeHits<Hit>([], r.hits, hitId).hits;
      total = r.total;
      facets = r.facets;
      nextCursor = r.next_cursor;
      selectedIndex = 0;
      applyStats(r, BOTH_PAIRS.filter((pr) => !probePairs.includes(pr)));
      if (probe) {
        void probe.then((pr) => {
          if (ctrl.signal.aborted || controller !== ctrl) return;
          applyStats(pr ?? r, probePairs);
        });
      }
      copyCountMap = {};
      refreshCopyCounts(hits); // P3-T10 batch badge counts
      vlist?.scrollTo(0);
      if (view === "map") loadMapPoints(); // keep the map on the same query
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return; // superseded — ignore
      error = errorMessage(e);
    } finally {
      if (controller === ctrl) loading = false;
    }
  }

  // Next page of the last fresh search. `retry` is the error block's Retry:
  // the only way past loadMoreBlocked.
  async function loadMore(retry = false) {
    if (loading || !controller || (loadMoreBlocked && !retry)) return;
    const params = pages.pageParams(nextCursor);
    if (!params) return;
    const ctrl = controller; // reuse the session controller: a new search aborts
    loading = true;
    loadMoreError = "";
    try {
      const r = await search(params, ctrl.signal);
      if (ctrl.signal.aborted || controller !== ctrl) return;
      // The offset cursor repeats a row when the index shifted between pages.
      const before = hits.length;
      hits = mergeHits(hits, r.hits, hitId).hits;
      nextCursor = r.next_cursor;
      loadMoreBlocked = false;
      refreshCopyCounts(hits.slice(before)); // badge the newly-appended rows too
    } catch (e) {
      if ((e as Error)?.name === "AbortError" || controller !== ctrl) return;
      loadMoreError = errorMessage(e, { verb: "load more results" });
      loadMoreBlocked = true;
    } finally {
      if (controller === ctrl) loading = false;
    }
  }

  // ---- R8-UI map points ---------------------------------------------------
  // The map plots the CURRENT query, not a second search: same buildParams(),
  // plus one extra constraint and a hard cap.
  //
  // Why the whole-world box when no area is drawn: `_geo` is the only marker of
  // a geo-bearing document and it is not a filterable facet, so "has
  // coordinates" is expressed as a bounding box covering the planet — which is
  // exactly what _geoBoundingBox does, and it costs nothing extra server-side.
  //
  // Two bounds, both stated in the UI rather than applied silently (see the
  // MapPanel caption): at most MAP_POINT_CAP points are fetched, over at most
  // ceil(MAP_POINT_CAP / MAP_PAGE) sequential pages, and the response's `total`
  // is kept so the map can say "showing N of M".
  async function loadMapPoints() {
    mapController?.abort();
    const ctrl = new AbortController();
    mapController = ctrl;
    mapLoading = true;
    mapError = false;
    try {
      const base = buildParams();
      base.limit = String(MAP_PAGE);
      if (!geoBox) {
        base.geo_top_lat = "90";
        base.geo_bottom_lat = "-90";
        base.geo_left_lng = "-180";
        base.geo_right_lng = "180";
      }
      let acc: MapPoint[] = [];
      let cursor: string | null = null;
      let seen = 0;
      do {
        const r = await search(cursor ? { ...base, cursor } : base, ctrl.signal);
        if (ctrl.signal.aborted || mapController !== ctrl) return;
        geoTotal = r.total;
        const page: MapPoint[] = [];
        for (const h of r.hits) {
          const g = geoOf(h);
          const id = str(h, "id");
          if (!g || !id) continue;
          page.push({
            id,
            lat: g.lat,
            lng: g.lng,
            label: str(h, "title") || str(h, "filename") || str(h, "rel_path") || id,
          });
        }
        // A12: the same offset cursor, so the same de-dupe (the cluster list
        // is keyed by id).
        acc = mergeHits(acc, page, (pt) => pt.id).hits;
        seen += r.hits.length;
        cursor = r.next_cursor;
      } while (cursor && seen < MAP_POINT_CAP);
      mapPoints = acc.slice(0, MAP_POINT_CAP);
    } catch (e) {
      if ((e as Error)?.name === "AbortError" || mapController !== ctrl) return; // superseded — ignore
      // The map panel shows an error with Retry — never an empty map, which
      // would read as "no result carries coordinates". The results list stays.
      mapPoints = [];
      mapError = true;
    } finally {
      if (mapController === ctrl) mapLoading = false;
    }
  }

  // A box drawn (or typed) on the map is applied to the query like any chip:
  // set the filter, re-run, and let reflectHash() put it in the URL.
  function onMapBox(b: GeoBox | null) {
    geoBox = b;
    runFresh();
  }

  // P3-T10: fetch copy counts for a batch of hits (ids only, cap 200 per call)
  // and merge into the badge map. Only groups with count > 1 come back, so a
  // badge appears only for genuine duplicates. A failure keeps the results and
  // shows a warning with Retry (A10); a stale batch's outcome is dropped.
  async function refreshCopyCounts(batch: Hit[]) {
    const ids = batch.map((h) => hitId(h)).filter(Boolean);
    if (!ids.length) return;
    const gen = copyCountsGen;
    try {
      for (const group of chunk(ids, 200)) {
        const counts = await apiCopyCounts(group);
        if (gen !== copyCountsGen) return;
        copyCountMap = { ...copyCountMap, ...counts };
      }
    } catch (e) {
      if (gen === copyCountsGen) copyCountsError = errorMessage(e, { verb: "load duplicate counts" });
    }
  }

  function retryCopyCounts() {
    copyCountsError = "";
    refreshCopyCounts(hits);
  }

  // ---- P3-T12 tag type-ahead -------------------------------------------
  function addTag(tag: string) {
    const t = tag.trim();
    if (t && !selectedTags.includes(t)) selectedTags = [...selectedTags, t];
    tagQuery = "";
    tagSuggestions = [];
    tagOpen = false;
    tagActiveIndex = -1;
    reset();
  }

  function removeTag(tag: string) {
    selectedTags = selectedTags.filter((t) => t !== tag);
    reset();
  }

  function onTagInput() {
    clearTimeout(tagDebounce);
    tagDebounce = setTimeout(fetchTagSuggestions, 150);
  }

  async function fetchTagSuggestions() {
    tagController?.abort();
    const ctrl = new AbortController();
    tagController = ctrl;
    try {
      // A31: scope the suggestion counts like the results — every category
      // (chips plus a `kind:` in the query; /search/tags takes a repeatable
      // file_category) and the library when exactly one is chosen (the
      // endpoint takes a single `library`).
      const p = buildParams();
      const res = await searchTags(
        tagQuery,
        {
          file_category: p.file_category ? p.file_category.split(",") : undefined,
          library: selectedLibraries.length === 1 ? selectedLibraries[0] : undefined,
        },
        ctrl.signal,
      );
      if (ctrl.signal.aborted) return;
      // Hide already-selected tags from the suggestion list.
      tagSuggestions = res.tags.filter((t) => !selectedTags.includes(t.value));
      tagOpen = tagSuggestions.length > 0;
      tagActiveIndex = tagOpen ? 0 : -1;
    } catch {
      if (!ctrl.signal.aborted) { tagSuggestions = []; tagOpen = false; }
    }
  }

  // Keyboard nav within the tag dropdown. stopPropagation keeps these keys from
  // reaching the window-level results-list handler (arrows/Enter are overloaded).
  function onTagKeydown(e: KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault(); e.stopPropagation();
      if (tagSuggestions.length) { tagOpen = true; tagActiveIndex = Math.min(tagSuggestions.length - 1, tagActiveIndex + 1); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault(); e.stopPropagation();
      tagActiveIndex = Math.max(0, tagActiveIndex - 1);
    } else if (e.key === "Enter") {
      e.preventDefault(); e.stopPropagation();
      if (tagOpen && tagActiveIndex >= 0 && tagSuggestions[tagActiveIndex]) {
        addTag(tagSuggestions[tagActiveIndex].value);
      } else if (tagQuery.trim()) {
        addTag(tagQuery); // free-text tag even without a suggestion
      }
    } else if (e.key === "Escape") {
      e.stopPropagation();
      tagOpen = false;
    }
  }

  // Typing replaces the current history entry (A9); a half-composed IME
  // string never searches (A43) — compositionend schedules the search instead,
  // since Chrome fires no plain input event after it.
  let inputPending = false;
  function onInput(e: Event) {
    if ((e as InputEvent).isComposing) return;
    userTyped = true;
    clearTimeout(debounce);
    inputPending = true;
    debounce = setTimeout(() => {
      inputPending = false;
      runFresh("replace");
    }, 150);
  }

  // Enter commits the query: a still-debounced edit runs now and pushes its own
  // history entry; an already-reflected one pushes nothing new.
  function onQueryKeydown(e: KeyboardEvent) {
    if (e.key !== "Enter" || e.isComposing) return;
    e.preventDefault();
    if (inputPending) {
      clearTimeout(debounce);
      inputPending = false;
      runFresh("push");
    } else {
      reflectHash("push");
    }
  }

  // FIX-12 (Item B): a "Query syntax" help chip appends its example to the query
  // box and runs it immediately, so users can learn the filter DSL by example.
  function insertDsl(frag: string) {
    q = q.trim() ? `${q.trim()} ${frag}` : frag;
    runFresh();
    searchInput?.focus();
  }

  // Chip / slider changes re-query immediately (no debounce needed).
  function reset() {
    runFresh();
  }

  // ---- filter helpers (P3-T4) --------------------------------------------
  // Extension type-ahead-lite: rank the live ext facet by count, filtered by the
  // small type-ahead box, bounded to EXT_LIMIT. Purely presentational — the
  // untrusted facet keys are rendered as text, never interpolated anywhere.
  const topExtensions = $derived.by(() => {
    const dist = facets.extension ?? {};
    const needle = extQuery.trim().toLowerCase();
    return Object.entries(dist)
      .filter(([e]) => !needle || e.toLowerCase().includes(needle))
      .sort((a, b) => b[1] - a[1])
      .slice(0, EXT_LIMIT);
  });

  function toggleExtension(e: string) {
    extension = extension === e ? "" : e;
    reset();
  }

  // file_category multi-select (coarse): toggle a key, then re-query.
  function toggleCategory(key: string) {
    selectedCategories = selectedCategories.includes(key)
      ? selectedCategories.filter((c) => c !== key)
      : [...selectedCategories, key];
    reset();
  }
  const categoryLabel = (key: string): string =>
    categoryOptions.find((c) => c.key === key)?.label ?? key;

  // Permission filter helpers (type-ahead mirrors the tag combobox).
  function addPrincipal(v: string) {
    const val = v.trim();
    if (!val) return;
    if (!selectedPrincipals.includes(val)) selectedPrincipals = [...selectedPrincipals, val];
    principalQuery = ""; principalSuggestions = []; principalOpen = false; principalActiveIndex = -1;
    reset();
  }
  function removePrincipal(v: string) {
    selectedPrincipals = selectedPrincipals.filter((x) => x !== v);
    reset();
  }
  function onPrincipalInput() {
    clearTimeout(principalDebounce);
    principalDebounce = setTimeout(fetchPrincipalSuggestions, 150);
  }
  async function fetchPrincipalSuggestions() {
    principalController?.abort();
    const ctrl = new AbortController();
    principalController = ctrl;
    try {
      const res = await searchPrincipals(principalQuery, ctrl.signal);
      if (ctrl.signal.aborted) return;
      principalSuggestions = res.principals.filter((t) => !selectedPrincipals.includes(t.value));
      principalOpen = principalSuggestions.length > 0;
      principalActiveIndex = principalOpen ? 0 : -1;
    } catch {
      if (!ctrl.signal.aborted) { principalSuggestions = []; principalOpen = false; }
    }
  }
  function onPrincipalKeydown(e: KeyboardEvent) {
    if (e.key === "ArrowDown") {
      e.preventDefault(); e.stopPropagation();
      if (principalSuggestions.length) { principalOpen = true; principalActiveIndex = Math.min(principalSuggestions.length - 1, principalActiveIndex + 1); }
    } else if (e.key === "ArrowUp") {
      e.preventDefault(); e.stopPropagation();
      principalActiveIndex = Math.max(0, principalActiveIndex - 1);
    } else if (e.key === "Enter") {
      e.preventDefault(); e.stopPropagation();
      if (principalOpen && principalActiveIndex >= 0 && principalSuggestions[principalActiveIndex]) {
        addPrincipal(principalSuggestions[principalActiveIndex].value);
      } else if (principalQuery.trim()) {
        addPrincipal(principalQuery);
      }
    } else if (e.key === "Escape") {
      e.stopPropagation();
      principalOpen = false;
    }
  }

  // Library multi-select (OR): toggle one id, or a whole agent's libraries.
  function toggleLibrary(id: string) {
    selectedLibraries = selectedLibraries.includes(id)
      ? selectedLibraries.filter((l) => l !== id)
      : [...selectedLibraries, id];
    reset();
  }
  function toggleAgentLibraries(ids: string[]) {
    const allOn = ids.every((id) => selectedLibraries.includes(id));
    selectedLibraries = allOn
      ? selectedLibraries.filter((l) => !ids.includes(l))
      : [...new Set([...selectedLibraries, ...ids])];
    reset();
  }
  function libraryLabel(id: string): string {
    const l = libs.get(id);
    if (!l) return id.slice(0, 8);
    return l.agent_name ? `${l.agent_name}: ${l.name}` : l.name;
  }
  // Library rows for the Filters panel: central libraries first (by name), then
  // one group per agent/machine (by agent name) holding that agent's libraries.
  type LibGroup = { agent: string | null; libs: Library[] };
  const libraryGroups = $derived.by<LibGroup[]>(() => {
    const all = [...libs.values()];
    const byName = (a: Library, b: Library) => a.name.localeCompare(b.name);
    const central = all.filter((l) => !l.source_agent_id).sort(byName);
    const agents = new Map<string, Library[]>();
    for (const l of all.filter((l) => !!l.source_agent_id)) {
      const key = l.agent_name || l.source_agent_id || "agent";
      agents.set(key, [...(agents.get(key) ?? []), l]);
    }
    const out: LibGroup[] = [];
    if (central.length) out.push({ agent: null, libs: central });
    for (const key of [...agents.keys()].sort((a, b) => a.localeCompare(b)))
      out.push({ agent: key, libs: (agents.get(key) ?? []).sort(byName) });
    return out;
  });

  // file_group multi-select (granular): toggle a key, then re-query.
  function toggleGroup(key: string) {
    selectedGroups = selectedGroups.includes(key)
      ? selectedGroups.filter((g) => g !== key)
      : [...selectedGroups, key];
    reset();
  }
  // Resolve a group key to its human label for the active-filter chips.
  const groupLabel = (key: string): string =>
    fileGroupOptions.find((g) => g.key === key)?.label ?? key;

  function snapSize() {
    if (sizeBounds) {
      sizeLo = sizeBounds.min;
      sizeHi = sizeBounds.max;
    }
    pendingRange = clearRangePair(pendingRange, "size");
    reset();
  }
  function snapMtime() {
    if (mtimeBounds) {
      mtimeLo = mtimeBounds.min;
      mtimeHi = mtimeBounds.max;
    }
    pendingRange = clearRangePair(pendingRange, "date");
    reset();
  }

  // Active filters rendered as removable chips (P3-T4). Each carries a clearer.
  // DSL-derived chips carry no clearer: the way to clear one is editing the query.
  type Chip = { key: string; label: string; clear?: () => void };
  const activeFilters = $derived.by<Chip[]>(() => {
    const out: Chip[] = [];
    for (const c of selectedCategories) out.push({ key: `fc:${c}`, label: `category: ${categoryLabel(c)}`, clear: () => toggleCategory(c) });
    if (extension) out.push({ key: "ext", label: `ext: ${extension}`, clear: () => { extension = ""; reset(); } });
    for (const g of selectedGroups) out.push({ key: `fg:${g}`, label: `group: ${groupLabel(g)}`, clear: () => toggleGroup(g) });
    for (const l of selectedLibraries) out.push({ key: `lib:${l}`, label: `library: ${libraryLabel(l)}`, clear: () => toggleLibrary(l) });
    for (const pr of selectedPrincipals) out.push({ key: `pr:${pr}`, label: `readable by: ${pr}`, clear: () => removePrincipal(pr) });
    if (worldReadable) out.push({ key: "world", label: worldReadable === "true" ? "world-readable" : "not world-readable", clear: () => { worldReadable = ""; reset(); } });
    if (exposure) out.push({ key: "exposure", label: exposure === "anonymous" ? "anonymous can read" : exposure === "authenticated" ? "any signed-in account can read" : "restricted access", clear: () => { exposure = ""; reset(); } });
    if (shareMismatch) out.push({ key: "share_mismatch", label: "share ≠ file permissions", clear: () => { shareMismatch = false; reset(); } });
    for (const t of selectedTags) out.push({ key: `tag:${t}`, label: `tag: ${t}`, clear: () => removeTag(t) });
    if (includeSidecars) out.push({ key: "sidecar", label: "sidecars shown", clear: () => { includeSidecars = false; reset(); } });
    // R8-UI: the map's area reads as an ordinary removable filter, so it can be
    // cleared from the chip row without opening the map.
    if (geoBox) out.push({ key: "geo", label: `area ${formatBox(geoBox)}`, clear: () => onMapBox(null) });
    if (sizeActive) out.push({ key: "size", label: `size ${fmtBytes(sizeLo)}–${fmtBytes(sizeHi)}`, clear: snapSize });
    if (mtimeActive) out.push({ key: "mtime", label: `date ${fmtDate(mtimeLo)}–${fmtDate(mtimeHi)}`, clear: snapMtime });
    // A7: a range that arrived by URL / saved search, until its slider moves.
    for (const c of pendingRangeChips(pendingRange)) out.push({ key: c.key, label: c.label, clear: () => clearPending(c.pair) });
    out.push(...dslChips(dsl.params));
    return out;
  });

  // "from query: …" chips for the filters searchDsl.ts lifted out of q.
  function dslChips(d: DslResult["params"]): Chip[] {
    const out: Chip[] = [];
    const list = (v: string | undefined) => (v ? v.split(",") : []);
    const range = (lo: string | undefined, hi: string | undefined, f: (n: number) => string) =>
      lo && hi ? `${f(Number(lo))}–${f(Number(hi))}` : lo ? `≥ ${f(Number(lo))}` : `≤ ${f(Number(hi))}`;
    for (const c of list(d.file_category)) out.push({ key: `dsl:fc:${c}`, label: `from query: kind ${c}` });
    for (const g of list(d.file_group)) out.push({ key: `dsl:fg:${g}`, label: `from query: group ${g}` });
    if (d.extension) out.push({ key: "dsl:ext", label: `from query: ext ${d.extension}` });
    for (const t of list(d.tags)) out.push({ key: `dsl:tag:${t}`, label: `from query: tag ${t}` });
    if (d.size_gte || d.size_lte) {
      out.push({ key: "dsl:size", label: `from query: size ${range(d.size_gte, d.size_lte, (n) => fmtBytes(n))}` });
    }
    if (d.mtime_gte || d.mtime_lte) {
      out.push({ key: "dsl:mtime", label: `from query: modified ${range(d.mtime_gte, d.mtime_lte, fmtDate)}` });
    }
    return out;
  }

  // ---- keyboard nav -------------------------------------------------------
  function inEditable(el: Element | null): boolean {
    if (!el) return false;
    const tag = el.tagName;
    return tag === "INPUT" || tag === "TEXTAREA" || (el as HTMLElement).isContentEditable;
  }

  function moveSel(delta: number) {
    const n = hits.length;
    if (!n) return;
    selectedIndex = Math.max(0, Math.min(n - 1, selectedIndex + delta));
    vlist?.scrollToIndex(selectedIndex, { align: "nearest" });
    // Keyboard nav can outrun the scroll-driven loader; prefetch near the end.
    if (selectedIndex >= n - 8 && !loadMoreBlocked) loadMore();
  }

  function openItem(h: Hit | undefined) {
    if (h) selected = hitId(h);
  }

  function resolvePath(h: Hit): string {
    const lib = libs.get(str(h, "library_id"));
    const rel = str(h, "rel_path");
    // UI-T15: honor the Paths (auto|url|unc) preference exactly like ItemDetail/
    // Breadcrumbs — the share location formatted per the viewer's choice wins
    // when the library maps one. (Previously this only used native_prefix, so
    // the Paths selector had no effect on search-results copy — live report.)
    const share = formatShare(
      shareLocation(lib?.share_prefix_effective, lib?.share_unc_effective),
      shareFormat.pref,
      detectedPlatform,
    );
    if (share) return buildDisplayPath(share, rel);
    // Invariant 3: else the NATIVE path (native_prefix + rel_path) when the
    // library maps one; else fall back to the container-absolute `path`.
    if (lib?.native_prefix) return buildDisplayPath(lib.native_prefix, rel);
    return str(h, "path") || rel;
  }

  async function copyPath(h: Hit | undefined) {
    if (!h) return;
    const p = resolvePath(h);
    if (await copyText(p)) toast(`Copied ${p}`, { tone: "ok" });
    else toast(COPY_FAILED, { tone: "warn" });
  }

  function onKeydown(e: KeyboardEvent) {
    const el = e.target instanceof Element ? e.target : document.activeElement;
    // A1: list keys only from the results listbox (or the bare page) — see
    // searchKeys.ts. IN-T4: Space toggles the roving row's selection (shift
    // extends); Cmd/Ctrl+Enter copies its path.
    const action = listKeyAction(
      e,
      {
        tag: el?.tagName ?? "BODY",
        editable: inEditable(el),
        inListbox: !!el?.closest("#search-results"),
        combobox: !!el?.hasAttribute("data-results-combobox"),
      },
      { modalOpen: !!selected, view, hasHits: hits.length > 0 },
    );
    if (!action) return;
    e.preventDefault();
    switch (action) {
      case "focusSearch":
        searchInput?.focus();
        searchInput?.select();
        break;
      case "move:1":
        moveSel(1);
        break;
      case "move:-1":
        moveSel(-1);
        break;
      case "open":
        openItem(hits[selectedIndex]);
        break;
      case "copy":
        copyPath(hits[selectedIndex]);
        break;
      case "toggle":
        toggleSelect(selectedIndex, e.shiftKey);
        break;
    }
  }

  // A clicked row takes DOM focus (rows are tabindex=-1, so focus returns to
  // it when the detail modal closes). The first key pressed on the row itself
  // hands focus back to the grid container, which owns the roving cursor
  // (aria-activedescendant); the key then bubbles on to onKeydown unchanged.
  // Keys on a control inside the row (its checkbox) are left to that control.
  function focusGrid(e: KeyboardEvent) {
    if (e.target !== e.currentTarget) return;
    (e.currentTarget as HTMLElement).closest<HTMLElement>("#search-results")?.focus({ preventScroll: true });
  }

  function onScroll() {
    if (!vlist || !nextCursor || loading || loadMoreBlocked) return;
    const end = vlist.getScrollOffset() + vlist.getViewportSize();
    if (end >= vlist.getScrollSize() - 400) loadMore();
  }

  // Grid infinite-scroll: the grid is a plain scroll container (not virtua), so
  // near-bottom drives the same cursor-based loadMore the list uses.
  function onGridScroll(e: Event) {
    if (!nextCursor || loading || loadMoreBlocked) return;
    const el = e.currentTarget as HTMLElement;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 400) loadMore();
  }

  const activeDescendant = $derived(
    hits.length && hits[selectedIndex] ? `row-${hitId(hits[selectedIndex])}` : undefined,
  );
  // The results grid is on screen (the query box's combobox popup).
  const resultsShown = $derived(searched && !error && view !== "map" && hits.length > 0);
  // Meilisearch totals are estimates (estimated_total_hits); say so when large (A45).
  const totalLabel = $derived(total >= 1000 ? `about ${total.toLocaleString("en-US")} results` : `${total} results`);

  // ---- lifecycle ----------------------------------------------------------
  // Vocabularies and flags the page decorates results with. Each loader
  // swallows its own failure (fallbacks below), so Promise.all never rejects.
  async function loadLibraries() {
    try {
      const list = await listLibraries();
      libs = new Map(list.map((l) => [l.id, l]));
    } catch {
      // Non-fatal: copy-path just falls back to the container path.
    }
  }

  // W8: category + group vocabularies come from the taxonomy tree (source of
  // truth). Prefer it; if it's offline, fall back to /system/file-groups for the
  // groups and a small static category list so the filters still render.
  async function loadTaxonomy() {
    try {
      const tax = await getTaxonomy();
      categoryOptions = tax.tree.map((n) => ({
        key: n.category.key,
        label: n.category.label,
        description: n.category.description,
      }));
      fileGroupOptions = tax.tree.flatMap((n) =>
        n.groups.map((g) => ({ key: g.key, label: g.label, description: g.description })),
      );
      if (!categoryOptions.length) categoryOptions = CATEGORY_FALLBACK;
      if (!fileGroupOptions.length) fileGroupOptions = FILE_GROUP_FALLBACK;
    } catch {
      categoryOptions = CATEGORY_FALLBACK;
      try {
        const gs = await apiFileGroups();
        fileGroupOptions = gs.length
          ? gs.map((g) => ({ key: g.id, label: g.label, description: g.description }))
          : FILE_GROUP_FALLBACK;
      } catch {
        fileGroupOptions = FILE_GROUP_FALLBACK;
      }
    }
  }

  // P3-T8: learn whether semantic search is enabled so the slider can show
  // (one cached /stats probe per page load, shared with ItemDetail — ruling 4).
  // A deep link's `semantic` ratio is only sent once the flag is known, so a
  // search that already ran without it re-runs with it.
  async function loadSemantic() {
    const on = await probeSemantic();
    if (destroyed) return;
    semanticEnabled = on;
    if (on && semantic > 0 && searched && isSearchHash(location.hash)) runFresh("replace");
  }

  // ---- lifecycle ----------------------------------------------------------
  onMount(async () => {
    // Listener registration MUST precede everything else. It used to sit
    // after the awaits, so switching tabs during the fetch window destroyed the
    // component before registration — onDestroy's removeEventListener was a
    // no-op and the continuation then LEAKED a listener on a dead component,
    // which re-wrote "#/search" over every later navigation until a hard
    // refresh (live 2026-08-04: tabs "snapped back" intermittently).
    window.addEventListener("hashchange", onHashChange);
    // A8 deep-link entry: restore state from #/search?... and query BEFORE any
    // vocabulary fetch — a bookmarked search no longer waits behind libraries,
    // taxonomy and the heavy /stats probe. Chips show their raw keys until the
    // labels land; nothing below resets the applied state. A BARE visit stays
    // empty (roadmap §20) — no match-all preload.
    const initial = isSearchHash(location.hash) ? parseSearchHash(location.hash) : {};
    if (!userTyped && Object.keys(initial).length) applyParams(initial, "replace");
    loadSaved();
    await Promise.all([loadLibraries(), loadTaxonomy(), loadSemantic()]);
  });

  let destroyed = false;
  onDestroy(() => {
    destroyed = true;
    controller?.abort();
    mapController?.abort();
    tagController?.abort();
    clearTimeout(debounce);
    clearTimeout(tagDebounce);
    window.removeEventListener("hashchange", onHashChange);
  });
</script>

<svelte:window onkeydown={onKeydown} />

<div>
  <Input
    bind:ref={searchInput}
    size="lg"
    aria-label="Search your library"
    placeholder="Search your library…  (press / to focus)"
    bind:value={q}
    oninput={onInput}
    oncompositionend={onInput}
    onkeydown={onQueryKeydown}
    role="combobox"
    aria-haspopup="grid"
    aria-controls="search-results"
    aria-expanded={resultsShown}
    aria-autocomplete="list"
    aria-activedescendant={resultsShown ? activeDescendant : undefined}
    data-results-combobox
  />
  {#if unsupportedKey && unsupportedKey !== dismissedUnsupported}
    <Alert tone="warn" live={false} class="mt-2" onDismiss={() => (dismissedUnsupported = unsupportedKey)}>
      Not supported in search:
      {#each dsl.unsupported as tok, i (i)}{#if i > 0}{", "}{/if}<code class="font-mono">{tok}</code>{/each}
      — see the query help.
    </Alert>
  {/if}

  <!-- W8 file_category chips (coarse, MULTI-select) with live facet counts. The
       primary type filter row; the granular file_group row sits below it. -->
  <div class="mt-3 flex flex-wrap items-center gap-2">
    <button
      class="rounded-full border px-3 py-1 text-sm {selectedCategories.length === 0 ? CHIP_ON : CHIP_OFF}"
      onclick={() => { selectedCategories = []; reset(); }}>all</button>
    {#each categoryOptions as c (c.key)}
      {@const count = facets.file_category?.[c.key]}
      {@const on = selectedCategories.includes(c.key)}
      <button
        class="rounded-full border px-3 py-1 text-sm {on ? CHIP_ON : CHIP_OFF} {count === 0 && !on
          ? 'opacity-40'
          : ''}"
        title={c.description || c.label}
        aria-pressed={on}
        onclick={() => toggleCategory(c.key)}>
        {c.label}{#if count != null}<span class="ml-1 text-xs opacity-70">{count}</span>{/if}
      </button>
    {/each}
    <span class="grow"></span>
    <!-- P3-T8 semantic blend slider — only rendered when the feature is enabled. -->
    {#if semanticEnabled}
      <span class="flex items-center gap-1 text-sm text-muted" title="Blend keyword and meaning-based (semantic) matching">
        Semantic
        <span class="w-24">
          <input
            type="range"
            min="0"
            max="1"
            step="0.1"
            class="w-full accent-accent"
            bind:value={semantic}
            onchange={() => runFresh("replace")}
            aria-label="Semantic blend ratio" />
        </span>
        <span class="w-8 tabular-nums">{semantic.toFixed(1)}</span>
      </span>
    {/if}
    <!-- Sort (P3-T7 deep-linkable). -->
    <span class="flex items-center gap-1 text-sm text-muted">
      Sort
      <span class="w-32">
        <Select size="sm" bind:value={sortMode} onchange={reset} aria-label="Sort results">
          {#each SORTS as s (s.value)}<option value={s.value}>{s.label}</option>{/each}
        </Select>
      </span>
    </span>
    <!-- P12 slice 2: list / grid layout toggle. -->
    <div class="flex items-center overflow-hidden rounded-full border border-border-strong" role="group" aria-label="Result layout">
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'list' ? 'bg-accent text-accent-fg' : ''}"
        aria-pressed={view === 'list'}
        title="List view"
        onclick={() => setView('list')}>List</button>
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'grid' ? 'bg-accent text-accent-fg' : ''}"
        aria-pressed={view === 'grid'}
        title="Grid view"
        onclick={() => setView('grid')}>Grid</button>
      <!-- R8-UI: the map is a third view of the SAME query, not a separate page. -->
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'map' ? 'bg-accent text-accent-fg' : ''}"
        aria-pressed={view === 'map'}
        title="Map view — plot the results that carry GPS coordinates, and drag an area to filter by it"
        onclick={() => setView('map')}>Map</button>
    </div>
    <!-- Saved searches (P3-T7). Roadmap §20: the open state reads as ACTIVE
         (accent fill, matching the chip convention) — the chevron alone was
         not a legible state indicator. Open/closed persists per browser. -->
    <button
      class="rounded-full border px-3 py-1 text-sm {savedOpen ? CHIP_ON : CHIP_OFF}"
      aria-expanded={savedOpen}
      title={savedOpen ? "Hide the saved-searches panel" : "Show saved searches"}
      onclick={toggleSavedOpen}>Saved {savedOpen ? "▲" : "▾"}</button>
    <button
      class="rounded-full border px-3 py-1 text-sm {filtersOpen ? CHIP_ON : CHIP_OFF}"
      aria-expanded={filtersOpen}
      title={filtersOpen ? "Hide the filters panel" : "Show filters (group, extension, tags, ranges)"}
      onclick={toggleFiltersOpen}>Filters {filtersOpen ? "▲" : "▾"}</button>
  </div>

  <!-- FIX-12 (Item B): expandable filter-DSL help; chips insert into the box. -->
  <DslHelp onInsert={insertDsl} context="search" />

  <!-- Saved searches panel (P3-T7): save current, apply/rename/delete. -->
  {#if savedOpen}
    <div class="mt-2 rounded-panel border border-border p-3">
      <Button variant="primary" size="sm" onclick={saveCurrent}>Save current search</Button>
      {#if savedErrors.save}
        <Alert tone="danger" class="mt-2" onDismiss={() => setSavedError("save", "")}>{savedErrors.save}</Alert>
      {/if}
      {#if saved.length}
        <ul class="mt-3 flex flex-col gap-1">
          {#each saved as ss (ss.id)}
            <li class="flex flex-col gap-1 text-sm">
              <div class="flex items-center gap-2">
                <button
                  class="min-w-0 flex-1 truncate rounded-control px-2 py-1 text-left hover:bg-surface-3"
                  title={`Apply "${ss.name}"`}
                  onclick={() => applySaved(ss)}>{ss.name}</button>
                <Button size="sm" onclick={() => renameSaved(ss)}>Rename</Button>
                <Button size="sm" variant="danger" onclick={() => removeSaved(ss)}>Delete</Button>
              </div>
              {#if savedErrors[ss.id]}
                <Alert tone="danger" onDismiss={() => setSavedError(ss.id, "")}>{savedErrors[ss.id]}</Alert>
              {/if}
            </li>
          {/each}
        </ul>
      {:else}
        <StateBlock
          state="empty"
          compact
          message="No saved searches yet. Run a search, then press Save current search to keep it." />
      {/if}
    </div>
  {/if}

  <!-- Active filters as removable chips (P3-T4). -->
  {#if activeFilters.length}
    <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
      {#each activeFilters as chip (chip.key)}
        <span class="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-1 text-accent-text">
          {chip.label}
          {#if chip.clear}
            <button
              type="button"
              class="rounded-full px-1 leading-none hover:bg-accent/25"
              aria-label={`Remove filter ${chip.label}`}
              onclick={chip.clear}>×</button>
          {/if}
        </span>
      {/each}
    </div>
  {/if}

  {#if filtersOpen}
    <div class="mt-3 rounded-panel border border-border p-3">
      <!-- Library / machine filter (MULTI-select, OR). Central libraries first,
           then one group per agent with a whole-machine toggle; counts come
           from the library_id facet when the backend surfaces it. Hidden on a
           single-library catalog (nothing to choose). -->
      {#if libs.size > 1}
        <div class="mb-3 flex flex-wrap items-start gap-2">
          <span class="mt-1 w-10 text-xs font-medium text-muted">Library</span>
          <div class="flex flex-wrap items-center gap-2">
            {#each libraryGroups as grp (grp.agent ?? "central")}
              {#if grp.agent}
                {@const ids = grp.libs.map((l) => l.id)}
                {@const allOn = ids.every((id) => selectedLibraries.includes(id))}
                <button
                  class="rounded-full border border-dashed px-2 py-0.5 text-xs {allOn ? CHIP_ON : CHIP_OFF}"
                  title="every library on this machine"
                  aria-pressed={allOn}
                  onclick={() => toggleAgentLibraries(ids)}>
                  🖥 {grp.agent}
                </button>
              {/if}
              {#each grp.libs as l (l.id)}
                {@const count = facets.library_id?.[l.id]}
                {@const on = selectedLibraries.includes(l.id)}
                <button
                  class="rounded-full border px-3 py-1 text-sm {on ? CHIP_ON : CHIP_OFF} {count === 0 && !on
                    ? 'opacity-40'
                    : ''}"
                  title={l.root_path}
                  aria-pressed={on}
                  onclick={() => toggleLibrary(l.id)}>
                  {l.name}{#if count != null}<span class="ml-1 text-xs opacity-70">{count}</span>{/if}
                </button>
              {/each}
            {/each}
          </div>
        </div>
      {/if}
      <!-- File-group chips (MULTI-select, granular second taxonomy level).
           Roadmap §20: moved INSIDE the collapsed Filters panel — the row is
           long and ate vertical space on every visit. Counts are shown only
           when the backend surfaces a file_group facet, so no fabricated zeros
           appear when facet stats aren't available. -->
      {#if fileGroupOptions.length}
        <div class="mb-3 flex flex-wrap items-center gap-2">
          <span class="w-10 text-xs font-medium text-muted">Group</span>
          {#each fileGroupOptions as g (g.key)}
            {@const count = facets.file_group?.[g.key]}
            {@const on = selectedGroups.includes(g.key)}
            <button
              class="rounded-full border px-3 py-1 text-sm {on ? CHIP_ON : CHIP_OFF} {count === 0 && !on
                ? 'opacity-40'
                : ''}"
              title={g.description || g.label}
              aria-pressed={on}
              onclick={() => toggleGroup(g.key)}>
              {g.label}{#if count != null}<span class="ml-1 text-xs opacity-70">{count}</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
      <!-- Extension type-ahead-lite: bounds come from the live ext facet. -->
      <div class="flex flex-wrap items-center gap-2 text-xs text-muted">
        <span class="w-10 font-medium">Type</span>
        <span class="w-32">
          <Input size="sm" aria-label="Filter extensions" placeholder="ext…" bind:value={extQuery} />
        </span>
        {#each topExtensions as [ext, count] (ext)}
          <button
            class="rounded-full border px-2 py-0.5 {extension === ext ? CHIP_ON : CHIP_OFF}"
            onclick={() => toggleExtension(ext)}>
            {ext}<span class="ml-1 opacity-70">{count}</span>
          </button>
        {/each}
        {#if !topExtensions.length}<span class="opacity-60">no extension facets</span>{/if}
      </div>

      <!-- Sidecar toggle (T3 sidecars hidden by default). -->
      <div class="mt-3 flex flex-wrap items-center gap-4 text-xs text-muted">
        <Checkbox size="sm" label="Show sidecar files (.nfo / artwork / JRiver)" bind:checked={includeSidecars} onchange={reset} />
        <span class="flex items-center gap-1"
          title="Which parts of an item the search text matches. 'File content only' searches the extracted body/OCR text and archive member names — a filename or path match alone no longer returns the file. 'Names only' is the inverse (no body text). Filters and facets are unaffected.">
          Match in
          <span class="w-40">
            <Select size="sm" aria-label="Match in" bind:value={searchIn} onchange={reset}>
              <option value="all">everything</option>
              <option value="content">file content only</option>
              <option value="names">names only</option>
            </Select>
          </span>
        </span>
      </div>

      <!-- Permission filter (2026-08-23): who can read (agent-collected ACLs,
           perm_principals facet type-ahead) + world-readable. Items without a
           collected ACL never match a principal; the world select is tri-state. -->
      <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-muted">
        <span class="mt-1 w-10 font-medium" title="File permissions read by agents on the machine that owns the files (enable an agent's 'permissions' inventory collector). Only items with a collected ACL can match.">Access</span>
        <div class="relative w-56">
          <Input
            size="sm"
            aria-label="Readable by principal"
            placeholder="readable by principal… (name or SID)"
            role="combobox"
            aria-expanded={principalOpen}
            aria-controls="principal-suggestions"
            aria-autocomplete="list"
            bind:value={principalQuery}
            oninput={onPrincipalInput}
            onkeydown={onPrincipalKeydown}
            onfocus={() => { if (!principalSuggestions.length) fetchPrincipalSuggestions(); else principalOpen = true; }} />
          {#if principalOpen && principalSuggestions.length}
            <ul id="principal-suggestions" role="listbox"
              class="absolute z-20 mt-1 max-h-56 w-72 overflow-auto rounded-control border border-border-strong bg-surface-2 shadow-lg">
              {#each principalSuggestions as sug, i (sug.value)}
                <li role="option" aria-selected={i === principalActiveIndex}>
                  <button type="button"
                    class="flex w-full items-center justify-between gap-2 px-2 py-1 text-left {i === principalActiveIndex
                      ? 'bg-accent/15 text-accent-text'
                      : 'hover:bg-surface-3'}"
                    onmousedown={(e) => { e.preventDefault(); addPrincipal(sug.value); }}>
                    <span class="truncate">{sug.value}</span>
                    <span class="shrink-0 opacity-60">{sug.count}</span>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
        {#each selectedPrincipals as pr (pr)}
          <span class="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-1 text-accent-text">
            {pr}
            <button type="button" class="rounded-full px-1 leading-none hover:bg-accent/25"
              aria-label={`Remove principal ${pr}`} onclick={() => removePrincipal(pr)}>×</button>
          </span>
        {/each}
        <span class="flex items-center gap-1"
          title="Who can READ, as it actually takes effect: through the SMB share when the agent collected share ACLs (access = share AND file permissions), else the file's own ACL. Anonymous = no credential needed (Everyone / POSIX other); authenticated = any signed-in account (Authenticated Users) but not anonymous; restricted = named principals only.">
          Readable by
          <span class="w-48">
            <Select size="sm" aria-label="Readable by" bind:value={exposure} onchange={reset}>
              <option value="">any</option>
              <option value="anonymous">anyone, no sign-in{#if facets.perm_exposure?.anonymous != null} ({facets.perm_exposure.anonymous}){/if}</option>
              <option value="authenticated">any signed-in account{#if facets.perm_exposure?.authenticated != null} ({facets.perm_exposure.authenticated}){/if}</option>
              <option value="restricted">named principals only{#if facets.perm_exposure?.restricted != null} ({facets.perm_exposure.restricted}){/if}</option>
            </Select>
          </span>
        </span>
        <span class="mt-1"
          title="Files whose SMB share ACL and own file ACL disagree on who can read — e.g. the share grants Everyone Read but the files behind it don't (anonymous is denied), or the reverse. The share-vs-file-system reconciliation list.">
          <Checkbox size="sm" label="share ≠ file permissions" bind:checked={shareMismatch} onchange={reset} />
        </span>
      </div>

      <!-- P3-T12 tag type-ahead: typo-tolerant, count-ordered facet search over
           the tags array. Selecting a suggestion (click / Enter) adds an AND tag. -->
      <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-muted">
        <span class="mt-1 w-10 font-medium">Tags</span>
        <div class="relative w-48">
          <Input
            size="sm"
            aria-label="Add tag filter"
            placeholder="add tag…"
            role="combobox"
            aria-expanded={tagOpen}
            aria-controls="tag-suggestions"
            aria-autocomplete="list"
            bind:value={tagQuery}
            oninput={onTagInput}
            onkeydown={onTagKeydown}
            onfocus={() => { if (tagSuggestions.length) tagOpen = true; }} />
          {#if tagOpen && tagSuggestions.length}
            <ul
              id="tag-suggestions"
              role="listbox"
              class="absolute z-20 mt-1 max-h-56 w-48 overflow-auto rounded-control border border-border-strong bg-surface-2 shadow-lg">
              {#each tagSuggestions as sug, i (sug.value)}
                <li role="option" aria-selected={i === tagActiveIndex}>
                  <button
                    type="button"
                    class="flex w-full items-center justify-between gap-2 px-2 py-1 text-left {i === tagActiveIndex
                      ? 'bg-accent/15 text-accent-text'
                      : 'hover:bg-surface-3'}"
                    onmousedown={(e) => { e.preventDefault(); addTag(sug.value); }}>
                    <span class="truncate">{sug.value}</span>
                    <span class="shrink-0 opacity-60">{sug.count}</span>
                  </button>
                </li>
              {/each}
            </ul>
          {/if}
        </div>
        {#each selectedTags as t (t)}
          <span class="inline-flex items-center gap-1 rounded-full bg-accent/15 px-2 py-1 text-accent-text">
            {t}
            <button type="button" class="rounded-full px-1 leading-none hover:bg-accent/25"
              aria-label={`Remove tag ${t}`} onclick={() => removeTag(t)}>×</button>
          </span>
        {/each}
      </div>

      <!-- facetStats range sliders (P3-T4): bounds are data-derived, never hardcoded. -->
      {#if sizeBounds && sizeBounds.max > sizeBounds.min}
        <div class="mt-3 flex flex-wrap items-center gap-3 text-xs text-muted">
          <span class="w-10 font-medium">Size</span>
          <span class="w-40">
            <input
              type="range" class="w-full accent-accent" min={sizeBounds.min} max={sizeBounds.max} step="1"
              bind:value={sizeLo}
              oninput={() => { if (sizeLo > sizeHi) sizeHi = sizeLo; onSliderInput("size"); }}
              onchange={() => onRangeChange("size")} aria-label="Minimum file size" />
          </span>
          <span class="w-40">
            <input
              type="range" class="w-full accent-accent" min={sizeBounds.min} max={sizeBounds.max} step="1"
              bind:value={sizeHi}
              oninput={() => { if (sizeHi < sizeLo) sizeLo = sizeHi; onSliderInput("size"); }}
              onchange={() => onRangeChange("size")} aria-label="Maximum file size" />
          </span>
          <span class="tabular-nums">{fmtBytes(sizeLo)} – {fmtBytes(sizeHi)}</span>
        </div>
      {:else}
        <div class="mt-3 flex items-center gap-3 text-xs text-muted">
          <span class="w-10 font-medium">Size</span>
          <span>range unavailable (no matching results)</span>
        </div>
      {/if}
      {#if mtimeBounds && mtimeBounds.max > mtimeBounds.min}
        <div class="mt-2 flex flex-wrap items-center gap-3 text-xs text-muted">
          <span class="w-10 font-medium">Date</span>
          <span class="w-40">
            <input
              type="range" class="w-full accent-accent" min={mtimeBounds.min} max={mtimeBounds.max} step="1"
              bind:value={mtimeLo}
              oninput={() => { if (mtimeLo > mtimeHi) mtimeHi = mtimeLo; onSliderInput("date"); }}
              onchange={() => onRangeChange("date")} aria-label="Earliest modified date" />
          </span>
          <span class="w-40">
            <input
              type="range" class="w-full accent-accent" min={mtimeBounds.min} max={mtimeBounds.max} step="1"
              bind:value={mtimeHi}
              oninput={() => { if (mtimeHi < mtimeLo) mtimeLo = mtimeHi; onSliderInput("date"); }}
              onchange={() => onRangeChange("date")} aria-label="Latest modified date" />
          </span>
          <span class="tabular-nums">{fmtDate(mtimeLo)} – {fmtDate(mtimeHi)}</span>
        </div>
      {:else}
        <div class="mt-2 flex items-center gap-3 text-xs text-muted">
          <span class="w-10 font-medium">Date</span>
          <span>range unavailable (no matching results)</span>
        </div>
      {/if}
    </div>
  {/if}

  {#if view === "map"}
    <!-- R8-UI: same query, plotted. The map renders even before a search has run
         — its own copy explains what it is waiting for, and the GPS-gate
         explanation is worth seeing immediately. The panel owns only its
         viewport; the area it reports comes straight back into this page's param
         state. -->
    {#if error}<StateBlock state="error" class="mt-6" message={error} onRetry={() => runFresh()} />{/if}
    {#await loadMapPanel()}
      <StateBlock state="loading" />
    {:then m}
      {#if mapError}
        <!-- Never an empty map on a failed fetch: it would read as "no result
             carries coordinates". -->
        <Alert tone="danger" class="mt-4">
          <span class="flex flex-wrap items-center gap-2">
            Could not load map points.
            <Button size="sm" onclick={() => loadMapPoints()}>Retry</Button>
          </span>
        </Alert>
      {:else}
      <m.default
        points={mapPoints}
        {geoTotal}
        queryTotal={total}
        loading={mapLoading}
        box={geoBox}
        {gpsGated}
        docsUrl={DOCS_URL}
        onBoxChange={onMapBox}
        onOpenItem={(id) => (selected = id)}
      />
      {/if}
    {:catch}
      <div role="alert" class="flex flex-col items-center gap-2 py-8 text-center text-sm text-muted">
        <p>The map failed to load — Filearr may have been updated since this tab was opened.</p>
        <Button size="sm" onclick={() => location.reload()}>Reload</Button>
      </div>
    {/await}
  {:else if !searched}
    <!-- Roadmap §20 empty start: nothing has been queried yet — an honest blank
         slate instead of a match-all dump of the whole catalog. -->
    <div class="mt-16 text-center text-muted">
      <p class="text-lg">Search your library</p>
      <p class="mt-2 text-sm">
        Type above, pick a category chip, or open
        <button type="button" class="underline decoration-dotted underline-offset-2 hover:text-accent-text"
          onclick={() => { if (!savedOpen) toggleSavedOpen(); }}>Saved</button>
        /
        <button type="button" class="underline decoration-dotted underline-offset-2 hover:text-accent-text"
          onclick={() => { if (!filtersOpen) toggleFiltersOpen(); }}>Filters</button>
        — results appear once a search starts.
      </p>
    </div>
  {:else if error}
    <StateBlock state="error" class="mt-6" message={error} onRetry={() => runFresh()} />
  {:else}
    {#if hashMode}
      <p class="mt-4 text-sm text-accent-text">Exact hash match ({total} found)</p>
    {:else}
      <p class="mt-4 text-sm text-muted">{totalLabel}</p>
    {/if}

    <!-- IN-T4 selection controls. Deliberately "all LOADED", never "all
         matching": the batch API takes explicit ids, and a button that silently
         meant "every one of 1.09M results" would be a trap. -->
    {#if hits.length}
      <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-muted">
        <Button size="sm" onclick={selectAllLoaded}>Select all loaded ({hits.length})</Button>
        {#if selectedIds.size}
          <Button size="sm" onclick={clearSelection}>Clear selection</Button>
        {:else}
          <span>
            Tick a row (or press Space) to select; shift-click extends a range.
          </span>
        {/if}
      </div>
    {/if}

    {#if selectedIds.size}
      <BulkEditBar
        count={selectedIds.size}
        targets={bulkTargets}
        busy={bulkBusy}
        onApply={applyBulk}
        onClear={clearSelection} />
    {/if}

    <!-- Duplicate badges failed to load: the results stand, the badges retry. -->
    {#if copyCountsError}
      <Alert tone="warn" class="mt-2" onDismiss={() => (copyCountsError = "")}>
        <span class="flex flex-wrap items-center gap-2">
          {copyCountsError}
          <Button size="sm" onclick={retryCopyCounts}>Retry</Button>
        </span>
      </Alert>
    {/if}

    <!-- A request-level failure of the bar's Apply, right below the bar. -->
    {#if bulkError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (bulkError = "")}>{bulkError}</Alert>
    {/if}

    <!-- Per-item outcomes. RBAC path grants can deny an arbitrary subset of a
         selection, so partial failure is NORMAL here, not exceptional — the
         reasons come back per item and are shown verbatim rather than folded
         into a single "some items failed". -->
    {#if bulkSummary}
      <div class="mt-2 rounded-panel border border-border px-3 py-2 text-xs">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-muted">
            {bulkSummary.ok.length} updated
            {#if bulkSummary.failures.length}
              · <span class="text-danger">{bulkSummary.failures.length} failed</span>
            {/if}
          </span>
          {#if bulkSummary.failures.length}
            <Button
              size="sm"
              aria-expanded={bulkFailuresOpen}
              onclick={() => (bulkFailuresOpen = !bulkFailuresOpen)}
              >{bulkFailuresOpen ? "Hide" : "Show"} failures</Button>
            <Button
              size="sm"
              title="Keep only the items that failed selected, so you can retry the same change"
              onclick={() => selectFailedOnly(bulkSummary?.failures ?? [])}
              >Select only the failed</Button>
          {/if}
          <span class="grow"></span>
          <Button size="sm" variant="ghost" onclick={() => (bulkSummary = null)}>Dismiss</Button>
        </div>
        {#if bulkFailuresOpen && bulkSummary.failures.length}
          <ul class="mt-2 max-h-48 overflow-auto">
            {#each bulkSummary.failures as f (f.id)}
              <li class="flex items-start gap-2 border-t border-border py-1">
                <button
                  type="button"
                  class="shrink-0 font-mono text-2xs text-accent-text underline decoration-dotted"
                  title="Open this item"
                  onclick={() => (selected = f.id)}>{f.id.slice(0, 8)}</button>
                <span class="min-w-0 flex-1 break-words text-danger">{f.reason}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    <!-- ARIA grid (ruling 6): the container holds the one tab stop and the
         roving cursor (aria-activedescendant → the cursor row); each row is
         `aria-selected` when it is in the bulk selection, and every child sits
         in a gridcell. Keys are handled on window (onKeydown + searchKeys.ts):
         arrows move the cursor, Enter opens, Space toggles the selection,
         Cmd/Ctrl+Enter copies the path. The controls inside rows are
         tabindex=-1 (pointer only; the keys above cover them), so Tab leaves
         the grid instead of walking every row. -->
    {#if !loading && hits.length === 0}
      <StateBlock
        state="empty"
        class="mt-2"
        message={noMatchesMessage} />
    {:else if view === "grid"}
      <!-- P12 slice 2: responsive thumbnail GRID. Non-virtualized (a CSS grid of
           the already-loaded, paginated hits) with lazy <img>s (loading="lazy")
           so only on-screen thumbs fetch; infinite-scroll reuses loadMore via the
           container's scroll handler, so a huge result set still streams in. -->
      <div
        class="mt-2 grid max-h-[65vh] gap-3 overflow-auto rounded-panel border border-border p-3
               grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
        role="grid"
        aria-multiselectable="true"
        aria-busy={loading}
        tabindex={0}
        id="search-results"
        aria-label="Search results (grid)"
        aria-activedescendant={activeDescendant}
        onscroll={onGridScroll}
      >
        {#each hits as hit, index (hitId(hit))}
          <div
            id={`row-${hitId(hit)}`}
            role="row"
            tabindex={-1}
            aria-selected={selectedIds.has(hitId(hit))}
            class="group relative flex cursor-pointer flex-col overflow-hidden rounded-panel border text-left {index === selectedIndex
              ? 'border-accent ring-1 ring-accent'
              : 'border-border hover:border-border-strong'}"
            onclick={() => { selectedIndex = index; openItem(hit); }}
            onkeydown={focusGrid}
          >
            <!-- IN-T4 tile checkbox. Hidden until hover while nothing is
                 selected (the grid is a picture wall — permanent chrome on every
                 tile would compete with the thumbnails), then permanently
                 visible once a selection exists so the user can see what is in
                 it while scrolling. -->
            <div
              role="gridcell"
              class="absolute left-1.5 top-1.5 z-10 rounded-control bg-surface/85 p-0.5 shadow-sm transition-opacity
                {selectedIds.size ? '' : 'opacity-0 focus-within:opacity-100 group-hover:opacity-100'}">
              <Checkbox
                size="sm"
                class="[&>span]:sr-only"
                label={selectText(hit)}
                tabindex={-1}
                checked={selectedIds.has(hitId(hit))}
                onmousedown={(e) => { if (e.shiftKey) e.preventDefault(); }}
                onclick={(e) => { e.stopPropagation(); toggleSelect(index, e.shiftKey); }} />
            </div>
            <div role="gridcell" class="flex min-w-0 flex-col">
              <Thumb id={hitId(hit)} size="aspect-square w-full h-auto" rounded="rounded-none" />
              <div class="flex min-w-0 flex-col gap-1 p-2">
                <span class="truncate text-sm font-medium" title={str(hit, "title") || str(hit, "filename")}
                  >{str(hit, "title") || str(hit, "filename")}</span>
                <span class="flex items-center gap-1">
                  <span class="shrink-0 rounded-control bg-surface-3 px-1.5 py-0.5 text-2xs"
                    >{str(hit, "file_category")}</span>
                  {#if num(hit, "year")}<span class="text-xs text-muted">({num(hit, "year")})</span>{/if}
                  {#if copyCountMap[hitId(hit)] > 1}
                    <span class="ml-auto shrink-0 rounded-full bg-warn-bg px-1.5 py-0.5 text-2xs font-medium text-warn"
                      title="Has duplicate copies">{copyCountMap[hitId(hit)]}×</span>
                  {/if}
                </span>
              </div>
            </div>
          </div>
        {/each}
      </div>
    {:else}
    <!-- Virtualized grid. aria-activedescendant points at the cursor row; real
         DOM focus stays on the container (rows unmount). -->
    <VList
      bind:this={vlist}
      data={hits}
      getKey={(h) => hitId(h)}
      itemSize={44}
      onscroll={onScroll}
      role="grid"
      aria-multiselectable="true"
      aria-busy={loading}
      tabindex={0}
      id="search-results"
      aria-label="Search results"
      aria-activedescendant={activeDescendant}
      class="mt-2 rounded-panel border border-border"
      style="height: 65vh;"
    >
      {#snippet children(hit: Hit, index: number)}
        <!-- Rows are click-only by design: keyboard nav (arrows/Enter/Space/
             Cmd+Enter) is handled at the grid container via the roving
             aria-activedescendant pattern. -->
        <!-- FIX-4: full-width rows. Filename/title gets flex priority (flex-1,
             min-w-0) and the path column shrinks FIRST (capped basis, truncates).
             Both carry a title tooltip so a truncated value is still readable. -->
        <div
          id={`row-${hitId(hit)}`}
          role="row"
          tabindex={-1}
          aria-selected={selectedIds.has(hitId(hit))}
          class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left {index === selectedIndex
            ? 'bg-accent/10 ring-1 ring-inset ring-accent'
            : 'hover:bg-surface-3'}"
          onclick={() => { selectedIndex = index; openItem(hit); }}
          onkeydown={focusGrid}
        >
          <!-- IN-T4 row checkbox. Same show/hide rule as the grid: on hover, or
               always once a selection exists. stopPropagation keeps ticking a box
               from also opening the detail modal; the mousedown guard stops
               shift-click from smearing a text selection across rows. -->
          <div
            role="gridcell"
            class="flex shrink-0 items-center transition-opacity
              {selectedIds.size ? '' : 'opacity-0 focus-within:opacity-100 group-hover:opacity-100'}">
            <Checkbox
              size="sm"
              class="[&>span]:sr-only"
              label={selectText(hit)}
              tabindex={-1}
              checked={selectedIds.has(hitId(hit))}
              onmousedown={(e) => { if (e.shiftKey) e.preventDefault(); }}
              onclick={(e) => { e.stopPropagation(); toggleSelect(index, e.shiftKey); }} />
          </div>
          <div role="gridcell" class="flex min-w-0 flex-1 items-center gap-3">
            <Thumb id={hitId(hit)} size="h-9 w-9" />
            <span class="shrink-0 rounded-control bg-surface-3 px-2 py-0.5 text-xs"
              >{str(hit, "file_category")}</span>
            <span class="flex min-w-0 flex-1 flex-col">
              <span class="flex items-center gap-2">
                <span class="truncate font-medium" title={str(hit, "title") || str(hit, "filename")}
                  >{str(hit, "title") || str(hit, "filename")}</span>
                {#if num(hit, "year")}<span class="shrink-0 text-sm text-muted">({num(hit, "year")})</span>{/if}
                {#if copyCountMap[hitId(hit)] > 1}
                  <!-- P3-T10 duplicate badge: opens the detail (Copies section). -->
                  <button
                    type="button"
                    tabindex={-1}
                    class="shrink-0 rounded-full bg-warn-bg px-2 py-0.5 text-xs font-medium text-warn"
                    title="This file has duplicate copies — click to view them"
                    onclick={(e) => { e.stopPropagation(); selectedIndex = index; openItem(hit); }}
                  >{copyCountMap[hitId(hit)]} copies</button>
                {/if}
              </span>
              {#if str(hit, "snippet")}
                <span class="truncate text-xs text-muted">
                  {#each parseSnippet(str(hit, "snippet")) as seg}{#if seg.mark}<mark class="bg-accent/25 text-inherit">{seg.text}</mark>{:else}{seg.text}{/if}{/each}
                </span>
              {/if}
            </span>
          </div>
          <div role="gridcell" class="hidden min-w-0 shrink basis-1/3 text-right sm:block">
            <span class="block truncate text-xs text-muted" title={str(hit, "path")}>{str(hit, "path")}</span>
          </div>
          <div role="gridcell" class="shrink-0 opacity-0 group-hover:opacity-100">
            <Button
              size="sm"
              tabindex={-1}
              title="Copy path (Cmd/Ctrl+Enter)"
              onclick={(e) => { e.stopPropagation(); selectedIndex = index; copyPath(hit); }}
            >Copy path</Button>
          </div>
        </div>
      {/snippet}
    </VList>
    {/if}

    {#if loadMoreError}
      <!-- A12: the next page failed; the pages above stay, paging resumes on Retry. -->
      <StateBlock state="error" compact message={loadMoreError} onRetry={() => loadMore(true)} />
    {:else if loading}
      <StateBlock state="loading" compact />
    {/if}
  {/if}

  {#if selected}
    <!-- No pending markup: a "Loading…" line at the foot of the page would go
         unseen, and the modal appears as soon as its chunk lands. A failure
         (stale chunk after a deploy) is pinned to the viewport instead, so it
         is seen wherever the user has scrolled to. -->
    {#await loadItemDetail() then m}
      <m.default id={selected} onClose={() => (selected = null)} onOpen={(nid) => (selected = nid)} />
    {:catch}
      <div class="fixed inset-x-0 bottom-4 z-50 mx-auto w-fit shadow">
        <Alert tone="danger">
          Item details failed to load — Filearr may have been updated.
          <Button size="sm" class="ml-2" onclick={() => location.reload()}>Reload</Button>
        </Alert>
      </div>
    {/await}
  {/if}
</div>
