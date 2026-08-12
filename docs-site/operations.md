# Operations & recovery

A runbook for keeping Filearr healthy and getting it back when something breaks.
Each section is **symptom → diagnosis → fix**, drawn from real incidents.

!!! note "Placeholders"
    Examples use placeholder identifiers — replace `filearr.example.com` /
    `ca.example.com` / `agents.example.com`, `192.0.2.10`, and `<your-vmid>` with
    your own. The stack is assumed at `/opt/filearr` with the API on `:8484`, the
    CA on `:9000`, and TLS on `:8443`.

## Working inside the containers

This is the toolkit everything below uses. On a Proxmox LXC the compose stack runs
*inside* the container, so from the Proxmox host prefix commands with `pct exec`:

```bash
# what's running / health
pct exec <your-vmid> -- docker ps
pct exec <your-vmid> -- docker compose -f /opt/filearr/docker-compose.yml ps

# follow one service's logs
pct exec <your-vmid> -- docker compose logs -f app
pct exec <your-vmid> -- docker compose logs -f worker
pct exec <your-vmid> -- docker compose logs -f postgres
pct exec <your-vmid> -- docker compose logs step-ca | grep -i "password is"

# a psql shell into the database
pct exec <your-vmid> -- docker compose exec -T postgres psql -U filearr -d filearr -c "SELECT 1;"

# run python / alembic inside the app image
pct exec <your-vmid> -- docker compose run --rm app python scripts/init_db.py
pct exec <your-vmid> -- docker compose exec app alembic current
```

If you are already inside the container (or on a single-host Unraid/Compose
deploy), drop the `pct exec <your-vmid> --` prefix and run the `docker compose …`
part from `/opt/filearr`.

## Maintenance schedules (Jobs page) {#maintenance-schedules}

The **Scheduled maintenance** panel on the Jobs page lists every housekeeping
task the worker runs — retention purges, search-index reconcilers, thumbnail
GC, the weekly [search-index compaction](#meili-compaction), monitors — with a
tooltip describing what each does, its schedule, next
occurrence, and last-run status (from job history: succeeded runs are visible
for ~48 h, failures for days).

- **Run now** (admin) queues any purge/reconciler/monitor or the on-demand
  jobs (full search-index rebuild, semantic-embedding backfill) immediately.
  A 409 means a run is already queued or executing — the queueing lock
  prevents pile-ups.
- **Edit** (admin) overrides a cleanup/integrity task's cron (five fields,
  evaluated in UTC) or disables its schedule entirely. Changes are picked up
  by the next scheduler tick — **within one minute, no restart**. "Reset to
  default" drops the override.
- Monitors and the minutely system ticks are **fixed by design**: the reaper
  and health monitors are the crash-safety net, and the scheduler ticks are
  the cron engine's own clock. They're shown read-only for visibility.

Overrides live in the `maintenance_schedules` table (row per task; no row =
default schedule, enabled). The API surface is
`GET/PATCH /api/v1/system/maintenance[/{task}]` and
`POST /api/v1/system/maintenance/{task}/run`.

Each task row also shows **how long the last run took** (wall time of the
latest attempt, derived from job history) — a purge that suddenly takes
minutes instead of seconds is an early signal worth investigating.

## Maintenance mode (Jobs page) {#maintenance-mode}

**Enter maintenance mode** (Jobs page header, admin) suspends all *regular*
work generation so long-running operator tasks — `pg_dump`, `VACUUM FULL`,
reindexing, storage moves — run against a quiet system:

- scheduled scans, the nightly maintenance tasks, and scheduled report
  exports stop being deferred (their cron occurrences are **not consumed** —
  each fires, collapsed to the latest occurrence, when the mode lifts);
- watch-mode triggers idle; manual scan triggers are refused with 409;
- distributed agents observe the mode on their next command poll and **pause
  their replication push** (the Agents page shows a `backing off` badge) —
  they keep scanning and collecting inventory locally, and their outbox
  backlog drains when the mode ends. Agent builds predating the
  advertisement are throttled by the replication endpoint itself
  (503 + `Retry-After`), feeding their normal flush backoff — nothing is
  lost either way.

What deliberately **keeps running**: the stalled-job reaper, command TTL
sweep, export reconciler, staging cleanup, and alert delivery — suspending
crash-consistency machinery during exactly the window an operator restarts
things would be self-defeating. Already-queued jobs drain normally; the mode
stops new work *generation*, it does not pause the worker's consumers.

The switch is a banner + toggle on the Jobs page (with an optional note shown
while active, e.g. "pg_dump, back ~03:00") and survives restarts — it is one
Postgres row, so app and worker both see it. API:
`GET/POST /api/v1/system/maintenance-mode` (`{"active": true, "reason": "…"}`).
Don't forget to exit it — nothing expires the mode automatically.

