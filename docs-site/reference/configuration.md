# Configuration reference

Filearr is configured entirely through environment variables prefixed
`FILEARR_` (plus the container-level `POSTGRES_PASSWORD`, `MEILI_MASTER_KEY`, and
`MEDIA_PATH`). Values load from the process environment and the `.env` file. This
page lists the **operationally meaningful** settings grouped by area — not every
internal knob. Defaults shown are the built-in defaults.

!!! tip "Only override what you need"
    Every setting has a sensible default. A minimal deployment sets the database /
    Meili connection strings, the two passwords, `MEDIA_PATH`, and (if you use
    alerts) `FILEARR_SECRET_KEY`. Everything else is tuning.

## Optional features

These are the product's opt-in (and one opt-out) feature switches. They are the
knobs most people want to find, so every deployment surface now declares them
**explicitly with their default** rather than leaving them to be inferred from
the code: the bundled `docker-compose.yml` sets them on `app` **and** `worker`,
`.env.example` lists them, the Unraid templates expose them as advanced
variables, and the Proxmox deploy writes each one into the container's `.env`
(only when the key is absent, so your edits are never overwritten).

| Variable | Default | What turning it on does |
|---|---|---|
| `FILEARR_SEMANTIC_ENABLED` | `false` | Semantic / hybrid search. The **worker** downloads and loads a local ONNX embedding model and embeds items in the background. Off = the model is never loaded, zero cost. Details: [Semantic search](#semantic-search-opt-in). |
| `FILEARR_CONTENT_SNIFF_ENABLED` | `false` | Unlocks the on-demand "Content-sniff extensionless files" maintenance action: libmagic MIME sniffing over a bounded prefix read, reclassifying files whose extension tells you nothing. Details: [Content sniffing](#content-sniffing-opt-in). |
| `FILEARR_UPDATE_CHECK_AUTO` | `false` | Lets the Jobs-page Updates card refresh a stale GitHub release cache by itself. This is the only automatic outbound request the product makes; with it off, the check is manual only. Details: [Update check](#update-check-jobs-page-updates-card). |
| `FILEARR_THUMBNAIL_BUDGET_GB` | `5` | Advisory thumbnail-cache budget in GiB. Over budget you get an hourly log reminder and an amber note on the Jobs thumbs card — generation continues and **nothing is deleted**. `0` disables the advisory. Details: [Thumbnails](#thumbnails). |
| `FILEARR_LOG_DB_ENABLED` | `true` | *(on by default)* Records the log stream into Postgres so the console's Logs panel has content. Set `false` to keep logs in the container output only. Details: [Console log stream](#console-log-stream-jobs-page-logs-panel). |
| `FILEARR_AGENTS_ENABLED` | `false` | Master switch for the distributed agent fleet surface (enrollment, agent API, fleet monitoring). Needs a CA and further setup. Details: [Distributed agents](#distributed-agents-all-off-unless-enabled). |

!!! note "Set the feature flags on the worker too"
    `app` and `worker` must agree. The worker is what actually loads the semantic
    embedder and runs the content-sniff pass; the app only serves the flags to
    the console. The bundled compose file keeps both in sync automatically — on
    Unraid it is a single `filearr` container running both processes, so there is
    only one place to set it.

**Not env vars:** OCR and RAG passage chunking are **per-library** toggles you
flip in the console's library settings (their `FILEARR_OCR_*` /
`FILEARR_CHUNK_*` variables only tune the behaviour once a library opts in) —
see [OCR](#ocr-per-library-opt-in) and
[RAG passage chunking](#rag-passage-chunking-per-library-opt-in).

The console's **Jobs page** carries an "Optional features" card showing the live
state of each of these switches in the running process, which is the fastest way
to confirm a change actually reached the containers.

## Core / connections

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_DATABASE_URL` | `postgresql+psycopg://filearr:filearr@postgres:5432/filearr` | SQLAlchemy DSN (source of truth). |
| `FILEARR_PROCRASTINATE_DSN` | `postgresql://filearr:filearr@postgres:5432/filearr` | Job-queue DSN. |
| `FILEARR_MEILI_URL` | `http://meilisearch:7700` | Meilisearch endpoint. |
| `FILEARR_MEILI_MASTER_KEY` | `change-me` | Meilisearch master key. |
| `FILEARR_MEILI_INDEX` | `items` | Index name. |
| `FILEARR_CONFIG_DIR` | `/config` | Thumbnails, caches, models, exports, staging. |
| `FILEARR_LOG_LEVEL` | `INFO` | Log verbosity. |
| `FILEARR_SOURCE_URL` | GitHub repo URL | AGPL §13 "Source" link (point at your fork). |
| `FILEARR_SECRET_KEY` | *(unset)* | Envelope key for alert-channel secret encryption (**required** for alerts; never auto-rotated). |
| `FILEARR_PUBLIC_BASE_URL` | *(unset)* | Absolute prefix for export/report download links; blank = site-relative. |
| `FILEARR_SHARE_MAP_PATH` | `/config/share-map.json` | Deploy-written share map for auto share locations. |
| `FILEARR_AUTO_INIT_DB` | `true` | Container-level (entrypoint, app command only): run the idempotent `scripts/init_db.py` bootstrap on start, retrying while Postgres comes up. Set `false` to manage migrations manually. |

## Authentication & sessions

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_AUTH_ENABLED` | `true` | Master switch for auth. |
| `FILEARR_SESSION_TTL_HOURS` | `720` | Absolute session lifetime (30d). |
| `FILEARR_SESSION_INACTIVITY_HOURS` | `168` | Idle window (7d). |
| `FILEARR_SESSION_ROTATION_MINUTES` | `10` | Opaque-token rotation cadence. |
| `FILEARR_SESSION_COOKIE_SAMESITE` | `lax` | `lax` (SSO-safe) / `strict` / `none`. |
| `FILEARR_AUTH_RATELIMIT_ENABLED` | `true` | Brute-force limiter. |
| `FILEARR_AUTH_RATELIMIT_MAX_ATTEMPTS` | `3` | Failures per window → lock. |
| `FILEARR_AUTH_RATELIMIT_WINDOW_SECONDS` | `120` | Find window. |
| `FILEARR_AUTH_RATELIMIT_LOCK_SECONDS` | `300` | Lockout duration. |
| `FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR` | `false` | Only enable behind a trusted proxy. |
| `FILEARR_AUDIT_READS` | `false` | Record a per-query search event (high volume). |

OIDC (`FILEARR_OIDC_*`) and LDAP (`FILEARR_LDAP_*`) are extensive, env-only
provider configs; both default **off**. See [Security](../security.md) for the
model and the source `config.py` for every field.

## Scanning & hashing

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_SCAN_HASH_FULL_MAX_BYTES` | `1073741824` | Skip the full content hash above this size (1 GiB). |
| `FILEARR_SCAN_BATCH_SIZE` | `500` | Files per batch commit. |
| `FILEARR_RECYCLE_RETENTION_DAYS` | `30` | Recycle-bin retention before purge. |
| `FILEARR_STAGED_PIPELINE` | `true` | Defer all extraction to scan end (vs trickle during walk). |
| `FILEARR_AUDIT_RETENTION_DAYS` | `90` | Retention for extractor-sourced item audit rows (user edits exempt). |
| `FILEARR_BACKUP_KEEP` | `7` | Bundles the [in-app backup](../operations.md#in-app-backup) keeps in `{config}/backups`. Matters more than it looks: those bundles sit on the volume the disk monitor watches. (`scripts/backup.sh` reads the same number from its own `BACKUP_KEEP`.) |

## Workers, queues & the reaper

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_WORKER_CONCURRENCY` | `4` | Parallel jobs per worker. |
| `FILEARR_WORKER_QUEUES` | *(all)* | Comma-separated queues a worker serves. |
| `FILEARR_JOB_HISTORY_RETENTION_DAYS` | `14` | Purge terminal job rows older than this. |
| `FILEARR_JOB_STALL_HEARTBEAT_SECONDS` | `30` | Heartbeat net for stalled jobs. |
| `FILEARR_JOB_STALL_SECONDS` | `3600` | Age net: a per-file/index/alert job still `doing` after this long is reaped even if its worker is alive. Whole-catalog jobs (`scan_library`, `nightly_reconcile`, `rebuild_index`, `reproject_library`, `rebuild_chunks_index`, `embed_missing`, `chunk_missing`, `backup_now`, `compact_meili`, `content_sniff`, `rehash_small_files`) are exempt — they legitimately run for hours and are reaped only by the heartbeat net. |
| `FILEARR_JOB_STALL_AGE_EXEMPT_TASKS` | `[]` | Extra task names (JSON list) to exempt from the age net. |
| `FILEARR_REAP_MAX_ATTEMPTS` | `10` | Requeue budget for a stalled non-scan job before it is failed. |
| `FILEARR_SCAN_SCHEDULE_MAX_CATCHUP_MINUTES` | `2880` | Furthest-back missed cron a recovery tick fires (48h). |
| `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` | `600` | Grace before finalizing an orphaned scan run. |

!!! note "Container-level variables, read by the entrypoint rather than the app"
    These two are consumed by the image's entrypoint script, so they do not
    appear in the settings object or on the About page.

    | Variable | Default | Purpose |
    |---|---|---|
    | `FILEARR_AUTO_INIT_DB` | `true` | Run the idempotent database bootstrap on start. `false` if you run `scripts/init_db.py` yourself. |
    | `FILEARR_STOP_GRACE_SECONDS` | `60` | **Merged mode only.** How long the supervisor waits for the worker to finish in-flight jobs after `SIGTERM` before `SIGKILL`. |

    **Merged mode** is what the container does when its command is the single
    word `all`: it bootstraps the database once, then runs uvicorn *and* a
    Procrastinate worker as children of one supervisor, forwarding signals to
    both and taking the container down if either dies. It is how the Unraid
    template ships. Docker Compose deliberately does **not** use it — separate
    `app` and `worker` services keep `docker compose up -d --scale worker=N`
    available, which one supervised container cannot express.

    `FILEARR_STOP_GRACE_SECONDS` must be ≤ the container's own stop timeout
    (`stop_grace_period: 60s` in compose, `--stop-timeout=60` in the Unraid
    template's Extra Parameters), or Docker kills the container before the grace
    can elapse. 60 s is not arbitrary: the 10 s default regularly cut
    Procrastinate jobs off mid-transaction during redeploys.

### Adaptive extract backpressure

Each worker varies how many extract jobs it runs at once: host load contracts
the ceiling, extract-queue depth expands it. Full behaviour, the log lines it
emits, and when to intervene:
[extraction throughput](../operations.md#extract-backpressure). Inert on hosts
without a load average (Windows dev).

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_EXTRACT_BACKPRESSURE` | `true` | Master switch for the controller (the static queue priority is unaffected). |
| `FILEARR_EXTRACT_BACKPRESSURE_MIN_CONCURRENCY` | `1` | Floor: extract jobs this worker keeps running under any load. |
| `FILEARR_EXTRACT_BACKPRESSURE_MAX_CONCURRENCY` | `0` (auto) | Ceiling cap; `0` = use `FILEARR_WORKER_CONCURRENCY`. Set explicitly if you pass `--concurrency` without matching that variable. |
| `FILEARR_EXTRACT_BACKPRESSURE_HIGH_LOAD` | `0.85` | 1-min loadavg per core at which the ceiling contracts. |
| `FILEARR_EXTRACT_BACKPRESSURE_LOW_LOAD` | `0.60` | Recovery threshold (hysteresis); expansion happens only at or below it. |
| `FILEARR_EXTRACT_BACKPRESSURE_SAMPLE_SECONDS` | `15` | Sampling cadence — also the minimum dwell between same-direction moves. |
| `FILEARR_EXTRACT_BACKPRESSURE_DECREASE_FACTOR` | `0.5` | Multiplicative decrease per sample under pressure (halve, not collapse to the floor). |
| `FILEARR_EXTRACT_BACKPRESSURE_EXPAND_COOLDOWN_SECONDS` | `60` | No expansion for this long after a contraction (the 1-min loadavg lags by about its own window). |

### Console log stream (Jobs page Logs panel)

App and worker each persist selected log records to a shared table so the Jobs
page shows one unified activity/error stream (the two processes are separate
containers). `filearr.*` loggers record at the configured level (the activity
stream); every other logger records warnings and up only; per-request access
lines are never recorded. The sink is fail-open: a broken database drops
records rather than blocking the application.

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_LOG_DB_ENABLED` | `true` | Record the log stream at all. |
| `FILEARR_LOG_DB_LEVEL` | `INFO` | Threshold for `filearr.*` loggers. |
| `FILEARR_LOG_RETENTION_DAYS` | `7` | Daily purge window for log rows. |
| `FILEARR_LOG_MAX_ROWS` | `200000` | Hard row cap (log-storm backstop). |

### Update check (Jobs page Updates card)

Compares the running build and the baked agent binaries against the source
repository's head and pulls recent commit messages (the changelog) for review
in the console. **Contacts GitHub only** — nothing about your instance or
catalog is sent. By default it runs solely when an operator clicks *Check
now*; results are cached for 6 hours.

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_UPDATE_CHECK_AUTO` | `false` | Opt-in: also refresh a stale cache on console loads (the only automatic outbound check in the product). |

## Search reconciliation & rebuild

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_MEILI_SEARCH_CUTOFF_MS` | `1500` | Per-search wall-clock circuit breaker. |
| `FILEARR_RECONCILE_MAX_FIXES` | `10000` | Cap on repairs per hourly reconcile sweep. |
| `FILEARR_MEILI_REBUILD_WAIT_S` | `900` | Total wait budget for a shadow rebuild before it fails cleanly. |
| `FILEARR_MEILI_SHADOW_MAX_AGE_HOURS` | `6` | Age at which an orphaned shadow index is reaped. |
| `FILEARR_MEILI_SCOPE_FILTER_CEILING` | `4096` | Max compiled RBAC scope-filter length (over → refuse). |
| `FILEARR_MEILI_COMPACTION_ENABLED` | `true` | Run the weekly [search-index compaction](../operations.md#meili-compaction). |
| `FILEARR_MEILI_COMPACTION_THRESHOLD` | `1.3` | Fragmentation ratio (store size ÷ used size) above which it compacts. |
| `FILEARR_MEILI_COMPACTION_WAIT_S` | `1800` | Wait budget for the compaction task; a timeout is reported, not failed. |
| `FILEARR_MEILI_DATA_PATH` | *(unset)* | Meili store path, when visible to this process — checked against the critical disk floor before compacting (compaction needs ~2× the index size). |

## Extraction limits (safety caps)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_FFPROBE_TIMEOUT_S` | `30` | ffprobe wall-clock cap. |
| `FILEARR_MODEL3D_MAX_BYTES` | `268435456` | Mesh size ceiling handed to trimesh (256 MiB). |
| `FILEARR_DOCUMENT_MAX_BYTES` | `268435456` | Doc/spreadsheet size ceiling. |
| `FILEARR_DIGEST_MAX_BYTES` | `53687091200` | On-demand MD5/SHA-256 size ceiling (50 GiB). |
| `FILEARR_ARCHIVE_MAX_MEMBERS` | `10000` | Archive member-listing cap. |

### EXIF / GPS

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_GPS_EXPOSE_DEFAULT` | `false` | Per-library GPS-exposure default (no global default-on). |
| `FILEARR_EXIF_TIMEOUT_S` | `30` | exiftool wall-clock cap. |

A library's `expose_gps` flag is also what puts a `_geo` point in the search
index, so it is the on/off switch for
[geo search](api.md#geo-search-radius-and-bounding-box). Turning it off queues a
re-projection that removes the coordinates already indexed for that library.
There is no environment variable that can expose GPS globally — only the
per-library flag.

### OCR (per-library opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_OCR_ENABLED` | `false` | Global default off (per-library toggle gates it). |
| `FILEARR_OCR_MAX_PAGES` | `10` | Scanned-PDF page ceiling. |
| `FILEARR_OCR_TIMEOUT_S` | `120` | Per-subprocess wall clock. |
| `FILEARR_OCR_LANG` | `eng` | Tesseract language. |

### Content sniffing (opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_CONTENT_SNIFF_ENABLED` | `false` | Enables the on-demand "Content-sniff extensionless files" maintenance action (libmagic MIME → taxonomy reclassify). |
| `FILEARR_CONTENT_SNIFF_BATCH` | `5000` | Candidates per run (idempotent — run again while `remaining` > 0). |
| `FILEARR_CONTENT_SNIFF_READ_BYTES` | `65536` | Bounded prefix read per file. |

### Semantic search (opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_SEMANTIC_ENABLED` | `false` | Load the local ONNX embedder (off = zero cost). |
| `FILEARR_EMBED_MODEL` | `BAAI/bge-small-en-v1.5` | Local embedding model (downloaded once). |
| `FILEARR_EMBEDDER_CONCURRENCY` | `1` | One memory-capped, lowest-priority worker. |
| `HF_TOKEN` | *(unset)* | Optional Hugging Face access token used ONLY for the one-off model download (higher anonymous rate limit, no "unauthenticated requests" warning). Leave blank — or any placeholder such as `none` — to download anonymously; a blank/placeholder is never sent as a token. This is the Hub's own variable name, so it is picked up by anything else in the container that talks to Hugging Face. Treated as a secret in the deployment templates (masked on Unraid, CT `.env` only on Proxmox). |

### RAG passage chunking (per-library opt-in)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_CHUNK_SIZE_CHARS` | `1000` | Passage window for the doc_chunks store (LLM `retrieve_passages`). |
| `FILEARR_CHUNK_OVERLAP_CHARS` | `150` | Overlap between consecutive passages. |
| `FILEARR_CHUNK_MAX_PER_ITEM` | `200` | Chunk cap per document. |
| `FILEARR_CHUNK_BACKFILL_BATCH` | `2000` | Items per "Chunk documents for RAG" run. |

### Natural-language query assist

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_NL_OLLAMA_URL` | *(unset)* | Local Ollama endpoint (e.g. `http://ollama:11434`) to upgrade `POST /query/assist` beyond the built-in heuristic; heuristic remains the automatic fallback. |
| `FILEARR_NL_OLLAMA_MODEL` | `qwen2.5:7b` | Model name used for translation. |

### Frecency personal ranking

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_FRECENCY_ENABLED` | `true` | Per-principal frequency+recency profile from item-detail opens; bounded page-local lift of habitual items in default-relevance search. Disable to stop recording AND reading. |

## Thumbnails

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_THUMBS_ENABLED` | `true` | Generate WebP thumbnails / posters. |
| `FILEARR_THUMBNAIL_GRID_PX` | `320` | Grid tier longest edge. |
| `FILEARR_THUMBNAIL_PREVIEW_PX` | `800` | Preview tier longest edge. |
| `FILEARR_THUMB_ACCEL` | `auto` | `auto` (QSV if `/dev/dri` present) / `off`. |
| `FILEARR_THUMBNAIL_BUDGET_GB` | `5` | Advisory cache-size budget in GiB (`0` disables). Over it: an hourly log reminder + an amber note on the Jobs thumbs card — generation continues, nothing is deleted (disk-floor GC is separate). |

## Disk guardrails

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_DISK_MIN_FREE_GB` | `5` | Critical below this (absolute floor). |
| `FILEARR_DISK_WARN_FREE_GB` | `20` | Warn below this (absolute floor). |
| `FILEARR_DISK_CRIT_PCT_FREE` | `2` | Critical below this percent free. |
| `FILEARR_DISK_WARN_PCT_FREE` | `10` | Warn below this percent free. |
| `FILEARR_DISK_PG_PATH` | *(unset; compose sets `/pgdata`)* | Postgres data path to watch; when critical, extract pauses. The bundled `docker-compose.yml` mounts the `pgdata` volume read-only into app/worker and points this at it. |
| `FILEARR_DISK_GC_TARGET_FREE_GB` | `0` | `>0` LRU-evicts valid thumbnails to this target at critical. |

## Distributed agents (all off unless enabled)

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_AGENTS_ENABLED` | `false` | Master switch for the agent fleet surface. |
| `FILEARR_ENROLLMENT_TOKEN_TTL_MINUTES` | `60` | Single-use enrollment-token TTL. |
| `FILEARR_CA_URL` | *(unset)* | step-ca URL handed to agents. |
| `FILEARR_CA_FINGERPRINT` | *(unset)* | Public root fingerprint (pin). |
| `FILEARR_CA_PROVISIONER` | `filearr-agents` | Provisioner name. |
| `FILEARR_CA_PROVISIONER_JWK` | *(unset)* | **Secret** — decrypted private JWK; without it `ca_ott` is null. |
| `FILEARR_AGENT_CERT_TTL_HOURS` | `48` | Advisory agent cert lifetime (24–72h band). |
| `FILEARR_AGENT_AUTH_MODE` | `fingerprint` | `fingerprint` / `mtls-header` / `both`. |
| `FILEARR_PROXY_SHARED_SECRET` | *(unset)* | **Secret** — mTLS proxy ↔ backend trust (required for mtls modes). |
| `FILEARR_AGENT_OFFLINE_ALERT_SECONDS` | `172800` | Agent-offline alert threshold (48h). |
| `FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS` | `21600` | Replication-stall alert threshold (6h). |
| `FILEARR_AGENT_DIST_DIR` | `/app/agent-dist` | First-install agent binaries + install scripts served by `/api/v1/agent-dist` (baked into the image; the API 404s gracefully when absent). |
| `FILEARR_AGENT_ASSOCIATE_DEBOUNCE_SECONDS` | `120` | Debounce for the post-replication sidecar-association pass on agent-backed libraries. |
| `FILEARR_AGENT_EXTRACTED_MAX_BYTES` | `262144` | Cap on the `extracted` object one replication event may carry ([agent-side extraction](../agents.md#agent-extraction)). Oversize is dropped with a warning and the event still applies. |
| `FILEARR_AGENT_RELEASES_DIR` | `{config_dir}/agent-releases` | Uploaded signed-release artifact binaries (manifests live in Postgres). |

!!! note "There is no release-staging variable"
    Every uploaded release is generally visible once its artifacts are present.
    Who actually takes one is decided by the `auto_update` key in a
    [configuration group](../agents.md#two-groupings), plus the per-agent update
    action — not by an environment variable.

## Alerting

| Variable | Default | Purpose |
|---|---|---|
| `FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS` | `false` | Permit RFC1918/ULA webhook targets (loopback/link-local still denied). |
| `FILEARR_ALERT_WEBHOOK_TIMEOUT_S` | `10` | Per-POST wall clock. |
| `FILEARR_ALERT_APPRISE_TIMEOUT_S` | `30` | Per-send wall clock for an [Apprise channel](../operations.md#apprise-channels) (one channel may hold several URLs, walked sequentially). |
| `FILEARR_ALERT_RULE_MAX_PER_HOUR` | `100` | Per-rule dispatch ceiling (storm safety net). |
| `FILEARR_ALERT_EVENTS_RETENTION_DAYS` | `30` | Terminal alert-event retention. |

For the complete, authoritative list see `backend/filearr/config.py`.
