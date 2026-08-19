# Data collected & how

This page is a thorough, honest accounting of **what Filearr reads, computes,
stores, and transmits** — and, just as importantly, what it does **not**.

!!! success "No external telemetry, ever"
    Filearr has **no phone-home, no analytics, no external telemetry**. It talks
    only to the services you configure (your Postgres, your Meilisearch, your
    media mounts, and — if you enable them — your own agents and their CA).
    Meilisearch analytics are disabled in the shipped configuration. The only
    outbound network calls Filearr makes are ones you explicitly turn on: an
    OIDC/LDAP provider you configure, alert webhooks/SMTP you create, an optional
    one-time embedding-model download, (for the agent CA) Let's Encrypt /
    Cloudflare DNS if you choose the ACME TLS mode, and the **operator-initiated
    update check** — clicking *Check now* on the Jobs page asks GitHub for the
    repository head and recent commit messages (nothing about your instance or
    catalog is sent; `FILEARR_UPDATE_CHECK_AUTO=true` is the explicit opt-in for
    automatic stale-cache refreshes, default off).

## What a scan reads

A scan is a **read-only** filesystem walk of a library root. Media mounts are
mounted read-only; Filearr never modifies your files.

### Filesystem walk and `stat`

For every file the walk records: the absolute path (as currently mounted), the
**path relative to the library root** (the stable identity), the filename and
extension, the **size**, and the **modification time**. Classification comes from
the extension via the editable taxonomy: a **file category** (the parent — video,
audio, image, document, …) and a finer **file group** (RAW vs. raster photo,
archive, source code, …) — see the [file-extension groups
reference](reference/file-extension-groups.md).

