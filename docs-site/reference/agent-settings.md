# Agent settings reference

A Filearr agent is configured from **three separate surfaces**, and they are not
interchangeable. Most support questions about "I changed the setting and nothing
happened" are really questions about *which* surface owns the setting and *what
beats what*.

| Surface | Lives where | Edited by | Applies when |
| --- | --- | --- | --- |
| **Central policy** | [Configuration groups](../agents.md#two-groupings) in Postgres on central, delivered over the mTLS policy channel | An admin, in the console (Agents → a group's **edit** dialog) | Within one poll interval, no restart |
| **Local override** | `local-settings.json` in the agent's data dir | Whoever is at the machine, in the agent's own web UI — **only** where central granted a `local_*_control` permission | On the next scheduler tick, no restart |
| **Environment variables** | The host: service unit, container `environment:`, shell | Whoever owns the host | At process start (restart required) |
| **Sidecar config file** | `filearr-agent.json` next to the binary or in the OS config dir | Whoever owns the host | At process start (restart required) |

Central policy is how you configure a **fleet**. Environment and the sidecar are
how you configure **one machine** — the things central cannot know (where ffmpeg
lives on this box, which folder holds the data dir, what hostname clients use to
reach its shares).

---

## Precedence

### Between the local surfaces

Every setting that has both a flag and an environment variable resolves the same
way, and the agent's own `--help` states it:

```text
explicit CLI flag  >  FILEARR_AGENT_* env var  >  sidecar file  >  built-in default
```

The sidecar is deliberately the **lowest** local source: it exists so an operator
has one durable place to record settings that survive service restarts without
re-passing flags, not to override a deliberate per-invocation choice.

Two consequences worth knowing:

- **Empty string is not a value.** The env fallback only fires on a *non-empty*
  variable. `FILEARR_AGENT_NAME=""` behaves exactly like not setting it.
- **A bad value falls back rather than failing.** Durations, ints and booleans
  that do not parse (or that parse to zero/negative where a positive value is
  required) are ignored and the built-in default is used. `FILEARR_AGENT_LOG_LEVEL`
  is the only one that prints a complaint first (`unknown log level %q; using
  info`). Nothing here crash-loops the daemon over a typo — which also means a
  typo is silent, so check the startup log if a knob looks inert.

### Between central policy and the local surfaces

There is no single rule; it is per setting, and the split is deliberate.

**Policy wins over env** for the settings central is meant to own fleet-wide:

| Setting | Policy key | Local fallback | Rule |
| --- | --- | --- | --- |
| Scan cron | `scan_cron` | local override, then `FILEARR_AGENT_SCAN_CRON` | The merged `policy` key > the merged `settings` key `scan_schedule_cron` > local override > env |
| Scan interval | `scan_interval_seconds` | local override, then `FILEARR_AGENT_SCAN_EVERY` | Policy wins whenever the key is present |
| Scan on start | `scan_on_start` | local override, then `FILEARR_AGENT_SCAN_ON_BOOT` | Policy wins whenever the key is present |
| Reconcile cadence | `reconcile_interval_seconds` | `FILEARR_AGENT_RECONCILE_INTERVAL` | Env seeds the interval at daemon start; a policy value **live-overrides** it on the next poll, without a restart |
| Update poll cadence | `update_poll_interval_seconds` | `FILEARR_AGENT_UPDATE_POLL_INTERVAL` | Same: env seeds, policy live-overrides (and wakes the poll loop when tightened) |

The scan-schedule knobs are resolved **per knob**, on every scheduler tick. An
absent policy key falls through to the next surface rather than to "off", so a
policy that only sets `scan_cron` leaves a locally-configured
`FILEARR_AGENT_SCAN_ON_BOOT` intact.

#### Local overrides, and why they sit *under* central {#local-overrides}

When central grants `local_schedule_control`, the agent's own web UI can write
the three scan-schedule knobs into `local-settings.json` in its data dir. That
file slots into the chain between central policy and the environment:

```text
central policy  >  local override  >  FILEARR_AGENT_* env  >  sidecar  >  default
```

Two consequences follow, and both are enforced, not merely documented:

- **A key central explicitly set cannot be edited locally.** The agent shows the
  value read-only, labelled *managed by central* with the scope and version that
  supplied it, and refuses the edit with a `409`. Central re-applies its document
  on every poll, so accepting a local edit to a key central owns would mean
  silently reverting it a minute later. Local editing exists to fill in the keys
  central left *unset*.
- **Config-group settings count as central.** `group.scan_schedule_cron` outranks
  a local cron for the same reason: it is delivered on the policy channel.

Clearing a local override is a first-class action (the agent's **Clear local
overrides** button), and it drops the key back to env → sidecar → default rather
than writing a zero value. Roots are a separate case: they live in the agent's
`scan.json`, are edited under `local_roots_control`, and are locked when a
configuration group derives them from `scan_selections`.

The local **scan pause** is state, not precedence: it is a scan-only flag stored
in the same file, it never stops replication, and clearing it cannot lift a
central [suspend](../agents.md#agent-suspend-maintenance). Full behaviour:
[Local scan controls](../agents.md#local-scan-controls).

#### Share mappings: the one place the environment wins {#share-map-precedence}

Share mappings — the per-root network locations behind
[`FILEARR_AGENT_SHARE_MAP`](#scanning) — are the exception to the chain above,
and deliberately so:

```text
FILEARR_AGENT_SHARE_MAP  >  mapping saved locally (local-settings.json
                            share_mappings)  >  share discovered on the host
```

Why this way round, when the schedule knobs put the local override *above* env:

- Share hints have **no central policy key at all** (they are host-shaped
  settings — see below), so the top of the chain is the host's own configuration,
  which is the environment.
- `FILEARR_AGENT_SHARE_MAP` is what your **deployment manifest** declares (the
  compose file, the Unraid template). You must be able to read that file and know
  it describes what the agent reports; a value typed into a web page that
  silently outranked it would make the manifest a lie, with nothing in the
  manifest to show for it.
- Nothing is silently reverted by this order — the failure the central-versus-local
  rule guards against. A path the environment maps renders **read-only** in the
  agent's roots editor, labelled with the variable that supplied it, and the edit
  endpoint refuses it with a `409` instead of storing a value that would never be
  used. Locally-saved mappings fill in the paths the environment does **not**
  mention, exactly as local schedule overrides fill in the keys central left unset.

The roots editor shows, per root, the location that resolves today and which of
those three layers supplied it — or an explicit *no share mapping* when nothing
covers the root. Malformed entries from either surface are listed verbatim as
skipped: a bad pair is never fatal, so that listing is the only symptom a typo
ever produces. See
[Scan roots and share mappings](../agents.md#local-share-mappings).

**Local wins over policy** for host-shaped settings — there is no policy key at
all for the data directory, log destination, host tool paths, share hints,
thumbnail cadence, web-UI bind address or the CA bundle. Central has no business
knowing them and cannot set them.

**Policy is the only source** for the local-access gates (`local_access_enabled`,
`web_ui_enabled`, `auth_required`, `path_scope`, `offline_grace_seconds`), the
three local self-administration permissions (`local_scan_control`,
`local_schedule_control`, `local_roots_control`), the extraction pass, and the
upload rate cap. `FILEARR_AGENT_WEBUI_ADDR` and `FILEARR_AGENT_WEBUI_ALLOW_REMOTE`
widen the *listener* only; the policy gate still decides whether anything is
served on it.

### `state.json` — the surprising one

At enrollment the agent writes `state.json` into its data dir with the agent id,
the CA URL, the bound certificate fingerprint, and **a copy of the central URL it
enrolled against**. That copy is identity, not configuration, and it is not
something you edit.

It is also not what the daemon runs off when you have configured something else —
repointing an agent at `agents.<domain>` via the sidecar, env or flag and
restarting it does take effect:

!!! info "A configured central URL outranks the enrollment-time copy"
    On daemon start, the resolved `central_url` (flag > env > sidecar) is compared
    to the `state.json` copy. If it differs, that is read as operator intent: the
    new value is adopted, persisted back into `state.json` (best effort — an
    in-memory switch still applies if the write fails), and the switch is logged
    as `central URL switched by config`. If nothing is configured locally, the
    `state.json` copy stands.

Everything else in `state.json` — agent id, CA URL, root hash, fingerprint — is
still authoritative and has no configuration override.

### The other trap: a superseded data directory

A service install **adopts** a per-user data dir and leaves a marker in the old
one. Running `enroll`, `reissue`, `scan` or `run` against the superseded copy is
refused outright, because both copies hold a complete identity and operating on
the wrong one silently diverges credentials from the running service's. This is
a guard rail, not a precedence rule — but it is the reason a command may exit
with "data dir … was adopted into the service install at …". Re-run with the
`-data` path it names.

---

## Central configuration keys

Central configuration lives in **[configuration groups](../agents.md#two-groupings)**.
A group is a named row with an integer `priority` and two document sections —
`settings` (typed; unknown keys are a 422) and `policy` (permissive; unknown
keys are preserved for forward compatibility). Every agent is implicitly in the
permanent **Global** group and can be a member of any number of others.

Two properties account for nearly all confusion, and both are covered in depth
in [Distributed agents → Policy keys](../agents.md#policy-keys):

- **Resolution is a per-key layered merge.** The agent's groups are applied in
  ascending `(priority, name)` order and a later group overrides only the keys
  it actually sets — [how a key gets its value](../agents.md#policy-scopes). A
  group therefore states what is *different* about its members; it does not need
  to repeat the fleet-wide baseline. Merging is shallow per section: a nested
  object such as `inventory` is replaced wholesale, not deep-merged.
- **Absent is not `false`.** An absent key means "a lower-priority group or the
  built-in default supplies this", which for several keys (`watch_mode`, every
  `extract_*` key) happens to be off — but for others it means "keep the agent's
  local value", which is a different thing entirely.

The full per-key table — types, what absent means, and whether the **agent** or
**central** enforces each one — is in
[Every policy key](../agents.md#every-policy-key). The console's group dialog
renders the same text as a hint under each field and as a hover tooltip.

The `settings` section is the stricter half (unknown keys rejected with a 422
rather than stored): see
[Group settings schema](../agents.md#group-settings-schema). One exception to
the strictness: `inventory.collectors` is free-form. The five shipped collectors
(`stat`, `owner`, `perms`, `permissions`, `placeholder`) are a **catalogue the console renders
as a checkbox list**, not a whitelist — a newer agent build's collector still
works, and a stored name central cannot describe is preserved rather than
dropped.

Three keys exist in both sections — `web_ui_enabled`, `local_access_enabled`,
`auth_required`. When a group sets both, **the `settings` value wins**; a `null`
there means "inherit" and overrides nothing.

!!! tip "Which group supplied this value?"
    `GET /api/v1/agents/{agent_id}/effective-config` (admin scope) returns the
    merged document plus per-key provenance — the group name and version behind
    every single key — and the console renders it as source badges in the
    agent's detail row. Full shape:
    [Checking one agent's effective configuration](../agents.md#effective-config).

!!! note "Changes can be phased"
    A group edit can be published to the whole group at once or handed out in up
    to five percentage tiers with delays between them, and either can be rolled
    back by republishing an older version. Neither changes what an agent parses —
    it still polls one document. See
    [Phased rollouts](../agents.md#phased-rollouts).

---

## The sidecar config file

`filearr-agent.json` — a plain JSON object, no comments — is the durable
per-machine record of enrollment and logging settings. Its main job is making a
service install self-sufficient: the service unit runs
`filearr-agent run --data … --log-dir … --config …` and everything else comes
from this file.

**Discovery order**, first hit wins:

1. `--config <path>`, or `FILEARR_AGENT_CONFIG`. Used verbatim — a load failure
   here is reported, because you asked for that exact file.
2. `filearr-agent.json` **beside the executable**.
3. The OS config directory:

    | OS | Path |
    | --- | --- |
    | Windows | `%ProgramData%\Filearr Agent\filearr-agent.json` |
    | macOS | `/Library/Application Support/FilearrAgent/filearr-agent.json` |
    | Linux / other Unix | `/etc/filearr-agent/filearr-agent.json` |

No sidecar anywhere is **not** an error — the agent falls through to env and
defaults.

**Accepted keys** (all optional; a zero file is the valid "nothing configured"
state):

| Key | What it sets | Notes |
| --- | --- | --- |
| `central_url` | Central base URL | Lowest-precedence source for it; see [`state.json`](#statejson-the-surprising-one) above |
| `enrollment_token` | Single-use enrollment token | **Erased after a successful enroll** and replaced by `enrollment_token_consumed_at`, so the secret is never left at rest and a re-run cannot attempt a (rejected) replay |
| `enrollment_token_consumed_at` | RFC3339 stamp | Written by the agent, not by you |
| `agent_name` | Friendly name in the console | Defaults to the device hostname |
| `config_group_names` | List of [configuration groups](../agents.md#two-groupings) to join. Global is implicit and never listed | Read at enrollment. The minted token also records the list, so central applies it whatever the agent sends |
| `config_group` | The first group name, repeated | Read at enrollment by shipped agent builds, which parse this key and not the list. The console's generated sidecar writes both |
| `data_dir` | Where key/cert/state/index live | |
| `log_level` | `error`\|`warn`\|`info`\|`verbose`\|`debug` | |
| `log_dir` | Directory for the rotating log | |
| `ffmpeg_path` | Explicit ffmpeg binary | Load-bearing on **Windows service** installs: the service runs with the SYSTEM environment and never sees a user `PATH`, where most ffmpeg installers land. Precedence: `FILEARR_AGENT_FFMPEG_PATH` > this > `PATH` lookup |

Parsing is strict JSON but **tolerant of unknown keys**: a newer agent build can
add fields without an older on-disk file breaking, a typo'd key does not discard
the rest of the file, and the raw key set is preserved byte-for-value when the
agent rewrites the file to consume a token. Rewrites are atomic and land at mode
`0600`.

The console can generate a ready-to-drop sidecar for you: **Agents → Enrollment &
installer → Generate installer sidecar**.

---

## Environment variables

Every variable below is read by the **agent** process. All are optional unless
noted. "Also settable by" names the higher-precedence flag (if any) and the
sidecar key (if any) for the same setting.

!!! warning "`FILEARR_AGENT_*` on the central server is a different namespace"
    Central reads its own variables that share the prefix —
    `FILEARR_AGENTS_ENABLED`, `FILEARR_AGENT_AUTH_MODE`,
    `FILEARR_AGENT_EXTRACTED_MAX_BYTES`, `FILEARR_AGENT_DIST_DIR`,
    `FILEARR_AGENT_RELEASES_DIR`, `FILEARR_AGENT_ONLINE_THRESHOLD_SECONDS`,
    `FILEARR_AGENT_OFFLINE_ALERT_SECONDS`,
    `FILEARR_AGENT_REPLICATION_STALL_ALERT_SECONDS`,
    `FILEARR_AGENT_COMMAND_MAX_ATTEMPTS`,
    `FILEARR_AGENT_RECONCILE_PAGE_MAX`,
    `FILEARR_AGENT_RECONCILE_SESSION_TTL_SECONDS`. Those go in central's `.env`
    and are documented in [Configuration](configuration.md). Setting them on an
    agent host does nothing. (`FILEARR_AGENT_COMMAND_POLL_MAX` and
    `FILEARR_AGENT_SELF_UPDATE` exist on *both* sides, with related but separate
    meanings — see the notes below.)

### Identity, enrollment and transport

| Variable | What it controls | Default | Also settable by |
| --- | --- | --- | --- |
| `FILEARR_AGENT_CENTRAL_URL` | Central base URL the agent talks to, e.g. `https://filearr.example.com`. Required for `enroll` and `run`. | unset — `enroll`/`run` refuse to start | `-central` flag; sidecar `central_url` |
| `FILEARR_AGENT_TOKEN` | Single-use enrollment token, minted in the console. Used by `enroll` only. | unset — `enroll` refuses to start | `-token` flag; sidecar `enrollment_token` |
| `FILEARR_AGENT_NAME` | Friendly name shown in the console. | this device's hostname | `-name` flag; sidecar `agent_name` |
| `FILEARR_AGENT_DATA_DIR` | Directory holding the identity key/cert, `state.json`, the SQLite index and outbox, `scan.json`, and caches. | the OS per-user config dir + `filearr-agent`: `%AppData%\filearr-agent`, `~/.config/filearr-agent`, `~/Library/Application Support/filearr-agent`. A **service** install uses the system layout instead; the **container** image sets `/config`. | `-data` flag; sidecar `data_dir` |
| `FILEARR_AGENT_CONFIG` | Path to the sidecar file, bypassing discovery. | unset — discovery runs (beside the exe, then the OS config dir) | `-config` flag |
| `FILEARR_AGENT_CA_OTT` | Recovery one-time token for `filearr-agent reissue`, which replaces an **expired** leaf certificate without re-enrolling (same agent id, same replication watermark). Mint it in the console (Agents → re-issue). | unset — `reissue` refuses to start | `-ott` flag |
| `FILEARR_AGENT_CA_BUNDLE` | Path to a PEM file of **extra** trusted server roots, appended to the host's system pool. Needed when central's TLS chains to a root the host does not already trust — a private/internal issuer, an LE staging root, a test CA. | unset — system roots only | — |
| `FILEARR_AGENT_AUTH_FINGERPRINT` | Fallback bearer token (the enrollment-time certificate fingerprint) used when the on-disk cert store cannot be loaded. The live leaf's fingerprint is preferred whenever it loads. Pin this on a host whose cert has rotated while central still holds the enrollment-time fingerprint. Read per request, so a change applies without a restart. | unset | — |

### Scanning

| Variable | What it controls | Default | Also settable by |
| --- | --- | --- | --- |
| `FILEARR_AGENT_SCAN_CRON` | 5-field cron, in the **agent's local time**, for the in-daemon scan scheduler. Wins over `_SCAN_EVERY`. | unset | Central policy `scan_cron`, then group `scan_schedule_cron`, both of which win over this |
| `FILEARR_AGENT_SCAN_EVERY` | Go duration (`6h`, `30m`) between in-daemon scans. | unset | Central policy `scan_interval_seconds` wins |
| `FILEARR_AGENT_SCAN_ON_BOOT` | Boolean: fire one scan roughly 30 s after the daemon starts. | unset (= off) | Central policy `scan_on_start` wins |
| `FILEARR_AGENT_HASH_TIMEOUT_SECONDS` | Wall-clock ceiling on hashing **one** file. A corrupt or locked file on a FUSE/network mount can block `read(2)` forever and freeze the whole walk; past the bound the agent skips that file's hashes and logs a warning with its path. `0` removes the bound. | `300` | — |
| `FILEARR_AGENT_SHARE_HOST` | Hostname rendered into share hints (`\\host\share`, `smb://host/…`) so central can offer network-open links. Set it when clients reach this machine by a different name — a DNS alias, a NAS identity. | this machine's `hostname` | — |
| `FILEARR_AGENT_SHARE_MAP` | Comma-separated `localpath=location` pairs statically mapping scan roots to the network locations they are shared at, where location is `smb://host/share[/sub]`, `\\host\share[\sub]` or `nfs://host/export`. **Required in containers**: inside one, share discovery sees nothing (no `smb.conf`, and the NAS exports paths under *its* name, not the container's). Longest local prefix wins per file; entries override a discovered export of the same path, and a malformed pair is skipped with a warning rather than failing the scan — the agent's roots editor lists the skipped entries verbatim, since that is the only symptom a typo produces. A worked Unraid example: `/mnt/user/media=smb://tower/media,/mnt/user/documents=\\tower\documents`. | unset | The agent's own roots editor, under `local_roots_control`, for paths this variable does **not** map ([precedence](#share-map-precedence)) |

!!! note "All three scheduler knobs absent = the scheduler is off"
    That is the correct state for a container, which runs its own scan loop from
    the entrypoint (below). Do not arm both — the container would double-scan.
    The scheduler variable names are deliberately *different* from the container
    ones so an existing container environment can never arm it by accident.

### Container-only scanning (entrypoint, not the binary)

These three are read by the container image's `entrypoint.sh`, never by the Go
binary. They have no effect on a service or bare-binary install.

| Variable | What it controls | Default |
| --- | --- | --- |
| `FILEARR_AGENT_SCAN_ROOTS` | Comma-separated directories to inventory. Roots **merge into** `scan.json`, so removing one from the environment does not remove it from the agent's configuration — edit `/config/scan.json` for that. Empty, with no existing `scan.json`, logs a warning and starts nothing. | unset |
| `FILEARR_AGENT_SCAN_INTERVAL` | Sleep between full rescans (Go duration). Rescans are mtime+size cheap — an unchanged file costs a `stat`. | `6h` |
| `FILEARR_AGENT_SCAN_ON_START` | `true` scans immediately at container start; anything else waits one interval first. | `true` |

### Replication, polling and reconciliation

| Variable | What it controls | Default |
| --- | --- | --- |
| `FILEARR_AGENT_RECONCILE_INTERVAL` | Go duration for the slow periodic full-manifest sweep — the safety net behind incremental replication. The same value is the reconnect-outage threshold and the offline-grace default. | `24h`. A policy `reconcile_interval_seconds` live-overrides it on the next poll. |
| `FILEARR_AGENT_COMMAND_POLL_INTERVAL` | Go duration between polls of central's per-agent command queue (the channel behind console-triggered stat checks, re-hashes, maintenance, suspend, re-extract, "update now"). Also carries the version and health snapshot the fleet console renders. | `60s` |
| `FILEARR_AGENT_COMMAND_POLL_MAX` | How many queued commands one poll drains. Central caps its own reply independently. | `10` |
| `FILEARR_AGENT_COMMAND_LEASE_SECONDS` | How long a picked-up command stays leased to this agent; the heartbeat runs at one third of it. | `300` |

The policy poll (settings, as opposed to commands) is a separate loop: its
cadence is `poll_interval_seconds` in policy, defaulting to `300s` and floored at
`60s`. It has no environment variable — a fleet's poll cadence is a central
decision.

### Thumbnails

The `run` daemon generates grid and preview thumbnails for locally-hosted items
and pushes them to central. There is no policy key for this pass.

| Variable | What it controls | Default |
| --- | --- | --- |
| `FILEARR_AGENT_THUMBS_ENABLED` | `false`, `0`, `no` or `off` disables the pass entirely. Any other non-empty value enables it. | on |
| `FILEARR_AGENT_THUMBS_INTERVAL` | Go duration between passes. | `5m` |
| `FILEARR_AGENT_THUMBS_RATE` | Items per second throttle — this is a deliberately low-priority background walk. | `5` |
| `FILEARR_AGENT_THUMB_MAX_EDGE` | Longest edge in pixels for the **preview** tier, mirroring central's. Drifting from central changes only the image's dimensions, never its cache key. The grid tier (320 px) is not configurable. | `800` |

Video poster frames additionally need `ffmpeg` — see below. Without it, video
thumbnails are skipped as an unavailable capability, never as an error.

### Host tools for extraction and thumbnails

Each variable **names the binary to use** — an absolute path, or a bare name
resolved on `PATH`. Unset means the conventional name is looked up on `PATH`.
An absent tool is never fatal: it is simply a capability the agent does not
advertise, and any policy setting that needed it is logged as ignored.

This is the whole "capability = host tool" story: you upgrade an agent's
capabilities by installing a binary, never by swapping the agent build. The
console's [per-agent capability matrix](../agents.md#agent-capabilities) shows
which tools each host actually resolved, and which versions.

| Variable | Tool | Buys you |
| --- | --- | --- |
| `FILEARR_AGENT_FFMPEG_PATH` | `ffmpeg` | Video poster-frame thumbnails. Also settable via sidecar `ffmpeg_path` (env wins) — the sidecar route exists for Windows services, which never see a user `PATH`. |
| `FILEARR_AGENT_FFPROBE_PATH` | `ffprobe` | The media technical probe: duration, codecs, resolution, bitrate. |
| `FILEARR_AGENT_EXIFTOOL_PATH` | `exiftool` | Deep EXIF for images — camera, lens, ISO, exposure, focal length, GPS. Gated by policy `extract_exif`. |
| `FILEARR_AGENT_TESSERACT_PATH` | `tesseract` | OCR of images, and (with `pdftoppm`) of scanned PDFs. Gated by policy `extract_ocr`. |
| `FILEARR_AGENT_PDFINFO_PATH` | `pdfinfo` | PDF properties — page count, page size, producer. |
| `FILEARR_AGENT_PDFTOTEXT_PATH` | `pdftotext` | PDF body text, which is what makes PDFs chunkable and embeddable rather than filename-only. Gated by policy `extract_body_text`. |
| `FILEARR_AGENT_PDFTOPPM_PATH` | `pdftoppm` | Rasterises PDF pages so scanned PDFs can be OCR'd. |

The three poppler binaries are detected **separately** rather than as one
"poppler" capability, because a host can ship a partial install — Windows zip
drops and minimal distro packages routinely do — and the honest answer to "will
PDF text work here" is per binary. Install guidance per OS is in
[Host tools: what each one buys](../agents.md#agent-host-tools).

### Logging

| Variable | What it controls | Default | Also settable by |
| --- | --- | --- | --- |
| `FILEARR_AGENT_LOG_LEVEL` | `error`, `warn`, `info`, `verbose` or `debug`. An unrecognised name prints a complaint and falls back to `info`. Since agent 1.5.3 a config-group `log_level` (delivered under the policy `group` section) **live-overrides** this on the next policy poll while set. | `info` | `-log-level` flag; sidecar `log_level`; group settings (wins while set) |
| `FILEARR_AGENT_LOG_DIR` | Directory for a rotating `filearr-agent.log` (10 MiB × 5, gzipped). Empty means stderr only, no file. **Exception:** `run` under an OS service manager with nothing configured defaults to `<data-dir>/logs`, because a service has no stderr and would otherwise log into the void. Each command gets its own file (`filearr-agent-scan.log` and so on) — rotation is not multi-process safe on a shared path, and a container runs the daemon and scans as concurrent processes. | unset (stderr only) | `-log-dir` flag; sidecar `log_dir` |
| `FILEARR_AGENT_LOG_STDERR` | Boolean. Forces the stderr echo alongside an active file sink even when stderr is not a terminal. The container image sets it, because its stderr *is* the `docker logs` stream and must keep carrying every line once a shared log dir is enabled. | off (stderr echo only on a tty) | — |

### Service, container and self-update

| Variable | What it controls | Default |
| --- | --- | --- |
| `FILEARR_AGENT_SELF_UPDATE` | Boolean. `false`/`0` switches the **whole** self-update subsystem off: no boot check, no poll loop, one quiet informational line instead. An unparseable value keeps updates on — a typo must never silently disable them. | on. The **container image sets it to `false`**: an image is immutable by design (update = pull a new image), and an unpinned build's fail-closed refusal is correct there but its every-boot warning reads like a fault. |
| `FILEARR_AGENT_UPDATE_POLL_INTERVAL` | Go duration between update-manifest polls. Seeds the cadence at start; the `update_poll_interval_seconds` policy key live-overrides it. | `6h` |
| `FILEARR_AGENT_CONTAINER` | Marks this process as containerized. Advertised to central as the `container` capability, so the console flags a newer build ("pull the new image") instead of offering an in-place binary swap. `0`/`false` opts back out. When unset, the presence of `/.dockerenv` is used instead, which catches hand-rolled containers. | The shipped image sets `1`; otherwise auto-detected |
| `FILEARR_AGENT_SERVICE` | **Set by the installer, not by you.** The service unit's environment carries `FILEARR_AGENT_SERVICE=1` so the running daemon knows it is service-managed and takes the clean-exit-for-restart path after a self-update swap, instead of self-re-exec (which a service manager would race, possibly ending up with two instances). | set to `1` in the service environment |

### Local query UI

The agent's only listener is the local web UI — read-only over the catalog, plus
the policy-gated [scan controls](../agents.md#local-scan-controls). Both variables
widen the *listener*; whether anything is served on it is decided by policy
(`web_ui_enabled`, `auth_required`, `local_access_enabled`, and cached-policy
freshness — past the offline grace window the web UI fails closed while the CLI
keeps answering). Neither variable can enable a scan control: those need
`local_scan_control` / `local_schedule_control` / `local_roots_control` in the
policy, and the bootstrap token on every action regardless of `auth_required`.

| Variable | What it controls | Default |
| --- | --- | --- |
| `FILEARR_AGENT_WEBUI_ADDR` | `host:port` bind for the local web UI. The `-web-addr` flag wins over it. | `127.0.0.1:8686`, or `0.0.0.0:8686` when `_WEBUI_ALLOW_REMOTE` is set and no address is given |
| `FILEARR_AGENT_WEBUI_ALLOW_REMOTE` | Boolean opting the web UI into a **non-loopback** bind. Needed for containers and NAS boxes, where a Docker port mapping cannot reach a loopback listener. Non-loopback binds are refused without it, and enabling it emits a startup warning naming the exposure. | off |

---

## Service install vs container install

The same binary, but the surfaces differ.

**Service install** (`filearr-agent install`, Windows/Linux/macOS). The installer
copies the binary into the system layout, optionally enrolls, and registers an
auto-start, restart-on-failure service that runs
`filearr-agent run --data <dir> --log-dir <dir> [--config <path>]`:

| OS | Binary | Data + config | Logs |
| --- | --- | --- | --- |
| Windows | `%ProgramFiles%\Filearr Agent\filearr-agent.exe` | `%ProgramData%\Filearr Agent` | `%ProgramData%\Filearr Agent\logs` |
| macOS | `/usr/local/bin/filearr-agent` | `/Library/Application Support/FilearrAgent` | `/Library/Logs/FilearrAgent` |
| Linux | `/usr/local/bin/filearr-agent` | `/var/lib/filearr-agent`, config `/etc/filearr-agent` | `/var/log/filearr-agent` |

Because the service unit passes `--data`, `--log-dir` and `--config` as *flags* —
the highest-precedence surface — setting `FILEARR_AGENT_DATA_DIR` or
`FILEARR_AGENT_LOG_DIR` in the machine's environment will **not** move a
service-installed agent. Edit the sidecar the service points at, or re-run
`install`.

Settings that only matter for a service install:

- **`ffmpeg_path` in the sidecar.** A Windows service runs with the SYSTEM
  environment and never sees a user `PATH`, so "ffmpeg is on the PATH" is
  routinely untrue inside the service process. Set it explicitly there (or as a
  **system**, not user, environment variable).
- **`FILEARR_AGENT_LOG_DIR`'s automatic default.** Only `run` under a service
  manager gets `<data-dir>/logs` for free.
- **The in-daemon scan scheduler** (`_SCAN_CRON` / `_SCAN_EVERY` / `_SCAN_ON_BOOT`
  and their policy keys). It exists precisely so a lone `run` service is
  self-sufficient and does not depend on an external cron entry or Windows
  scheduled task — one that vanished across a re-install once froze a fleet
  member's catalog for nine days while it kept heartbeating.

**Container install.** The image sets several variables itself:

| Variable | Image value | Why |
| --- | --- | --- |
| `FILEARR_AGENT_DATA_DIR` | `/config` | Everything the agent persists under one bind-mountable dir |
| `FILEARR_AGENT_SELF_UPDATE` | `false` | Images are immutable — update by pulling a new one |
| `FILEARR_AGENT_CONTAINER` | `1` | Central then flags new builds instead of offering a binary swap |
| `FILEARR_AGENT_LOG_DIR` | `/config/logs` | So the web UI's Logs tab can merge lines from the daemon, each scan process, and the entrypoint |
| `FILEARR_AGENT_LOG_STDERR` | `true` | Keeps `docker logs` carrying every line despite the file sink |

Container-specific notes:

- The image ships **every** extraction tool (ffmpeg, poppler-utils, exiftool,
  tesseract + English data), so a containerized host never has a capability gap.
  Extra OCR languages need a derived image.
- Scanning is driven by the entrypoint loop (`_SCAN_ROOTS` / `_SCAN_INTERVAL` /
  `_SCAN_ON_START`), **not** by the in-daemon scheduler. Do not arm both.
- Share hints need `FILEARR_AGENT_SHARE_MAP`; discovery finds nothing inside a
  container. The agent's web UI shows the resolved location **next to each scan
  root** (read-only here — the mapping comes from the container's environment),
  so you can confirm the variable took effect without shelling in.
- Exposing the web UI needs `FILEARR_AGENT_WEBUI_ALLOW_REMOTE=true` on top of the
  policy gate.
- `PUID` / `PGID` (default `99`/`100`, the Unraid convention) select the uid/gid
  the agent processes drop to after `/config` ownership is fixed.

---

## See also

- [Distributed agents](../agents.md) — the operating model, enrollment
  walkthrough, and per-key policy table
- [Configuration](configuration.md) — central's own environment variables
- [Security](../security.md) — the agent-plane auth modes and the mTLS migration