For pausing or cleaning up an *individual agent* (rather than central), see
[agent suspend & maintenance](agents.md#agent-suspend-maintenance).

## The Logs panel (Jobs page) {#logs-panel}

Below the maintenance table, the **Logs** panel tails a unified log stream
from both processes — the app container *and* the worker container — so you
don't need shell access and two `docker logs` sessions to see what the system
is doing. `filearr.*` loggers record activity at INFO and up; every other
component records warnings and errors only; per-request access lines are never
recorded. Error records carry their traceback (expandable per row).

Filters: minimum level, process (`app`/`worker`), and a message substring;
auto-refresh re-fetches the newest page every 10 s; "Load older" pages
backward. The stream is bounded — rows are purged after
`FILEARR_LOG_RETENTION_DAYS` (default 7) by the "Purge console logs" task,
with a hard row cap as a log-storm backstop — and it stays entirely local (see
[data collection](data-collection.md)). The API is `GET /api/v1/system/logs`.
Recording can be disabled with `FILEARR_LOG_DB_ENABLED=false`.

For *why a background job failed*, the *failed jobs* list (with per-job error
text) remains the sharper tool; the Logs panel is the wider net — warnings
that never failed a job, app-side request errors, startup messages.

## Checking for updates (Jobs page) {#update-check}

The **Updates** card compares this install against the source repository and
shows the recent commit messages — the project's changelog — so you can review
what a redeploy would bring, per component:

- **central** — a source-deployed instance's build stamp embeds its build
  time, so the card reports whether the repository has newer commits;
  container-image installs compare the git commit their agent bake was built
  from against the repository head.
- **agent binaries** — the baked agent version vs the canonical
  `agent/VERSION` at the repository head.

Clicking **Check now** (admin) contacts GitHub — that is the entire network
footprint, nothing about your instance is sent, and *nothing runs
automatically* unless you opt in with `FILEARR_UPDATE_CHECK_AUTO=true`
(stale-cache refresh on console loads). Results are cached for 6 hours; an
offline box degrades to a "could not reach GitHub" note. Commits newer than
the running build are marked with a dot; click a subject to read the full
message. The API is `GET`/`POST /api/v1/system/update-check`.

## The About page — what this deployment is actually running {#about-page}

The footer **About** link (`#/about`) answers the first question of nearly every
support conversation: *which versions is this instance running?* It is the page
to open before filing a bug, before checking whether an advisory applies to you,
and after a deploy that you are not certain landed.

Everything on it is read from the **running system**, never from a configuration
file. A pin in `pyproject.toml` says what the image was asked to install; the
About page reports what it *did* install, and those two disagree exactly when it
matters (a stale layer cache, a hand-patched container, a redeploy that did not
take). Anything that cannot be determined is shown as an explicit
"unreachable" / "not installed" / "not downloaded" with the reason — never as a
blank cell or a zero.

Sections:

| Section | Where the numbers come from |
|---|---|
| **Application** | The running process: app version, the deploy **build stamp** (the same value the deploy verifier checks), licence, Python version, kernel/architecture. |
| **Services** | Probed live. Meilisearch reports its own engine version through the same client every search uses; PostgreSQL reports `SELECT version()`; Procrastinate and SQLAlchemy report the version of the code this process imported. Each probe degrades **independently** — a Meilisearch outage shows as one "unreachable" row, not a broken page. |
| **Backend dependencies** | Every direct Python dependency with the version actually importable in this process, plus a link to its documentation (taken from the package's own metadata, so it cannot drift). |
| **Frontend bundle** | Recorded when the bundle was compiled — the image ships only built assets, so these are the resolved versions baked into the JavaScript you are running, along with the Node that built it. |
| **Extraction tools** | ffprobe, ffmpeg, exiftool, tesseract and the poppler trio **on the central server**: present or absent, the version each one reports, and whether that version clears the [minimum Filearr recommends](agents.md#agent-tool-minimums) — a version shown in amber is below it, and the tooltip says what that costs. Judged by the same rule as an agent's tools, so the two pages cannot disagree; agents report their own on the Agents page. |
| **Agent fleet** | Distinct `agent_version` values with a count each, so a rollout still in flight is visible at a glance. Hidden when `FILEARR_AGENTS_ENABLED` is off. |
| **Embedding model** | See below. |

**No outbound network calls.** The page links to upstream projects; it never
fetches from them, so it renders identically on an air-gapped box. (The
Meilisearch and PostgreSQL probes talk to this stack's own services, the same
peers every search already uses.) The one page that *can* reach the internet is
the [Updates card](#update-check), and only when you click it.

**Copy as Markdown** dumps the whole stack as Markdown tables — paste it
straight into a bug report rather than transcribing versions by hand.

### The embedding-model section, and what its date means

The semantic-search section reports the configured Hugging Face repository and
file, whether the model is actually **cached on this machine**, the size of the
cached file, and a link to the model page.

Two things are worth reading carefully:

- **Revision.** `huggingface_hub` stores a cached repository as
  `models--{org}--{name}/snapshots/{commit_sha}/`, so the snapshot directory
  name *is* the upstream commit these exact weights came from. The page shows
  that sha and links to it on the Hub — much stronger provenance than any date.
- **"Downloaded here" is a LOCAL time.** It is the modification time of the
  cached file: **when this machine downloaded it**, not when the model was
  published. The publication date is not knowable without asking Hugging Face,
  and this page makes no outbound requests, so it is not guessed. Use the
  revision link for the upstream history.

On a default install semantic search is off and the model has never been
fetched. That is the normal state, and the page says so plainly rather than
presenting it as a fault — see
[Semantic search](reference/configuration.md#semantic-search-opt-in) to turn it
on.

The API behind the page is `GET /api/v1/system/about` (read scope).

!!! note "Why read scope, not admin"
    The page is a version fingerprint, which is mildly useful to an attacker who
    already holds a read key. It is read scope anyway: an AGPL §13 deployment
    already publishes its source and app version, and the people who need this
    page are usually the ones looking at a broken instance without an admin key.
    If your threat model disagrees, restrict the `read` scope itself — that is
    the control that decides who sees it.

## The console is a PWA — stale UI after an upgrade {#pwa-service-worker}

The web console installs a service worker (it is an installable PWA) that
precaches the UI and auto-updates itself. Two practical consequences:

- **After upgrading the server, reload the page once.** The new service worker
  is picked up on the next visit and takes control immediately; until that
  reload the tab may still render the previous UI build. The footer's
  `build` stamp shows which build the page came from.
- **Historical symptom** (fixed): opening `/api/docs` showed the search page
  instead of the API reference. That was the pre-fix service worker answering
  the navigation with the app shell. After upgrading, one reload installs the
  corrected worker and `/api/docs` renders Swagger again. If a client is
  somehow stuck, DevTools → Application → Service workers → *Unregister*, then
  reload.

Each instance serves its own copy of this manual at `/docs/` and the Swagger
assets for `/api/docs` from its own origin — neither needs internet access.

## "Task exception was never retrieved … AppNotOpen" from the worker {#appnotopen}

**Symptom.** The worker's container log (not the Logs panel — this prints on
stderr, outside the logging system) shows:

```text
Task exception was never retrieved
future: <Task finished name='process job …' … exception=AppNotOpen('App was not open. …')>
```

**Root cause (fixed 2026-08-08).** Filearr's defer helpers wrapped every
defer in `async with proc_app.open_async():`. In the API process that was
correct — but the same helpers also run **inside worker tasks**, where the
worker already owns an open Procrastinate app: there the enter was a no-op
while the context **exit closed the worker's shared connection pool**
(Procrastinate's close is unconditional, not reference-counted). Every
*concurrently running* job then failed with `AppNotOpen` — printed lazily by
asyncio whenever the failed task got garbage-collected, which is why the
message appears amid otherwise-healthy log lines — until the next defer
incidentally reopened the pool. Current builds route every such site through
a guard that never closes a pool it did not open (regression-tested,
including a sweep that fails if a raw `open_async()` context reappears), and
the API process now opens its pool once for its lifetime instead of per
call.

**Impact: none lasting, even on affected builds.** The interrupted jobs keep
status `doing`; the reaper requeues them within its 5-minute tick (FIX-6).
If you still see this on a current build, find what actually failed around
it:

```bash
docker inspect --format '{{.RestartCount}} {{.State.StartedAt}}' filearr-worker-1
docker compose logs --tail=200 worker | grep -B 25 "Task exception was never retrieved" | head -60
```

## Scan-scheduling storms / stalled jobs / the reaper

**Symptom.** A library's scheduled scan fires every scheduler tick instead of on
its cron, stacking several concurrent scan jobs per library. The worker dies
repeatedly (OOM — each full scan holds a whole-library map in RAM). Job attempt
counts climb into the dozens; the Jobs page shows many `doing` jobs with no live
worker.

**Diagnosis.**

```sql
-- concurrent / stalled scan jobs
SELECT task_name, status, attempts, count(*)
FROM procrastinate_jobs
WHERE task_name = 'filearr.tasks.scan.scan_library'
GROUP BY task_name, status, attempts ORDER BY attempts DESC;

-- scan runs that never finished (a crashed scan must end 'failed')
SELECT id, library_id, status, started_at
FROM scan_runs WHERE status IN ('running','stopping');

-- runaway retry counts on any job
SELECT task_name, status, attempts FROM procrastinate_jobs WHERE attempts >= 10;
```

Healthy is at most **one** `scan_library` per library in `todo`/`doing`
(also visible via `GET /api/v1/system/jobs`).

**Fix.**

1. Deploy the current version (dedupe now covers `todo`/`doing`/`aborting`; scans
   fire once per cron occurrence; the reaper caps non-scan requeues and finalizes
   orphaned scan runs).
2. Clean up rows the old code left (idempotent — or wait ~5 min for the reaper):

    ```sql
    UPDATE procrastinate_jobs SET status='failed'
    WHERE task_name='filearr.tasks.scan.scan_library' AND status='doing';

    UPDATE scan_runs SET status='failed', finished_at=now()
    WHERE status IN ('running','stopping');

    UPDATE procrastinate_jobs SET status='failed'
    WHERE status='doing' AND attempts >= 10;
    ```

3. Force a reaper pass and confirm: `GET /api/v1/system/jobs/reap` (admin).
4. Re-enable schedules (Admin → each library → Scan schedule, or `PATCH
   /api/v1/libraries/{id}` with `scan_cron`).

**Tunables.** `FILEARR_SCAN_SCHEDULE_MAX_CATCHUP_MINUTES` (default 2880 = 48h —
the furthest-back missed occurrence a recovery tick fires; never storms, never
back-fills a week of downtime) and `FILEARR_REAP_MAX_ATTEMPTS` (default 10).

**If OOM recurs.** Concurrent full scans over SMB each hold a whole-library map in
RAM. **Stagger** library `scan_cron` times so two large libraries never scan at
once, and/or raise the worker container's memory limit.

## Scan runs stuck in `stopping` (or orphaned `running`)

**Symptom.** A scan run is wedged in `stopping` (or `running`) with nothing to
clear it, and that library's *scheduled* scans silently stop happening (the
scheduler's busy-set counts those states, so one wedged row blocks the library
forever).

**Diagnosis.**

```sql
SELECT id, library_id, status, started_at FROM scan_runs
WHERE status IN ('running','stopping') ORDER BY started_at;
```

It is stuck if there is **no** `scan_library` job for that library still in
`todo`/`doing`/`aborting`.

**Fix.**

- **Auto-heal:** the 5-minutely maintenance reconciler drives any run older than
  the grace window terminal (`stopping → stopped`, `running → failed`).
- **Immediate:** `POST /api/v1/scans/{id}/force-clear` (admin; audited). Also a
  **Force clear** button on the Admin page. Returns `409 "still active; use stop"`
  only if a live worker is genuinely draining it.
- A **manual** library scan (`POST /libraries/{id}/scan`) also self-heals leftover
  rows before deferring.

**Tunable.** `FILEARR_SCAN_RUN_RECONCILE_GRACE_SECONDS` (default 600). Verify via
`GET /api/v1/system/jobs/reap` → `scan_runs_reconciled > 0`.

## Console unresponsive, host CPU pegged {#console-unresponsive-high-cpu}

**Symptom.** The web console stops loading (often right as a redeploy starts,
during the "quiesce jobs" step), the host/CT shows sustained high CPU, and
some ports answer while others don't. Full runbook:
[`docs/ops/troubleshooting.md`](https://github.com/pwsh/filearr/blob/main/docs/ops/troubleshooting.md).

**Triage from any machine.** `ping` the host, then `curl -m 5` each service
port and read `%{time_connect}`:

- port **connects but never responds** → the process is alive but starved
  (CPU or swap-thrash); the kernel accepted the connection for it.
- port **doesn't even connect** → that listener is *gone*: the process
  crashed or was OOM-killed.
- reverse proxy dead + app half-alive is the classic memory-pressure
  signature. Check `journalctl -k | grep -i oom` **on the host** (inside an
  unprivileged container you cannot see the kernel log) — but an empty result
  does **not** rule memory out: pure reclaim-thrash (`top` header showing
  `%sy` ≫ `%us`, available memory near zero, swap full) melts the box without
  a single kill line, and a drowning listener drops SYNs without ever dying.

**Usual cause at ≥1M items.** Stopping a scan flushes deferred documents to
Meilisearch; unbounded, Meili budgets ~2/3 of all visible RAM for that
indexing task and its RSS can blow past 6 GiB. The spike swap-thrashes the
box, and the single-worker API — itself near-idle — stops responding because
it's starved, not broken. `docker stats --no-stream` tells you hog vs victim,
and recovery is often spontaneous when the indexing task finishes (watch
`free -h` — climbing `available` means it's already healing).

**Recovery ladder** (each rung is safe: scans mark themselves `failed` after a
crash, Postgres is crash-safe, the search index is a disposable projection):
wait out a finite indexing/build task → `docker compose restart caddy app` →
restart the hog (`meilisearch`; run `rebuild_index` if search looks partial)
→ full `down`/`up -d` → reboot the VM/CT → rerun the deploy script
(idempotent).

**Prevention.** The compose file defaults `MEILI_MAX_INDEXING_MEMORY` to
`2 GiB` (override in `.env`) so indexing slows at a ceiling instead of eating
the host. Beyond that: size memory to the catalog (8 GiB+ at ≥1M items),
give the container real swap headroom (the 512 MiB LXC default converts
spikes into reclaim storms), and expect a busy minute at the start of every
redeploy — the quiesce step deliberately triggers the wrap-up flush *before*
containers are replaced.

## Unraid says an update is available when there isn't one {#phantom-update}

**Symptom.** The Unraid Docker page shows *update ready* for `filearr` (or
`filearr-agent`) immediately after updating, and it comes straight back if you
apply it.

**Confirm you really are current** before chasing anything. The image records the
commit it was built from, and the console's About page shows the commit it is
running — if the two agree, the update flag is wrong:

```bash
# what the tag points at, and what commit that image was built from
docker image inspect ghcr.io/pwsh/filearr:latest   --format '{{index .RepoDigests 0}}{{"
"}}{{index .Config.Labels "org.opencontainers.image.revision"}}'
```

**Cause.** `docker buildx` attaches provenance attestations by default when
pushing, and they ride along *inside the tag's manifest index* as extra entries
whose platform is literally `unknown/unknown`:

```text
amd64 · arm64 · unknown/unknown (attestation) · unknown/unknown (attestation)
```

An update checker walks that index looking for the entry matching the host
architecture and compares its digest against the local image. The
`unknown/unknown` rows break that match, so the comparison comes out unequal
forever. It is not specific to Unraid — Watchtower and Portainer report the same
phantom updates against attested images.

**Fix.** Builds from 2026-08-11 onward publish with `provenance: false` and
`sbom: false`, so each tag is a clean two-entry index. Build provenance is
unaffected: it still ships via Sigstore keyless attestation (publicly logged in
Rekor), which attaches as an OCI *referrer* rather than as an index entry, and is
the stronger of the two —

```bash
gh attestation verify oci://ghcr.io/pwsh/filearr:latest -R pwsh/filearr
```

You will see one *legitimate* update after this lands (dropping the attestation
entries changes the index digest). Apply it once; the flag should stay quiet
afterwards.

## The dashboard shows "unavailable" for a section {#stats-degraded}

**Symptom.** A panel on the dashboard reads *unavailable* with a reason instead
of a number, or the deploy smoke test logs `DEGRADED sections: ...` next to an
otherwise passing `/api/v1/stats` check.

This is the endpoint working as designed. `/api/v1/stats` fans out to seven
independent aggregates (item counts by category, job queues, extraction errors,
Meilisearch health, semantic coverage, thumbnail cache, disk headroom). Each one
runs under three bounds: a 5 s Postgres `statement_timeout`, an 8 s client-side
backstop for the parts a statement timeout can't reach (the Meilisearch HTTP
call, the `statvfs` thread), and a 10 s deadline shared across all of them. A
section that overruns is reported in a top-level `degraded` map and the rest of
the payload is served normally.

The distinction matters: a degraded section is **not** the same as a zero.
"No extraction errors" and "we could not count the extraction errors" call for
opposite reactions, so the console labels the gap rather than rendering a
confident `0`.

**Finding the culprit.** The app log names it:

```bash
docker compose logs app | grep 'stats: section'
```

**Why it exists.** Before this was bounded, any single slow aggregate hung the
whole endpoint indefinitely. On a ~1.09M-item instance `/api/v1/stats` connected
instantly and then timed out past 15 s on every attempt — with `/health`,
`/version` and `/search` all answering 200 — which failed the deploy's smoke gate
while the stack was otherwise healthy and gave the operator nothing but
`HTTP 000` to work with.

**If `semantic` is the degraded section.** The semantic-coverage counts are the
one aggregate whose cost scales with how much text extraction you run: OCR and
PDF text are stored in `items.metadata`, which pushes those JSONB values over
Postgres' TOAST threshold, and a query that reads the value of every active row
has to reconstruct each one. The counts are now written so the value read is
confined to rows that already carry an embedding fingerprint (the existence test
is answered from the GIN index instead), so this should stay fast; if it still
degrades on your catalogue, `VACUUM ANALYZE items` first — a stale plan on a
table that has grown by a million rows is the usual reason the planner stops
using the index.

## Extraction throughput and adaptive backpressure {#extract-backpressure}

Extraction is the greediest stage of the pipeline: one job per file, each one
opening a file over SMB/NFS and running a parser. Two mechanisms keep it from
eating the box.

**Static (always on).** The `extract` queue carries a negative job priority, so
a freshly triggered scan — or a cancel — is never queued behind a 5k-file
extract backlog on a shared worker. Extraction never preempts scan control.

**Adaptive (the controller).** Each worker process runs a small control loop
that varies *how many extract jobs it runs at once*, between
`FILEARR_EXTRACT_BACKPRESSURE_MIN_CONCURRENCY` and
`FILEARR_EXTRACT_BACKPRESSURE_MAX_CONCURRENCY`. Two different signals move the
ceiling in the two different directions:

- **Host load contracts it.** Every
  `FILEARR_EXTRACT_BACKPRESSURE_SAMPLE_SECONDS` the worker reads the 1-minute
  load average per core — the same number an operator eyeballs. At or above
  `FILEARR_EXTRACT_BACKPRESSURE_HIGH_LOAD` the ceiling is *multiplied* by
  `FILEARR_EXTRACT_BACKPRESSURE_DECREASE_FACTOR` (halved by default), again on
  each sample while the pressure lasts, down to the minimum. Halving rather
  than dropping straight to the minimum means a brief spike costs one step of
  throughput instead of the whole recovery window. The ceiling only starts
  recovering below `FILEARR_EXTRACT_BACKPRESSURE_LOW_LOAD` (hysteresis — a
  single threshold would flap).
- **Queue depth expands it.** One slot per sample, and only when the host is
  quiet *and* extract jobs are actually waiting. Backlog is deliberately never
  a reason to throttle — the only way a deep extract queue gets shorter is by
  running extract jobs — but a deep queue on an idle host is exactly when more
  concurrency is free throughput. Nothing waiting means nothing to gain, so
  the ceiling stays put. The backlog reading is a bounded query (it saturates
  at "deep enough" rather than counting a multi-million-row job table).
- **Anti-thrash.** At most one adjustment per sample, and no expansion within
  `FILEARR_EXTRACT_BACKPRESSURE_EXPAND_COOLDOWN_SECONDS` of a contraction —
  the 1-minute load average lags reality by about a minute, so expanding
  sooner reacts to a number that has not caught up yet.

Jobs above the ceiling are **not** parked on a worker slot: they are
rescheduled 15–45 s out (jittered, attempt-agnostic — never counted as a
failure and never recorded as a job error), so the slot goes to scan, index or
maintenance work and the queue drains itself as pressure subsides.

**What you will see.** Every transition is logged at INFO by the *worker*
(`filearr.backpressure`) with its reason and inputs, visible in the Jobs page
[Logs panel](#logs-panel):

```text
extract backpressure: tripped (load/core 1.42 >= 0.85); contracting extract concurrency in this worker
extract backpressure: contracting 4 -> 2 (load/core 1.42 >= 0.85, in flight 4)
extract backpressure: recovered (load/core 0.51 <= 0.60); ceiling 1/4, 137 jobs were rescheduled while tripped
extract backpressure: expanding 1 -> 2 (load/core 0.22 <= 0.60, backlog >=100 waiting, in flight 1)
```

The state is deliberately **per worker process** and not shown on any
dashboard: each worker samples its own host and protects its own share, and
the API process — which never runs extract jobs — would only ever report an
idle limiter. Read the worker's log lines, not a gauge.

**When to intervene.** Extraction that never seems to reach full concurrency
on a busy box is the controller working, not a fault. If you want it out of
the way entirely, set `FILEARR_EXTRACT_BACKPRESSURE=false`; the static queue
priority still stands. On hosts with no load average (Windows dev) the
controller never activates at all. Note that the ceiling's default maximum is
`FILEARR_WORKER_CONCURRENCY` — if you pass `--concurrency` to the worker
command without setting that variable to match, set
`FILEARR_EXTRACT_BACKPRESSURE_MAX_CONCURRENCY` explicitly or extraction will
cap below the slots you actually have.

## A library indexes fewer files than the OS reports {#library-file-count-mismatch}

**Symptom.** The folder's Windows *Properties* (or `find | wc -l`) reports far
more files than the library contains, and `seen + excluded` from the last scan
still does not close the gap.

**This is usually correct behaviour**, not data loss — but until you know *which*
mechanism dropped the files, you cannot tell that apart from a broken scan.

**Worked example (real, 2026-07-19).** A documents library reported
`seen 77,394 · excluded 318` against **99,694** files on disk:

```text
seen              77,394
excluded         +   318
                 ────────
                   77,712     ← what the UI could account for
on disk            99,694
                 ────────
unexplained        21,982     ← all of it inside pruned dot-directories
```

Every missing file lived under a `.`-prefixed directory (`.git`, `.venv`, `.vs`)
pruned wholesale by the default-on `hidden_dotfiles` preset.

### The four mechanisms

| Mechanism | Counted as | Visible? |
| --- | --- | --- |
| Taxonomy **category/group gate** — the library's selection did not admit the file | `excluded_gate` | yes |
| **Exclusion spec** — presets, `exclude_globs`, hidden dotfiles | `excluded_filtered` | yes |
| **Pruned directories** — whole trees skipped *without being enumerated* | `pruned_dirs` (count of dirs) | **files invisible by default** |
| **Unreadable directories** — `PermissionError` on the directory | `permission_denied` | **files invisible** |

The last two are the trap: pruning deliberately never reads the tree, so the
files inside are counted **nowhere**. `seen + excluded` is therefore a *lower
bound* whenever `pruned_dirs > 0`.

### Bisect it: can the container even see the files?

Run this **inside the CT**, against the same directory, before blaming the scan:

```bash
pct exec 300 -- bash -c 'P="/data/media/<share>/<path>"
echo "files:       $(find "$P" -type f | wc -l)"
echo "symlinks:    $(find "$P" -type l | wc -l)"
echo "unreadable:  $(find "$P" -type d ! -readable | wc -l)"'
```

If the **file count here is also below** what the OS reports, the loss is in the
**mount, not Filearr** — rclone over SMB silently drops entries it cannot map
(invalid characters, over-long paths, listing errors). No scan counter will ever
explain that; fix the mount.

If it matches, the loss is Filearr's filtering — carry on below.

### Attribute the gap

```bash
pct exec 300 -- bash -c 'P="/data/media/<share>/<path>"
echo "under dot-dirs: $(find "$P" -type f -path "*/.*/*" | wc -l)"   # pruned
echo "dotfiles:       $(find "$P" -type f -name ".*" ! -path "*/.*/*" | wc -l)"
echo "no extension:   $(find "$P" -type f ! -name "*.*" | wc -l)"
find "$P" -type f -name "*.*" | sed "s|.*\.||" | tr "[:upper:]" "[:lower:]" \
  | sort | uniq -c | sort -rn | head -40'
```

- **`under dot-dirs`** large → the `hidden_dotfiles` prune. Expected for source
  trees; `.git` alone can hold tens of thousands of objects.
- **`no extension`** large **and** the library has categories selected → the gate
  drops all of them (an extensionless file classifies into no `file_group`).
- **top extensions** → anything not in the library's selected categories/groups is
  dropped by the gate. An **empty** selection means *index everything*, so
  `excluded_gate` will be 0.

### Make the numbers reconcile exactly

Enable **Count files in pruned folders** on the library (Admin → edit library →
Content processing), or via the API:

```bash
curl -X PATCH http://<host>:8484/api/v1/libraries/<id> \
  -H 'Content-Type: application/json' -d '{"count_pruned_files": true}'
```

Then rescan. The walk does a second, deliberately cheap pass over each pruned
subtree — `scandir` only, no `stat`, no matching, no ingestion — and the identity
holds exactly:

```text
seen + excluded + pruned_files == files on disk
```

The scan also records `pruned_paths` (a capped sample) so the UI names the
culprits rather than showing a bare count.

**It is off by default on purpose:** that pass fully lists directory trees you
have deliberately chosen not to index, and directory listing is the expensive
operation on rclone/SMB mounts — exactly where large pruned trees live. Turn it
on to investigate, then turn it back off.

**Reading the badge.** The Libraries page shows `excluded N` next to the last
scan. A trailing **`+`** (e.g. `excluded 318+`) means directories were pruned and
the count is a lower bound; `excluded 318 + 21,978 pruned` means the opt-in is on
and the accounting is complete. Hover for the full breakdown.

## Disk fills up (unbounded generation → Postgres crash)

**Symptom.** On single-volume deploys the thumbnail cache, Postgres data dir and
the Meili store share one filesystem. Unbounded thumbnail generation fills it; at
0 bytes free Postgres can no longer extend a file and the platform crashes.

**Diagnosis.**

```bash
curl -s localhost:8484/api/v1/system/disk | jq     # {status, paths:[{label,free,pct_free,status}]}
du -sh /config/thumbnails
du -sh /config/* | sort -h                           # biggest consumer
```

`/api/v1/stats` also carries a `disk` section; the Jobs page shows an amber
(warn) / red (critical) banner.

**How the guardrails behave.** Two floors, the more conservative winning: absolute
GB (warn 20 / critical 5) and percent-free (warn 10% / critical 2%). At **WARN**
producers keep writing; at **CRITICAL** thumbnail writes fail-closed (no retry
loop; serve path returns a 404 placeholder, never a 500), OCR is skipped, the
embedding-model download is refused, and — when `FILEARR_DISK_PG_PATH` is set
(the bundled compose does this by default via a read-only `pgdata` mount) —
extract pauses. Other queues and the workers stay alive.

**Recovery of a box that already filled.**

```bash
docker compose stop worker watcher     # 1. stop the write pressure (API stays read-only)
rm -rf /config/thumbnails/*             # 2. thumbnails are disposable — every byte regenerates
du -sh /config/* | sort -h              # 3. free a few GB for Postgres to breathe
docker compose up -d postgres           # 4. bring PG back
docker compose logs -f postgres         #    watch for "database system is ready" (PG replays WAL)
docker compose up -d worker watcher     # 5. restart workers
```

Do **not** delete Postgres data. Then re-trigger the failed thumbnails from the
Jobs page (or let the serve path regenerate them lazily).

**Prevent recurrence.** Raise the floors on a small volume
(`FILEARR_DISK_WARN_FREE_GB` / `FILEARR_DISK_MIN_FREE_GB`), set
`FILEARR_DISK_GC_TARGET_FREE_GB > 0` so the emergency GC LRU-evicts to a target,
grow the volume, and enable a low-space alert.

### Disk fills after repeated deploys (Docker layers, not thumbnails) {#deploy-disk-fill}

**Symptom.** `disk warn/critical` on the *temp (app disk)* / *database* rows
right after redeploying — especially after several rebuilds in a row (a failed
build retried, `FORCE_REBUILD=1`). The emergency thumbnail GC logs that it
found **nothing to reclaim**: it frees the *thumbnail* filesystem, and the
pressure here is on the disk holding Docker and Postgres. (Live example: a
rebuild storm took a 125 GB CT from 74 GB free to **0.6% free** in one
evening.)

**Diagnosis** — run *inside the container* (`pct enter <vmid>` from the
Proxmox host), or prefix each command with `pct exec <vmid> --` from the host:

```bash
df -h /                    # how bad is it
docker system df           # images vs containers vs build cache vs volumes
docker system df -v        # ...itemized (which image/volume is the pig)
du -x -d1 -h / | sort -h   # non-Docker consumers on the root fs (logs, etc.)
```

**Recovery, in order of safety:**

```bash
docker image prune -f                        # dangling layers from old rebuilds — always safe
docker builder prune -f --keep-storage 6GB   # trim BuildKit cache (next deploy stays incremental)
journalctl --vacuum-size=200M                # if du shows the journal grew large
```

Interpret the prune output honestly: if it reclaims little (hundreds of MB)
the space went somewhere else — `docker system df -v` tells you whether it's
the Postgres volume growing with your catalog (legitimate; grow the disk) or
the build cache, and `du` catches everything outside Docker.

**Prevent recurrence.** Deploys from 2026-08-08 on prune dangling images
automatically after every build (alongside the existing BuildKit cache trim),
so rebuild storms no longer accrete layers.

!!! tip "Proxmox web-console paste eats the first character"
    Pasting into the noVNC console frequently drops the leading keystroke —
    `pct exec …` arriving as `ct exec …` (`-bash: ct: command not found`) is
    the classic symptom. Retype the first letter, or use SSH to the host
    instead of the web console for anything copy-pasted.

## Migration failures / Alembic state / stamping

**Symptom.** After an upgrade or restore the app errors on schema mismatch, or a
pre-Alembic database has no `alembic_version` table.

**Diagnosis.**

```bash
docker compose exec app alembic current    # what revision the DB is at
docker compose exec app alembic heads       # what the code expects
docker compose exec -T postgres psql -U filearr -d filearr \
  -c "SELECT version_num FROM alembic_version;"
```

**Fix.** `scripts/init_db.py` is idempotent and does the right thing in every case
— it detects a pre-Alembic DB, **stamps the baseline**, then upgrades to head,
applies the procrastinate schema, and ensures the Meili index exists:

```bash
docker compose run --rm app python scripts/init_db.py
```

For a manual upgrade only: `docker compose exec app alembic upgrade head`.
Requires Postgres 18+ (the baseline uses `uuidv7()` defaults). **Always take a
fresh dump before a downgrade** — downgrades are best-effort and can lose
recycle-bin data.

## The ltree bind-cast (42804) error class

**Symptom.** Item writes 500 with a Postgres `42804` (datatype mismatch) — e.g. an
agent push returns HTTP 500. Blast radius is every INSERT/UPDATE that writes an
item's path scope, and every path-grant creation.

**Diagnosis.**

```bash
curl -sk -X POST https://filearr.example.com/api/v1/... -d '<payload>'   # observe the 500
docker compose logs app | grep -i 42804                                   # confirm the class
```

**Cause.** The scope columns are real Postgres `ltree` columns (PG18 ships the
`ltree` contrib), but if they are ORM-mapped as plain text the driver renders a
`::VARCHAR` bind cast, and Postgres has no `varchar → ltree` assignment cast. This
was invisible in tests where the test Postgres shipped no contrib.

**Fix.** Upgrade to the version whose ORM binds the parameter as *unknown* (no
cast) and lets the server coerce it, with a text DDL fallback where the extension
is absent. Nothing operator-side beyond deploying it. **General lesson:** any
column backed by a Postgres **extension type** (`ltree`, `vector`, …) must not be
mapped as a plain scalar, or the driver emits a cast the server rejects.

## Agent enrollment / CA (step-ca) failures {#agent-enrollment-ca-step-ca-failures}

**Symptom.** An agent registers successfully but the register response's `ca_ott`
is **null**, so it can never fetch a certificate. By design a bad CA config never
takes registration down — it just yields a null token.

**Diagnosis — narrow it down.**

```bash
# 1. Is the CA healthy and reachable?
docker compose exec step-ca step ca health \
  --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
docker compose logs step-ca | tail -50

# 2. Central emits an audit event per successful mint (jti only, never the token):
docker compose exec -T postgres psql -U filearr -d filearr \
  -c "SELECT event_type, details, ts FROM security_events
      WHERE event_type='agent_ca_ott_minted' ORDER BY ts DESC LIMIT 5;"
```

**Root causes seen live, and their fixes.**

- **`FILEARR_CA_PROVISIONER_JWK` unset or malformed.** Central signs the OTT with
  the provisioner's decrypted private JWK; without a valid one, `ca_ott` is null.
  Central validates the shape (EC P-256, private) on first use and logs only
  *that* it is unset/malformed — never the key.

- **JWK read from the wrong place (remote-management gotcha).** The compose stack
  enables remote management, so step-ca keeps provisioners in its **admin
  database**, not in `authority.provisioners` in `ca.json` — any procedure that
  edits/reads `ca.json` is a no-op. Confirm:

    ```bash
    docker compose exec step-ca cat /home/step/config/ca.json | grep -c provisioners  # 0 under remote mgmt
    ```

    The provisioner list (including the JWE-encrypted key) is instead served by the
    CA's public `/provisioners` endpoint (publishing the JWE is by design — only
    the password opens it). Extract and decrypt:

    ```bash
    ENC=$(curl -sk https://localhost:9000/provisioners \
      | python3 -c 'import json,sys; d=json.load(sys.stdin);
          print(next(p["encryptedKey"] for p in d.get("provisioners", d if isinstance(d,list) else [])
          if p.get("name")=="filearr-agents" and p.get("type")=="JWK"), end="")')
    printf '%s' "$ENC" | docker compose exec -T step-ca \
      step crypto jwe decrypt --password-file /home/step/secrets/password
    # -> {"kty":"EC","crv":"P-256","kid":"...","x":"...","y":"...","d":"..."}
    ```

    Paste the decrypted private JWK into `FILEARR_CA_PROVISIONER_JWK` (a secret —
    never commit it, never echo it) and recreate the app/worker.

- **Which password opens the JWE?** Under remote management the key is encrypted
  with the CA **administrative password**, printed **once** in the first-boot log
  — *not* the `secrets/password` (CA-key) password:

    ```bash
    docker compose logs step-ca | grep -i "password is"
    ```

    The deploy automation tries `secrets/password`, then `secrets/admin_password`,
    then recovers the log-printed password and persists it (mode 0600 in the CA
    volume) so recovery never again depends on log retention. If the first-boot
    log is gone and the password was never persisted, it is unrecoverable — rotate
    the provisioner key (`step ca provisioner update filearr-agents
    --private-key=…`) and put the new plaintext private JWK in `.env`.

- **Provisioner claims (cert lifetimes) not set.** Under remote management set
  them through the admin API:

    ```bash
    docker compose exec -T step-ca step ca provisioner update filearr-agents \
      --x509-min-dur=24h --x509-default-dur=48h --x509-max-dur=72h \
      --allow-renewal-after-expiry \
      --admin-subject=step --admin-provisioner=filearr-agents \
      --admin-password-file=/home/step/secrets/password \
      --ca-url https://localhost:9000 --root /home/step/certs/root_ca.crt
    ```

    `allow-renewal-after-expiry` lets a long-offline agent renew a just-expired
    cert over mTLS instead of re-enrolling.

**Recovery once the key is fixed.** Hand a fresh OTT to already-registered agents:
`POST /api/v1/agents/{id}/ca-ott` (admin; works for a pending or active agent;
`409` if revoked, `404` unknown, `503` if the JWK is still unconfigured).
Enrollment tokens are **single-use** — if a re-enroll fails with "token
consumed", mint a **new** token rather than reusing the old one.

## Orphaned pending agents; revoke vs delete

A failed enrollment leaves a **pending** agent (registered, no cert bound). To
clean up: **revoke** to deny it at the application layer while keeping its row and
history, or **hard delete** (`DELETE /api/v1/agents/{id}?purge=true`) to remove
the row entirely — refused (409) while any library/item still references it. See
[Agents → revoke vs delete](agents.md#killing-an-agent-revoke-vs-delete).

## TLS and ACME issuance failures

**Symptom.** Wildcard cert issuance hangs with `timed out waiting for record to
fully propagate` and no Cloudflare API errors above it.

**Cause / fix.** The TXT record published fine but the propagation self-check
can't see it — almost always **split-horizon DNS** (a LAN resolver answering
authoritatively for your domain hides the public `_acme-challenge` record from the
container). The shipped Caddyfile pins the check to public resolvers
(`1.1.1.1`, `8.8.8.8`) for exactly this — don't remove it on a homelab network.

**Related gotchas.**

- With split-horizon overrides, **every** hostname the box serves needs its own
  LAN override → container IP (`192.0.2.10`): `filearr.example.com`,
  `agents.example.com`, `ca.example.com`. A missing `ca.` override breaks agent
  cert renewal from inside the LAN.
- A scoped Cloudflare token needs **both** `Zone:Read` and `DNS:Edit` on the zone.
- `ca.example.com` must be **raw SNI/L4 passthrough** to step-ca — an L7
  terminator silently breaks `/renew` (which authenticates with the agent's client
  cert on the direct TLS connection).
- **LAN/homelab mode** (`FILEARR_TLS_MODE=internal`) needs no DNS/ACME/egress;
  Caddy mints a self-signed root. Trust it on clients:

    ```bash
    docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./filearr-root-ca.crt
    # import into each client's OS/browser trust store
    ```

## Alerting doesn't fire

**Symptom.** No notifications for scan failures, extract spikes, low disk, agent
offline/stall, or failed report deliveries.

**Fix.** All system rules ship **seeded, disabled, with no channel**. In Admin →
Alerts: create a channel (webhook / SMTP / Apprise), attach it to the rule, and
**enable** the rule. Use the channel-row **Test** button to confirm delivery.

**Webhook specifics.** A Discord webhook rejects a generic body (`400 … Cannot
send an empty message`). Set the channel's payload format to `discord`
(auto-detected from a `discord.com/api/webhooks/…` URL) or `slack`; leave the HMAC
secret blank for those (they don't verify it). All other protections (SSRF
default-deny, no-redirect, bounded I/O) are identical.

### Apprise channels {#apprise-channels}

[Apprise](https://github.com/caronc/apprise) is the "everything else" channel: one
URL selects one of ~100 notification services — `tgram://`, `ntfy://`, `pover://`,
`matrixs://`, `gotify://`, `discord://` and so on — so Filearr does not need a
driver per service. Pick channel type **apprise** in Admin → Alerts and paste the
service URL; the [Apprise URL syntax][apprise-urls] page documents the format for
each service.

[apprise-urls]: https://github.com/caronc/apprise/wiki

**It is an optional extra.** Apprise is not installed by default (it pulls a large
dependency tree that webhook/SMTP-only deployments never use). Install it
alongside Filearr:

```bash
pip install "filearr[apprise]"
```

For Docker, add it to the image (a one-line `RUN pip install apprise` layer on top
of `ghcr.io/pwsh/filearr`, rebuilt with each upgrade) — installing into a running
container does not survive a recreate.

**A missing extra never drops alerts silently.** An apprise channel configured
without the package fails with a **permanent** (non-retryable) error naming the
fix, visible in the channel's **Test** result and in the alert's `last_error` on
the Events tab. The alert goes terminal on the first attempt rather than retrying
an outcome that cannot change — no retry storm, no silence.

**The whole URL is the secret.** An apprise URL embeds its credential inline
(`tgram://<bot-token>/<chat-id>`), so Filearr treats the entire string as a secret:
it is AES-GCM encrypted at rest under `FILEARR_SECRET_KEY` (like the SMTP password
and webhook HMAC secret — see [Security](security.md)), never returned by the API
(reads show `__redacted__`; leave the field blank when editing to keep it), and
never written to a log or an error message — even when a service quotes it back in
a failure, the URL and its components are scrubbed before the error is stored.

**Two other apprise-specific behaviours.** A multi-target channel takes **one URL
per line** (newline is the only separator, because apprise URLs legitimately
contain commas in query parameters). And unlike webhook channels, apprise targets
are **not** SSRF-vetted — apprise owns URL parsing and connection setup for every
plugin, so there is no seam to check a resolved address; the compensating control
is that these URLs are admin-scope configuration carrying an embedded service
credential, not attacker-suppliable targets. One send is bounded by
`FILEARR_ALERT_APPRISE_TIMEOUT_S` (30s).

**Agent alert thresholds.** *Agent offline* defaults to a generous 48h (offline is
normal for laptops); *replication stalled* is the sharper 6h signal (alive but not
draining its outbox). Tune with `FILEARR_AGENT_OFFLINE_ALERT_SECONDS` /
`FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS`.

## Search index drift → rebuild-index

**Symptom.** Search results are missing items, stale, or (for scoped non-admin
users after an RBAC upgrade) empty. Postgres and Meilisearch have diverged.

**Diagnosis.** An hourly reconcile sweep detects divergence and never writes
Postgres — check the Jobs page / worker logs for its results.

**Fix — always safe** (Meili is a disposable projection):

```bash
curl -X POST http://localhost:8484/api/v1/system/rebuild-index
```

The rebuild uses a **shadow-index swap**, so live search stays up. A rebuild is
**required** after any upgrade that changes indexed fields — notably enabling
path-scoped RBAC search (until the rebuild finishes, scoped non-admin users fail
*closed* to empty results; admins/API keys are unaffected). A crashed rebuild can
leave an orphaned shadow index; an hourly reaper deletes shadows older than
`FILEARR_MEILI_SHADOW_MAX_AGE_HOURS` (6h), never touching a young in-flight one.

## Search index grows on disk → compaction {#meili-compaction}

**Symptom.** The Meilisearch store keeps growing after big deletes, re-scans or
a rebuild, and `du` reports far more than the catalogue should need. Meili's
LMDB store never shrinks by itself: freed pages stay allocated to the file.

**Diagnosis.** Compare the two sizes Meili reports — `database_size` is what the
store occupies, `used_database_size` is what is actually in use:

```bash
curl -s -H "Authorization: Bearer $MEILI_MASTER_KEY" \
  http://localhost:7700/stats | jq '{databaseSize, usedDatabaseSize}'
```

Their ratio is the fragmentation. Around 1.0 is healthy; past
`FILEARR_MEILI_COMPACTION_THRESHOLD` (default 1.3) there is real space to
reclaim.

**Fix.** The **Compact search index** maintenance task runs weekly (Sunday 06:00
UTC by default, editable on the Jobs page) and compacts only when the ratio is
past the threshold — otherwise it logs "not fragmented" and stops. **Run now**
on the Jobs page triggers it immediately.

Two things to know before triggering one by hand:

- Compaction transiently needs **roughly twice the index size free**, because
  Meili writes the compact copy alongside the old one. If
  `FILEARR_MEILI_DATA_PATH` points at a store this process can see, the task
  **refuses to start** at the critical disk floor and says so in the log; on the
  bundled compose stack Meili owns its own volume, so set that variable only
  where the path is genuinely visible to the app/worker.
- It reclaims space and nothing else. The index is a disposable projection of
  Postgres, so a compaction that is skipped, refused or fails costs you bytes,
  never data — every failure path logs and exits cleanly rather than failing the
  job. Set `FILEARR_MEILI_COMPACTION_ENABLED=false` to switch the weekly job off
  entirely; the Jobs page then shows it with a "will no-op" chip.

## Upgrading Meilisearch (the pin moved) {#meili-upgrade}

**A newer Meilisearch refuses to open an older database.** It does not
auto-migrate and it does not start degraded — it prints the version mismatch and
exits, which in a container means a restart loop, search down, and a failing
deploy smoke test. This is the single thing to know before changing the pinned
tag:

```text
Your database version (1.49.0) is incompatible with your current engine
version (1.53.0). ... you can set the `--upgrade-db` flag (or the
MEILI_UPGRADE_DB environment variable) to upgrade the database on startup.
```

**The stack already handles this.** `docker-compose.yml` sets
`MEILI_UPGRADE_DB=true`, and the Unraid template exposes it as an advanced
variable defaulting to `true`, so a version bump migrates in place on first
start. Leaving it on is deliberate and safe: once the database matches the
engine it is a **no-op** — no second migration task, no warning. (Verified by
running the real binaries against a real database: 1.49.0 → 1.53.0 migrated with
documents and settings intact, and a restart with the variable still set
produced no further upgrade task.)

**Before bumping a pin:**

1. **Snapshot first.** Meilisearch's own documentation warns the in-place
   upgrade "may partially fail and result in a corrupted database". The index is
   a disposable projection — a rebuild from Postgres is always the fallback — but
   on a large catalogue a rebuild is hours and a snapshot restore is minutes.
2. Expect **new task processing to pause** while the `UpgradeDatabase` task runs.
   Search keeps answering. Watch it with
   `GET /tasks?types=upgradeDatabase`.
3. How long it takes on a million-item catalogue **is not published anywhere and
   we have not measured it**. The mechanism is a format migration rather than a
   re-index (a two-document test took 12 ms), which suggests short — but treat
   that as an expectation, not a promise, and keep the snapshot.
4. Re-read the **full** advisory list for every version you are traversing, not
   just the one that set your current floor. Meilisearch does not publish through
   GitHub Security Advisories and its CVE ids do not resolve in NVD or MITRE, so
   a scanner will come back clean and tell you nothing — the release notes are
   the only authoritative source.

## Recycle-bin / tombstone recovery {#recycle-bin-tombstone-recovery}

**Model.** Scans never hard-delete. A file gone from disk is tombstoned `missing`;
a user-deleted item becomes `trashed` (awaiting recycle-bin purge). Only `active`
items appear in search and browse.

**Recovery.**

- A **`missing`** item returns to `active` automatically the next time a scan sees
  the file again (identity is `(library, rel_path)`, so it re-attaches — no manual
  step).
- A **`trashed`** item is recoverable until the recycle-bin purge removes it.
  Retention is `FILEARR_RECYCLE_RETENTION_DAYS` (default **30**); the purge runs
  daily.
- Inspect what is recoverable:

    ```sql
    SELECT status, count(*) FROM items GROUP BY status;
    SELECT id, rel_path, status, deleted_at FROM items
    WHERE status='trashed' ORDER BY deleted_at DESC LIMIT 50;
    ```

- **Buy time before a purge:** raise `FILEARR_RECYCLE_RETENTION_DAYS` and restart
  before the daily purge runs.

## Backup and restore {#backup-and-restore}

This page used to say "what must be backed up: **Postgres only**". That is true
about your *catalogue* and wrong about your *deployment*, in two ways — and one
of them fails **silently**. Read the inventory before you trust a backup.

### State inventory — what exists, and what losing it costs {#state-inventory}

| State | Where it lives | Backed up by | If you lose it |
|---|---|---|---|
| **Postgres** (`user_metadata`, tags, custom fields, saved searches, libraries/schedules, alert config + history, provenance/audit, extracted metadata, job queue) | `pgdata` volume | `scripts/backup.sh` (and the in-app backup) | **Everything you cannot re-derive from the files themselves.** This is the source of truth. |
| **`FILEARR_SECRET_KEY`** | the deployment `.env` — deliberately **outside** Postgres | `env.backup` in the bundle | ⚠ **Silent loss inside a successful restore.** It is the AES-GCM envelope key for alert-channel secrets. A dump carries the ciphertext, not the key. Restore under a *different* key and everything reports success while every SMTP password, webhook HMAC secret and apprise URL becomes permanently undecryptable. You find out weeks later, from the alert that never arrived. Since 2026-08-12 the app records `sha256(key)[:16]` in the database and reports a mismatch loudly (see [below](#secret-key-mismatch)) — but the key itself is only ever recoverable from your backup. |
| **`POSTGRES_PASSWORD`** | `.env` | `env.backup` | The stack cannot connect to its own restored database until you set it to whatever the restored roles expect (or reset the role). |
| **`MEILI_MASTER_KEY`** | `.env` | `env.backup` | Search is unreachable until it matches what the Meili volume was initialised with; simplest fix is to wipe Meili and rebuild the index. |
| **`FILEARR_CA_PROVISIONER_JWK`** | `.env` only (never the committed compose file) | `env.backup` | Registration still succeeds but no agent can obtain a client certificate — the register response's `ca_ott` goes null. |
| **step-ca data** (CA root + intermediate private keys, provisioner state) | `stepca_data` volume | `stepca/` in the bundle | **Fleet-wide loss of trust.** An empty volume makes step-ca auto-init a **brand new root** on next start; every certificate it ever issued stops validating and **every enrolled agent must re-enroll**. No `pg_restore` repairs this. |
| **Meilisearch index** | `meilidata` volume | *nothing, deliberately* | Nothing permanent. It is a disposable projection: one `POST /api/v1/system/rebuild-index` rebuilds it from Postgres. |
| **Thumbnail cache** | `{config}/thumbnails` | *nothing, deliberately* | Nothing permanent — regenerated lazily on first view. |
| **`{config}/exports`** | config volume | *nothing, deliberately* | Nothing permanent: artifacts are TTL-purged anyway and the `report_exports` **row** (which is in Postgres) is the audit trail. |
| **`{config}/inventory`** | config volume | *nothing, deliberately* | Nothing permanent — re-request the inventory from the agent. |
| **`{config}/agent-releases`** | config volume | ⚠ **nothing — copy it yourself** | **The one `/config` subtree that is not regenerable.** Stock builds re-download, but an agent binary *you* uploaded and signed exists nowhere else. If you have ever uploaded a custom build, this directory belongs in your backup. |
| **`{config}/models`** | config volume | *nothing, deliberately* | Nothing permanent — the embedding model re-downloads (needs internet, and a first re-download delays semantic search). |
| **`{config}/share-map.json`** | config volume | *nothing* | On Proxmox it is regenerated by `proxmox/deploy-proxmox.sh`. **On Unraid it is operator-authored and regenerated by nothing** — copy it, or be prepared to rewrite it by hand. |
| **`caddy_data` / `caddy_config`** | volumes | *nothing needed* | Certificates re-issue on next start (a fresh internal CA means clients must re-trust it once). |
| **Agent-side SQLite** | on each agent host | the agent's own concern | No catalogue loss — central holds the rows — but the agent re-walks its whole tree to rebuild local state, which on a large share is hours of I/O. |
| **Media files** | your NAS | your existing NAS strategy | Filearr never writes to media (invariant 6); it is not a media backup and must not be treated as one. |
| **The application code** | this repository | your git remote / the project bundle | Nothing — reinstall the image. |

### Back up {#taking-a-backup}

```bash
cd /opt/filearr
bash scripts/backup.sh
```

That writes a **bundle**, not just a dump, into a `backups/` directory beside the
compose project (override with `BACKUP_DIR`; it warns when the destination shares
a filesystem with the Postgres volume):

```text
backups/filearr-20260812T030000Z/
├── filearr-20260812T030000Z.dump   # pg_dump -Fc
├── env.backup                      # your .env, mode 0600   ⚠ SECRETS
├── stepca/stepca-data.tar.gz       # CA private keys        ⚠ SECRETS
└── MANIFEST.json                   # versions, sizes, key fingerprints, restore notes
```

!!! danger "A bundle is a secret"
    `env.backup` is your `.env` verbatim (database password, Meili master key,
    `FILEARR_SECRET_KEY`, the CA provisioner JWK) and the step-ca archive holds
    private CA keys. Treat a bundle exactly like the `.env` itself: `0600`,
    off-box, encrypted at rest if it ever leaves your control. `SKIP_ENV=1` and
    `SKIP_CA=1` omit either half if you archive them another way.

`MANIFEST.json` records **fingerprints, never values** — `sha256(secret)[:16]`
hex — so you can check a backup against a target machine without handling the
secrets themselves. `BACKUP_KEEP` (default 7) sets retention; the write is
atomic (`.partial` → rename), so a crash never leaves a half-bundle that looks
complete.

**Copy the bundle off-box.** A backup on the disk it protects is not a backup.
Schedule it, e.g. from the Proxmox host crontab:

```bash
30 3 * * *  pct exec <your-vmid> -- bash /opt/filearr/scripts/backup.sh >> /var/log/filearr-backup.log 2>&1
```

On Unraid there is no `docker compose`; use the native commands and the User
Scripts schedule in [Unraid → Backup and restore](deployment/unraid.md#backup-and-restore).

#### Without a shell: the in-app backup {#in-app-backup}

The Jobs page has a **Back up now** button (admin) that writes a bundle to
`{config}/backups` and offers it for download. It exists so a backup needs no
SSH — and it is deliberately **honest about being partial**:

!!! warning "An in-app backup is not, on its own, a disaster-recovery backup"
    A container cannot read the host `.env` (so it cannot include
    `FILEARR_SECRET_KEY`) and cannot read the `stepca_data` volume (so it cannot
    include the CA). Its `MANIFEST.json` says so — `"complete": false` plus a
    `missing` list — and the Jobs page repeats it above the button. It records
    the *fingerprints* of both, which is enough to **check** a restore but not to
    perform one. Pair it with a copy of `.env` and the CA volume, or use
    `scripts/backup.sh`, which takes all three.

It has **no schedule by default** — an unattended dump filling the config volume
is worse than no dump. Set a cron on the same row to opt in. `pg_dump` must be at
least the server's major version; the task reads `SHOW server_version_num`,
compares, and **fails loudly** rather than writing a silently partial dump.

### Restore {#restoring}

Follow the steps in order. Step 1 is not optional.

```bash
cd /opt/filearr
BUNDLE=/path/to/backups/filearr-YYYYmmddTHHMMSSZ

# 1. VERIFY THE BACKUP FIRST. Restores the dump into a throwaway postgres:18.4,
#    counts items, and compares the key fingerprints against this deployment.
#    Do this before you tear anything down.
bash scripts/verify-backup.sh "$BUNDLE"

# 2. SECRETS BEFORE DATA. FILEARR_SECRET_KEY must be the ORIGINAL value or every
#    encrypted alert-channel secret in the dump is lost — with no error at any
#    point. Also restores POSTGRES_PASSWORD / MEILI_MASTER_KEY / the CA JWK.
cp "$BUNDLE/env.backup" .env && chmod 600 .env

# 3. CA BEFORE FIRST START (only if you run agents). An empty stepca volume makes
#    step-ca mint a NEW root and every issued agent certificate stops validating.
docker volume create filearr_stepca_data
docker run --rm -i -v filearr_stepca_data:/ca alpine:3.21 \
  tar -xz -C /ca < "$BUNDLE/stepca/stepca-data.tar.gz"

# 4. load the dump (--clean --if-exists makes it re-runnable)
docker compose up -d postgres
docker compose exec -T postgres pg_isready -U filearr
docker compose exec -T postgres \
  pg_restore -U filearr -d filearr --clean --if-exists --no-owner < "$BUNDLE"/*.dump

# 5. bring the schema to head (idempotent; the only thing that can stamp/upgrade
#    from an arbitrary prior state — always AFTER the load, never before)
docker compose run --rm app python scripts/init_db.py

# 6. start everything and rebuild the search index (Meili was never backed up)
docker compose up -d
curl -X POST http://<your-host>:8484/api/v1/system/rebuild-index
```

Thumbnails regenerate lazily on first view (or pre-warm with a scan).

**Then check the console.** On boot the app compares `FILEARR_SECRET_KEY` against
the fingerprint recorded inside the restored database. A match is silent; a
mismatch is an error in the log, a red row on the **About** page and a banner on
the Admin dashboard. Confirm it is silent before you consider the restore done.

**If you restored without the CA volume**, every agent needs re-enrollment: the
new root cannot validate the certificates the old one issued, and the agents'
existing certificates are unrecoverable. Budget for that up front rather than
discovering it when the fleet goes dark.

### The secret-key mismatch warning {#secret-key-mismatch}

**What you see:** `KEY FINGERPRINT MISMATCH (secret_key)` in the log, a red
"Secret key" row on the About page, and a banner on the Admin dashboard.

**What it means:** the encrypted alert-channel secrets in this database were
written under a different `FILEARR_SECRET_KEY` than the one this process is
using. They cannot be decrypted and their channels will fail to send. The app
does **not** refuse to boot — you may be knowingly migrating — but it will not
stop saying so.

**Fix it one of two ways:**

1. **Restore the original key.** Put it back in `.env` and restart. The warning
   clears on the next check (no restart of anything else needed).
2. **Accept the loss and re-key.** Re-enter every alert-channel secret through
   the console; each save re-encrypts under the current key. Then clear the
   stale fingerprint so the warning stops:

    ```sql
    DELETE FROM instance_meta WHERE key = 'secret_key_fingerprint';
    ```

    The next boot re-stamps against the current key. Do this **only** after
    re-entering the secrets — it silences the warning either way.

The same mechanism covers the CA: `ca_root_fingerprint` records the first 16 hex
of `FILEARR_CA_FINGERPRINT`, so a replaced step-ca root is reported instead of
being discovered when agents start failing authentication.

### Why Meilisearch needs no backup

It is a projection, fully rebuildable from Postgres in one `rebuild-index` call.
Backing it up would buy a shorter rebuild in exchange for a store that can drift
out of agreement with the source of truth — see architecture invariant 1.

**Source of truth for code:** the repository itself is the backup for the
application; the project's history is kept in a git bundle that you clone rather
than initializing a repo on a network share (SMB corrupts git lock/rename ops).

## Authentication and the first admin {#enabling-authentication}

Auth is **on by default** (`FILEARR_AUTH_ENABLED=true`). On a fresh install the
**first browser visit shows a one-time "create the administrator account"
screen** — that's the whole bootstrap for the normal path. The equivalent API
call (once only; 409 after any user exists — and the last admin can't be
deleted, so you can't lock yourself out):

```bash
curl -X POST http://<host>:8484/api/v1/auth/bootstrap \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"a-strong-passphrase"}'
```

Notes:

- **Serve over TLS before real use** (the session cookie is `Secure` only over
  HTTPS; behind Caddy, start uvicorn with `--proxy-headers`).
- `FILEARR_AUTH_ENABLED=false` opens every route — a development / trusted-LAN
  convenience, not a production posture. Flipping auth on later is additive
  and zero-downtime; Bearer API keys keep working alongside cookie sessions.

**Break-glass (SSO/LDAP lockout).** The first admin is always local. If an IdP is
down or a role map locks everyone out, set `FILEARR_OIDC_ENABLED=false` (and/or
`FILEARR_LDAP_ENABLED=false`), restart, and log in with the local admin — the
local password path is always available.

**Locked out by rate limiting (429).** Inspect/clear locks:

```sql
SELECT * FROM auth_rate_limits ORDER BY locked_until DESC NULLS LAST LIMIT 20;
```

The auth audit trail is in `security_events` (`GET /api/v1/audit`, admin scope).
