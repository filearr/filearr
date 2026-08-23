<script lang="ts">
  import { copyText } from "./clipboard";
  import { onMount, onDestroy } from "svelte";
  import { VList, type VListHandle } from "virtua/svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import Thumb from "./Thumb.svelte";
  import DslHelp from "./DslHelp.svelte";
  import BulkEditBar from "./BulkEditBar.svelte";
  import {
    ApiError,
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
    semanticStats,
    fileGroups as apiFileGroups,
    getTaxonomy,
    type SearchResponse,
    type Library,
    type SavedSearch,
    type TagSuggestion,
    type ItemPatchBody,
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
  import { encodeSearchHash, parseSearchHash } from "./searchparams";
  import MapPanel from "./map/MapPanel.svelte";
  import {
    MAP_POINT_CAP,
    boxParams,
    formatBox,
    geoOf,
    parseBox,
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
  // Explicit sort options (P3-T7 deep-linkable). "" = relevance; "newest" uses the
  // FIX-3 clamped mtime_sort so bogus future mtimes cannot float to the top.
  const SORTS: { value: string; label: string }[] = [
    { value: "", label: "Relevance" },
    { value: "newest", label: "Newest" },
    { value: "size:desc", label: "Largest" },
    { value: "title:asc", label: "Name (A–Z)" },
  ];
  // Top-N extensions surfaced by the type-ahead-lite (P3-T4). The facet list can
  // be long; we rank by live count and show a bounded slice.
  const EXT_LIMIT = 10;
  // A bare lowercase-hex string (16..64 chars) is an exact-hash lookup (P3-T1).
  const HASH_RE = /^[0-9a-f]{16,64}$/;

  let q = $state("");
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
  // (same localStorage pattern as the theme choice) — collapsed by default.
  let filtersOpen = $state(localStorage.getItem("searchFiltersOpen") === "1");
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

  // P3-T7 saved searches (named, persisted param bundles).
  let saved = $state<SavedSearch[]>([]);
  let savedOpen = $state(localStorage.getItem("searchSavedOpen") === "1");

  function toggleFiltersOpen() {
    filtersOpen = !filtersOpen;
    localStorage.setItem("searchFiltersOpen", filtersOpen ? "1" : "0");
  }
  function toggleSavedOpen() {
    savedOpen = !savedOpen;
    localStorage.setItem("searchSavedOpen", savedOpen ? "1" : "0");
  }

  // Roadmap §20: the page starts EMPTY — no match-all preload implying the
  // whole catalog is "a result". Flips true on the first query (typed, chip,
  // deep link, or saved search) and stays true for the session.
  let searched = $state(false);
  let savedError = $state("");
  // Range params carried through from an applied/deep-linked search until the user
  // next touches a slider (the sliders are facetStats-derived, not directly
  // restorable — see applyParams).
  let pendingRange = $state<Record<string, string>>({});
  // The last hash WE wrote, so our own reflect does not re-trigger applyParams.
  let lastWrittenHash = "";

  let hits = $state<Hit[]>([]);
  let total = $state(0);
  let facets = $state<Record<string, Record<string, number>>>({});
  let nextCursor = $state<string | null>(null);
  let error = $state("");
  let loading = $state(false);

  // Roving LOGICAL selection (P3-T3): NOT DOM focus — rows unmount under
  // virtualization, so we track an index + drive aria-activedescendant instead.
  let selectedIndex = $state(0);
  let selected = $state<string | null>(null); // open ItemDetail id
  let toast = $state<string | null>(null);
  let toastTimer: ReturnType<typeof setTimeout>;

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
  let searchInput = $state<HTMLInputElement | undefined>();
  let debounce: ReturnType<typeof setTimeout>;
  let controller: AbortController | null = null;

  // ---- accessors (hits are untyped Meili docs) ----------------------------
  const str = (h: Hit, k: string): string =>
    typeof h[k] === "string" ? (h[k] as string) : "";
  const num = (h: Hit, k: string): number | null =>
    typeof h[k] === "number" ? (h[k] as number) : null;
  const hitId = (h: Hit): string => str(h, "id");

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

  // Local mirror of AgentsPage/TaxonomyPage's helper: pull FastAPI's ``detail``
  // out of a 4xx body instead of dumping "<status>: <raw json>" at the user.
  function errDetail(e: unknown): string {
    if (e instanceof ApiError) {
      try {
        const j = JSON.parse(e.body);
        if (j?.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* body not JSON */
      }
      return e.body || String(e);
    }
    return String(e);
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
        flash("Nothing to change — every selected item already matches.");
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
      flash(
        sum.failures.length
          ? `${sum.ok.length} updated, ${sum.failures.length} failed — search index updates shortly.`
          : `${sum.ok.length} updated — search index updates shortly.`,
      );
    } catch (e) {
      // A request-level failure (auth, 413, network). Per-ITEM failures never
      // land here — they come back inside a 200 and are shown in the list.
      bulkError = errDetail(e);
    } finally {
      bulkBusy = false;
    }
  }

  // ---- query params -------------------------------------------------------
  function buildParams(): Record<string, string> {
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

  // The current, non-empty param bundle — what a saved search stores and what the
  // URL hash reflects (deep-linkable). Identical shape to the backend /search
  // params, so save -> apply -> /search replays byte-identically server-side.
  function currentParams(): Record<string, string> {
    return Object.fromEntries(
      Object.entries(buildParams()).filter(([, v]) => v !== ""),
    );
  }

  // Reflect the current params into the location hash (#/search?...). Guarded
  // to SEARCH routes only: a late async continuation (or a hashchange handled
  // while unmount is pending) must never clobber a navigation to another tab —
  // writing "#/search" over "#/jobs" is exactly the snap-back bug this fixes
  // (live 2026-08-04).
  function reflectHash() {
    if (location.hash && !location.hash.startsWith("#/search")) return;
    const h = encodeSearchHash(currentParams());
    if (h === location.hash) return;
    lastWrittenHash = h;
    location.hash = h;
  }

  // Toggle list/grid. A layout change needs no re-query -- just re-render the
  // already-loaded hits and reflect the new ``view`` into the URL hash so the
  // choice is deep-linkable / saved-search-persisted.
  function setView(v: "list" | "grid" | "map") {
    if (view === v) return;
    view = v;
    reflectHash();
    // The map needs its own bounded, geo-filtered fetch (the results page is
    // ordered by relevance and capped at a screenful) — pull it on first open.
    if (v === "map" && searched) loadMapPoints();
  }

  // Apply a flat param bundle (deep link, back/forward, or a saved search) into
  // the UI state and re-query. Range params ride pendingRange until a slider moves.
  function applyParams(p: Record<string, string>) {
    q = p.hash ?? p.q ?? "";
    extension = p.extension ?? "";
    selectedCategories = p.file_category
      ? p.file_category.split(",").map((t) => t.trim()).filter(Boolean)
      : [];
    selectedGroups = p.file_group
      ? p.file_group.split(",").map((t) => t.trim()).filter(Boolean)
      : [];
    selectedLibraries = p.library
      ? p.library.split(",").map((t) => t.trim()).filter(Boolean)
      : [];
    selectedPrincipals = p.principal
      ? p.principal.split(",").map((t) => t.trim()).filter(Boolean)
      : [];
    worldReadable = p.world_readable === "true" ? "true" : p.world_readable === "false" ? "false" : "";
    selectedTags = p.tags ? p.tags.split(",").map((t) => t.trim()).filter(Boolean) : [];
    tagQuery = "";
    tagOpen = false;
    includeSidecars = p.include_sidecars === "true";
    searchIn = (p.search_in as "content" | "names") || "all";
    sortMode = p.sort ?? "";
    view = p.view === "grid" || p.view === "map" ? p.view : "list";
    // A deep link / saved search carrying all four geo edges restores the drawn
    // area; a partial one restores nothing (parseBox is all-or-nothing) so we can
    // never send half a box.
    geoBox = parseBox(p);
    semantic = p.semantic ? Number(p.semantic) : 0;
    pendingRange = {};
    for (const k of ["size_gte", "size_lte", "mtime_gte", "mtime_lte"]) {
      if (p[k]) pendingRange[k] = p[k];
    }
    // Deactivate the sliders so pendingRange drives the filter until one moves.
    if (sizeBounds) { sizeLo = sizeBounds.min; sizeHi = sizeBounds.max; }
    if (mtimeBounds) { mtimeLo = mtimeBounds.min; mtimeHi = mtimeBounds.max; }
    // Auto-run ONLY when the params actually FILTER something. Display-only
    // leftovers (view / sort / semantic ride the hash after any search) used
    // to count as "params present" and re-ran a match-all on the next visit —
    // results appearing before any query, the exact roadmap-§20 blank-slate
    // regression (live report 2026-08-08). Restoring the display prefs while
    // staying blank is correct for those.
    const displayOnly = new Set(["view", "sort", "semantic"]);
    const searchable = Object.entries(p).some(
      ([k, v]) => !displayOnly.has(k) && v !== "",
    );
    if (searchable) runFresh();
  }

  function onHashChange() {
    // Only search-route hashes are ours. On a back/forward to another tab this
    // listener still fires once before the unmount flush; treating "#/jobs" as
    // empty params re-ran a search and wrote "#/search" back — reverting the
    // user's navigation.
    if (!location.hash.startsWith("#/search")) return;
    if (location.hash === lastWrittenHash) return; // our own write — ignore
    applyParams(parseSearchHash(location.hash));
  }

  // A manual slider interaction takes over from any applied/deep-linked range.
  function onRangeChange() {
    pendingRange = {};
    reset();
  }

  // ---- saved searches (P3-T7) --------------------------------------------
  async function loadSaved() {
    try {
      saved = await listSavedSearches();
    } catch {
      // Non-fatal: the panel just shows nothing (e.g. read scope missing).
    }
  }

  async function saveCurrent() {
    const name = window.prompt("Name this search:");
    if (!name?.trim()) return;
    savedError = "";
    try {
      await createSavedSearch({ name: name.trim(), params: currentParams() });
      await loadSaved();
    } catch (e) {
      savedError = String(e);
    }
  }

  function applySaved(ss: SavedSearch) {
    applyParams(ss.params ?? {});
  }

  async function renameSaved(ss: SavedSearch) {
    const name = window.prompt("Rename search:", ss.name);
    if (!name?.trim() || name.trim() === ss.name) return;
    savedError = "";
    try {
      await updateSavedSearch(ss.id, { name: name.trim() });
      await loadSaved();
    } catch (e) {
      savedError = String(e);
    }
  }

  async function removeSaved(ss: SavedSearch) {
    if (!window.confirm(`Delete saved search "${ss.name}"?`)) return;
    savedError = "";
    try {
      await deleteSavedSearch(ss.id);
      await loadSaved();
    } catch (e) {
      savedError = String(e);
    }
  }

  function applyStats(r: SearchResponse) {
    // Update slider bounds from facetStats ONLY while that facet isn't being
    // narrowed — otherwise an active filter would shrink its own track.
    const ss = r.facet_stats?.size;
    if (ss && !sizeActive) {
      sizeBounds = { min: ss.min, max: ss.max };
      sizeLo = ss.min;
      sizeHi = ss.max;
    }
    const ms = r.facet_stats?.mtime;
    if (ms && !mtimeActive) {
      mtimeBounds = { min: ms.min, max: ms.max };
      mtimeLo = ms.min;
      mtimeHi = ms.max;
    }
  }

  // ---- fetching -----------------------------------------------------------
  async function runFresh() {
    // Cancel any in-flight request so a stale response can never overwrite a
    // newer one (P3-T3 AbortController on the 150 ms debounce).
    controller?.abort();
    const ctrl = new AbortController();
    controller = ctrl;
    searched = true;
    error = "";
    loading = true;
    // IN-T4: a NEW query means a new result set — carrying a selection across it
    // would let an invisible, unrelated item ride along into the next bulk edit.
    // (Paging the SAME query via loadMore() deliberately keeps the selection.)
    clearSelection();
    bulkSummary = null;
    bulkError = "";
    reflectHash(); // keep the URL in sync with the query being run (deep-linkable)
    try {
      const r = await search(buildParams(), ctrl.signal);
      if (ctrl.signal.aborted) return;
      hits = r.hits;
      total = r.total;
      facets = r.facets;
      nextCursor = r.next_cursor;
      selectedIndex = 0;
      applyStats(r);
      copyCountMap = {};
      refreshCopyCounts(r.hits); // P3-T10 batch badge counts
      vlist?.scrollTo(0);
      if (view === "map") loadMapPoints(); // keep the map on the same query
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return; // superseded — ignore
      error = String(e);
    } finally {
      if (controller === ctrl) loading = false;
    }
  }

  async function loadMore() {
    if (loading || !nextCursor || !controller) return;
    const ctrl = controller; // reuse the session controller: a new search aborts
    loading = true;
    try {
      const params = buildParams();
      params.cursor = nextCursor;
      const r = await search(params, ctrl.signal);
      if (ctrl.signal.aborted || controller !== ctrl) return;
      hits = [...hits, ...r.hits];
      nextCursor = r.next_cursor;
      refreshCopyCounts(r.hits); // badge the newly-appended rows too
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return;
      error = String(e);
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
    try {
      const base = buildParams();
      base.limit = String(MAP_PAGE);
      if (!geoBox) {
        base.geo_top_lat = "90";
        base.geo_bottom_lat = "-90";
        base.geo_left_lng = "-180";
        base.geo_right_lng = "180";
      }
      const acc: MapPoint[] = [];
      let cursor: string | null = null;
      let seen = 0;
      do {
        const r = await search(cursor ? { ...base, cursor } : base, ctrl.signal);
        if (ctrl.signal.aborted || mapController !== ctrl) return;
        geoTotal = r.total;
        for (const h of r.hits) {
          const g = geoOf(h);
          const id = str(h, "id");
          if (!g || !id) continue;
          acc.push({
            id,
            lat: g.lat,
            lng: g.lng,
            label: str(h, "title") || str(h, "filename") || str(h, "rel_path") || id,
          });
        }
        seen += r.hits.length;
        cursor = r.next_cursor;
      } while (cursor && seen < MAP_POINT_CAP);
      mapPoints = acc.slice(0, MAP_POINT_CAP);
    } catch (e) {
      if ((e as Error)?.name === "AbortError") return; // superseded — ignore
      // Non-fatal: the map shows its empty state rather than replacing the
      // results list with an error.
      mapPoints = [];
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

  // P3-T10: fetch copy counts for a batch of hits (ids only, cap 200) and merge
  // into the badge map. One /items/copy-counts call — only groups with count > 1
  // come back, so a badge appears only for genuine duplicates. Best-effort.
  async function refreshCopyCounts(batch: Hit[]) {
    const ids = batch.map((h) => hitId(h)).filter(Boolean).slice(0, 200);
    if (!ids.length) return;
    try {
      const counts = await apiCopyCounts(ids);
      copyCountMap = { ...copyCountMap, ...counts };
    } catch {
      // Non-fatal: no badges rather than a broken results list.
    }
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
      // Scope tag suggestions to the first selected category when present (the
      // backend's tag facet scope accepts a single coarse type).
      const scope = selectedCategories.length ? { type: selectedCategories[0] } : {};
      const res = await searchTags(tagQuery, scope, ctrl.signal);
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

  function onInput() {
    clearTimeout(debounce);
    debounce = setTimeout(runFresh, 150);
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
    pendingRange = { ...pendingRange, size_gte: "", size_lte: "" };
    reset();
  }
  function snapMtime() {
    if (mtimeBounds) {
      mtimeLo = mtimeBounds.min;
      mtimeHi = mtimeBounds.max;
    }
    pendingRange = { ...pendingRange, mtime_gte: "", mtime_lte: "" };
    reset();
  }

  // Active filters rendered as removable chips (P3-T4). Each carries a clearer.
  type Chip = { key: string; label: string; clear: () => void };
  const activeFilters = $derived.by<Chip[]>(() => {
    const out: Chip[] = [];
    for (const c of selectedCategories) out.push({ key: `fc:${c}`, label: `category: ${categoryLabel(c)}`, clear: () => toggleCategory(c) });
    if (extension) out.push({ key: "ext", label: `ext: ${extension}`, clear: () => { extension = ""; reset(); } });
    for (const g of selectedGroups) out.push({ key: `fg:${g}`, label: `group: ${groupLabel(g)}`, clear: () => toggleGroup(g) });
    for (const l of selectedLibraries) out.push({ key: `lib:${l}`, label: `library: ${libraryLabel(l)}`, clear: () => toggleLibrary(l) });
    for (const pr of selectedPrincipals) out.push({ key: `pr:${pr}`, label: `readable by: ${pr}`, clear: () => removePrincipal(pr) });
    if (worldReadable) out.push({ key: "world", label: worldReadable === "true" ? "world-readable" : "not world-readable", clear: () => { worldReadable = ""; reset(); } });
    for (const t of selectedTags) out.push({ key: `tag:${t}`, label: `tag: ${t}`, clear: () => removeTag(t) });
    if (includeSidecars) out.push({ key: "sidecar", label: "sidecars shown", clear: () => { includeSidecars = false; reset(); } });
    // R8-UI: the map's area reads as an ordinary removable filter, so it can be
    // cleared from the chip row without opening the map.
    if (geoBox) out.push({ key: "geo", label: `area ${formatBox(geoBox)}`, clear: () => onMapBox(null) });
    if (sizeActive) out.push({ key: "size", label: `size ${fmtBytes(sizeLo)}–${fmtBytes(sizeHi)}`, clear: snapSize });
    if (mtimeActive) out.push({ key: "mtime", label: `date ${fmtDate(mtimeLo)}–${fmtDate(mtimeHi)}`, clear: snapMtime });
    return out;
  });

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
    if (selectedIndex >= n - 8) loadMore();
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
    try {
      await copyText(p);
      flash(`Copied ${p}`);
    } catch {
      flash("Copy failed (clipboard blocked)");
    }
  }

  function flash(msg: string) {
    toast = msg;
    clearTimeout(toastTimer);
    toastTimer = setTimeout(() => (toast = null), 2500);
  }

  function onKeydown(e: KeyboardEvent) {
    const editable = inEditable(document.activeElement);
    // '/' or Cmd/Ctrl+K → focus search (guarded: '/' only when not already
    // typing; the shortcut alias works from anywhere).
    if ((e.key === "/" && !editable) || ((e.metaKey || e.ctrlKey) && (e.key === "k" || e.key === "K"))) {
      e.preventDefault();
      searchInput?.focus();
      searchInput?.select();
      return;
    }
    if (selected) return; // ItemDetail modal owns keys while open
    // R8-UI: on the map, arrows pan. The map's own handler stops propagation
    // while it has focus; this keeps the arrows from silently walking a results
    // list nobody can see when it does not.
    if (view === "map") return;
    if (!hits.length) return;
    if (e.key === "ArrowDown") {
      e.preventDefault();
      moveSel(1);
    } else if (e.key === "ArrowUp") {
      e.preventDefault();
      moveSel(-1);
    } else if (e.key === "Enter") {
      e.preventDefault();
      if (e.metaKey || e.ctrlKey) copyPath(hits[selectedIndex]);
      else openItem(hits[selectedIndex]);
    } else if (e.key === " ") {
      // IN-T4: Space toggles the roving row's SELECTION (shift extends). The
      // keyboard path had no way to build a selection otherwise, and Space was
      // previously unbound here (its default is to page-scroll the listbox,
      // which arrow navigation already does better).
      e.preventDefault();
      toggleSelect(selectedIndex, e.shiftKey);
    }
  }

  function onScroll() {
    if (!vlist || !nextCursor || loading) return;
    const end = vlist.getScrollOffset() + vlist.getViewportSize();
    if (end >= vlist.getScrollSize() - 400) loadMore();
  }

  // Grid infinite-scroll: the grid is a plain scroll container (not virtua), so
  // near-bottom drives the same cursor-based loadMore the list uses.
  function onGridScroll(e: Event) {
    if (!nextCursor || loading) return;
    const el = e.currentTarget as HTMLElement;
    if (el.scrollTop + el.clientHeight >= el.scrollHeight - 400) loadMore();
  }

  // ---- formatting ---------------------------------------------------------
  function fmtBytes(b: number): string {
    if (!isFinite(b) || b <= 0) return "0 B";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  function fmtDate(epoch: number): string {
    if (!isFinite(epoch) || epoch <= 0) return "—";
    return new Date(epoch * 1000).toISOString().slice(0, 10);
  }
  const activeDescendant = $derived(
    hits.length && hits[selectedIndex] ? `opt-${hitId(hits[selectedIndex])}` : undefined,
  );

  // ---- lifecycle ----------------------------------------------------------
  onMount(async () => {
    // Listener registration MUST precede the awaits below. It used to sit
    // after them, so switching tabs during the fetch window destroyed the
    // component before registration — onDestroy's removeEventListener was a
    // no-op and the continuation then LEAKED a listener on a dead component,
    // which re-wrote "#/search" over every later navigation until a hard
    // refresh (live 2026-08-04: tabs "snapped back" intermittently).
    window.addEventListener("hashchange", onHashChange);
    try {
      const list = await listLibraries();
      libs = new Map(list.map((l) => [l.id, l]));
    } catch {
      // Non-fatal: copy-path just falls back to the container path.
    }
    loadSaved();
    // W8: category + group vocabularies come from the taxonomy tree (source of
    // truth). Prefer it; if it's offline, fall back to /system/file-groups for the
    // groups and a small static category list so the filters still render.
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
    // P3-T8: learn whether semantic search is enabled so the slider can show.
    try {
      const sem = await semanticStats();
      semanticEnabled = !!sem?.enabled;
    } catch {
      semanticEnabled = false;
    }
    // Deep-link entry: restore state from #/search?... then query. A BARE visit
    // stays empty (roadmap §20) — no match-all preload; results render only
    // once a search actually starts. Skipped entirely if the user has already
    // navigated away while the fetches above were in flight (destroyed) or the
    // hash is no longer a search route.
    if (destroyed || !location.hash.startsWith("#/search")) return;
    const initial = parseSearchHash(location.hash);
    if (Object.keys(initial).length) applyParams(initial);
  });

  let destroyed = false;
  onDestroy(() => {
    destroyed = true;
    controller?.abort();
    mapController?.abort();
    tagController?.abort();
    clearTimeout(debounce);
    clearTimeout(tagDebounce);
    clearTimeout(toastTimer);
    window.removeEventListener("hashchange", onHashChange);
  });
</script>

<svelte:window onkeydown={onKeydown} />

<div>
  <input
    bind:this={searchInput}
    class="w-full rounded-xl border border-slate-300 bg-transparent px-4 py-3 text-lg outline-none
           focus:border-[var(--accent)] dark:border-slate-700"
    placeholder="Search your library…  (press / to focus)"
    bind:value={q}
    oninput={onInput}
  />

  <!-- W8 file_category chips (coarse, MULTI-select) with live facet counts. The
       primary type filter row; the granular file_group row sits below it. -->
  <div class="mt-3 flex flex-wrap items-center gap-2">
    <button
      class="rounded-full border px-3 py-1 text-sm {selectedCategories.length === 0
        ? 'border-transparent bg-[var(--accent)] text-white'
        : 'border-slate-300 dark:border-slate-700'}"
      onclick={() => { selectedCategories = []; reset(); }}>all</button>
    {#each categoryOptions as c (c.key)}
      {@const count = facets.file_category?.[c.key]}
      {@const on = selectedCategories.includes(c.key)}
      <button
        class="rounded-full border px-3 py-1 text-sm {on
          ? 'border-transparent bg-[var(--accent)] text-white'
          : 'border-slate-300 dark:border-slate-700'} {count === 0 && !on
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
      <label class="flex items-center gap-1 text-sm text-slate-500" title="Blend keyword and meaning-based (semantic) matching">
        Semantic
        <input
          type="range"
          min="0"
          max="1"
          step="0.1"
          bind:value={semantic}
          onchange={reset}
          class="w-24 accent-[var(--accent)]"
          aria-label="Semantic blend ratio" />
        <span class="w-8 tabular-nums">{semantic.toFixed(1)}</span>
      </label>
    {/if}
    <!-- Sort (P3-T7 deep-linkable). -->
    <label class="flex items-center gap-1 text-sm text-slate-500">
      Sort
      <select
        class="rounded-full border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
        bind:value={sortMode}
        onchange={reset}
        aria-label="Sort results">
        {#each SORTS as s (s.value)}<option value={s.value}>{s.label}</option>{/each}
      </select>
    </label>
    <!-- P12 slice 2: list / grid layout toggle. -->
    <div class="flex items-center overflow-hidden rounded-full border border-slate-300 dark:border-slate-700" role="group" aria-label="Result layout">
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'list' ? 'bg-[var(--accent)] text-white' : ''}"
        aria-pressed={view === 'list'}
        title="List view"
        onclick={() => setView('list')}>List</button>
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'grid' ? 'bg-[var(--accent)] text-white' : ''}"
        aria-pressed={view === 'grid'}
        title="Grid view"
        onclick={() => setView('grid')}>Grid</button>
      <!-- R8-UI: the map is a third view of the SAME query, not a separate page. -->
      <button
        type="button"
        class="px-3 py-1 text-sm {view === 'map' ? 'bg-[var(--accent)] text-white' : ''}"
        aria-pressed={view === 'map'}
        title="Map view — plot the results that carry GPS coordinates, and drag an area to filter by it"
        onclick={() => setView('map')}>Map</button>
    </div>
    <!-- Saved searches (P3-T7). Roadmap §20: the open state reads as ACTIVE
         (accent fill, matching the chip convention) — the chevron alone was
         not a legible state indicator. Open/closed persists per browser. -->
    <button
      class="rounded-full border px-3 py-1 text-sm {savedOpen
        ? 'border-transparent bg-[var(--accent)] text-white'
        : 'border-slate-300 dark:border-slate-700'}"
      aria-expanded={savedOpen}
      title={savedOpen ? "Hide the saved-searches panel" : "Show saved searches"}
      onclick={toggleSavedOpen}>Saved {savedOpen ? "▲" : "▾"}</button>
    <button
      class="rounded-full border px-3 py-1 text-sm {filtersOpen
        ? 'border-transparent bg-[var(--accent)] text-white'
        : 'border-slate-300 dark:border-slate-700'}"
      aria-expanded={filtersOpen}
      title={filtersOpen ? "Hide the filters panel" : "Show filters (group, extension, tags, ranges)"}
      onclick={toggleFiltersOpen}>Filters {filtersOpen ? "▲" : "▾"}</button>
  </div>

  <!-- FIX-12 (Item B): expandable filter-DSL help; chips insert into the box. -->
  <DslHelp onInsert={insertDsl} context="search" />

  <!-- Saved searches panel (P3-T7): save current, apply/rename/delete. -->
  {#if savedOpen}
    <div class="mt-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <div class="flex items-center gap-2">
        <button
          class="rounded-full bg-[var(--accent)] px-3 py-1 text-sm text-white"
          onclick={saveCurrent}>Save current search</button>
        {#if savedError}<span class="text-xs text-red-500">{savedError}</span>{/if}
      </div>
      {#if saved.length}
        <ul class="mt-3 flex flex-col gap-1">
          {#each saved as ss (ss.id)}
            <li class="flex items-center gap-2 text-sm">
              <button
                class="min-w-0 flex-1 truncate rounded px-2 py-1 text-left hover:bg-slate-100 dark:hover:bg-slate-800"
                title={`Apply "${ss.name}"`}
                onclick={() => applySaved(ss)}>{ss.name}</button>
              <button
                class="rounded border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700"
                onclick={() => renameSaved(ss)}>Rename</button>
              <button
                class="rounded border border-slate-300 px-2 py-0.5 text-xs text-red-500 dark:border-slate-700"
                onclick={() => removeSaved(ss)}>Delete</button>
            </li>
          {/each}
        </ul>
      {:else}
        <p class="mt-2 text-xs text-slate-500">No saved searches yet.</p>
      {/if}
    </div>
  {/if}

  <!-- Active filters as removable chips (P3-T4). -->
  {#if activeFilters.length}
    <div class="mt-2 flex flex-wrap items-center gap-2 text-xs">
      {#each activeFilters as chip (chip.key)}
        <span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/15 px-2 py-1 text-[var(--accent)]">
          {chip.label}
          <button
            type="button"
            class="rounded-full px-1 leading-none hover:bg-[var(--accent)]/25"
            aria-label={`Remove filter ${chip.label}`}
            onclick={chip.clear}>×</button>
        </span>
      {/each}
    </div>
  {/if}

  {#if filtersOpen}
    <div class="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <!-- Library / machine filter (MULTI-select, OR). Central libraries first,
           then one group per agent with a whole-machine toggle; counts come
           from the library_id facet when the backend surfaces it. Hidden on a
           single-library catalog (nothing to choose). -->
      {#if libs.size > 1}
        <div class="mb-3 flex flex-wrap items-start gap-2">
          <span class="mt-1 w-10 text-xs font-medium text-slate-500">Library</span>
          <div class="flex flex-wrap items-center gap-2">
            {#each libraryGroups as grp (grp.agent ?? "central")}
              {#if grp.agent}
                {@const ids = grp.libs.map((l) => l.id)}
                {@const allOn = ids.every((id) => selectedLibraries.includes(id))}
                <button
                  class="rounded-full border border-dashed px-2 py-0.5 text-xs {allOn
                    ? 'border-transparent bg-[var(--accent)] text-white'
                    : 'border-slate-400 dark:border-slate-600'}"
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
                  class="rounded-full border px-3 py-1 text-sm {on
                    ? 'border-transparent bg-[var(--accent)] text-white'
                    : 'border-slate-300 dark:border-slate-700'} {count === 0 && !on
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
          <span class="w-10 text-xs font-medium text-slate-500">Group</span>
          {#each fileGroupOptions as g (g.key)}
            {@const count = facets.file_group?.[g.key]}
            {@const on = selectedGroups.includes(g.key)}
            <button
              class="rounded-full border px-3 py-1 text-sm {on
                ? 'border-transparent bg-[var(--accent)] text-white'
                : 'border-slate-300 dark:border-slate-700'} {count === 0 && !on
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
      <div class="flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <span class="w-10 font-medium">Type</span>
        <input
          class="w-32 rounded border border-slate-300 bg-transparent px-2 py-1 text-xs outline-none
                 focus:border-[var(--accent)] dark:border-slate-700"
          placeholder="ext…"
          bind:value={extQuery} />
        {#each topExtensions as [ext, count] (ext)}
          <button
            class="rounded-full border px-2 py-0.5 {extension === ext
              ? 'border-transparent bg-[var(--accent)] text-white'
              : 'border-slate-300 dark:border-slate-700'}"
            onclick={() => toggleExtension(ext)}>
            {ext}<span class="ml-1 opacity-70">{count}</span>
          </button>
        {/each}
        {#if !topExtensions.length}<span class="opacity-60">no extension facets</span>{/if}
      </div>

      <!-- Sidecar toggle (T3 sidecars hidden by default). -->
      <div class="mt-3 flex flex-wrap items-center gap-4">
        <label class="flex items-center gap-2 text-xs text-slate-500">
          <input type="checkbox" bind:checked={includeSidecars} onchange={reset} />
          Show sidecar files (.nfo / artwork / JRiver)
        </label>
        <label class="flex items-center gap-1 text-xs text-slate-500"
          title="Which parts of an item the search text matches. 'File content only' searches the extracted body/OCR text and archive member names — a filename or path match alone no longer returns the file. 'Names only' is the inverse (no body text). Filters and facets are unaffected.">
          Match in
          <select class="rounded border border-slate-300 bg-transparent px-1 py-0.5 dark:border-slate-700 dark:bg-slate-800"
            bind:value={searchIn} onchange={reset}>
            <option value="all">everything</option>
            <option value="content">file content only</option>
            <option value="names">names only</option>
          </select>
        </label>
      </div>

      <!-- Permission filter (2026-08-23): who can read (agent-collected ACLs,
           perm_principals facet type-ahead) + world-readable. Items without a
           collected ACL never match a principal; the world select is tri-state. -->
      <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-slate-500">
        <span class="mt-1 w-10 font-medium" title="Agent-collected file permissions (W7). Only items whose ACL was collected can match.">Access</span>
        <div class="relative">
          <input
            class="w-56 rounded border border-slate-300 bg-transparent px-2 py-1 text-xs outline-none
                   focus:border-[var(--accent)] dark:border-slate-700"
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
              class="absolute z-20 mt-1 max-h-56 w-72 overflow-auto rounded border border-slate-300 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
              {#each principalSuggestions as sug, i (sug.value)}
                <li role="option" aria-selected={i === principalActiveIndex}>
                  <button type="button"
                    class="flex w-full items-center justify-between gap-2 px-2 py-1 text-left {i === principalActiveIndex
                      ? 'bg-[var(--accent)]/15 text-[var(--accent)]'
                      : 'hover:bg-slate-100 dark:hover:bg-slate-800'}"
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
          <span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/15 px-2 py-1 text-[var(--accent)]">
            {pr}
            <button type="button" class="rounded-full px-1 leading-none hover:bg-[var(--accent)]/25"
              aria-label={`Remove principal ${pr}`} onclick={() => removePrincipal(pr)}>×</button>
          </span>
        {/each}
        <label class="flex items-center gap-1">
          World
          <select class="rounded border border-slate-300 bg-transparent px-1 py-0.5 dark:border-slate-700 dark:bg-slate-800"
            bind:value={worldReadable} onchange={reset}>
            <option value="">any</option>
            <option value="true">readable by everyone{#if facets.perm_world?.true != null} ({facets.perm_world.true}){/if}</option>
            <option value="false">not world-readable</option>
          </select>
        </label>
      </div>

      <!-- P3-T12 tag type-ahead: typo-tolerant, count-ordered facet search over
           the tags array. Selecting a suggestion (click / Enter) adds an AND tag. -->
      <div class="mt-3 flex flex-wrap items-start gap-2 text-xs text-slate-500">
        <span class="mt-1 w-10 font-medium">Tags</span>
        <div class="relative">
          <input
            class="w-48 rounded border border-slate-300 bg-transparent px-2 py-1 text-xs outline-none
                   focus:border-[var(--accent)] dark:border-slate-700"
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
              class="absolute z-20 mt-1 max-h-56 w-48 overflow-auto rounded border border-slate-300 bg-white shadow-lg dark:border-slate-700 dark:bg-slate-900">
              {#each tagSuggestions as sug, i (sug.value)}
                <li role="option" aria-selected={i === tagActiveIndex}>
                  <button
                    type="button"
                    class="flex w-full items-center justify-between gap-2 px-2 py-1 text-left {i === tagActiveIndex
                      ? 'bg-[var(--accent)]/15 text-[var(--accent)]'
                      : 'hover:bg-slate-100 dark:hover:bg-slate-800'}"
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
          <span class="inline-flex items-center gap-1 rounded-full bg-[var(--accent)]/15 px-2 py-1 text-[var(--accent)]">
            {t}
            <button type="button" class="rounded-full px-1 leading-none hover:bg-[var(--accent)]/25"
              aria-label={`Remove tag ${t}`} onclick={() => removeTag(t)}>×</button>
          </span>
        {/each}
      </div>

      <!-- facetStats range sliders (P3-T4): bounds are data-derived, never hardcoded. -->
      {#if sizeBounds && sizeBounds.max > sizeBounds.min}
        <div class="mt-3 flex flex-wrap items-center gap-3 text-xs text-slate-500">
          <span class="w-10 font-medium">Size</span>
          <input
            type="range" class="w-40" min={sizeBounds.min} max={sizeBounds.max} step="1"
            bind:value={sizeLo}
            oninput={() => { if (sizeLo > sizeHi) sizeHi = sizeLo; }}
            onchange={onRangeChange} aria-label="Minimum file size" />
          <input
            type="range" class="w-40" min={sizeBounds.min} max={sizeBounds.max} step="1"
            bind:value={sizeHi}
            oninput={() => { if (sizeHi < sizeLo) sizeLo = sizeHi; }}
            onchange={onRangeChange} aria-label="Maximum file size" />
          <span class="tabular-nums">{fmtBytes(sizeLo)} – {fmtBytes(sizeHi)}</span>
        </div>
      {:else}
        <div class="mt-3 flex items-center gap-3 text-xs text-slate-400">
          <span class="w-10 font-medium">Size</span>
          <span>range unavailable (no matching results)</span>
        </div>
      {/if}
      {#if mtimeBounds && mtimeBounds.max > mtimeBounds.min}
        <div class="mt-2 flex flex-wrap items-center gap-3 text-xs text-slate-500">
          <span class="w-10 font-medium">Date</span>
          <input
            type="range" class="w-40" min={mtimeBounds.min} max={mtimeBounds.max} step="1"
            bind:value={mtimeLo}
            oninput={() => { if (mtimeLo > mtimeHi) mtimeHi = mtimeLo; }}
            onchange={onRangeChange} aria-label="Earliest modified date" />
          <input
            type="range" class="w-40" min={mtimeBounds.min} max={mtimeBounds.max} step="1"
            bind:value={mtimeHi}
            oninput={() => { if (mtimeHi < mtimeLo) mtimeLo = mtimeHi; }}
            onchange={onRangeChange} aria-label="Latest modified date" />
          <span class="tabular-nums">{fmtDate(mtimeLo)} – {fmtDate(mtimeHi)}</span>
        </div>
      {:else}
        <div class="mt-2 flex items-center gap-3 text-xs text-slate-400">
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
    {#if error}<p class="mt-6 text-red-500">{error}</p>{/if}
    <MapPanel
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
  {:else if !searched}
    <!-- Roadmap §20 empty start: nothing has been queried yet — an honest blank
         slate instead of a match-all dump of the whole catalog. -->
    <div class="mt-16 text-center text-slate-500">
      <p class="text-lg">Search your library</p>
      <p class="mt-2 text-sm">
        Type above, pick a category chip, or open
        <button type="button" class="underline decoration-dotted underline-offset-2 hover:text-[var(--accent)]"
          onclick={() => { if (!savedOpen) toggleSavedOpen(); }}>Saved</button>
        /
        <button type="button" class="underline decoration-dotted underline-offset-2 hover:text-[var(--accent)]"
          onclick={() => { if (!filtersOpen) toggleFiltersOpen(); }}>Filters</button>
        — results appear once a search starts.
      </p>
    </div>
  {:else if error}
    <p class="mt-6 text-red-500">{error}</p>
  {:else}
    {#if hashMode}
      <p class="mt-4 text-sm text-[var(--accent)]">Exact hash match ({total} found)</p>
    {:else}
      <p class="mt-4 text-sm text-slate-500">{total} results</p>
    {/if}

    <!-- IN-T4 selection controls. Deliberately "all LOADED", never "all
         matching": the batch API takes explicit ids, and a button that silently
         meant "every one of 1.09M results" would be a trap. -->
    {#if hits.length}
      <div class="mt-2 flex flex-wrap items-center gap-2 text-xs text-slate-500">
        <button
          type="button"
          class="rounded-full border border-slate-300 px-3 py-1 dark:border-slate-700"
          onclick={selectAllLoaded}>Select all loaded ({hits.length})</button>
        {#if selectedIds.size}
          <button
            type="button"
            class="rounded-full border border-slate-300 px-3 py-1 dark:border-slate-700"
            onclick={clearSelection}>Clear selection</button>
        {:else}
          <span class="opacity-70">
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

    {#if bulkError}
      <p class="mt-2 rounded-lg bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-200">
        {bulkError}
      </p>
    {/if}

    <!-- Per-item outcomes. RBAC path grants can deny an arbitrary subset of a
         selection, so partial failure is NORMAL here, not exceptional — the
         reasons come back per item and are shown verbatim rather than folded
         into a single "some items failed". -->
    {#if bulkSummary}
      <div class="mt-2 rounded-lg border border-slate-200 px-3 py-2 text-xs dark:border-slate-800">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-slate-600 dark:text-slate-300">
            {bulkSummary.ok.length} updated
            {#if bulkSummary.failures.length}
              · <span class="text-red-500">{bulkSummary.failures.length} failed</span>
            {/if}
          </span>
          {#if bulkSummary.failures.length}
            <button
              type="button"
              class="rounded border border-slate-300 px-2 py-0.5 dark:border-slate-700"
              aria-expanded={bulkFailuresOpen}
              onclick={() => (bulkFailuresOpen = !bulkFailuresOpen)}
              >{bulkFailuresOpen ? "Hide" : "Show"} failures</button>
            <button
              type="button"
              class="rounded border border-slate-300 px-2 py-0.5 dark:border-slate-700"
              title="Keep only the items that failed selected, so you can retry the same change"
              onclick={() => selectFailedOnly(bulkSummary?.failures ?? [])}
              >Select only the failed</button>
          {/if}
          <span class="grow"></span>
          <button
            type="button"
            class="rounded px-2 py-0.5 hover:bg-slate-100 dark:hover:bg-slate-800"
            onclick={() => (bulkSummary = null)}>Dismiss</button>
        </div>
        {#if bulkFailuresOpen && bulkSummary.failures.length}
          <ul class="mt-2 max-h-48 overflow-auto">
            {#each bulkSummary.failures as f (f.id)}
              <li class="flex items-start gap-2 border-t border-slate-100 py-1 dark:border-slate-800">
                <button
                  type="button"
                  class="shrink-0 font-mono text-[10px] text-[var(--accent)] underline decoration-dotted"
                  title="Open this item"
                  onclick={() => (selected = f.id)}>{f.id.slice(0, 8)}</button>
                <span class="min-w-0 flex-1 break-words text-red-600 dark:text-red-400">{f.reason}</span>
              </li>
            {/each}
          </ul>
        {/if}
      </div>
    {/if}

    {#if view === "grid"}
      <!-- P12 slice 2: responsive thumbnail GRID. Non-virtualized (a CSS grid of
           the already-loaded, paginated hits) with lazy <img>s (loading="lazy")
           so only on-screen thumbs fetch; infinite-scroll reuses loadMore via the
           container's scroll handler, so a huge result set still streams in. -->
      <!-- svelte-ignore a11y_no_noninteractive_tabindex -->
      <div
        class="mt-2 grid max-h-[65vh] gap-3 overflow-auto rounded-lg border border-slate-200 p-3 dark:border-slate-800
               grid-cols-2 sm:grid-cols-3 md:grid-cols-4 lg:grid-cols-5 xl:grid-cols-6"
        role="listbox"
        tabindex={0}
        aria-label="Search results (grid)"
        aria-activedescendant={activeDescendant}
        onscroll={onGridScroll}
      >
        {#each hits as hit, index (hitId(hit))}
          <!-- svelte-ignore a11y_click_events_have_key_events -->
          <div
            id={`opt-${hitId(hit)}`}
            role="option"
            tabindex={-1}
            aria-selected={index === selectedIndex}
            class="group relative flex cursor-pointer flex-col overflow-hidden rounded-lg border text-left {index === selectedIndex
              ? 'border-[var(--accent)] ring-1 ring-[var(--accent)]'
              : 'border-slate-200 hover:border-slate-300 dark:border-slate-800 dark:hover:border-slate-700'}"
            onclick={() => { selectedIndex = index; openItem(hit); }}
          >
            <!-- IN-T4 tile checkbox. Hidden until hover while nothing is
                 selected (the grid is a picture wall — permanent chrome on every
                 tile would compete with the thumbnails), then permanently
                 visible once a selection exists so the user can see what is in
                 it while scrolling. -->
            <span
              class="absolute left-1.5 top-1.5 z-10 rounded bg-white/85 p-0.5 shadow-sm transition-opacity dark:bg-slate-900/85
                {selectedIds.size ? '' : 'opacity-0 focus-within:opacity-100 group-hover:opacity-100'}">
              <input
                type="checkbox"
                class="block accent-[var(--accent)]"
                aria-label={`Select ${str(hit, "title") || str(hit, "filename") || hitId(hit)}`}
                checked={selectedIds.has(hitId(hit))}
                onmousedown={(e) => { if (e.shiftKey) e.preventDefault(); }}
                onclick={(e) => { e.stopPropagation(); toggleSelect(index, e.shiftKey); }} />
            </span>
            <Thumb id={hitId(hit)} size="aspect-square w-full h-auto" rounded="rounded-none" />
            <div class="flex min-w-0 flex-col gap-1 p-2">
              <span class="truncate text-sm font-medium" title={str(hit, "title") || str(hit, "filename")}
                >{str(hit, "title") || str(hit, "filename")}</span>
              <span class="flex items-center gap-1">
                <span class="shrink-0 rounded bg-slate-200 px-1.5 py-0.5 text-[10px] dark:bg-slate-800"
                  >{str(hit, "file_category")}</span>
                {#if num(hit, "year")}<span class="text-xs text-slate-500">({num(hit, "year")})</span>{/if}
                {#if copyCountMap[hitId(hit)] > 1}
                  <span class="ml-auto shrink-0 rounded-full bg-amber-500/20 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:text-amber-300"
                    title="Has duplicate copies">{copyCountMap[hitId(hit)]}×</span>
                {/if}
              </span>
            </div>
          </div>
        {/each}
      </div>
    {:else}
    <!-- Virtualized listbox. aria-activedescendant points at the logically
         selected row; real DOM focus stays on the container (rows unmount). -->
    <VList
      bind:this={vlist}
      data={hits}
      getKey={(h) => hitId(h)}
      itemSize={44}
      onscroll={onScroll}
      role="listbox"
      tabindex={0}
      aria-label="Search results"
      aria-activedescendant={activeDescendant}
      class="mt-2 rounded-lg border border-slate-200 dark:border-slate-800"
      style="height: 65vh;"
    >
      {#snippet children(hit: Hit, index: number)}
        <!-- Rows are click-only by design: keyboard nav (arrows/Enter/Cmd+Enter)
             is handled at the listbox container via the roving aria-activedescendant
             pattern, so per-row key handlers would be dead code. -->
        <!-- svelte-ignore a11y_click_events_have_key_events -->
        <!-- FIX-4: full-width rows. Filename/title gets flex priority (flex-1,
             min-w-0) and the path column shrinks FIRST (capped basis, truncates).
             Both carry a title tooltip so a truncated value is still readable. -->
        <div
          id={`opt-${hitId(hit)}`}
          role="option"
          tabindex={-1}
          aria-selected={index === selectedIndex}
          class="group flex min-h-11 w-full items-center gap-3 px-3 py-1 text-left {index === selectedIndex
            ? 'bg-[var(--accent)]/10 ring-1 ring-inset ring-[var(--accent)]'
            : 'hover:bg-slate-50 dark:hover:bg-slate-800/50'}"
          onclick={() => { selectedIndex = index; openItem(hit); }}
        >
          <!-- IN-T4 row checkbox. Same show/hide rule as the grid: on hover, or
               always once a selection exists. stopPropagation keeps ticking a box
               from also opening the detail modal; the mousedown guard stops
               shift-click from smearing a text selection across rows. -->
          <input
            type="checkbox"
            class="shrink-0 accent-[var(--accent)] transition-opacity
              {selectedIds.size ? '' : 'opacity-0 focus:opacity-100 group-hover:opacity-100'}"
            aria-label={`Select ${str(hit, "title") || str(hit, "filename") || hitId(hit)}`}
            checked={selectedIds.has(hitId(hit))}
            onmousedown={(e) => { if (e.shiftKey) e.preventDefault(); }}
            onclick={(e) => { e.stopPropagation(); toggleSelect(index, e.shiftKey); }} />
          <Thumb id={hitId(hit)} size="h-9 w-9" />
          <span class="shrink-0 rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800"
            >{str(hit, "file_category")}</span>
          <span class="flex min-w-0 flex-1 flex-col">
            <span class="flex items-center gap-2">
              <span class="truncate font-medium" title={str(hit, "title") || str(hit, "filename")}
                >{str(hit, "title") || str(hit, "filename")}</span>
              {#if num(hit, "year")}<span class="shrink-0 text-sm text-slate-500">({num(hit, "year")})</span>{/if}
              {#if copyCountMap[hitId(hit)] > 1}
                <!-- P3-T10 duplicate badge: opens the detail (Copies section). -->
                <button
                  type="button"
                  class="shrink-0 rounded-full bg-amber-500/20 px-2 py-0.5 text-xs font-medium text-amber-700 dark:text-amber-300"
                  title="This file has duplicate copies — click to view them"
                  onclick={(e) => { e.stopPropagation(); selectedIndex = index; openItem(hit); }}
                >{copyCountMap[hitId(hit)]} copies</button>
              {/if}
            </span>
            {#if str(hit, "snippet")}
              <span class="truncate text-xs text-slate-500">
                {#each parseSnippet(str(hit, "snippet")) as seg}{#if seg.mark}<mark class="bg-[var(--accent)]/25 text-inherit">{seg.text}</mark>{:else}{seg.text}{/if}{/each}
              </span>
            {/if}
          </span>
          <span
            class="hidden min-w-0 shrink basis-1/3 truncate text-right text-xs text-slate-500 sm:inline"
            title={str(hit, "path")}>{str(hit, "path")}</span>
          <button
            type="button"
            title="Copy path (Cmd/Ctrl+Enter)"
            class="shrink-0 rounded border border-slate-300 px-2 py-0.5 text-xs opacity-0 group-hover:opacity-100 dark:border-slate-700"
            onclick={(e) => { e.stopPropagation(); selectedIndex = index; copyPath(hit); }}
          >Copy path</button>
        </div>
      {/snippet}
    </VList>
    {/if}

    {#if loading}
      <p class="mt-2 text-center text-xs text-slate-400">Loading…</p>
    {/if}
  {/if}

  {#if selected}
    <ItemDetail id={selected} onClose={() => (selected = null)} onOpen={(nid) => (selected = nid)} />
  {/if}

  {#if toast}
    <div
      class="fixed bottom-4 left-1/2 z-50 -translate-x-1/2 rounded-lg bg-slate-900 px-4 py-2 text-sm text-white shadow-lg dark:bg-slate-100 dark:text-slate-900"
      role="status"
    >{toast}</div>
  {/if}
</div>
