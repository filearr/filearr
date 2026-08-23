# API

Filearr exposes a public REST API that supports **search and metadata updates**,
plus everything the web UI does. It is served under `/api/v1` and documented
interactively via OpenAPI.

## Interactive docs

Every running instance serves interactive API documentation:

- **Swagger UI:** `http://<host>:8484/api/docs`
- The OpenAPI schema underlies it, so client generators work out of the box.

This is the authoritative, always-current reference for request/response shapes —
this page is an orientation, not a substitute.

## Authentication

Two credential carriers are accepted on the same routes (see
[Security](../security.md#authentication-model)):

- **API key** — a Bearer token with `read` / `write` / `admin` scope:

    ```http
    Authorization: Bearer <api-key>
    ```

- **Session cookie** — issued by the login flow for the interactive UI; a
  principal's global role maps onto the same read/write/admin scope vocabulary.

With `FILEARR_AUTH_ENABLED=false`, routes are open (development only).

## Surface areas

| Area | Routes (under `/api/v1`) | What it does |
|---|---|---|
| Search | `search`, `search/tags`, `search/federated` | Typo-tolerant search, facets, filters, [geo bounds](#geo-search-radius-and-bounding-box), tag type-ahead, and [federated item+passage search](#federated-multi-search). |
| Items | `items` (incl. `PATCH`, batch, `digests`, `POST /items/{id}/touch`) | Read items; edit `user_metadata`/tags; batch edits; on-demand digests; frecency use pings. |
| Libraries | `libraries`, `scan-paths` | Define libraries, roots, schedules, presets. |
| Scans | `scans` (with SSE) | Trigger/stop scans; live progress via Server-Sent Events. |
| Query | `query/preview`, `query/keys`, `query/assist` | Filter-builder DSL preview, value pickers, and natural-language → DSL translation (heuristic; optional local Ollama). |
| Reports | `reports`, `reports/folder-tree`, `custom-reports`, `report-schedules`, `exports` | Prebuilt (canned) reports — unmapped extensions, future-dated files, extraction errors, largest files, largest folders (du-style recursive totals), low-quality video, duplicate groups **and per-copy duplicate detail**, files not modified in *N* days — plus saved reports, scheduled delivery, and CSV/NDJSON/XML/XLSX exports. `GET /reports/folder-tree` returns one drill level of folder totals (direct children of a parent) for the treemap view. See [Reports & exports](../reports.md) for the export formats and the [ready-made cleanup scripts](../reports.md#acting-on-duplicates). |
| Saved searches | `saved-searches` | Persisted search definitions. |
| Metadata | `metadata-profiles`, `custom-fields` | Extraction profiles and user-defined fields. |
| Filesystem | `fs/browse` | Allow-listed server-side folder browser (for the library form). |
| System | `system` (health, disk, jobs, `logs`, `update-check`, `rebuild-index`, `retry-extracts`, `backup`/`backups`, share-map, version) | Ops endpoints, including the unified app+worker log tail, the operator-initiated GitHub update check, and the [in-app backup](../operations.md#in-app-backup) (`POST /system/backup`, `GET /system/backups`, `GET /system/backups/{name}` — admin; the download is audited unconditionally, and the bundle is explicitly *not* a complete disaster-recovery backup). |
| Stats | `stats` (`timeline`, `libraries`) | Catalog statistics: the mtime histogram and per-library file counts / total bytes with catalog-wide totals. |
| Auth & identity | `auth`, `oidc`, `rbac`, `audit`, `principal-aliases`, `directory` | Login/session, SSO, path grants, audit log, principal-alias canonicalisation, and the [AD/LDAP directory](../security.md#directory-sync) (`directory/objects`, `directory/status`, `POST directory/sync` — resolve agent-pushed permission SIDs to named identities). |
| Alerts | `alerts` | Channels, rules, events. |
| Agents *(when enabled)* | `agents` (incl. `agents/config-groups`, `agents/config-rollouts`, `agents/{id}/effective-config`), `agent-commands`, `agent-releases`, `transfers`, `agent-staging`, `agent-thumbs`, `agent-share-maps` | The distributed-agent control and data planes — [configuration groups](../agents.md#two-groupings) with versioned history and [phased rollouts](../agents.md#phased-rollouts), plus `POST /agents/{id}/self-update` (queue an update for the agent's next check-in). |
| Agent install *(when enabled)* | `agent-dist` | **Unauthenticated** first-install surface: platform-binary manifest with sha256s, downloads, the templated `install.sh` / `install.ps1` scripts, and the one-script Windows lifecycle tool `manage-windows-agent.ps1`. |

## A few common calls

```bash
# health
curl http://localhost:8484/api/v1/health

# create a library and scan it
curl -X POST http://localhost:8484/api/v1/libraries \
  -H 'Content-Type: application/json' \
  -d '{"name":"media","root_path":"/data/media"}'
curl -X POST http://localhost:8484/api/v1/libraries/<id>/scan

# edit an item's user metadata (write scope) — never touches extracted metadata
curl -X PATCH http://localhost:8484/api/v1/items/<id> \
  -H 'Authorization: Bearer <write-key>' -H 'Content-Type: application/json' \
  -d '{"user_metadata": {"note": "keep"}, "tags": ["favorite"]}'

# rebuild the search index (admin) — always safe; Meili is disposable
curl -X POST http://localhost:8484/api/v1/system/rebuild-index \
  -H 'Authorization: Bearer <admin-key>'

# hash staleness (2026-08-20): per-library count of items hashed under an
# older scheme, and the LIGHT re-hash trigger (hashes only — no metadata
# extraction, thumbnails or embeddings; agent-owned libraries 422 and use the
# agent's re-hash sweep instead). See operations.md#hash-staleness.
curl http://localhost:8484/api/v1/libraries/hash-status
curl -X POST http://localhost:8484/api/v1/libraries/<id>/rehash \
  -H 'Authorization: Bearer <write-key>'

# content-only search (2026-08-20): the query text matches indexed FILE CONTENT
# (body/OCR text, archive member names) — a filename/path hit alone no longer
# returns the item. search_in=names is the inverse; default matches everything.
curl "http://localhost:8484/api/v1/search?q=invoice+total&search_in=content"

# scope a search to one library — or to one MACHINE: every agent root is its own
# library, so repeat `library` (OR) with all of that agent's library ids. The
# console's Filters panel has a Library row with a whole-machine toggle; the
# response carries a `library_id` facet with per-library counts.
curl "http://localhost:8484/api/v1/search?q=budget&library=<lib-id>&library=<other-lib-id>"

# permission search (2026-08-23): files a principal can read (agent-collected
# ACLs; names/SIDs as /search/principals returns them), or world-readable files
curl "http://localhost:8484/api/v1/search?principal=HOLZHUETER%5Ceric"
curl "http://localhost:8484/api/v1/search?world_readable=true&file_category=document"
curl "http://localhost:8484/api/v1/items/<id>/permissions"

# the AGPL §13 source link + running version
curl http://localhost:8484/api/v1/version
```

!!! note "Edits go to `user_metadata` only"
    `PATCH /items/{id}` writes the **user** overlay; a rescan can never clobber
    it. Extracted metadata is read-only through the API. This is
    [architecture invariant 2](../data-collection.md#extracted-metadata-vs-user-edits-the-separation-contract).

## Geo search (radius and bounding box) {#geo-search-radius-and-bounding-box}

`GET /api/v1/search` accepts optional geographic bounds, compiled to
Meilisearch's `_geoRadius` / `_geoBoundingBox` filters over the reserved `_geo`
attribute. They compose with every other filter (and with RBAC path scoping)
with `AND`, so a geo query can only ever **narrow** what a caller is already
allowed to see. Omit them and the query is unchanged.

| Parameter | Type | Purpose |
|---|---|---|
| `geo_lat` / `geo_lng` | float | Centre point. Latitude ∈ [-90, 90], longitude ∈ [-180, 180]. |
| `geo_radius_m` | float > 0 | Radius **in metres** around the centre → `_geoRadius`. |
| `geo_top_lat` / `geo_right_lng` / `geo_bottom_lat` / `geo_left_lng` | float | Bounding-box edges → `_geoBoundingBox`. All four are required together. |
| `geo_sort` | `asc` \| `desc` | Order by distance from the centre (`asc` = nearest first). Takes precedence over `sort`, which becomes the tie-break. |

```bash
# photos within 2 km of San Francisco, nearest first
curl "http://localhost:8484/api/v1/search?file_category=image\
&geo_lat=37.7749&geo_lng=-122.4194&geo_radius_m=2000&geo_sort=asc"

# everything inside a map viewport (top-right / bottom-left corners)
curl "http://localhost:8484/api/v1/search?geo_top_lat=38&geo_right_lng=-122\
&geo_bottom_lat=37&geo_left_lng=-123"
```

Nonsense is refused with **422**, never quietly normalised: half a centre, a
radius with no centre, a bare centre that feeds neither a radius nor a sort, a
partially specified box, an inverted box (top below bottom, or west edge east of
the east edge — Meilisearch boxes cannot cross the 180th meridian; issue two
queries), an out-of-range coordinate, or a non-positive radius.

!!! warning "Geo results depend on the per-library GPS gate"
    Only files in a library with **`expose_gps` enabled** carry coordinates in
    the search index. GPS is extracted and stored in Postgres as usual, but the
    search projection omits it unless the library opted in — the default-hidden
    control described in
    [Data collected](../data-collection.md#per-type-extractors).

    A geo query on a deployment where **no** library exposes GPS therefore
    returns **zero hits with HTTP 200** — not an error. That silence is
    deliberate: failing loudly would itself disclose how the server is
    configured. Malformed geo *parameters* are still a 422.

    The console's [map view](#map-view-in-the-console) says so in words rather
    than showing an empty map, and links back here.

    `PATCH /api/v1/libraries/{id}` with `expose_gps` queues a re-projection of
    that library's documents in **both** directions. Turning the flag **off**
    rewrites each document without its coordinates, so points already in the
    index are removed rather than left behind; the change is asynchronous (an
    index job), so allow a moment on a large library — or run
    `POST /api/v1/system/rebuild-index` to force it.

### Map view in the console {#map-view-in-the-console}

The web console exposes the same geo filters as a map, next to **List** and
**Grid** in the search results toolbar. It is a *lens on the current query*, not
a second search box: it plots the geo-bearing hits of whatever search is already
running, so text, chips, tags and ranges all still apply.

**Drawing an area filters the search.** Drag on the map (or type coordinates into
the North / South / West / East fields below it, which is also the keyboard path
and how you paste coordinates from elsewhere) and the selection becomes
`geo_top_lat` / `geo_right_lng` / `geo_bottom_lat` / `geo_left_lng` on the same
query. Because those are ordinary flat search params, the area:

- appears as a removable chip alongside the other active filters;
- rides the deep-link hash (`#/search?...`), so it survives a reload and browser
  back/forward;
- is captured by a **saved search** like any other filter.

The console normalises what it sends: a drag cannot produce an inverted box, and
the numeric fields refuse a box that is inverted, crosses the 180th meridian, or
carries an out-of-range coordinate — the same cases the API answers **422** to.

**Bounded, not truncated.** A photo library can hold tens of thousands of
geo-bearing files. The map fetches at most **1000** points per search (in pages
of 200) and reports what it did — *"Showing 1000 of 24 812 results with
coordinates"* — rather than silently drawing a subset. On screen, points are
clustered into a fixed pixel grid, so the number of drawn markers is bounded by
the size of the viewport, not by the number of points; zooming in splits clusters
apart, and clicking one lists its items. Clicking a single point opens the normal
item detail panel.

!!! info "The map makes no network requests by default"
    Filearr is meant to run with no outbound internet, so the map ships with its
    own basemap: a simplified **Natural Earth 1:110m** country outline (public
    domain), bundled with the console as a ~50 KB encoded coordinate table and
    loaded only when you open the map. No tile server, no CDN, no web font.

    A **Background tiles** checkbox in the map toolbar can overlay raster tiles
    from a third-party XYZ server (OpenStreetMap by default; the URL template is
    configurable per browser). It is **off by default and never enabled for
    you**. Turning it on means your browser — not the Filearr server — requests
    images directly from that server, which then sees your IP address and, tile
    by tile, which places you are looking at in your own library. The toolbar
    always states which mode is active, and says so plainly when tiles are on but
    the browser is offline.

## Federated multi-search {#federated-multi-search}

`GET /api/v1/search/federated` searches the **item** index and the **passage**
(chunk) index in one Meilisearch federated query and returns **one merged,
ranked list** instead of two the caller has to interleave. Every hit carries a
`score` — the normalised **weighted ranking score** (0..1) the federation
sorted by, comparable across the two indexes (for passage hits under a
semantic/hybrid query it is effectively query similarity). Cut weak tails
client-side; below ~0.5 is loose association.

| Parameter | Default | Purpose |
|---|---|---|
| `q` | `""` | Query text, applied to both indexes. |
| `library` | – | Restrict both sub-queries to one library id (must be a UUID — the value is interpolated into every sub-query's filter alongside the RBAC clause, so it is validated rather than trusted). |
| `status` | `active` | Item status (item sub-query only — the chunks index has no status attribute). |
| `include_sidecars` | `false` | Include sidecar files in the item sub-query. |
| `facets` | `false` | Also return Meilisearch's `facetsByIndex` distribution for the item index. |
| `limit` / `offset` | `20` / `0` | Federation-level paging over the merged list. |

```json
{
  "hits": [
    {"source": "items", "item_id": "0191…", "score": 0.91,
     "fields": {"filename": "beach.jpg", "rel_path": "2019/beach.jpg"}},
    {"source": "passages", "item_id": "0192…", "score": 0.44,
     "fields": {"chunk_no": 3, "filename": "notes.pdf", "text": "…"}}
  ],
  "total": 2, "limit": 20, "offset": 0,
  "indexes": ["items", "passages"], "facets_by_index": {}
}
```

- `source` is the **logical** index name (`items` | `passages`); the underlying
  Meilisearch uids are deployment-configurable and never appear in the contract.
- `item_id` resolves every hit back to a catalog item. Passage documents already
  carry the item id plus a denormalised filename/`rel_path`, so rendering the
  merged list costs **no per-hit lookup**.
- RBAC path scoping is applied to **every** sub-query, identically to the
  single-index `/search` path — Meilisearch does the row-level filtering.
- Passage indexing (chunking) is a per-library opt-in. When no chunks index
  exists the endpoint answers over the item index alone and says so in
  `indexes`, rather than failing.
