# Proxmox LXC

`proxmox/deploy-proxmox.sh` deploys the whole Filearr stack into a
**Docker-enabled LXC** on Proxmox VE, mounting your network storage **inside**
the container so the deployment does not depend on host-side mounts. It is a
wizard on first run and an idempotent redeploy on later runs.

Run it on the **Proxmox host shell** (as root), from inside the project folder:

```bash
git clone https://github.com/filearr/filearr.git && cd filearr   # once
bash proxmox/deploy-proxmox.sh                # first run -> wizard, then deploy
bash proxmox/deploy-proxmox.sh               # later runs -> redeploy with saved defaults
bash proxmox/deploy-proxmox.sh --reconfigure # re-run the wizard
bash proxmox/deploy-proxmox.sh --storages    # re-run only the storage definitions
bash proxmox/deploy-proxmox.sh --status      # CT + mounts + stack status
bash proxmox/deploy-proxmox.sh --destroy     # stop & delete the container
```

## What the wizard asks

The wizard saves your answers so redeploys never re-ask. It prompts once for:

- **Container basics** — starting VMID (first free `>=` your number is used),
  hostname, network bridge, DHCP or static IP, rootfs storage, disk size, CPU
  cores, memory, web UI port (default 8484), HTTPS port (default 8443).
- **Public base URL** — the absolute prefix for export/report download links
  (e.g. `https://filearr.example.com:8443`); blank means site-relative links.
- **TLS mode** — `internal` (self-signed LAN CA in the container; no public DNS)
  or `acme-dns` (Let's Encrypt **wildcard** `*.<domain>` via Cloudflare DNS-01;
  the container terminates public TLS itself and also fronts the agent mTLS plane
  and the step-ca SNI passthrough).
- **Distributed agents** — enable the step-ca certificate authority and the
  enrollment endpoints. Safe to enable now and enroll machines later.
- **Thumbnail volume** — an optional dedicated volume for the thumbnail cache
  (`THUMBS_STORAGE` + size in GB): pick a storage to keep a large cache off
  the CT rootfs, or leave blank to share the rootfs. The cache is disposable
  either way (bounded by the thumbnail GC).
- **Optional app settings** — the optional feature knobs (semantic search,
  content sniffing, update check, log recorder, thumbnail budget, and — only
  when agents are enabled — the agent auth mode). See below.
- **Storage definitions** — one or more network shares (see below).

### The "Optional app settings" step {#optional-app-settings}

Optional features ship **off**. The wizard asks about them and writes the answers
into `deploy.conf`, where you can see and edit them on the host:

```text
── Optional app settings ──
Configure optional app settings (semantic search, thumbnail budget, ...)? [y/N]
```

- **Answer `N` (the default)** and the deploy still **records the current
  effective values** as `FILEARR_*=VALUE` lines in `deploy.conf` — the knobs
  become explicit without any interrogation. Values you already answered are
  never rewritten.
- **Answer `y`** and it walks the knobs one at a time, each with a one-line
  explanation. The default offered for each is the **current effective value**:
  an existing line in `deploy.conf`, else a line in `env.overrides`, else the
  shipped default. Press Enter to keep it. Booleans accept only `true`/`false`.

| Prompt | Default | What it does |
|---|---|---|
| `FILEARR_SEMANTIC_ENABLED` | `false` | Semantic/hybrid search. The worker loads a local embedding model (~500 MB RSS) and backfilling 1M+ items takes hours. |
| `HF_TOKEN` | *(blank)* | **Only asked when semantic search is `true`.** Optional Hugging Face token for the one-off model download (higher rate limit). A secret: written to the CT `.env` only, never `deploy.conf`, never echoed. Blank keeps what the CT has (or downloads anonymously); `none` removes it. |
| `FILEARR_CONTENT_SNIFF_ENABLED` | `false` | libmagic reclassification of extensionless files. |
| `FILEARR_UPDATE_CHECK_AUTO` | `false` | Auto-refresh the GitHub update check (outbound network call). |
| `FILEARR_LOG_DB_ENABLED` | `true` | The database log recorder behind the console Logs panel. |
| `FILEARR_THUMBNAIL_BUDGET_GB` | `5` | Advisory thumbnail-cache budget; `0` disables the advisory. |
| `FILEARR_AGENT_AUTH_MODE` | `fingerprint` | **Only asked when distributed agents are enabled.** `fingerprint` = interim bearer; `both` = accept both during migration; `mtls-header` = mTLS only — flip after every agent shows the mTLS badge. |