**Not every file on disk is ingested.** A scan skips files four ways — the
library's category/group selection, the exclusion presets/globs, pruned
directories, and unreadable directories. The first two are counted and shown on
the Libraries page; **pruned and unreadable directories are skipped without being
read at all**, so the files inside them are counted nowhere and the reported
totals are a lower bound. This is why a library can legitimately show far fewer
files than the folder's properties. See [A library indexes fewer files than the
OS reports](operations.md#library-file-count-mismatch) for how to attribute the
difference and how to make the counts reconcile exactly.

### Hashing (xxh3)

Filearr computes non-cryptographic **xxh3** hashes for change- and move-detection:

- **quick hash** — over the **first and last 64 KiB** of the file. Cheap; used as
  the first tier of move detection.
- **content hash** — the full (chunked) file hash, used to disambiguate move
  candidates. There is a size ceiling (`FILEARR_SCAN_HASH_FULL_MAX_BYTES`,
  default 1 GiB) above which the full content hash is skipped.

These are for identity/move-detection, not integrity attestation. **Cryptographic
digests (MD5 / SHA-256) are computed only on demand** via
`POST /api/v1/items/{id}/digests`, which streams the file once and caches the
result — never automatically during a scan, and with a hard size ceiling.

### Per-type extractors

Extraction runs per file, after the scan batch commits, on dedicated worker
queues. What each extractor reads and stores:

| Type | Tool | What it extracts |
|---|---|---|
| Video | `ffprobe` | Codecs, resolution, duration, bitrate, streams, container facts (bounded runtime + output size). HDR signalling (HDR10 / HLG / Dolby Vision incl. profile, level and base-layer compatibility from the DOVI record); for HDR streams a second, bounded first-frames probe tells **HDR10+** from HDR10 and records MaxCLL / MaxFALL / mastering display (`FILEARR_FFPROBE_DEEP_HDR`, default on; SDR files never pay it). |
| Audio / audiobook / sample | tag libraries | Tags (artist/album/title/etc.), duration, channels, sample rate, cover art. |
| Image | `exiftool` | Curated camera / lens / exposure / dimension fields under an `exif.` namespace. **GPS is gated — see below.** |
| Document | pypdf / python-docx / openpyxl | Document properties; optional **body text** for search snippets (bounded, opt-in per feature). |
| Spreadsheet | openpyxl | Workbook properties (metadata only; cell extraction is a future capability). |
| 3D model | trimesh | Geometry facts for safe formats (fast `process=False` path by default; `FILEARR_MODEL3D_ACCURATE_MAX_BYTES` opts small files into vertex-merged "accurate" geometry with a true watertight flag, recorded as `geometry_tier`); a lightweight file-fact record for formats with no safe pure loader (STEP/FBX/BLEND — a native CAD kernel is out of scope). |
| Archive | zip / tar / 7z / rar readers | **Member name listing** (searchable) *without unpacking* — stdlib for zip/tar, `py7zr` and `rarfile` header-only for 7z/rar (and `.cb7`/`.cbr`); guarded against zip/decompression bombs, declared-size bombs and encrypted headers. Agents list zip/tar only. |
| E-mail | stdlib `email` / `mailbox`, `olefile` | `.eml`: subject/from/to/cc/date/Message-ID, attachment names, **body text** (plain, or HTML reduced to text); `.mbox`: message count, date range and a searchable subject/sender digest (capped at `FILEARR_EMAIL_MBOX_MAX_MESSAGES`); Outlook `.msg`: the same headers/body from the OLE MAPI streams. PST/OST are marked unsupported — convert with `readpst` to mbox. |
| PDF / image / video | Pillow / PDFium / ffmpeg | Content-addressed WebP **thumbnails** + video poster frames (a disposable cache). |

Every extractor is bounded (timeouts, output-size caps, pixel/decompression bomb
guards) so a hostile or oversized file cannot stall or OOM a worker.

!!! warning "GPS coordinates are hidden by default"
    Image EXIF GPS keys are **stored raw** but **stripped from the search index
    and the API** unless the owning library's `expose_gps` toggle is on. There is
    deliberately **no** global default-on path — location data does not leak into
    search results or API responses unless you opt a library in.

    The same flag governs **geo search**: only an opted-in library's files carry
    a `_geo` point in the search index, so
    [radius / bounding-box queries](reference/api.md#geo-search-radius-and-bounding-box)
    simply return nothing on a deployment where no library exposes GPS. Turning
    `expose_gps` **off** again queues a re-projection that **rewrites** that
    library's documents without their coordinates — points already indexed are
    removed, not left behind.

    The console's [map view](reference/api.md#map-view-in-the-console) is subject
    to exactly this gate: with no library opted in it explains the toggle instead
    of drawing an empty map. The map itself sends nothing outward — it draws a
    basemap bundled with the console, and the optional third-party tile layer is
    off by default.

!!! note "OCR and semantic embeddings are opt-in"
    - **OCR** (Tesseract) runs only for a library with `ocr_enabled` — the default
      install pays zero OCR cost. When on, it OCRs pages/images with no usable
      text layer, bounded by page/pixel/time caps, storing capped text.
    - **Semantic search** (a local ONNX embedder) is **globally off by default**;
      when enabled it computes dense vectors locally (never a cloud API — private
      files never leave the box) and downloads a ~130 MB model once.

        That download is the only outbound call, it happens once per model into
        the persistent cache volume, and inference is local forever after.
        `huggingface_hub` ships with **telemetry enabled by default**, so Filearr
        sets `HF_HUB_DISABLE_TELEMETRY=1` and `DO_NOT_TRACK=1` before the client
        loads — the fetch happens, the analytics do not. Set them to `0` if you
        want the library's default behaviour back.

        You may see `You are sending unauthenticated requests to the HF Hub` in
        the log while it downloads. That is Hugging Face's own notice, not an
        error: anonymous downloads work and are only rate-limited by IP. Set
        `HF_TOKEN` in the environment if you hit a limit (the Unraid template,
        compose file and Proxmox wizard all expose it; blank or a placeholder
        such as `none` means "download anonymously" and is never sent as a
        token). **If that line appears repeatedly rather than once, the
        model cache is not persisting** (check the `/config` volume), because a
        warm cache never reaches the network. Once it is warm you can set
        `HF_HUB_OFFLINE=1` to guarantee the worker never tries.

### File origin (download provenance) {#file-origin}

Browsers and download tools stamp the source URL onto the file itself, and the
extract pass reads it for **every** file type into `origin_url` /
`referrer_url` (extracted metadata, so it is never user-editable and shows under
**Origin** in the item detail):

| Where the file lives | What is read | Who reads it |
| --- | --- | --- |
| Linux mount scanned by central, or a Linux agent | freedesktop xattrs `user.xdg.origin.url` / `user.xdg.referrer.url` (Firefox, Chrome, wget, `curl --xattr`, GNOME/KDE downloaders) | central `file_origin.py` / agent |
| macOS agent | `com.apple.metadata:kMDItemWhereFroms` (Safari, Chrome, Finder copies keep it) | agent |
| Windows agent | the `Zone.Identifier` alternate data stream (`HostUrl` / `ReferrerUrl` — the Mark-of-the-Web every browser writes) | agent |

One `listxattr` (or ADS open) per file; filesystems without user xattrs (cifs
without `user_xattr`, FAT) answer "none" and cost nothing further. Only
`http(s)`/`ftp(s)`/`sftp` URLs with a host are kept — a `file:` or `javascript:`
"origin" is dropped. `origin_url` is also the lowest-ranked searchable field, so
a query for a site name finds what you downloaded from it without outranking a
filename or body match. `FILEARR_PROVENANCE_ENABLED=false` switches the central
read off entirely.

## Sidecar files (.nfo, .xmp, artwork) {#sidecar-files}

Media collections are full of files that *describe* other files: Kodi/Emby
`.nfo` metadata, `poster.jpg`/`folder.jpg` artwork, `-thumb` images, JRiver
`*_JRSidecar.xml`, and photo-tool XMP sidecars (both `photo.xmp` and the
digiKam-style `photo.jpg.xmp` double extension). Filearr catalogs them but
treats them as **sidecars, not content**:

- Every scan classifies sidecars by path shape and links each to its parent
  item (`sidecar_of`) — the same-stem sibling, or the folder's primary media
  file for folder-level artwork. Folder-level artwork (`poster.jpg`,
  `movie.nfo`, `season.nfo`) is attributed to the folder's largest primary
  file **only when it clearly dominates** (at least twice the runner-up — the
  movie-plus-featurette case); in a folder of comparable files such as a season
  of episodes the artwork describes the folder, so it is left unlinked rather
  than pinned to an arbitrary episode.
- **Kodi NFO** title/plot/year/genre/ids fold into the parent's extracted
  metadata under `nfo_*`; **JRiver `*_JRSidecar.xml`** (its MPL dialect: Name,
  Year, Genre, Director, Actors, Description, Rating, Series/Season/Episode,
  IMDb/TMDb/TVDb ids — a conservative known-field subset) fold in under
  `jr_*`. Both promote `title`/`year` onto the item when those are empty; set
  `FILEARR_SIDECAR_METADATA_PRIORITY=sidecar` to make the sidecar the
  authority and overwrite an already-derived title/year instead (the raw
  `nfo_*`/`jr_*` values are kept either way, and your own edits still win at
  read time).
- Sidecars are **hidden from default search results and from the timeline
  histogram** (they'd otherwise dominate both — a bulk photo-tool metadata
  export can stamp hundreds of thousands of `.xmp` files in a week). They stay
  in the catalog and are reachable with the search filters
  `include_sidecars=true` or `sidecar_of=<parent-id>`.
- **Agent-replicated libraries get the same treatment**: replication itself
  never sets the link, so central runs a debounced link-only association pass
  after each replication burst plus a nightly sweep ("Agent sidecar
  association" on the Jobs page). NFO parsing is skipped there — central
  cannot read files on the agent's disk.

## Extracted metadata vs. user edits (the separation contract)

Filearr keeps two distinct metadata stores on every item:

- **`metadata`** — everything extractors and scans discover. Rescans and
  re-extraction **overwrite** this freely.
- **`user_metadata`** — everything a human edits through the API/UI. Scans and
  extractors **never** write here.

The **effective value** a user sees is `user_metadata` overlaid on `metadata`, so
your manual edits always win and are never clobbered by a rescan. This is
architecture invariant 2 and it is enforced at the API and ORM layers.

## What agents replicate {#what-agents-replicate}

When you run the optional agent fleet, each agent replicates a **narrow,
lightweight** change set to central — never your file contents.

**What leaves the agent machine** (the "R1" replication field set), per changed
file:

- `rel_path`, `size`, `mtime`
- `quick_hash` and `content_hash` (content hash may be null for large/networked
  files)
- a `moved` event carries the old path (delete+create pair)
- an optional, best-effort `share_hint` (a network-share URL/UNC/host, so the
  central UI can offer an "open on the network" link)
- a compact **health snapshot** on each command poll: uptime, replication
  backlog count, local index size, and scan status/counters — operational
  numbers only, never paths beyond the scan roots you configured and never
  file contents
- **when you enable agent-side extraction** (`extract_enabled`, off by default),
  an additional compact `extracted` object per file — see below

**Extraction changes what leaves the machine, which is why it is opt-in.**
Central cannot open a file on an agent host, so an agent library stays
identity-only until you turn extraction on in the agent's policy. Once you do,
each event also carries the metadata the agent parsed locally:

- technical properties — image dimensions/format, audio tags, video codec and
  duration, document page counts and properties, 3D geometry counts, archive
  member names
- **document text** (`extract_body_text`, separately opt-in) — up to 100 000
  characters of the actual text of your documents and PDFs. This is what makes
  agent items chunkable and content-embeddable, and it is genuinely your
  documents' words travelling to central.
- **OCR text** (`extract_ocr`, separately opt-in) — the text tesseract read out
  of images and scanned pages, under the same cap.
- **EXIF, including GPS coordinates** (`extract_exif`, separately opt-in and
  needing `exiftool` on the agent host). GPS is stored raw in the item's
  extracted metadata and then hidden
  everywhere by default: it is stripped from the API and the search index unless
  the owning library sets `expose_gps`. That gate is the same one central's own
  photo libraries use, but if you would rather the coordinates never leave the
  machine at all, do not install exiftool on that host — capability is per host,
  and the console's agent details show which hosts have it.

None of this is retained anywhere else on the way: the object rides the existing
replication batch, is size-capped, and lands in the item's *extracted* metadata
column (never `user_metadata`).

**What never leaves the agent machine:**

- **File contents** — never, except an explicit **retrieve** you initiate, or a
  small size-capped **thumbnail** the agent uploads for the grid.
- **Local search history** — the frecency store is a separate local database the
  replication subsystem is architecturally incapable of reading; central holds no
  copy.
- The filename-derived **title** and any local-only fields stay agent-side until
  central enriches the item itself.
- **Anything extraction is not turned on for.** With `extract_enabled` off — the
  default — no content metadata, no text and no EXIF is produced at all, and the
  events are exactly the identity fields listed above.

## What the search index stores

Meilisearch holds a **projection** of the catalog optimized for search: item
identity and display fields (path, filename, type, size, times), searchable text
(titles, tags, selected metadata, capped document body text, archive member
names and the download-origin URL when present), facet values, and — when RBAC search is enabled — the item's
path scope for tenant filtering. GPS is excluded unless a library opts in — when
it does, the coordinates are also projected as Meilisearch's `_geo` point so
[geo queries](reference/api.md#geo-search-radius-and-bounding-box) can filter and
sort by location. The index is **disposable**: every field is rebuildable from
Postgres, and it is never a store of record.

## Usage signals: frecency profiles {#frecency}

With `FILEARR_FRECENCY_ENABLED` (default on), opening an item's detail view
records one row per (user, item): a use counter and a last-used timestamp —
nothing else (no dwell time, no query text, no history log). It exists solely
to nudge your habitually-opened files up within a search page. Profiles are
**per principal** (one user's habits never affect another's results; with
auth disabled, the single shared profile is keyed `anonymous`), rows older
than 90 days are pruned automatically, and everything lives in Postgres —
nothing leaves your infrastructure. Setting the flag to `false` stops both
recording and ranking use.

## Document passages (RAG chunking) {#doc-chunks}

Libraries opted into **RAG chunking** get their already-extracted text
(document bodies, OCR results) additionally stored as ~1,000-character
passages in Postgres (`doc_chunks`) and projected into a local search index
for the LLM `retrieve_passages` tool. This duplicates text Filearr already
holds — no new reading of your files happens — and both copies stay local.
Disabling the library toggle stops new chunking; passages are removed when
their item is deleted.

## What audit logs record

The security-events log records login/logout/session lifecycle, grant changes,
and agent lifecycle mutations, plus (unconditionally) data-egress actions
(download/export/verify). Optional read auditing records a per-query event when
enabled. **Raw tokens and secrets are never recorded** — only non-secret
references (e.g. an OTT's `jti`) appear. See
[Security → Audit log](security.md#audit-log).

## Summary: where each kind of data lives

| Data | Store | Leaves your infrastructure? |
|---|---|---|
| Paths, sizes, times, hashes | Postgres | No |
| Extracted metadata (ffprobe/EXIF/tags/…) | Postgres | No |
| Your edits (`user_metadata`, tags, custom fields) | Postgres | No |
| GPS coordinates | Postgres (hidden unless opted in) | No |
| Search projection | Meilisearch (local) | No |
| Thumbnails / posters | Config-volume cache (disposable) | No |
| Application log stream (Jobs page Logs panel) | Postgres (7-day retention) | No |
| Agent local search history | Agent-local SQLite | **Never** |
| File contents | Your media (read-only) | Only on explicit retrieve/thumbnail |
| Telemetry / analytics | — | **None exists** |
