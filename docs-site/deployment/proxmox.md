# Proxmox LXC

`proxmox/deploy-proxmox.sh` deploys the whole Filearr stack into a
**Docker-enabled LXC** on Proxmox VE, mounting your network storage **inside**
the container so the deployment does not depend on host-side mounts. It is a
wizard on first run and an idempotent redeploy on later runs.

Run it on the **Proxmox host shell** (as root), from inside the project folder:

```bash
git clone https://github.com/pwsh/filearr.git && cd filearr   # once
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
- **Storage definitions** — one or more network shares (see below).

### The prompt-once model, and where secrets go

Answers persist to `~/.config/filearr/deploy.conf`; storage definitions
(including credentials) persist to `~/.config/filearr/storages.env` (mode 0600).
Both are re-applied on every redeploy, so **the container is fully disposable** —
destroy and rebuild it and your configuration returns.

!!! danger "Secrets never go in `deploy.conf`"
    `deploy.conf` holds only non-secret settings. The Cloudflare API token, the
    auto-generated proxy shared secret, the auto-generated `FILEARR_SECRET_KEY`,
    and the extracted CA provisioner JWK are **secrets** and live in the
    container's `.env` **only** — never in `deploy.conf`, never echoed to the
    terminal. On a redeploy, a blank token answer means "keep the container's
    existing one".

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
  before that step) — restart them from the Admin page, or just rerun the
  deploy.

The two usual causes are a **full CT disk** (`pct exec <vmid> -- df -h /`)
and **Docker Hub being unreachable** (the build pulls base images); the
script now aborts at the build step with the real error, so scroll up to it.
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