Every answer (including an accepted default) is written to `deploy.conf`, so
after any wizard or `--reconfigure` run the knobs are literally **set** there —
and applied to the container's `.env` on that same deploy. An ordinary
redeploy with saved answers never re-asks; `--reconfigure` always re-offers the
section. If `env.overrides` pins a different value for a key, the deploy says
so — that file still wins (see the precedence rules below).

### The prompt-once model, and where secrets go

Answers persist to `~/.config/filearr/deploy.conf`; storage definitions
(including credentials) persist to `~/.config/filearr/storages.env` (mode 0600).
Both are re-applied on every redeploy, so **the container is fully disposable** —
destroy and rebuild it and your configuration returns.

The three host-side files, and what each is for:

| File (`~/.config/filearr/`) | Holds | Applied |
|---|---|---|
| `deploy.conf` | Wizard answers (VMID, bridge, ports, TLS mode …) **plus, optionally, any `FILEARR_*=VALUE` app-setting lines** — so one file can describe the whole deployment. | Every deploy. `FILEARR_*` lines are upserted into the container's `.env`. |
| `env.overrides` | `FILEARR_*=VALUE` app settings only (the dedicated file). | Every deploy, **after** the `deploy.conf` lines — so it **wins** on a duplicate key. |
| `storages.env` | Storage definitions incl. share credentials (mode 0600). | Every deploy (mount units rebuilt). |

!!! danger "Secrets never go in `deploy.conf`"
    `deploy.conf` holds only non-secret settings. The Cloudflare API token, the
    auto-generated proxy shared secret, the auto-generated `FILEARR_SECRET_KEY`,
    and the extracted CA provisioner JWK are **secrets** and live in the
    container's `.env` **only** — never in `deploy.conf`, never echoed to the
    terminal. On a redeploy, a blank token answer means "keep the container's
    existing one".

    The deploy enforces this: `FILEARR_SECRET_KEY`,
    `FILEARR_PROXY_SHARED_SECRET`, `FILEARR_CA_PROVISIONER_JWK`,
    `FILEARR_MEILI_MASTER_KEY`, `FILEARR_DATABASE_URL` and
    `FILEARR_PROCRASTINATE_DSN` found in `deploy.conf` are **skipped with a ⚠
    warning**, not applied. Those six are container-managed.

## Storage: rclone/NFS mounts inside the container

Each storage mounts **read-only** at `/data/media/<name>` inside the container,
and Docker Compose binds `/data/media` into the app and worker. Mounts are
installed as systemd units ordered `Before=docker.service`, so containers always
see them.

| Type | How it is mounted | Container privilege |
|---|---|---|
| SMB/CIFS, FTP, SFTP, WebDAV | rclone FUSE mount (userspace) | **Unprivileged** CT with the `fuse=1` feature |
| NFS | kernel mount | **Privileged** CT (the script switches automatically and warns) |
| local | host path bind-mounted via `pct` | the only type that touches the host |

!!! warning "NFS forces a privileged container"
    Kernel NFS mounts require a **privileged** LXC. If you define an NFS storage,
    the script switches the container to privileged and warns you. SMB/FTP/SFTP/
    WebDAV all work in an unprivileged container via rclone FUSE — prefer those
    where you can. FUSE mounts also need `fuse=1` on the container (the script
    sets it).

For SMB, credentials are collected once per host and reused across every share on
that host. AD domain goes in the separate domain field; use a **bare** username
(no domain prefix).

### Adding, removing, and redefining storages

`bash proxmox/deploy-proxmox.sh --storages` re-opens the storage wizard against
the saved definitions: **add** more, **remove** some by name, **redefine from
scratch**, or keep as-is. Applying the config reconciles the CT — mount units,
NFS fstab entries, and mountpoints for storages that are no longer defined are
stopped and removed (a `local` bind is a `pct`-level mapping and fully
disappears only when the CT is recreated).

Before a removal or a from-scratch redefine, the wizard **checks the running
stack for libraries rooted on the affected storage** and asks for explicit
confirmation if any exist — their files become unreachable, so the next scan
tombstones every item (recoverable from the recycle bin until retention purges;
nothing is hard-deleted). Delete or repoint such libraries in Admin first if
that isn't what you want. When the stack isn't reachable the wizard says it
could not check instead of pretending nothing is affected.

### The credential-free share map

Because the deploy alone knows the real share URL behind each mount, it writes a
**credential-free** map to `/config/share-map.json` in the container
(regenerated on every deploy). Filearr reads it read-only and auto-populates each
library's user-facing **share location** from the mount that covers its root — so
the "open in Explorer / Finder" hint survives remounts and redeploys with no hand
maintenance. A manual share location always wins. A missing or malformed file
simply disables the feature; the app never fails to start.

