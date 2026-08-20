# Filearr — Future Functionality Roadmap (v2/v3: Distributed Platform)

Extends the v1 single-node catalog into a centrally managed, multi-machine file
intelligence platform. Research-verified 2026-07-06 (prior-art citations in the
research transcripts; Meilisearch inventory verified against v1.48.3 releases).

## Status index (refreshed 2026-08-19)

| § | Area | Status |
|---|---|---|
| 1 | Distributed agents | **shipped** (+ scheduled/held updates, phased binary rollouts 2026-08-18/19) |
| 2 | Local query access | **shipped** |
| 3 | Identity, auth & RBAC | **shipped** except SAML (**blocked**: pysaml2 pins pyopenssl<24.3); roles-as-data + service accounts (P6-T10) shipped 2026-08-16/19; AD/LDAP directory sync — SID→identity resolution + group expansion (§30) shipped 2026-08-20 |
| 4 | Indexing controls | **shipped**; custom exclusion presets (P2-T7) + per-OS agent presets driving roots (P2-T8) 2026-08-19 |
| 5 | Search & findability | **All shipped** incl. P3 provenance (2026-08-19); email/mbox indexing tracked in §15; geo map filter is in the search UI (R8); federated multi-search only if indexes ever split (§8) |
| 6 | Alerting | **shipped**; apprise in the image (P8-T3); **polish shipped 2026-08-19** — inhibition (`inhibited_by` + `inhibit_window_s`, mute semantics), `group_by` extras (folder / extension / file), `FILEARR_WEBHOOK_ALLOWED_CIDRS` |
| 7 | Data model extensions | **shipped** |
| 8 | Meilisearch adoption | **closed 2026-08-20** — quantization opt-in shipped; dumpless upgrades already adopted (MEILI_UPGRADE_DB); task webhooks declined final |
| 9 | License | **decided** (AGPL-3.0-or-later) |
| 10 | Sequencing | both waves **delivered** |
| 11 | ffprobe follow-ups | **shipped 2026-08-19** (DV profile/level/compat from the DOVI record; deep first-frames probe for HDR10+ / MaxCLL / MaxFALL / mastering display, Python + Go); pymediainfo cross-check declined |
| 12 | Sidecar follow-ups | **closed 2026-08-19** — JRiver sidecar parsing, directory-artwork dominance rule, `FILEARR_SIDECAR_METADATA_PRIORITY` |
| 13 | Move detection follow-ups | **closed 2026-08-19** — 2 of 4 shipped 2026-07-24; hash-policy interplay already honoured; in-place swap a documented non-goal |
| 14 | SSE follow-ups | **shipped** |
| 15 | Extractor follow-ups | **closed 2026-08-19** — 7z/RAR listing, e-mail (.eml/.mbox/.msg), accurate-geometry tier; CAD kernels declined; PST → readpst |
| 16 | Hash-policy follow-ups | **closed 2026-08-19** — opt-in content-hash backfill task + mountinfo memo |
| 17 | Extraction throughput | **shipped** (adaptive backpressure) |
| 18 | Error surfacing | **shipped** (+ traceback head/tail, Meili task ack 2026-08-18) |
| 19 | Test suite + CI | **shipped** (+ cross-compiled images 2026-08-16) |
| 20 | UX/preview backlog | **shipped** |
| 21 | LLM / RAG | **shipped** (M1–M3) |
| 22 | Permissions audit (W7) | **CLOSED 2026-08-20** — v1 + T9 drift/alert (08-19); T4 macOS ACL read, T8 principal aliases (report-side canonicalisation), T10 effective access (pure evaluator + inspection endpoint) all shipped 08-20 |
| 23 | Binary release rollouts on the tier engine | **shipped 2026-08-19** |
| 24 | GPU acceleration | **declined** (assessed 2026-08-12) |
| 25 | Insight features | **shipped 2026-08-13** |
| 26 | joserfc migration | **done 2026-08-14** |
| 27 | Filesystem identity (symlinks/hardlinks) | **shipped 2026-08-20** — central scan captures nlink/inode/dev + symlink_target from the walk's existing lstat; symlinks catalogued but never hashed/extracted; partial hardlink index. Agent-side parity + a duplicate-vs-hardlink report are the deferred tail |

Everything not marked shipped/decided/declined is a small, explicitly deferred
tail. W7 (§22) is fully closed (the last scaffold, `permissions_explicit_
outliers`, went live 2026-08-20). The 2026-08-20 code review also fixed a batch
of security + correctness defects (see §28) and closed the remaining stubs.

---

## 1. Distributed agent architecture

> **SHIPPED** (Phase 5 + P10 + W6 waves, July 2026; auto-update extensions
> 2026-08-05; config-group unification P13 2026-08-11). Everything below is
> live: step-ca enrollment + short-lived mTLS certs (+ fingerprint rebind
> self-heal, expired-leaf recovery OTT), versioned **configuration-group** push
> (a permanent Global group plus prioritised member groups, merged per key,
> ETag poll) with per-group history, rollback and **phased rollouts** (≤5
> percent/delay tiers over a stable agent-id bucket), local SQLite+FTS5 index,
> outbox replication with per-agent seq + full-manifest reconcile sweep,
> tombstones, and signed updates — extended with central-version tracking
> (unsigned dist channel for unpinned builds), a server-side `auto_update`
> gate, and a console per-agent update trigger. First-install distribution is
> served by central itself (`/api/v1/agent-dist` + install scripts). Runbook:
> `docs/ops/agents.md`.
>
> P13 retired the parallel grouping: policy scopes
> (`global`/`group:<name>`/`agent:<uuid>`, whole-document replacement),
> `agents.rollout_group`, and binary-release staging are gone. Releases are now
> generally visible on upload, with `auto_update` as the brake — see §23 for
> the deferred half.