## TLS and reverse-proxy topology

The container runs a Caddy TLS front. Two modes:

- **`internal`** — Caddy mints a self-signed LAN CA and serves HTTPS on your
  chosen port (default 8443). No DNS, no ACME, no egress needed. Trust the root
  CA on your clients once to remove the browser warning
  (see [Operations → TLS](../operations.md#tls-and-acme-issuance-failures)).
- **`acme-dns`** — a Let's Encrypt **wildcard** `*.<domain>` via Cloudflare
  DNS-01. The container terminates public TLS itself (no external nginx), and its
  Caddy also carries a layer-4 listener that raw-TCP-proxies `ca.<domain>`
  straight to step-ca (SNI passthrough — the CA must **never** be L7-terminated,
  or agent cert renewal silently breaks).

An **acme-dns example pattern** (all three hostnames share port 443 via SNI):

```text
filearr.example.com   A/AAAA -> <container-ip>   # web UI / API
agents.example.com    A/AAAA -> <container-ip>   # agent mTLS plane
ca.example.com        A/AAAA -> <container-ip>   # step-ca SNI passthrough
```

The Cloudflare API token needs **both** `Zone:Read` and `DNS:Edit` on the zone.
DNS-01 needs no inbound port 80, so issuance works behind NAT. On a split-horizon
LAN (a local resolver answering for your domain), add an override for each
hostname to the container IP — a missing `ca.` override breaks agent renewal from
inside the LAN.

## Optional: iGPU passthrough for video thumbnails

iGPU passthrough is deliberately **not** wired automatically (it is host-specific
and the thumbnail pipeline degrades cleanly to software). To enable QSV video
poster frames after the first deploy, add the DRI cgroup allow and `/dev/dri`
mount entry to the container config and reboot it — the worker compose service
already carries the device mapping, added conditionally only when `/dev/dri`
exists. See the comments in `deploy-proxmox.sh`.

## Redeploy behavior

A redeploy is safe and self-quiescing. It:

1. Gracefully **stops running scans** (progress kept, no tombstoning) and
   remembers their libraries.
2. Updates the container OS packages and Docker engine.
3. Re-applies the storage mounts and regenerates the share map.
4. Pushes the current source (with a dirty-tree guard — it refuses to silently
   deploy uncommitted work) and does a clean extract, preserving `.env`,
   `config/`, and the compose override.
5. Builds and starts the stack, runs the idempotent DB bootstrap, and **verifies
   the running image was built from the source just pushed** (a build-stamp
   check) plus a functional smoke test.
6. **Re-triggers** the scans it stopped in step 1.

See [Upgrades & migrations](upgrades.md) for what happens to the schema on
redeploy.

### Deploy fails with `STAMP MISMATCH`

Every deploy fingerprints the source it pushes (a content hash + push
timestamp), bakes that stamp into the image, and — after the stack starts —
reads the stamp back out of the *running* app container. `STAMP MISMATCH`
means the running container was built from an **earlier** push: the source
just pushed is on the CT's disk, but the image build for it didn't take, so
Docker kept running the previous build.

What state you're in:

- **Nothing partial** — the stack still runs the previous build in its
  entirety, and no new database migrations were applied.
- Scans the deploy quiesced were **not** auto-resumed (the script aborts
  before that step) — restart them from the Libraries page, or just rerun the
  deploy.

The two usual causes are a **full CT disk** (`pct exec <vmid> -- df -h /`)
and **Docker Hub being unreachable** (the build pulls base images); the
script aborts at the build step with the real error, so scroll up to it.
After fixing the cause, retry with `FORCE_REBUILD=1 ./deploy-proxmox.sh
<same args>` — that forces a cache-less rebuild, which also cures the rarer
corrupted-build-cache case.

## After it's up

The wizard's summary prints your URLs (and, with agents on, the one-command
agent installer line). Then follow the shared
[first-run guide](index.md#first-run): the one-time create-admin screen on
first visit, your first library, a scan, and a search. Back up **Postgres**
from the host on a schedule — `scripts/backup.sh` is `pct exec`-friendly and
wraps a `pg_dump -Fc` with retention; everything else in the CT is
disposable/rebuilt by redeploy.

## Changing a configuration setting (`.env`) {#changing-configuration}

Every `FILEARR_*` setting (see the [configuration
reference](../reference/configuration.md)) ends up in **`/opt/filearr/.env`
inside the container**, which the deploy preserves across redeploys. There are
two ways to manage settings — prefer the first:

**Host-side (recommended): `deploy.conf` or the overrides file.** Put
`KEY=VALUE` lines either straight into `~/.config/filearr/deploy.conf` (keeps
the whole deployment in **one file**) or into
`~/.config/filearr/env.overrides` next to it; every deploy upserts them into
the CT's `.env`, last, so they always win over anything the deploy writes.
Settings managed this way survive redeploys *and even a CT recreation* — the
host file is the source of truth. Only `FILEARR_*` keys are applied (other
lines are ignored), and the six container-managed secret/plumbing keys listed
above are refused from `deploy.conf` with a ⚠ warning.

If the same key appears in both files, **`env.overrides` wins** — its lines are
merged after `deploy.conf`'s. The deploy prints what it staged, e.g.
`host env overrides staged (2 from deploy.conf + 1 from env.overrides)`.

```bash
# example: accept a larger thumbnail cache — 50 GiB advisory budget
mkdir -p ~/.config/filearr
echo 'FILEARR_THUMBNAIL_BUDGET_GB=50' >> ~/.config/filearr/env.overrides
# ...or, single-file style, the same line in deploy.conf:
echo 'FILEARR_THUMBNAIL_BUDGET_GB=50' >> ~/.config/filearr/deploy.conf
bash proxmox/deploy-proxmox.sh        # applied on this and every future deploy
```

!!! tip "Every optional feature is already written into `.env`"
    The deploy writes each [optional feature
    knob](../reference/configuration.md#optional-features) —
    `FILEARR_SEMANTIC_ENABLED`, `FILEARR_CONTENT_SNIFF_ENABLED`,
    `FILEARR_UPDATE_CHECK_AUTO`, `FILEARR_THUMBNAIL_BUDGET_GB`,
    `FILEARR_LOG_DB_ENABLED`, `FILEARR_AGENTS_ENABLED`,
    `FILEARR_AGENT_AUTH_MODE` — into the CT's `.env` with its default value, so
    `cat /opt/filearr/.env` shows what exists instead of leaving it implicit.
    This happens **only when the key is absent**: your in-CT edits, the agents
    wizard's `FILEARR_AGENTS_ENABLED=true`, and your host-side lines are never
    overwritten.

    To set them on the **host** instead — visible and editable in
    `deploy.conf`, surviving a CT rebuild — use the wizard's [Optional app
    settings step](#optional-app-settings) (`--reconfigure` re-offers it).

**In-CT (immediate, no redeploy needed):** edit `.env` directly with the
duplicate-safe remove-then-append pattern, then recreate the stack (compose
only recreates services whose environment actually changed):

```bash
pct exec <vmid> -- bash -c "cd /opt/filearr \
  && grep -v '^FILEARR_THUMBNAIL_BUDGET_GB=' .env > .env.new \
  && echo 'FILEARR_THUMBNAIL_BUDGET_GB=50' >> .env.new \
  && mv .env.new .env \
  && docker compose up -d"
```

If you use both, mirror the value into the overrides file — otherwise the next
deploy re-asserts whatever the host file says.

Verify what the running container actually sees:

```bash
pct exec <vmid> -- bash -c "cd /opt/filearr && docker compose exec -T app env | grep FILEARR_THUMB"
```

The same procedure applies to every variable in the reference — only
`FILEARR_SECRET_KEY` and `FILEARR_PROXY_SHARED_SECRET` should never be
changed after first use (rotating them orphans encrypted alert-channel
secrets / breaks the agent mTLS proxy trust); neither belongs in the
overrides file.

## Shell access — where's the container password?

Nowhere: the wizard deliberately never sets a root password inside the CT, so
there is no stored credential to look up. Administration happens from the
**Proxmox host** shell (node → **Shell** in the web UI, or SSH to the host),
which can always enter an LXC as root without one:

```bash
pct enter <vmid>            # root shell inside the container
pct exec <vmid> -- <cmd>    # run one command without entering
```

The CT's **>_ Console** in the web UI shows a `login:` prompt, and with no
password set `root` cannot log in there. If you want console (or in-CT SSH)
logins to work, set a password once from the host:

```bash
pct exec <vmid> -- passwd   # interactively set root's password
```

Inside the container the stack lives at `/opt/filearr` — that directory holds
`.env`, `docker-compose.yml`, and `config/`, and it's where `docker compose`
commands run. The **application's** admin login is separate and unrelated: it
is whatever you created on the first-visit create-admin screen
([first-run guide](index.md#first-run)).