**Native clients (agents)** installable on Windows/Linux/macOS, centrally
configured:
- **Enrollment:** one-time token → agent generates key + CSR → central server
  signs a short-lived client certificate, auto-renewed (**step-ca pattern**, as
  used by Fleet/osquery-style fleets). Certificate = machine identity
  (Syncthing's cert-hash-as-device-ID is the proven precedent). All
  agent↔server traffic is **mutual TLS** — authorization and integrity in one
  mechanism.
- **Config push:** central server holds prioritised **configuration groups**
  (what paths to index, type/exclusion rules, watch schedules) that merge per
  key into one document per agent; agents poll or hold a long-poll channel;
  every group is versioned and auditable.
- **Local-first indexing:** agents scan to a **local SQLite index** (FTS5),
  fully functional offline.
- **Replication:** transactional **outbox + per-agent monotonic sequence
  number**, batched upserts to central Postgres keyed on `(agent_id, seq_no)`
  for idempotency. No CRDTs/vector clocks needed in a hub-and-spoke topology
  (research conclusion — cr-sqlite/Litestream do NOT solve many-SQLite→one-
  Postgres). Tombstones replicate deletes; periodic full-reconciliation sweep
  as safety net.
- **Agent updates:** signed packages, central version pinning, and a staged
  rollout (Fleet/Wazuh precedent). Signing + pinning + `auto_update` gating are
  live; percentage-tiered *binary* staging is deferred — §23.

## 2. Local query access

> **SHIPPED** (P7, July 2026): local CLI query + loopback web UI against the
> agent index, policy-gated from central (`local_access_enabled`,
> `web_ui_enabled`, `auth_required`, fail-closed `read_only`), with the
> container's opt-in remote-bind override for Docker hosts.

- **Local CLI** (`filearr query ...`) and optional **local web UI** against the
  agent's SQLite index — answers "where did I put that file" even when the
  central server is unreachable.
- Local access is **policy-controlled from the central console**: enabled/
  disabled per machine group, optionally auth-required, read-only scope.

## 3. Identity, auth & RBAC (central console)

> **MOSTLY SHIPPED** (P6/P7 waves, July 2026): local accounts (argon2id,
> first-visit bootstrap-admin), **LDAP** (via `ldap3` after all — the
> python-ldap preference was reversed, see `docs/ops/auth.md`), **OIDC/SSO**
> (Authlib), two-layer RBAC with path-scope grants, and Meili **tenant-token**
> enforcement. **SAML remains deliberately unshipped**: pysaml2 hard-pins
> `pyopenssl<24.3.0` (would downgrade the crypto stack) and python3-saml drags
> in-process libxmlsec1 — rationale recorded in `filearr/authx.py`; revisit
> only if a maintained dependency path appears.

- **Auth providers:** local accounts, **LDAP** (`python-ldap` — ldap3 is
  stalled since 2021), **SAML** (`pysaml2`), **OIDC/SSO** (`Authlib`).
  fastapi-users is in maintenance mode — avoid.
- **RBAC, two layers** (Grafana/Portainer precedent):
  1. Global roles: Admin / User / Viewer.
  2. Group-based resource ACLs on **machine groups** and **file locations**
     (path scopes) with inheritance + override. Grantable actions:
     `search_metadata`, `search_content`, `download`, `upload`, `modify`,
     `delete`, `edit_metadata`, `manage_alerts`.
- **Enforcement in search:** ACLs compile into **Meilisearch tenant tokens**
  (per-session signed JWT with embedded filter, e.g.
  `acl_groups IN [...]`) — row-level security without trusting the client.
  Requires Meili ≥ 1.48.2 (tenant-token CVEs patched). Meilisearch has no
  native RBAC (roadmap-only) — the app layer owns the permission model.

## 4. Indexing controls

> **SHIPPED** (July 2026; content-sniffing 2026-08-06): include/exclude globs
> + one-click preset bundles, layered per-group agent configuration, taxonomy
> extension groups (W8), config-group default scan selections, hot-folder scheduling,
> AND the extensionless content-sniffing reclassify job — an opt-in
> (`FILEARR_CONTENT_SNIFF_ENABLED`) on-demand maintenance action that
> libmagic-sniffs a bounded prefix, maps MIME → the live taxonomy's groups,
> and re-projects/re-extracts what it reclassifies. Bounded batches,
> idempotent stamps, agent-hosted files excluded.

- **Inclusions/exclusions:** per-library and per-agent folder include/exclude
  (globs — already in v1 schema), plus **preset bundles** toggled with one
  click: "system files", "hidden/dotfiles", "caches/temp", "node_modules &
  build artifacts", "OS metadata (Thumbs.db/.DS_Store)".
- **Default search locations** per platform (Documents/Desktop/Downloads/
  user-defined) offered at agent setup, easily amended centrally.
- **File-type presets:** extend v1's per-library media-type toggles to
  arbitrary extension groups, centrally managed.
- **Content-sniffing for extensionless / mis-extensioned files (OPS-T4
  follow-up):** the live 750k corpus has ~3k extensionless files (`''` in the
  `unmapped_extensions` report) that carry no extension signal and stay
  `media_type=other`. A future pass could magic-sniff a bounded prefix
  (`python-magic`/libmagic, already an available dep) to classify them by
  content — but content-sniffing 3k+ files *during scan* over SMB/NFS is a real
  cost, so this is deferred: run it as an opt-in, off-scan reclassify job over
  the extensionless set only, never inline in the walk.
- **Hot-folder scheduling:** per-directory watch/scan frequency overrides —
  "watch Downloads every minute, archive shares nightly" — implemented as
  per-path Procrastinate periodic tasks; local watchfiles where the
  filesystem supports it.

## 5. Search & findability (the "where did I put that file?" feature set)

> **STATUS (2026-08-06):** P0 and P1 fully shipped — similar-files landed in
> P3-T9 all along (`GET /items/{id}/similar` over Meili `/similar`; the
> detail-panel section was upgraded 2026-08-06 to a clickable thumbnail grid,
> hidden entirely when semantic search is off). P2 shipped (timeline, EXIF/GPS
> gate, tag type-ahead, archive member indexing) except **email indexing
> (mbox/PST)**. Natural-language query assist SHIPPED 2026-08-06
> (`POST /query/assist`: deterministic heuristic translator, optional local
> Ollama via FILEARR_NL_OLLAMA_URL with automatic fallback; Filter Builder
> "describe it" box). P3 central frecency SHIPPED 2026-08-06 (item_frecency
> per-principal store mirroring the agent's zoxide-style scoring; detail-open
> touches + bounded page-local search re-rank, FILEARR_FRECENCY_ENABLED).
> P3 provenance download-URL SHIPPED 2026-08-19 (`file_origin.py` xattr
> reader central-side; agent reads Zone.Identifier / xdg xattrs /
> kMDItemWhereFroms; `origin_url` searchable last; Origin block in item
> detail). §5 now has no open items except email/mbox/PST indexing (→ §15).

Priority-ordered from prior-art research (Everything, Recoll, sist2, Spotlight,
Paperless-ngx, Immich, 2024-26 local-AI tools):

**P0 (core payoff)**
- **Search-as-you-type filename/fuzzy matching** (already v1) with path
  breadcrumbs and **"open containing folder" / copy-path actions**.
- **File hash search:** exact-match lookup by xxh3 (stored) plus optional
  MD5/SHA-256 (computed on demand or per-policy) — filterable attribute, typo
  tolerance disabled. Answers "do I already have this file / where else is it?"
- **Snippets & highlighting** (Meili `attributesToHighlight`/`crop`) once
  content indexing lands.
- **Quick filters:** kind/size/date chips (facetStats drive range sliders).
- **Recency ranking boost** ("the file I touched last week beats the 2019 copy").

**P1**
- **Content extraction** for documents (Tika-class: pypdf/docx text, plus OCR
  via Tesseract for scans/images — Recoll/Paperless precedent; cached so OCR
  runs once). This unlocks "what files CONTAIN this information".
- **Saved searches / smart folders** (persisted queries, shareable, ACL-aware).
- **Semantic/hybrid search:** Meili hybrid mode with **local embedders**
  (huggingFace/ONNX or Ollama — never cloud for private files), **binary
  quantization from day one** (~10x smaller, required at millions of vectors);
  Hannoy backend (default ≥ v1.29) makes this practical.
- **Similar-files** ("more like this") via Meili `/similar` endpoint —
  duplicate-adjacent discovery for free once embeddings exist.
- **Duplicate awareness** surfaced in results ("3 copies exist" badge, hash-based).

**P2**
- **Timeline browsing** (created/modified date histogram navigation).
- **EXIF deep extraction** (exiftool sidecar service): camera, GPS (enables
  Meili geo filters for photo maps), lens, dimensions — extending v1's Pillow
  basics. Same pattern for extended audio/video technical metadata.
- **Tag system** with facet-search-powered type-ahead (Meili facet search
  endpoint, v1.3+) once tag cardinality grows.
- **Archive/email indexing** (zip/7z member listings; mbox/PST later —
  Recoll precedent).
- **Natural-language query assist** (query→filter translation, local LLM optional). *(SHIPPED 2026-08-06)*

**P3**
- File provenance (download source URL, originating agent/machine). *(SHIPPED 2026-08-19 — origin/referrer URL from xattrs / Zone.Identifier; originating agent was already the item's agent library)*
- Frecency (frequency+recency) personal ranking profiles. *(SHIPPED 2026-08-06 — central per-principal store + search re-rank)*

## 6. Alerting

> **SHIPPED** (P8, July 2026): file-change rules (path glob + event type
> created/modified/deleted/moved), channels (webhook w/ SSRF guards + HMAC
> signing, email), per-rule throttling/windows, AND the operational alerts
> (scan failures, agent offline / replication stall, disk pressure,
> extract-error spikes). Channel secrets AES-GCM-encrypted at rest.
> **Polish shipped 2026-08-19:** rule inhibition (Alertmanager mute: a rule
> lists `inhibited_by` rules + `inhibit_window_s`; a group is marked
> delivered with "suppressed: inhibited by …" while an inhibitor fired for the
> same library / library-less), `group_by` extras beyond the R1 base
> (`folder` / `extension` / `file` split one library batch into finer
> notifications; dedup key + rendered header carry them), and
> `FILEARR_WEBHOOK_ALLOWED_CIDRS` (explicit per-target allowlist, finer than
> the private-class boolean; unspecified never allowlisted). Remaining: none.

- **File-change alert rules:** watch expressions (path glob + event type:
  created/modified/deleted/moved + optional hash-change) → notification
  channels (webhook, email, Apprise → Discord/ntfy/etc.), with per-rule
  throttling/digests. Runs on agent events post-replication; local evaluation
  for offline machines with delivery on reconnect.
- Operational alerts: scan failures, agent offline, replication lag,
  extract-error spikes (extends Phase-1 T11).

## 7. Data model extensions

> **SHIPPED** (July 2026): metadata profiles with validation, custom
> user-defined fields, per-type detail displays + the always-available Raw
> tab, and full provenance columns (source agent, first/last seen,
> replication seq).

- **Extended metadata:** keep JSONB `metadata` bag; add **typed per-domain
  schemas** (registered "metadata profiles" per file type with validation),
  and **custom user-defined fields** (central definition, per-location
  applicability) — powers both faceting and the RBAC `edit_metadata` action.
- **Customizable displays:** per-file-type card/detail templates (image →
  preview+EXIF grid; audio → waveform+tags; 3D → mesh stats+render; generic →
  key facts), plus an always-available **"Raw" tab showing every stored field**
  (core columns, extracted metadata, user metadata, sync/provenance info).
- Full-fidelity provenance columns per item: source agent, first/last seen,
  replication seq, policy version that indexed it.

## 8. Meilisearch feature adoption plan (verified against v1.48.3)

> **STATUS (2026-08-20): SECTION CLOSED.** Adopt-now list done — tenant
> tokens, ≥1.48.2 pin (now v1.53.0), facet search, index-swap (shadow-index
> reaper), per-attribute typo tolerance, cutoff guard. **Binary quantization
> shipped 2026-08-20** as opt-in `FILEARR_SEMANTIC_QUANTIZE` (embedder
> `binaryQuantized`; one-way on a live index — un-quantize = rebuild-index,
> logged at boot). **Dumpless upgrades were already adopted**: compose/Unraid
> set `MEILI_UPGRADE_DB=true`, verified live on the 1.49→1.53 bump (ops
> runbook §meili-upgrade). **Task webhooks: DECLINED, final** — the original
> SSRF caution is moot on the ≥1.48 pin, but every document write is now
> acked synchronously (`meili_write_ack_seconds`) and failures surface via
> /stats + MeiliWriteFailed, so a webhook ingress would add attack surface
> for no remaining benefit. `/similar` adopted (P3-T9,
> `search_similar_documents`, now with ranking scores);
> **federated multi-search + `facetsByIndex` adopted** (R8, 2026-08-10 —
> `GET /search/federated` merges the item and chunks indexes via the SDK's
> `multi_search(..., federation=Federation(...))`, RBAC scope filter in EVERY
> sub-query) and **geo filters adopted** (R8 — `_geo` projection plus
> `_geoRadius` / `_geoBoundingBox` / `_geoPoint` params on `/search`).
> **GPS STAYS GATED:** `_geo` is emitted only for a library with `expose_gps`;
> the gate still lives in `exif.strip_gps` + `Library.expose_gps` and NEVER in
> an extractor; flipping the flag off queues `reproject_library`, which rewrites
> the library's documents with add-or-REPLACE semantics so already-indexed
> points are actually removed (a plain re-sync merges and would not).

**Adopt now:** tenant tokens (RBAC); pin ≥1.48.2 (CVE-2026-57823/4); facet
search endpoint; **index-swap** pattern for zero-downtime settings changes;
**task webhooks** (replace polling in index-sync); prefer PATCH-style document
updates (delete-by-filter breaks task batching); per-attribute typo tolerance
(off for hash/extension fields); `searchCutoffMs` guard.
**Adopt later:** hybrid/vector + local embedders + binary quantization;
`/similar` endpoint; federated multi-search + `facetsByIndex` (if indexes split
per tenant/type); dumpless upgrades; geo filters (photo GPS).
**Not applicable:** sharding/"network" (BUSL Enterprise — single node covers
millions of docs; ~2TiB practical index ceiling), SSO/SCIM (Enterprise, console
identity only), waiting on native RBAC (roadmap-only — build app-side).
Operational notes: LMDB never shrinks on delete → periodic swap-based
compaction; task DB capped 20GiB; `maxTotalHits` governs deep paging.

## 9. License recommendation

> **DECIDED:** AGPL-3.0-or-later adopted (LICENSE + the §13 "Source" footer
> link, `FILEARR_SOURCE_URL`).

Goal: **stays open source, commercial use allowed.** All OSI licenses permit
commercial use; the real question is copyleft scope:
- **GPL-3.0** forces derivatives to stay open **only when distributed** — a
  competitor may modify Filearr and run it as a closed hosted service (the
  "SaaS loophole"). For predominantly server-side software this is the main leak.
- **AGPL-3.0** closes that loophole (network use = distribution). It is the
  de-facto standard in exactly this app category: Immich, Manyfold (moved
  MIT→AGPL deliberately), MediaManager, Mydia, MediaLyze. Commercial use,
  selling support/hosting, and enterprise self-hosting all remain permitted.
- Dependency compatibility is clean either way (MIT/Apache/BSD/LGPL deps;
  Meilisearch/Postgres run as separate processes — no license coupling).
- Trade-off: some corporations blanket-ban AGPL code even for internal use,
  which can cost adoption/contributors. Agent binaries distributed to end
  machines are equally fine under either license.

**CONFIRMED (2026-07-07): AGPL-3.0-or-later** for the server + agents
(strongest guarantee the project and its forks stay open, matching category
precedent). Already reflected in LICENSE and backend/pyproject.toml. Keep
contributions CLA-free (DCO sign-off instead) so no single party can
relicense, and register the "Filearr" name/logo as the trademark lever.

## 10. Sequencing sketch

> **Both waves DELIVERED** (v2 July 2026; v3 July–August 2026, SAML excepted —
> see §3). What remains project-wide is the tail-item inventory in §§11–19
> plus similar-files/content-sniffing (§5/§4, in progress) and LLM M2/M3
> (§21).

v1.x (current roadmap) → **v2**: content extraction + OCR, saved searches,
hash search UI, semantic search, alerting rules, extended metadata profiles,
custom displays → **v3**: agent platform (enrollment CA, local index +
replication, central policy), RBAC + LDAP/SAML/OIDC, machine groups, local
CLI/web, hot-folder scheduling, per-agent alerting.

## 11. Deferred enhancements from T1 (ffprobe video extraction)

> **Status 2026-08-19 (later the same day): SHIPPED.** Dolby Vision
> profile/level/base-layer compatibility now come from the stream-level DOVI
> configuration record (no frame probe needed), and a bounded "deep probe"
> (`-read_intervals %+#6 -show_entries frame_side_data`, HDR streams only,
> `FILEARR_FFPROBE_DEEP_HDR`) tells HDR10+ (ST 2094-40) from HDR10 and records
> MaxCLL / MaxFALL / mastering display — Python and the Go agent in lock-step,
> verified live against an x265 HDR10 sample. pymediainfo cross-check:
> DECLINED (single source of truth; nothing it adds has been asked for).
- **Precise HDR10+/DoVi profiling.** *(SHIPPED 2026-08-19, see above.)* T1 detects HDR from stream-level colour
  signalling (transfer=smpte2084 → HDR10, arib-std-b67 → HLG, DOVI side-data →
  Dolby Vision, bt2020 primaries → generic HDR). Distinguishing HDR10 vs HDR10+
  reliably, and reading the Dolby Vision profile/level, needs per-frame side
  data (`ffprobe -read_intervals` / `-show_frames`) — a heavier probe. Deferred
  as a major item; revisit with a dedicated "deep probe" opt-in when a real HDR
  library exists to validate against.
- **pymediainfo cross-check.** *(DECLINED 2026-08-19.)* The stack already pins `pymediainfo`; a second
  extractor could corroborate codecs/track languages and fill fields ffprobe
  omits (e.g. some container-specific tags). Left out of T1 to keep a single
  source of truth; consider a merge strategy later.

## 12. Sidecar follow-ups (deferred from T3, 2026-07-07)

> **Status 2026-08-19 (later): CLOSED.** JRiver `*_JRSidecar.xml` parsing
> shipped (`filearr/jriver.py`, MPL `<Item><Field Name=…>` dialect, conservative
> known-field map → `jr_*` + external ids, defusedxml posture, association
> stat `jriver_parsed`); directory-artwork ambiguity resolved with a dominance
> rule (folder-level artwork links only when the largest primary is ≥ 2× the
> runner-up, else unlinked — season folders no longer mis-attribute); NFO
> authority via `FILEARR_SIDECAR_METADATA_PRIORITY=fill|sidecar` (per-field /
> per-library priority remains a possible refinement, unrequested). Subtitle
> sidecars were already decided/shipped.
T3 shipped detection + parent linking (`items.sidecar_of`, ondelete CASCADE),
Kodi NFO → parent `metadata` parsing (defusedxml, XXE-safe), and search
exclusion (`is_sidecar` filterable; endpoint hides sidecars unless
`include_sidecars=true` or `sidecar_of=<id>`). Remaining, non-trivial:
- **JRiver `*_JRSidecar.xml` parsing.** T3 detects + links these only. JRiver
  has no ecosystem API and the XML schema is proprietary/verbose; treat as a
  future extracted-metadata source once the field mapping is reverse-engineered
  (parse defensively, same untrusted-input posture as NFO).
- **Subtitle sidecars (`.srt`/`.ass`/`.sub`) — DECIDED 2026-08-11, already
  shipped.** User decision: keep them searchable, with a `subtitle` facet.
  That is the live behaviour and predates the decision — the W8 taxonomy work
  gave subtitle formats their own `file_group` (`subtitle`, "Subtitle /
  caption") under `file_category: video`, sidecar detection never touches
  them, and `file_group` is both filterable and facet-searchable, so
  `?file_group=subtitle` is the facet. This entry was stale, not open.
- **Directory-artwork ambiguity.** Directory-level artwork (`poster.jpg`,
  `movie.nfo`) links to the *largest* primary media file in the folder. For
  multi-movie or season folders this may mis-attribute. A stronger model =
  a per-directory "primary item" concept (folder → canonical item) rather than
  the size heuristic; revisit if users report mis-links.
- **NFO as user-facing metadata source.** NFO values currently land under
  `nfo_*` keys in extracted `metadata` and only promote to `title`/`year` when
  empty. A future "metadata source priority" setting (NFO vs. ffprobe vs.
  guessit vs. online scraper) would let users choose authority per field.


## 13. Move/rename detection follow-ups (deferred from T2, 2026-07-07)

> **SHIPPED 2026-07-24** (two of the four): **cross-library moves** — at scan
> end, after the intra-library pass, unmatched new rows are matched against
> `missing` tombstones in OTHER libraries and, when byte-confirmed
> (content_hash, or the new mid_hash sample) AND unambiguous, the tombstone is
> revived into the scanning library with identity intact
> (`detect_cross_library_moves`; kill switch
> `FILEARR_SCAN_CROSS_LIBRARY_MOVES`; agent-owned libraries excluded; a size
> prefilter keeps the walk free of speculative hashing). **Mid-file sampling**
> — `items.mid_hash` (64 KiB xxh3 centred on the midpoint, NULL <=128 KiB),
> stamped by extraction and used by `plan_moves` as the confirm/veto tier when
> content_hash is unavailable, rescuing quick_only collision-family moves that
> were previously refused as ambiguous. **Closed 2026-08-19:** the
> scan-thread hashing/policy interplay was already honoured — `detect_moves`
> / `detect_cross_library_moves` take the library's resolved T7 policy
> (`compute_content`, `full_max_bytes`) and skip full-hash confirmation for
> quick_only libraries and above the ceiling (`_ensure_hashes`); in-place
> swap stays a documented non-goal. **Nothing open in §13.**
T2 shipped identity transfer on rename/move: at scan end, before tombstoning,
vanished rows are matched to newly-discovered rows by `(quick_hash, size)`,
confirmed with `content_hash` when both sides have one, and — only when
unambiguous — the original id/tags/user_metadata/external_ids/first_seen are kept
while path/rel_path/filename/mtime/hashes move onto the surviving row. Ambiguous
buckets (multiple candidates a content hash can't separate) fall back to
tombstone+create; counts land in `ScanRun.stats` (`moved`, `move_ambiguous`).
Deliberately out of scope / open for later:
- **Hashing new files on the scan thread.** `detect_moves` computes quick/content
  hashes for newly-discovered rows synchronously so it can match at scan end. On a
  network mount with a large first-scan or a big new drop this front-loads IO that
  the extract queue would otherwise spread out. When T7 (per-library "quick_hash
  only on network storage" / size-ceiling) lands, move detection should honour the
  same policy and skip full-hash confirmation above the ceiling (matching then
  rests on `(quick_hash, size)` alone — acceptable given the ambiguity guard).
- **In-place content change is not an identity event.** A file whose *bytes*
  change but whose `rel_path` is unchanged is treated as `changed` (identity =
  `(library_id, rel_path)` is stable), never a move. That is correct by the
  identity invariant, but means a "swap in place" (A.mkv and B.mkv exchange
  contents, names unchanged) does not swap identities. No action expected; noted
  so the behaviour is not mistaken for a bug.
- **Cross-library moves.** Detection is scoped to a single library's row set. A
  file relocated from library X to library Y tombstones in X and is created fresh
  in Y (identity not carried). Cross-library identity transfer would need a global
  hash index and a policy for differing include/exclude rules; defer to v2.
- **quick_hash-only ambiguity at scale.** quick_hash is first+last 64 KiB xxh3;
  distinct files that share head+tail+size (e.g. same intro/outro, padded
  containers) collide. Today such a collision during a move is refused
  (`move_ambiguous`) unless a full `content_hash` disambiguates. A future
  mid-file sampling tier could rescue more true moves without a full re-hash.

## 14. SSE live-progress follow-ups (deferred from T4, 2026-07-07)

> **SHIPPED 2026-07-24** (the two actionable items): **push instead of poll**
> — the scan task fires `pg_notify('filearr_scan_progress', scan_id)` on the
> same transaction as every stats commit; the API runs ONE listener connection
> (`filearr.pgnotify.ScanProgressHub`) fanning out to per-stream queues, with
> a 5 s fallback poll as the degraded path (a lost listener degrades to
> polling, never a stall). **Query-param key replaced** — `?api_key=` is GONE
> from the scans AND transfers SSE endpoints; browsers now POST
> `/scans/{id}/events-token` (or `/transfers/{id}/events-token`) under normal
> auth and attach the returned single-use, 60 s, resource-scoped
> `?stream_token=` (`filearr.streamtokens`); session-cookie users need no
> token at all (EventSource sends cookies). Still open: a multiplexed
> all-scans firehose (scalability, when it matters); the transfers stream
> still polls internally (its writers are agent-driven; token auth shipped,
> push did not).
T4 shipped native `EventSourceResponse` for `GET /scans/{id}/events` (progress /
done / error events, framework keepalive pings, clean disconnect teardown), an
SSE-consuming Admin page (live batch counter + files/s, bounded-backoff
reconnect, one authoritative refresh on stream end), and the AGPL §13 footer
"Source" link (`__SOURCE_URL__`, overridable via `FILEARR_SOURCE_URL` at build
time). Remaining, non-trivial:
- **Push instead of poll (major).** The stream still polls `ScanRun.stats` once
  per second inside the request handler. A true push path — Postgres
  `LISTEN/NOTIFY` fired from the scan task's batch commit, or a Procrastinate
  event — would cut latency to real-time and drop the per-connection DB read
  loop. It touches the scan task's publish mechanism (owned by another agent
  during T4) and needs a NOTIFY payload contract, so it was deferred. The
  current handler is additive-only over `stats`, so a NOTIFY layer can slot in
  without changing the wire schema.
- **Query-param API key for SSE (revisit for tenant tokens).** `EventSource`
  can't set an `Authorization` header, so the events endpoint also accepts the
  key as `?api_key=` (read scope, this endpoint only; verified via the same
  hash+scope path, never logged). Query-string secrets can leak into proxy /
  access logs. When the v2 auth work lands (roadmap items 4/5), replace this
  with a short-lived same-origin cookie or a scoped one-time stream token minted
  by an authenticated POST, and drop the query-param path.
- **Multiplexed progress stream (scalability).** One `EventSource` per running
  scan is fine for a handful of libraries; a single `/scans/events` firehose
  (all running scans over one connection, filtered client-side) would scale
  better for large multi-library deployments. Deferred until it matters.

## T5 — Scheduled + watch-mode scanning (shipped, with follow-ups)

Shipped: one static Procrastinate periodic task `filearr.worker.schedule_scans`
(`@periodic(cron="* * * * *")`) that evaluates each enabled library's
`scan_cron` against the tick with **cronsim** (croniter is EOL) and defers a
scan for the due ones; a scan already `running` for a library is skipped, and a
`queueing_lock` of `scan:<library_id>` collapses a duplicate/late tick (or a
tick racing a manual scan) so a minute can enqueue at most one scan. `scan_cron`
is validated at the API on create/PATCH (invalid → 422; empty/null disables).
Watch mode is watchfiles-based, refused server-side for network roots
(`/proc/self/mountinfo` fstype classification: cifs/nfs/fuse-remote → refused),
debounces change bursts into one normal full scan, and is supervised by a
reconcile loop that starts/stops watchers on config change without a restart.

Follow-ups (deferred):
- **Incremental watch scans — SHIPPED 2026-07-24** (the pragmatic form of the
  "incremental single-file updates" item below): a small event batch
  (<= `FILEARR_WATCH_INCREMENTAL_MAX_EVENTS`, default 64) narrows to ONE
  targeted recursive scan (the W9 machinery) of the nearest existing ancestor
  directory covering every event path (`watch._narrow_scope`), composed under
  the watcher's own scan_path scope. Same pipeline and invariants — scoped
  diff never tombstones outside the target, move detection works within the
  scope — so a single dropped file costs one directory walk instead of a
  1M-item rescan. Big bursts and root-level events fall back to the legacy
  whole-tree scan; scheduled full scans stay on as the reconciliation
  backstop. A TRUE per-file upsert path (no walk at all) remains future work,
  with the same caveats as before.
- **Run the watch supervisor as a first-class process (medium).** The supervisor
  entrypoint `filearr.worker.run_watch_supervisor()` exists but is not yet wired
  into a container. Options: (a) a dedicated `watcher` compose service running
  `python -m filearr.watchd` next to the Procrastinate worker; (b) a Procrastinate
  worker startup hook that launches it as a background task in the same loop.
  Chose to ship the reusable supervisor + entrypoint now and wire the process in
  a follow-up so the periodic scheduler (the primary T5 deliverable) isn't
  blocked on the watcher's deployment shape. Until wired, watch_mode is validated
  and persisted but only *acted on* once the supervisor process runs.
- **Incremental single-file updates on watch events (major).** Today a watch
  event triggers a normal whole-library scan (walk + diff + tombstone). That is
  intentional — move/rename detection and sidecar association only make sense
  with whole-library context, and one scan path is far less risky than two. A
  true incremental path (upsert/extract just the changed paths from the
  watchfiles event set, skipping the full walk) would cut latency and IO on
  large libraries but needs: partial move/sidecar reconciliation, a correct
  tombstone story for deletes seen only via inotify, and careful interaction
  with the batched-commit/cancellation invariant. Deferred as a major item.
- **Sub-minute / second-level cron (low priority).** cronsim supports 6-field
  (seconds) expressions, but the tick is 1-minute (Procrastinate periodic
  granularity), so seconds are ignored. If sub-minute scheduling is ever wanted,
  it needs a faster tick or an in-process timer, not the periodic task.

## 15. Extractor follow-ups (deferred from T6, 2026-07-07)

> **Status 2026-08-19 (later): CLOSED.** Shipped the same day: **7z + RAR
> listing** (py7zr / rarfile, header-only, declared-size + ratio guard,
> encrypted-header refusal; `.cb7`/`.cbr` ride along; agent stays zip/tar by
> its no-new-deps rule), the **e-mail extractor** (`tasks/email_extract.py`:
> `.eml` headers/attachments/body, `.mbox` summary + searchable subject digest,
> Outlook `.msg` via olefile MAPI streams; PST/OST marked unsupported →
> `readpst`; routed by the `email` file_group override; `_EMAIL_FIELDS` on the
> system profile), and the **accurate-geometry tier**
> (`FILEARR_MODEL3D_ACCURATE_MAX_BYTES`, `geometry_tier`). Already shipped
> earlier: document/e-book body text (P3-T5) and the zip decompression-ratio
> guard (`guard_decompression`). **CAD/proprietary 3D: DECLINED** — a native
> CAD kernel (OpenCASCADE/Blender) is a deployment-footprint decision no user
> has asked for; the `unsupported` marker stands.
T6 shipped the remaining per-type property extractors: **model3d** (trimesh —
triangle/vertex counts, bbox extents + volume, watertight flag, multi-mesh scene
aggregation for GLTF/GLB/3MF), **document** (pypdf page count + core properties +
encrypted flag; python-docx core props + paragraph count), **spreadsheet**
(openpyxl read_only/data_only — sheet names/count + core props, no cell load, no
formula evaluation), and **audiobook** m4b chapters (mutagen `chpl`, layered on
top of the existing tinytag tag read). All parsers are size-ceiling-guarded
(`FILEARR_MODEL3D_MAX_BYTES` / `FILEARR_DOCUMENT_MAX_BYTES`, both 256 MiB) and
degrade to `_extract_error` on hostile/corrupt input without failing the job.
Remaining, non-trivial:
- **CAD/proprietary 3D geometry (major).** STEP/STP, FBX, and BLEND get only a
  lightweight `{"unsupported": true}` marker — trimesh has no safe pure-Python
  loader for them, and pulling in `cascadio`/OpenCASCADE (STEP) or the Blender
  Python API (BLEND) is a heavy, native-dependency decision. Defer until there
  is real demand; when it lands, keep the same size ceiling + subprocess
  isolation discipline as ffprobe (never load an untrusted CAD kernel in-process
  without a sandbox).
- **Watertight accuracy vs. cost (medium, deferred).** model3d parses with
  trimesh `process=False` (no vertex-merge/repair) so an untrusted mesh gets no
  expensive processing pass. A side effect: meshes with duplicated vertices
  (e.g. a naive STL export) report `watertight: false` and an inflated vertex
  count even when the surface is closed. An opt-in "accurate geometry" tier
  (`process=True` under a stricter, smaller size ceiling) would fix this for
  users who want it, without exposing the default scan path to the extra cost.
- **Document/e-book text extraction for search (major, v2).** v1 deliberately
  extracts *properties only* — no PDF/DOCX body text, no EPUB/MOBI/CBZ content.
  Full-text indexing (feeding extracted body text into the Meili projection) is
  a v2 feature with its own resource-bounding, language-detection, and
  zip-bomb/decompression-ratio-guard requirements. The current extractors are
  structured so a text pass can be added as a separate, independently-bounded
  stage without changing the property schema.
- **Zip decompression-ratio guard (medium).** docx/xlsx/3mf are ZIP archives;
  today they are bounded only by the *compressed* file-size ceiling. openpyxl
  read_only + no-cell-load and python-docx's structure-only reads keep memory
  modest in practice, but a belt-and-suspenders decompressed-size / entry-count
  guard (reject archives whose declared uncompressed size exceeds a ratio
  threshold before handing them to the parser) would harden against a crafted
  zip bomb. Deferred as medium — the size ceiling covers the common case.

## 16. Hash-policy follow-ups (deferred from T7, 2026-07-07)

> **Status 2026-08-19 (later): CLOSED.** The two remaining tails shipped:
> the opt-in **content-hash backfill** for quick_only libraries
> (`backfill_content_hashes` maintenance task, no default cron; byte budget +
> rate throttle, skips agent libraries and the <=128 KiB band, age-net exempt)
> and the **per-root network-classification memo** in `hashpolicy` (60 s TTL;
> the extract worker no longer re-parses mountinfo per file). The QH-T6 rehash
> sweep (2026-08-12) had closed the correctness item.
T7 shipped per-library hash policy: `hash_policy` (`auto` | `full` |
`quick_only`) + a nullable `hash_full_max_bytes` per-library ceiling override
(null → global `FILEARR_SCAN_HASH_FULL_MAX_BYTES`). `auto` detects the root's
filesystem via T5's `is_network_path` (network → `quick_only` behaviour, local →
`full`), resolved ONCE per scan run and recorded in `ScanRun.stats.hash_policy`
for observability. quick_hash is always computed; only the whole-file
`content_hash` stream is gated. Move detection (T2) honours the resolved
`compute_content` flag: under `quick_only` a `(quick_hash, size)` collision that
would need `content_hash` to disambiguate stays ambiguous and is refused
(counted `move_ambiguous`) — integrity is never traded for a blind transfer.
Remaining, non-trivial:
- **RESOLVED — shipped 2026-07-18 (QH-T1..T5), convergence CONFIRMED 2026-08-11
  (`still_stale = 0` on the live 1.09M catalogue).** Root cause: a file in the
  64-128 KiB band had its middle and tail silently unhashed, so head-identical
  files of equal size collided. Fixed in both languages — anything <=131072
  bytes is now hashed IN FULL and gets a real `content_hash` regardless of
  `hash_policy`; `full_hash` moved to xxh3-128; provenance cfg1->cfg2 with
  `HASH_IMPL_VERSION=2`; a nightly `rehash_small_files` sweep migrated stored
  rows (now complete); the `duplicate_files` report gained a `hash_tier` column
  and a hard `size=0` exclusion (the live 3,711-file zero-byte cluster). Full
  write-up: `docs/research/hash-quickhash-false-duplicates.md`.
  Two bounded limitations remain, both by design and neither a defect:
  (a) files LARGER than 128 KiB are still head+tail sampled, so a genuinely
  ambiguous pair needs `content_hash` or `mid_hash` to separate — the report's
  `hash_tier` column says which tier grouped a cluster. **Hardened 2026-08-13
  (IN-T1):** `hash_tier` now also rides EVERY row of the new per-copy
  `duplicate_files_detail` report (computed as a window max, so all rows of a
  group report the same tier the aggregate does), and the documented cleanup
  scripts in docs-site `reports.md` **skip `quick_hash`-tier groups by default** —
  a sampled signal is a candidate for verification, never an input to `rm`.
  Opt-in `--allow-quick-hash`, or `--verify-hash` which re-hashes the keeper and
  the candidate with `xxhsum -H2` and compares them to EACH OTHER (never to a
  stored digest, so it is immune to how we happen to store hashes today);
  (b) **CLOSED 2026-08-12
  (QH-T6)** — agent-owned libraries were excluded from central's sweep (central
  cannot open those files, and `agentsync.apply_batch` never writes
  `policy_version` for agent rows, so no central query can even identify a stale
  agent hash), leaving stable agent-side files with their pre-fix `quick_hash`
  indefinitely (brief §9.2). The brief declined to build an agent-side sweep on
  the assumption that agent libraries were a fraction of the catalogue; they are
  now effectively all of it (98,628 affected rows across seven libraries), so the
  ruling was revisited and the sweep built:
  `agent/internal/rehash` + the agent-scoped `rehash_sweep` command
  (`POST /agents/{id}/rehash-sweep`, admin scope, TTL 86,400s, 409 single-sweep
  guard). Operator-triggered, never automatic — an unprompted fleet-wide I/O
  storm is exactly what an operator needs to schedule. Defaults to the defect
  band 65537..131072 (overridable per run; the wider <=131072-from-zero QH-T2
  `content_hash` backfill is a deliberate opt-in, ~10x the reads). Resumable and
  idempotent on a durable per-agent cursor (`rehash_state`, fingerprinted
  `h<scan.HashSchemeVersion>-<min>-<max>`), emits ONLY on change (a row already
  repaired by an ordinary rescan is counted `verified` and costs nothing), and
  never attaches `extracted` — which is what keeps a ~99k-row hash correction
  from cascading into a fleet-wide re-extraction and Meili re-index. State is
  reported in the agent HEALTH block and rendered as the "Hash migration" row on
  the per-agent About panel; that is the only place it can be seen, since central
  holds no hash provenance for agent rows. Runbook: `docs/ops/agents.md` §14;
  user-facing: docs-site `agents.md#agent-rehash`.
  ORIGINAL REPORT (retained for context): quick_hash produced thousands of
  false duplicate detections on small files. The duplicates
  surface (P3-T10 badge + the `duplicate_files` canned report) falls back to
  `(quick_hash, size)` grouping when `content_hash` is absent (`quick_only` /
  network libraries), and live data shows large clusters of small files
  flagged as duplicates that are NOT byte-identical. Research must (a)
  reproduce and root-cause the collisions — quick_hash samples first+last
  64KiB via xxh3, so for files ≤128KiB it covers the whole file and identical
  hash+size *should* imply identical bytes: establish whether the false
  positives come from the sampling window (files >128KiB with identical
  head/tail, e.g. padded/templated formats), from zero/sparse regions, from a
  hashing bug (offset/length handling on short files), or from the grouping
  logic itself (e.g. size not actually constrained within a group); (b) design
  the size-floor logic — do not use sampled hashing below a threshold where
  it adds nothing (small files should get a cheap FULL-file hash instead:
  full xxh3/xxh128 of a ≤128KiB file is faster than two seeks), and pick the
  threshold from data; (c) benchmark more reliable full-file hash candidates
  for the replacement tier (xxh3-128 full-file, BLAKE3, SHA-256 for the
  crypto-needed paths) on representative corpus sizes over local disk AND SMB
  — document throughput/collision trade-offs and a recommendation. Output: a
  `docs/research/` brief with the reproduction, the chosen size floor, the
  benchmark table, and the migration story for already-stored quick_hash
  values (hash_policy_version bump + lazy re-hash per FIX/cfg1 provenance
  machinery). Duplicate-report UX must state which hash tier grouped each
  cluster until the fix lands.
  A `quick_only` library never stores `content_hash`, so exact-duplicate
  detection and cross-library dedupe are weaker there. A future opt-in,
  low-priority background task could stream full hashes for network items during
  idle windows (rate-limited, cancellable, respecting the same ceiling) so the
  integrity benefit of content hashes is available without paying the cost on
  the hot scan path. Deferred until dedupe/versioning (roadmap) actually needs
  it.
- **Per-file `auto` re-detection in the extract worker (minor).** The scan
  resolves `auto` once (a mountinfo parse) and stashes the result in
  `ScanRun.stats`; the extract worker, running in a separate process, currently
  re-resolves from the library row per item (one mountinfo parse per file). This
  is correct but slightly redundant; a short-TTL per-(library, mount) cache of
  the network classification would remove the repeat parse. Minor — mountinfo
  parsing is cheap and bounded — so left as a future optimisation rather than
  threading a resolved-policy token across the job queue.

## 17. Extraction throughput (T8 follow-ups)

> **SHIPPED 2026-07-24** (both, in pragmatic form): **adaptive backpressure**
> — a worker-local, loadavg-per-core-driven trip with hysteresis
> (`filearr.backpressure.ExtractLimiter`): while tripped, extract jobs beyond
> `FILEARR_EXTRACT_BACKPRESSURE_MIN_CONCURRENCY` are rescheduled 15-45 s
> (jittered, attempt-agnostic — never failed) instead of occupying worker
> slots, so scan/index/maintenance work keeps its slots under host pressure.
> Queue depth remains untouched as a signal (throttling extract on depth
> would deepen the very queue) — host load IS the "don't starve the API"
> signal. No-op on hosts without loadavg and via
> `FILEARR_EXTRACT_BACKPRESSURE=false`. **Per-library throughput history** —
> `GET /libraries` now annotates `last_scan` with the run's `files_per_s`
> plus a rolling 30-day median over finished FULL scans
> (`median_files_per_s`, `throughput_runs`); the Admin page badges "slower
> than usual" when a run comes in under 60% of a >=3-run median.
>
> **SHIPPED 2026-08-10 — the control loop.** The binary trip is now an AIMD
> controller in `filearr.backpressure`: a ceiling floating between
> `..._MIN_CONCURRENCY` and `..._MAX_CONCURRENCY` (auto =
> `FILEARR_WORKER_CONCURRENCY`), **multiplicative decrease** on host pressure
> (halve per sample above the high water, so a brief spike costs one step, not
> the whole recovery window) and **additive increase** (one slot per sample)
> only when pressure is at/below the low water AND a *bounded* probe of the
> extract queue says work is waiting. Anti-thrash: one move per sample plus a
> 60 s post-contraction expansion cooldown (the 1-min loadavg lags by about
> its own window). A 32-sample in-process ring backs `snapshot()`; transitions
> are logged INFO with their inputs, still not dashboarded (the API process's
> limiter is idle). **This resolves the contradiction below**: the deferred
> bullet ("keep queue depth as the primary signal") and the 2026-07-24 note
> ("depth remains untouched — host load IS the signal") are each right about
> one DIRECTION. Depth may never throttle (that deepens the very queue it
> reacts to) but it is the only sound reason to expand; pressure only ever
> contracts. Reasoning in the `backpressure.py` module docstring; operator
> view in `docs-site/operations.md#extract-backpressure`.
- **Adaptive extract concurrency / backpressure (major, deferred).** T8 ships
  *static* knobs: per-worker `--concurrency`, per-queue worker pinning, and a
  negative extract priority so scan-control jumps the queue. It does **not**
  auto-tune. A v2 controller could watch the `extract` queue depth (already
  exposed in `/api/stats`) and the DB/CPU pressure and scale worker concurrency
  or throttle defer rate dynamically (token-bucket on the defer path, or a
  Procrastinate worker `--shutdown-graceful-timeout`-aware autoscaler). Deferred
  as major: it needs a control loop, metrics history, and careful anti-thrash
  hysteresis; the static knobs cover the "don't starve the API during a big
  scan" acceptance criterion today. When built, keep queue depth as the primary
  signal and never let extraction preempt scan-control (the priority invariant).
- **Per-library / per-run throughput history (medium, deferred).** `files_per_s`
  and `walk_seconds` are recorded on each `ScanRun.stats` but not aggregated. A
  small rollup (rolling median files/s per library, extract-drain time) would let
  the Admin UI show "this scan is slower than usual" and size worker counts from
  real history rather than a guessed default. Cheap to add on top of the existing
  per-run stats; deferred only for UI scope.

## 18. Error surfacing (T11 follow-ups)

> **SHIPPED 2026-07-24 (first item):** persisted job failure text. A
> Procrastinate 3.9 **worker middleware** (`filearr.joberrors` — cleaner than
> the custom failure hook sketched below) records a sanitized message +
> 8 KB-capped traceback per failed attempt into the new `job_errors` table
> (no FK into Procrastinate's tables; purged on the same
> `job_history_retention_days` window by the FIX-8 purge). Control-flow
> reschedules (`filearr_transient`) and operator aborts are never recorded.
> `/system/failed-jobs` now fills `error` (+ `traceback`) from the newest
> recorded attempt; the Admin/Jobs failed tables render the message with the
> traceback on hover. The per-run counter design note below still stands.
- **Persisted job error text / tracebacks (major, deferred).** Procrastinate
  3.9's `procrastinate_events` table stores only `(job_id, type, at)` — it does
  **not** persist the exception message or traceback of a failed job (that goes
  to worker logs). So `/api/system/failed-jobs` can surface *which* job failed,
  its queue/task, attempt count, and *when* the last event fired, but `error` is
  always null. Capturing the actual traceback would need a custom failure hook
  (Procrastinate `JobContext`/retry callback) writing to a dedicated
  `job_errors` table (job_id, attempt, sanitized message, ts) with its own
  retention purge. Deferred as major: it adds a write path + table + purge and
  duplicates what structured worker logs already give operators. The item-level
  `_extract_error` path (parser messages, surfaced per-library) already covers
  the "corrupt file is visible, not silent" acceptance criterion; the missing
  piece is only *infra* job failures (OOM, DB blips), which logs capture today.
- **Per-run error counter is best-effort, not authoritative (design note).**
  The atomic `ScanRun.stats.extract_errors` counter is bumped by the extract
  worker via a single race-free SQL `jsonb_set` increment, but extract jobs run
  asynchronously *after* the scan finishes and a run row can be purged/absent, so
  the counter can undercount (never over). The **authoritative** count is the
  live GIN-indexed aggregate (`items.metadata ? '_extract_error'`, exposed in
  `/api/stats.extract_errors` and `/api/libraries/{id}/errors`). If a strictly
  exact per-run attribution is ever required, it needs a transactional outbox
  linking item→run at extract time (major); the current split (authoritative
  live count + convenience per-run counter) is the confirmed T11 approach.

## 19. Test suite + CI (T10 follow-ups)

> **SHIPPED 2026-07-24 (first item):** the N->0 empty-mount guard moved from
> "infra-owned" to a code-level check after all: a FULL scan whose walk
> observes literally no entries (nothing seen, excluded, or pruned — i.e. the
> tree is empty, not merely filtered) over a library holding active items now
> FAILS the run (nothing tombstoned) with a clear dead-mount message. The
> false-positive escape hatch the note demanded exists twice over:
> `POST /libraries/{id}/scan?force_empty=true` consents per-run, and
> `FILEARR_SCAN_EMPTY_GUARD=false` disables globally. A readable-but-filtered
> tree (entries seen but all excluded) passes the guard — the mount is
> demonstrably alive, so a genuine bulk deletion still tombstones normally.
- **Empty-but-mounted root vs dead mount (deferred; infra-owned).** The T10
  `scan.assert_scannable_root` pre-flight aborts a scan when the root is missing,
  not a directory, or `scandir` raises (ENOENT/ENOTCONN/EACCES) — this stops a
  dropped mount that *disappears* or *errors* from tombstoning the whole library
  (invariant 7). It intentionally does NOT abort on an **empty but readable**
  directory: that is indistinguishable from a legitimately-emptied library, and
  refusing to scan it would break real "user deleted everything" cases. A dead
  FUSE bind that presents as a *stale-empty* readable mountpoint is therefore
  still handled at the infra layer (compose `bind.propagation: rslave` + the
  deploy-time read-test), not in code. A future code-level guard could compare
  the walk's seen-count against the prior scan and refuse a scan that drops from
  N>0 to 0 files unless a `--force-empty` / library flag is set (heuristic;
  needs a false-positive escape hatch). Deferred: risk of blocking legitimate
  bulk deletions outweighs the marginal gain over the infra fix.
- **CI matrix / caching polish (minor).** The gate runs a single Python (3.13)
  and single Node (24) to mirror production; a version matrix (e.g. Python 3.13
  only for now, 3.14 once C-ext wheels land per the Dockerfile note) can be added
  when 3.14 support is in scope. uv + npm caches are enabled; a pytest-xdist
  parallel split and a Meili-backed projection integration job (currently the
  index sync is unit-tested with the defer mocked) are candidates as the suite
  grows past Phase 1.
- **Ruff scoped ignores are documented, not silent (design note).** CI is a true
  lint gate (`ruff check .` must pass). Pre-existing idioms are accepted via
  documented per-file/global ignores rather than code churn: `B008` (FastAPI
  `Depends()` default), `UP042` (`class X(str, enum.Enum)` — load-bearing for
  SQLAlchemy Enum + JSON/Meili serialization; migrating to `enum.StrEnum` changes
  `str(member)` semantics), init_db `E402` (deliberate `sys.path` shim), and
  `alembic/versions` excluded (autogenerated). Revisit `UP042` only alongside a
  deliberate StrEnum migration with serialization tests.

## 20. UX + preview backlog (user-requested, 2026-07-24)

> **All seven items SHIPPED 2026-07-24** (same-day). The bullets below stay as
> the design record — notably the STL rendering investigation (central =
> trimesh + an in-process numpy point-splat rasterizer with a supersample→blur→
> WebP-ladder pipeline; agent = pure-Go fauxgl, STL-only) and the ffmpeg
> policy (optional dep: install-time WARN + `capabilities.ffmpeg`
> advertisement, never a hard requirement).

Small, high-leverage items from live daily use. None are architectural; each is
scoped enough to ship independently.

- **Search page starts empty.** Do not auto-load results (the match-all query)
  when the main page opens; render results only once a search/filter is actually
  submitted. Empty state = the search box + a short hint. Saves the initial
  Meili round-trip on a 1M+ catalog and stops implying "these files are a
  result of something".
- **Collapse the filter + advanced-search boxes by default.** Both panels take
  vertical space on every visit; collapse them behind their toggles (persist
  the open/closed choice in localStorage next to the existing theme choice).
- **File details / raw metadata: clamp long values.** `metadata_` for some
  items (OCR text, archive member lists, ffprobe dumps) makes the details and
  Raw views extremely long. Give the metadata area a max height with an
  expand/collapse control when the rendered content overflows (CSS line-clamp +
  "show all (N lines)"); never truncate what expansion reveals.
- **Click-outside closes the file-details view.** Clicking the backdrop around
  the details window returns to the search results (same path as the existing
  close button; keep Esc working; ignore clicks that started inside the panel —
  text selection must not dismiss).
- **Saved/Filters toggle buttons need a real state affordance.** The header
  icons for "saved" and "filters" look identical active and inactive. Give the
  active state a filled icon variant + accent color + `aria-pressed`, so the
  toggle state is legible at a glance (and to screen readers).
- **STL/3MF preview thumbnails — what it takes.** Investigated 2026-07-24:
  - *Central (worker):* no new dependency is strictly needed. trimesh (already
    a dep) loads the mesh; render via a small in-process software rasterizer —
    orthographic isometric projection, per-face Lambert shading, numpy
    z-buffer/painter sort into a Pillow image — then hand the bitmap to the
    existing tier ladder/byte caps (thumbs.py). trimesh's own
    `scene.save_image()` is NOT usable headless (pyglet needs a GL context /
    xvfb). Guard rails: reuse `model3d_max_bytes` for load, cap rendered
    triangles (decimate/sample above ~1-2M faces), route the `model3d`
    extractor category into `_resolve_source` beside image/video/document.
  - *Agent (Go, CGO_ENABLED=0):* `github.com/fogleman/fauxgl` — pure-Go
    software renderer with a built-in STL loader, MIT — fits the static-binary
    constraint; encode JPEG like the existing agent thumb path (P12-T13 upload
    contract unchanged).
  - 3MF needs the mesh extracted from the zip container first (trimesh handles
    it centrally; the agent would start STL-only).
- **ffmpeg on agents: document + install-time check.** Agent video
  poster-frames require an ffmpeg binary on the agent host (resolved from PATH
  or `FILEARR_AGENT_FFMPEG_PATH` — thumbs.go). Today a missing binary just
  means silently absent video thumbs. Needed: (a) a requirements note in
  docs/ops/agents.md + the install instructions (Windows: winget/choco or a
  static build; Linux: distro package; macOS: brew), and (b) an install/enroll
  -time check — `exec.LookPath` in the installer/`filearr-agent install` that
  WARNS (not fails: ffmpeg is optional, image/audio thumbs work without it)
  and states what will not work; surface the same probe in the agent's
  capabilities advertisement so central's fleet console can show which agents
  lack it.

## 20b. Phase-2 leftovers P2-T7 / P2-T8 — DONE 2026-08-19

- **P2-T7 preset bundles → DB**: `preset_bundles` table (migration
  c8d9e0f1a2b3), builtins mirrored from code (fork-not-mutate; a builtin never
  overwrites a same-named custom row), `filearr.preset_registry` TTL cache
  merged into the live `PRESET_BUNDLES` mapping every walk reads, CRUD/fork API,
  Admin → Exclusion presets panel, `version` column reserved for agent
  distribution.
- **P2-T8 default search locations per platform**: delivered by the agent's
  per-OS presets (`user-documents`, `user-media`, `downloads`, `server-data`,
  `user-profiles-full`) consumed via config-group `scan_selections`, which drive
  scan roots since 2026-08-18; enrolment tokens can pre-assign the group.

## 20c. P6-T10 service accounts — DONE 2026-08-19

`api_keys.service_account_id` (migration e0f1a2b3c4d5, existing keys backfilled
under a "Pre-existing keys" account), `/service-accounts` CRUD, key minting
requires an owning account (no orphan keys), disabling an account refuses its
keys per request, deleting revokes them (CASCADE); Admin → Service accounts +
owner selector on the API-keys form.

## 20d. P8-T3 apprise channel — DONE 2026-08-19

The driver, tests and docs shipped with Phase 8; the deferred piece was that
the extra was not in the image. `apprise==1.12.0` is now installed in the
runtime image (still an optional extra for bare-Python installs).

## 21. LLM / RAG integration (SHIPPED: M1 2026-07-28; M2+M3 2026-08-06)

Let LLMs (Ollama, OpenWebUI, MCP clients) query the catalog as a tool
backend and analyze retrieved content RAG-style. Full design in
`docs/research/llm-rag-integration.md`: one 8-tool facade (`/api/llm/v1`)
exposed as OpenAPI tool server + MCP + OpenAI function specs; LLM-grade
API keys (role, path/library scope, content flag, rate limit) on the
existing Bearer/RBAC substrate; a server-rendered capability handshake and
system prompt so the model always knows the system and its assigned role;
two RAG tiers (metadata-RAG over existing search/extract now; doc_chunks +
Meili vector chunk index later). Phases M1 (read tools) / M2 (content
chunks) / M3 (curator writes) — ALL SHIPPED: M2 (2026-08-06) added
doc_chunks + persisted chunk vectors + the `<index>_chunks` projection +
retrieve_passages behind the per-library chunking_enabled opt-in; M3
(2026-08-06) added the curator role (tag_files/annotate, PATCH-only,
ItemVersion-attributed) + the per-key tool-call dashboard (and fixed M1's
silently-dropped facade audit events — ApiKey uuid vs principals FK).

## 22. Permissions enumeration, reconciliation & audit (W7)

> **v1 SHIPPED 2026-08-19** (W7-T2 Linux read, T3 Windows read, T5 fidelity,
> T6 `permission_snapshots` + ingestion, T7 two canned reports; T2a resolved as
> "pure Go: POSIX ACL xattr decode, no CGO/shell-out"). Rulings on the §9.1
> questions: storage = wide JSONB per (agent, path, run) + denormalised
> `principals text[]` (GIN); agent-only capture (no central-scanner parity);
> exclusion applied report-side (collection stays full-fidelity); no SACL in v1;
> Samba share-ACLs docs-only; retention 10 per path. **T9 SHIPPED 2026-08-19**:
> `permission_changes` report (LAG over consecutive snapshots per (agent, path),
> diffed row-side with `diff_records` via the `record_from_wire` adapter,
> `summarize_diff` details column, threshold = last N days) + the "System:
> permission change" rule fed by `permission_ingest` after the batch commit
> (`alerts.ops.emit_permission_change`, hourly+digest dedup; fidelity-only
> changes recorded, not alerted); ingest also links snapshots to catalog items
> via the agent library root. **Still open:** T4 macOS read, T8 principal
> canonicalisation, T10 effective access. **All three shipped 2026-08-20:**
> T4 — darwin `ls -led` reader (named perms → verbs, order preserved, BSD
> flags as `Record.Flags`, TCC/FDA denial = collector error; parser untagged
> and unit-tested on Linux); T8 — `principal_aliases` table + admin CRUD,
> LEFT-JOINed into the by-principal/broad-access reports (canonical identity
> shown, raw id kept; snapshots never rewritten); T10 —
> `permissions.effective_access` pure evaluator (ordered deny-before-allow,
> POSIX class selection, `full` expansion, local ∩ share intersection) +
> `GET /permissions/effective-access` over the newest snapshot (caller
> supplies the identity closure). **Section closed.** Original note follows.

**Status: scaffolded on both sides 2026-07-18, inert since.** Recorded here on
2026-08-10 because it had no roadmap entry at all — the design lived only in
`docs/research/permissions-enumeration-audit.md`, which made the largest
unstarted item on the project invisible to anyone reading this file.

The idea: enumerate filesystem ACLs (Windows DACLs, POSIX modes + ACLs, macOS)
for indexed items, reconcile them against the catalog, and report on them — "who
can actually read this share", "what changed since last week", "which files are
world-readable". Permissions are read **agent-side** by design: only the agent
can see a remote host's ACLs, exactly as with content extraction.

What exists today is deliberately non-functional:

- `backend/filearr/permissions.py` — the record schema plus pure diff/filter
  logic. Explicitly inert: it performs no OS reads, and the four permission
  report builders are typed stubs raising `NotImplementedError`. They are NOT
  registered in the live canned-report registry, so nothing can invoke them.
- `permission_snapshots` — documented in the research doc, **not** a live
  SQLAlchemy model and not migrated.
- `agent/internal/inventory/permissions/` — the collector returns
  "permissions collector is scaffold-only: per-OS ACL reads not implemented
  (W7)". `permissions_windows.go` / `permissions_posix.go` /
  `permissions_darwin.go` / `masks.go` carry `TODO(W7-Tn)` markers where the
  actual reads belong; only the record/mask/diff cores are real.

Why it stays low priority: nothing depends on it, the catalog is fully useful
without it, and the per-OS ACL surface is the kind of work that is only worth
starting when someone actually needs the answer it produces. When that happens,
the sequence is: per-OS reads behind the existing collector interface → the
snapshot table + migration → wire the report builders into the registry → an
audit/diff view. The pure cores are already tested, so the remaining work is
genuinely the OS-specific reads and the plumbing, not the design.

**Do not mistake the scaffold for a partial implementation.** Every entry point
fails loudly and on purpose; there is no half-working path a user could stumble
into.

## 23. Staged binary-release rollout on the config tier engine

> **DONE 2026-08-19.** `agent_release_rollouts` (migration b7c8d9e0f1a2), same
> tier engine + bucket + minute tick; the manifest poll consults it; console
> shows release rows in *Rollouts in flight* and a *Phased release rollout…*
> form; cancel = stop offering (documented as such). Below is the original note.

**Deferred from P13 (2026-08-11).** The phased-rollout engine that P13 built for
*configuration* is generic in everything except what it targets. It already has:
tier validation (≤5 entries, `{percent, delay_minutes}`, strictly ascending,
last must be 100), a stable storage-free cohort
(`sha256(agent.id.bytes)[:4] % 100`, so a fleet that grows mid-rollout keeps a
uniform slice), scheduled starts, one-live-per-target enforcement, promote-now
and cancel endpoints, and the every-minute worker tick that advances one tier at
a time and skips itself during maintenance mode. What it does **not** do is
decide which agents are offered a binary.

Binary releases meanwhile lost their staging in the same change: every uploaded
release is generally visible once its artifacts are present, the `auto_update`
key in a config group is the brake, and per-agent `self_update` commands are the
targeting mechanism. That is honest and simple, but staging a fleet-wide binary
update means hand-maintaining a group's membership — the busywork the tier
engine exists to remove.

The work, when someone wants it:

- an `agent_release_rollouts` row (or a nullable `release_version` discriminator
  on `agent_config_rollouts`) targeting a release version instead of a group
  version;
- the update-manifest poll (`api/agent_updates.py`) consults it: an agent whose
  bucket is not covered gets the same `204` it gets today with `auto_update`
  off. `auto_update: false` must still win — a rollout tier is "may be offered",
  not "must take";
- the worker's `_advance_config_rollouts()` generalises to "advance every due
  rollout"; the tier semantics, audit events and cancel/promote endpoints are
  reused verbatim;
- the console's live-rollouts panel gains release rows alongside config rows.

**Open question to settle first:** what "cancel" means for a binary rollout.
Cancelling a config rollout makes covered agents fall *back* on their next poll,
which is free. A binary cannot be un-swapped that way — the agent has already
replaced its own executable — so cancel can only mean "stop offering it", and
the doc must say so plainly rather than borrowing the config wording. The
boot-counter automatic rollback (§8.3 of `docs/ops/agents.md`) stays the only
real un-install path.

## 24. Nvidia GPU acceleration — assessed 2026-08-12, not worth building today

Assessed against the reference deployment (1.09M items; pictures 470k, video
330k; agent libraries over SMB). Verdict: **no current workload justifies it.**

- **Video thumbnails (NVDEC):** the workload is seek-then-ONE-frame per fresh
  subprocess over SMB — decode is not the bottleneck, the CUDA context init is
  paid on every call, and consumer-card session caps (2-3) collide with the
  configured inline concurrency (4). The existing QSV path is decode-only with
  software fallback for the same reason.
- **Image thumbnails:** Pillow has no GPU path; not GPU-eligible work.
- **Text embeddings (bge-small, 384-dim):** maybe 2-4x on a job deliberately
  throttled to one lowest-priority worker (~5.5h one-time backfill that blocks
  nothing). onnxruntime-gpu's CUDA/cuDNN layers add hundreds of MB-GB to EVERY
  operator's image and pin a CUDA version against Unraid's driver-plugin
  lineage — the same version-matrix tax this file already tracks for
  Meilisearch and Vite.
- **OCR:** the blocker is licensing/stability, not GPU availability — Surya
  rejected (OpenRAIL-M revenue cap), PaddleOCR blocked on a Docker segfault at
  research time (re-verify upstream before revisiting; that check was NOT
  completed in this assessment).
- **The one genuine GPU payoff** — CLIP-class IMAGE embeddings (Immich's
  actual use case: semantic photo search, faces) — is a feature Filearr does
  not have and this roadmap does not plan. THE RULE: the GPU question is
  downstream of greenlighting that feature. If vision search is ever built,
  ship GPU support with it (as an opt-in image variant, never in the default
  image); until then, do not add CUDA anything.

Caveat recorded honestly: the web half of the assessment (NVDEC one-shot
numbers, PaddleOCR status) is reasoned rather than freshly source-verified;
the codebase half is first-hand. The verdict's load-bearing argument — no
live bottleneck exists for a GPU to relieve — rests on the codebase half.

## 25. Insight features — duplicates-as-action, staleness, treemap, bulk edit (IN-T1..T4, 2026-08-13)

Design: `archive/docs/design-insight-features.md` (approved 2026-08-13; built by
two parallel agents, A = backend + docs, B = frontend). The governing principle
is the user's own framing, and it is worth restating because it decides every
open question in this area:

> "This project is not for management of the files, but providing insight."

Filearr never acts on media. So the answer to "the duplicates report is not
actionable" is **not** a delete button — it is a per-copy export plus documented,
native-tool scripts the operator runs themselves.

- **IN-T1 — `duplicate_files_detail` (SHIPPED).** One row per COPY alongside the
  untouched aggregate `duplicate_files`. Window query over the same base
  predicate: `COUNT(*) OVER (PARTITION BY dup_key) > 1`, `group_rank` =
  `ROW_NUMBER()` by (mtime DESC, item_id) minus one, `keep_hint` = keep/candidate.
  The item_id tie-break is load-bearing: without it Postgres may reorder freely
  and a nightly script would delete a different copy each run. Ordered
  wasted-bytes DESC then group, so a truncated export is still whole-groups-first.
  Because a window function is illegal in `WHERE`, the statement selects from a
  subquery — which is why `CannedReport` gained `scoped_build` + `statement()`:
  an outer `.where(Item.path_scope ...)` on such a statement would re-add `items`
  to the FROM list and CARTESIAN-JOIN, i.e. wrong rows *and* a silently
  ineffective ACL. Any future subquery-topped report must use the same hook.
  This closes the Phase-11 research §11 Q6 tension (the aggregate was explicitly
  an interim shape "awaiting per-copy convergence"); §16's `hash_tier` caveat is
  carried onto every per-copy row.
- **IN-T2 — `stale_files` + a parameterized threshold (SHIPPED).**
  `ReportParams.threshold_days` (ONE generic numeric slot, validated 1..36500 at
  the API layer — 0 would mean "every file" and a negative would invert into
  `bad_mtime`'s query) plus `CannedReport.supports_threshold` /
  `threshold_label` / `default_threshold_days` surfaced in `meta()` so the UI
  renders the input for exactly the reports that declare it. Threaded through
  `export.params` in BOTH directions, because a queued export silently running at
  the default while the UI showed the operator their number is the failure mode.
  **Honesty requirement, restated in the description, the UI and the docs:** this
  is LAST-MODIFIED age. No atime is captured anywhere and none will be inferred —
  `noatime` is the norm and network mounts are worse, so an access-based
  "untouched" report would be confidently wrong.
- **IN-T3 — `GET /reports/folder-tree` (backend SHIPPED).** Deliberately not a
  canned report: `largest_folders` is a flat global top-N across all depths and
  cannot drive a treemap (a du-style recursive list double-counts every
  ancestor). Returns the direct children of one parent, one level, ordered bytes
  DESC, with a reserved `"."` files-here child, a `has_children` drill
  affordance, and an all-libraries root mode (library-sized rectangles).
  `has_children` is a single-pass `bool_or` over a deeper-separator probe rather
  than N per-child `EXISTS` round-trips.
- **IN-T4 — batch-edit hardening (SHIPPED).** `POST /items/batch` gained the
  single PATCH's null-pops-key semantics (it previously wrote a literal JSON
  `null`, so a bulk "clear this field" would have poisoned every row it touched
  and fed the null into the Meili projection and every export) and a 500-key
  request cap returning 413. Bulk edit is the first surface that makes clearing a
  field routine, which is why the divergence had to close before it shipped.

Still open in this area (not started, deliberately):

- **Per-level treemap has no category dimension.** Colour is library hash at the
  root and a size-graded single hue inside a library; category colouring would
  need a category dimension in the drill query and is deferred until someone
  actually wants it.
- **Select-type custom-field membership is not server-enforced** — the bulk-edit
  UI constrains to the defined options, but the API accepts any value. If that
  matters, it is a validator change in `custom_fields`, not a UI change.
- **No "act on it" beyond documentation.** By design, per the principle above.
  If this is ever revisited, the bar is not "add a delete button" but "explain
  why a catalog that deletes is still trustworthy when it is wrong".

## 26. Migrate oidc.py off authlib.jose to joserfc (before Authlib 2.0)

> **DONE 2026-08-14.** Authlib is **removed**, not merely bypassed: joserfc is now
> a direct dependency (`joserfc>=1.6.0`) and `oidc.py` validates ID-token claims
> in-tree (`_validate_id_token_claims`, OIDC Core §3.1.3.7, per-check comments).
> Gated by `tests/test_oidc_p6t5.py` (42 tests), which grew azp and clock-leeway
> cases first — those exposed that Authlib's `azp` check had been **wired dead**
> here all along (`CodeIDToken.validate_azp` reads `params["client_id"]`, which
> `oidc.py` never passed), so the migration closes a real gap rather than
> reproducing one. The suppression-ordering dance and the `<2` pin are gone.

Recorded 2026-08-14 after a deployed container surfaced the deprecation
warning. Authlib's `jose` module is deprecated and REMOVED in 2.0; 1.7 already
delegates its crypto to joserfc, so today's exposure is an import shim, not a
crypto risk — pyproject pins `authlib>=1.7.1,<2` until this lands.

The migration is NOT mechanical: `oidc.py` uses `JsonWebToken.decode(...,
claims_cls=CodeIDToken)` — Authlib's OIDC Core ID-token validation (iss/aud/
exp/nonce/azp/at_hash semantics). joserfc provides JWS/JWT primitives but not
that OIDC claims layer, so moving means re-wiring the validation while keeping
every fail-closed property (the `_JOSE_ERRORS` funnel, the at_hash
defence-in-depth check). Security-sensitive; do it deliberately with the OIDC
test suite as the gate, not as part of a routine bump. The suppression-ordering
gotcha (authlib.deprecate's module body installs an "always" filter that
defeats callers' ignores) dies with the migration.

---

## 27. Filesystem identity — symlinks & hardlinks (shipped 2026-08-20)

Central scans now record filesystem identity for every item, captured from the
walk's already-taken `lstat` (nlink/inode/dev cost nothing extra; a `readlink`
fires only for symlinks):

- `nlink` / `inode` / `dev` — a hardlink group is `nlink > 1` plus a shared
  `(dev, inode)`. These are files sharing storage, NOT independent duplicates,
  so a future duplicate report can exclude them (or surface them as their own
  "reclaimable via dedup already done" class). `inode`/`dev` are the signed
  wrap of the unsigned stat fields (bijection preserves group identity). A
  partial index `ix_items_hardlink WHERE nlink > 1` keeps the singleton
  majority out.
- `symlink_target` — non-NULL marks a symlink. A symlink is catalogued but
  NEVER hashed or extracted: opening it follows the link and would stamp the
  target's bytes/metadata onto the link's row (wrong identity).

Nullable, no default → an instant metadata-only migration
(`8f3b6a1d92c7`); backfilled on the next full scan (the diff's unchanged-branch
fills identity onto pre-§27 rows without re-extraction).

**Deferred tail:** (a) agent-side parity — the Go walker does not yet capture
these (agentsync never sets them, so agent-replicated rows stay NULL); (b) a
`hardlink_groups` / duplicate-vs-hardlink report; (c) NTFS/ReFS hardlink
(`nNumberOfLinks`) and reparse-point/junction capture on Windows agents. None
blocks the central catalog; each is worth doing when a user needs the answer.

**FAT/NTFS table reading (assessed, not built):** parsing the raw FAT/MFT
would let an agent enumerate files without a per-file `stat` (the MFT already
holds size/timestamps/nlink, and `$J`/USN journal is a ready-made change
feed). It is a real speedup for a cold walk of millions of files on a local
NTFS volume, but: it is Windows/local-volume only (useless over the SMB/FUSE
mounts that dominate this project's deployments, where there is no raw device
to read), needs raw volume access (Administrator + `\\.\C:` handle), and
duplicates what `os.scandir`+`lstat` already deliver portably. The USN change
journal is the genuinely differentiated piece — a v3 candidate for
near-real-time change detection on Windows agents, tracked next to the
Event-Log SACL idea in §22's audit-over-time discussion. Not worth the
platform-specific raw-filesystem parser today.

## 28. 2026-08-20 code review — security + correctness fixes

A full Fable review (security sweep + logic sweep, each adversarially verified)
landed the following. Security:

- **LLM keys were full-privilege `read` keys on the MAIN API.** An LLM facade
  key (scope-confined only inside `/api/llm/v1`) also authenticated against
  `/api/v1/*` as an unrestricted read key — bulk item/thumb/export reads over
  the whole catalog. `security._verify_credentials` now refuses any key whose
  `llm_role` is set (the facade has its own auth path).
- **Transfer SSE auth bypass** — a mis-indented `return` authorised a
  no-credential request; the 401 was unreachable (a copy divergence from the
  correct `scans.py`).
- **Meili filter injection** — five free-string `/search` params (`library`,
  `status`, `extension`, `tags`, `sidecar_of`) and the LLM `kind` arg were
  interpolated into single-quoted Meili filter literals unescaped; because
  clauses join with `AND` (binds tighter than `OR`), an embedded quote dangled
  an `OR` outside the AND-chain and bypassed the RBAC scope filter. Added
  `search.meili_quote` (escape `\` then `'`) on every string interpolation;
  `kind` is validated against the closed category vocabulary.
- **LLM facade scope leaks** — `run_report` passed `scope_clause=None` (a
  library-scoped key read every library's report rows); `where_is`/`get_file`
  duplicate-copy lookups ignored the key's scope (leaking out-of-scope library
  names, hosts, absolute paths). Both now apply the key's `_sql_scope`.
- **OIDC open redirect** via `/\evil` (browsers normalise `\`→`/`); the
  `return_to` guard now rejects `raw[1] in "/\\"` and control chars.
- **`secrets.compare_digest` TypeError → 500** on a non-ASCII header (proxy
  trust secret, agent bearer/fingerprint) — now byte-compared.
- Webhook-allowlist docstring corrected to match the code (an explicit CIDR
  admits any class except unspecified — a documented metadata-SSRF footgun).

Correctness:

- **`rehash_library_now` / `backfill_content_hashes_now` committed inside a
  server-side cursor stream** → the cursor died after the first `yield_per`
  buffer, so a library over ~200 rows failed mid-run. Both now collect IDs
  first, then process in per-chunk transactions.
- **`library_health.last_success_at`** matched `status == "completed"` (a
  rollout status, never a scan status) → always NULL. Now `("finished",
  "stopped")`.
- **Intra-library rename survivors were never re-indexed** — the search doc
  kept the old path until the nightly rebuild (id unchanged, so the reconcile
  sweep saw no drift). A `sync_items` is now deferred for move survivors.
- **`effective_access`** — a `dir_default` ACE consumed the POSIX class slot
  and materialised an empty source layer before being skipped (owner reported
  zero verbs; local∩share wiped local grants); and broad-principal matching was
  substring (`Power Users` → contains `USERS` → applied to everyone). Both
  fixed (skip-first; exact-name match).
- **Hygiene reports missed artwork** — `sidecar_hygiene` / `library_health`
  `unlinked_sidecars` matched only `nfo/xmp/thm`, missing directory + stem
  artwork (`poster.jpg`, `Movie-thumb.jpg`) and JRiver — the dominant search
  pollution. Now use a shared shape predicate mirroring `sidecar.classify`.
- **Go agent:** a transient read error / hash timeout on a CHANGED file
  clobbered the good digests with `""` (removing the item from move detection
  and shipping blank hashes to central) — now keeps the prior value; and the
  agent gained the N→0 empty-mount guard (parity with central §19) so a dead
  FUSE/SMB bind can no longer tombstone a whole replicated library.

**Explicitly deferred** (noted, not yet done): agent-side mid_hash move tier
(central refuses an ambiguous move Go would transfer); several alert-pipeline
nuances (partial multi-channel retry re-notifies delivered channels;
per-path permission-change dedup keys defeat grouping on a recursive chmod;
inhibited groups consume the hourly ceiling); an agent-side rename tombstones
the central row losing its edits (needs an identity-transfer decision). These
are tracked here rather than silently carried.

## 29. Research & automation coverage (2026-08-20)

Closing the "gaps that assist research/automation" ask, verified against the
shipped surface:

- **LLM facade parity** — `run_report` now scope-safe; `find_similar`,
  `search_in` (content-only / names-only), per-passage scores all present; the
  facade tracks the report + filter-DSL registries automatically, so new canned
  reports (incl. `permissions_explicit_outliers`) appear without facade edits.
- **Permissions audit is complete** — by-principal, broad-access, drift, and
  now explicit-vs-parent **outliers** (the meaningful-deviation view) are all
  live canned reports, schedulable to e-mail; effective-access is a queryable
  endpoint; macOS ACL read shipped (agent ≥ 1.5.3), so all three OSes report.
- **Hygiene automation** — `library_health` digest + `sidecar_hygiene` +
  `empty_files`, weekly-emailable via report schedules, now catch artwork too.

Remaining research-assist candidates (deferred, not requested): a
`hardlink_groups` report; a saved-search → alert bridge; agent USN-journal
change feed (§27).

## 30. AD/LDAP directory sync — permission attribution (LDAP-T1, 2026-08-20)

User request: "add LDAP discovery and authentication with RBAC so permissions
attribute to accounts and AD objects; setup + reconcile on central; agents push
the SIDs, resolved on central." Bind auth + group→role login mapping already
existed (§3, P6-T6). This adds the CENTRAL directory-of-record + SID resolution:

- **Enumeration** (`ldap_directory.py`, reuses the login stack's TLS transport +
  injected-connector seam): paged AD search of users + groups capturing
  objectSid/objectGUID (decoded from binary), sAMAccountName, displayName, UPN,
  memberOf. Requires a service bind; bounded by `max_objects`.
- **Storage**: `directory_objects` table (guid PK, sid, sam, display, dn, kind,
  domain, member_of_sids, disabled, tombstone on removal). Migration
  a3f6c1d84b29, which also adds `principal_aliases.source` ('manual' | 'ldap').
- **Reconciliation** (`worker.sync_directory`, scheduled 03:40 + on-demand): for
  every SID actually present in a permission snapshot, upsert a
  `principal_aliases` row (source='ldap') mapping SID → `DOMAIN\name (Full Name)`
  — the existing permission reports resolve through that table unchanged. A
  manual override is never clobbered (conflict update gated on source='ldap');
  the manual PUT is symmetric (won't overwrite an ldap row without ?force). A
  since-deleted account tombstones and still attributes as `name (deleted)`.
- **Group expansion**: `member_of_sids` (resolved DN→SID during sync) lets
  `GET /permissions/effective-access?expand_groups=true` (default) grow a
  caller's closure by nested AD group membership, so a group grant attributes to
  members. This is the "RBAC so permissions attribute to AD objects" half.
- **API**: `/directory/objects` (browse), `/directory/status` (resolved vs
  unresolved snapshot SIDs — the operator's reconciliation health), `POST
  /directory/sync` (trigger, 422 when disabled).
- Config: `FILEARR_LDAP_DIRECTORY_*`, reusing the `ldap_*` transport/bind. OFF
  by default; fails closed (no service bind → refuse, not a partial tree).
- Tests: `tests/test_ldap_directory.py` (SID/GUID decoders, MOCK_SYNC
  enumeration, reconcile→alias, unresolved+tombstone, group expansion,
  manual-not-clobbered, API endpoints). Also fixed a pre-existing date-brittle
  assertion in test_agent_inventory_w6d3 (hardcoded collected_at vs a relative
  threshold window; surfaced when the clock rolled to 2026-08-20).

**Cross-forest + multi-domain (added 2026-08-20):** `FILEARR_LDAP_DIRECTORIES`
is a JSON list of endpoints, each its own bind (overriding the global `ldap_*`);
all feed the one `directory_objects` table (SIDs are globally unique — no
collision), tagged with `source_directory`. Multi-domain within a forest is
covered by a Global Catalog (`:3269`) endpoint. `endpoints_from_settings`
overlays a per-endpoint `Settings` and reuses `LdapConfig.from_settings` so the
transport-security policy is never duplicated. **Fault isolation:** an
unreachable forest records a per-endpoint error and is skipped, and tombstoning
is scoped to the endpoints that synced — a DC outage never mass-tombstones
another forest (or its own) objects. Tests: multi-forest enumeration,
unreachable-forest-does-not-tombstone, bad-endpoint-config-skipped.

**Deferred tail:** transitive-group closure is direct-membership-per-sync-hop
(bounded depth), which resolves nested groups but re-reads memberOf each level —
fine for typical directories; an `LDAP_MATCHING_RULE_IN_CHAIN` single-query
variant is a possible optimisation. Agent-side AD lookup is unchanged (it still
resolves what it can locally; central fills the rest).

## 31. Console error triage (2026-08-20)

- **Benign shadow-delete race surfaced as "Latest Meili failure"** (user report):
  an `indexDeletion` failing `index_not_found` on an `items_rebuild_<epoch>`
  shadow is the swap-rebuild's post-swap delete racing the stale-shadow reaper
  (or a retried rebuild) over the same throwaway index — the index being gone is
  the *desired* end state. `meili_stats._recent_failed_tasks` now filters these
  (`_is_benign_failed_task`) out of both the surfaced `last_failed_task` and the
  failed-task counts (over a bounded recent window), so a self-healing race no
  longer reads as an operational failure. We never delete the LIVE index, so any
  `indexDeletion` there is a shadow delete and `index_not_found` on it is always
  benign. Tests: test_meili_failed_task_surface.py.

## 32. GUI configuration of auth providers (2026-08-20)

User request: GUI-based config of AD/LDAP sync, auth and roles, and OIDC SSO —
previously env-only. Built on the existing `app_settings` KV store + the
`alerts/crypto` secret-encryption scheme:

- `authconfig.py` owns three config blobs (`ldap_config` /
  `ldap_directory_config` / `oidc_config`) with strict per-provider field
  allow-lists (the injection guard). `effective_settings(session)` overlays the
  decrypted blobs onto the env `Settings` via `model_copy` — **GUI overrides env
  per field** — and EVERY auth reader now sources config through it: login
  (`authenticate_ldap`, `auth_status`), OIDC (`oidc_login`/`callback`), and the
  directory sync. Env stays the bootstrap/fallback; a DB-read failure falls back
  to pure env so a blip never locks everyone out.
- Secrets (`ldap_bind_password`, `oidc_client_secret`, per-endpoint
  `bind_password`) AES-GCM encrypted at rest; write-only over the API (`has_*`
  flags), `SECRET_UNCHANGED` sentinel keeps a stored secret, `""` clears it.
- API `/auth-config/{provider}` GET/PUT (redacted, per-field `_source` map) +
  pre-save Test actions: `/ldap/test` (service bind + sample enumeration),
  `/directory/test` (per-endpoint, cross-forest), `/oidc/test` (discovery +
  JWKS fetch) — all run against the FORM values, never persisting. Audited
  (`auth_config_changed`, field names only).
- Frontend: **Admin → Authentication** (AuthProvidersPanel) — metadata-driven
  collapsible forms for the three providers, source badges, write-only secret
  fields, and the Test buttons. Cross-forest endpoints edited as a JSON list.
- No migration (the `app_settings` table already existed). Tests:
  test_authconfig.py (store, encryption-at-rest, redaction, sentinel, endpoint
  secrets, API roundtrip). The role MAPS (group→role) are part of each provider
  blob; roles-as-data already had its own UI (RolesPanel).
