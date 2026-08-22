# Unraid templates for the Filearr stack

Six Community Applications–format templates (`Container version="2"`). You do not
need all of them — pick a tier, then install that tier's templates **in order**.

| Tier | Install order | Templates |
|---|---|---|
| **Simple** | 1 → 2 → 3 | `filearr-postgres`, `filearr-meilisearch`, `filearr` |
| **Proxied** | 1 → 2 → 3 | the same three, behind a reverse proxy you already run |
| **Full parity** | 1 → 2 → 3 → 4 → 5 | the same three, then `filearr-stepca`, then `filearr-caddy` |

| # | Template | Image | Role |
|---|---|---|---|
| 1 | `filearr-postgres.xml` | `postgres:18.4` | Source of truth + job queue — **back this up** |
| 2 | `filearr-meilisearch.xml` | `getmeili/meilisearch:v1.53.0` | Disposable, rebuildable search index |
| 3 | `filearr.xml` | `ghcr.io/filearr/filearr` | Web UI + API **and** the background worker (port 8484) |
| 4 | `filearr-stepca.xml` | `smallstep/step-ca:0.30.2` | Private CA that issues every agent's client certificate — **required for any agent**, in both `fingerprint` and `mtls-header` auth modes |
| 5 | `filearr-caddy.xml` | `ghcr.io/filearr/filearr-caddy` | TLS reverse proxy + the mTLS agent plane |
| — | `filearr-agent.xml` | `ghcr.io/filearr/filearr-agent` | Standalone inventory agent — independent install, needs only a central URL + enrollment token |

The install order matters for one reason each: Postgres before everything because
the app bootstraps its schema on first start; Meilisearch before the app because
the app configures its index at startup; step-ca before Caddy because Caddy reads
the CA root as its client-certificate trust pool.

**Templates 1 and 2 are the "I do not already have these" path.** If you already
run a Postgres 18+ server or a Meilisearch 1.53.x instance, skip them and point
the app's DSNs at yours — but read the three constraints (PG18 floor, a dedicated
DATABASE rather than a schema, and the broad-reach Meili key) in
`docs-site/deployment/unraid.md#existing-servers` first.

**Templates 4 and 5 are optional** unless you set `FILEARR_AGENT_AUTH_MODE` to
`mtls-header` or `both`, which is the only configuration that requires them.

Full setup guide, field by field, from empty box to enrolled agent:
[`docs-site/deployment/unraid.md`](../docs-site/deployment/unraid.md).

## Upgrading: `filearr-worker` has been removed {#worker-removed}

**If you run a `filearr-worker` container, remove it.** As of 2026-08-12 the
`filearr` container runs both the API and the background worker, selected by `all`
in its Post Arguments.

1. Docker tab → `filearr-worker` → **Remove**. It has no volumes of its own that
   `filearr` does not also mount, so nothing is lost.
2. Delete `my-filearr-worker.xml` from
   `/boot/config/plugins/dockerMan/templates-user/`.
3. Edit `filearr`: add `all` to **Post Arguments** and `--stop-timeout=60` to
   **Extra Parameters**. Apply.

Nothing in the database changes and no re-scan is needed. The log should show one
database bootstrap followed by a line naming both child processes.

`--stop-timeout=60` is not decoration. Docker SIGTERMs the container, waits, then
SIGKILLs — and the wait defaults to 10 seconds. The merged entrypoint forwards
SIGTERM to both children and waits up to `FILEARR_STOP_GRACE_SECONDS` (60) so the
worker can finish in-flight jobs; `docker-compose.yml` carries the same 60s as
`stop_grace_period` because the 10s default regularly cut Procrastinate jobs off
mid-transaction during redeploys. Without the flag the container dies at 10s and
the in-container grace never applies.

If you deliberately ran a second worker for throughput, you still can: install
`filearr` again under a different name with the Procrastinate worker command in
Post Arguments — or use the repo's `docker-compose.yml`, which keeps separate
`app` and `worker` services and the documented `--scale worker=N` scale-out for
exactly that case.

## Why three containers and not one

Three templates is more setup than one, and that friction is real. It is a
deliberate homelab trade, not borrowed enterprise practice:

1. **A bundled Postgres welds its major version to the app image.** When the pin
   moves (18 → 19), every existing data directory needs `pg_upgrade` run with
   BOTH majors present — which a single image shipping exactly one major cannot
   do. Separate means the operator chooses when Postgres moves.
2. **Updating Filearr does not cycle the database.** Pull the app image, restart
   one container; Postgres and Meilisearch keep running with warm caches.
3. **Memory isolation.** The documented >6 GiB Meilisearch indexing flush kills
   Meilisearch alone — and the index is a rebuildable projection — instead of
   taking the database down with it.
4. **Per-container logs and health are Unraid's primary debugging affordance.**
   "Which one is unhealthy" is a glance at the Docker tab.

The app and the worker had none of those properties: same image, same
environment, same volumes, differing only in the command — and every variable had
to be kept byte-identical across two templates by hand, which was a standing
drift risk (`FILEARR_SEMANTIC_ENABLED` set on one and not the other produced
behaviour nobody could explain). Merging *those two* cost nothing and removed the
risk. Collapsing the database into the app image would instead trade a one-time
setup cost for a permanent upgrade cliff, which is why it is not on the list.

## Backups

Full runbook (native `docker exec` commands, a User Scripts schedule, the
restore sequence, and how the CA "Backup/Restore Appdata" plugin differs):
`docs-site/deployment/unraid.md#backup-and-restore`. Moving to a different host
rather than restoring in place: `docs-site/operations.md#migrate-to-a-new-host`.
The short version:

- `docker exec filearr-postgres pg_dump -U filearr -Fc filearr > /mnt/user/backups/filearr/filearr-$(date -u +%Y%m%dT%H%M%SZ).dump`
  (no `-T` — that flag is a `docker compose exec` requirement, and adding `-t`
  here corrupts the binary dump).
- **`FILEARR_SECRET_KEY` is NOT in the dump.** Restoring under a different key
  succeeds while leaving every encrypted alert-channel secret permanently
  undecryptable. Record it separately.
- **Full parity: back up `/mnt/cache/appdata/filearr-stepca` too.** It is CA
  private key material — the one thing in the stack that re-scanning cannot
  rebuild. A new CA invalidates every certificate it ever issued and every agent
  must re-enrol.
- If the Backup/Restore Appdata plugin is doing the work, point it at **all** of
  `/mnt/user/appdata/filearr`, `/mnt/cache/appdata/filearr-postgres` and
  `/mnt/cache/appdata/filearr-stepca` — the split below is deliberate, and a
  plugin sweeping only `/mnt/user/appdata` backs up thumbnails and misses the
  database.

`filearr-agent` is independent of the stack above: install it when this Unraid
box should *feed* a central Filearr running elsewhere (it inventories
`/mnt/user` read-only and replicates outbound over mTLS). It needs no
Postgres/Meilisearch/network setup here — just a token minted from the central
console. Full runbook: `docs-site/agents.md`.

## One-time setup

**The scripted path does all of this for you.** `scripts/setup-unraid.sh` runs
on the Unraid box and handles the Docker setting, the network, the appdata
directories and their ownership, the secrets, and all five templates with every
field filled in — then walks you through the Apply clicks one container at a
time, probing each for real readiness, and harvests the step-ca fingerprint,
admin password and provisioner JWK inline. Run it in the Unraid terminal (the
`>_` icon, or SSH — that shell is already root):

```bash
mkdir -p /boot/config/plugins/filearr
curl -fsSL https://raw.githubusercontent.com/filearr/filearr/main/scripts/setup-unraid.sh \
  -o /boot/config/plugins/filearr/setup-unraid.sh
bash /boot/config/plugins/filearr/setup-unraid.sh
```

It downloads onto the **flash** deliberately: `/boot` survives reboots, so
re-runs, saved answers and a `--check` months later all use the same copy. Use
`bash <file>` rather than `./<file>` — the flash is vfat, where permission bits
are a mount-time fiction — and never `sh <file>`, which fails on the first
bashism. To pin the exact revision you reviewed, fetch by commit SHA instead of
`main`. Later: re-run to resume, `--check` to validate, `--summary` to re-print
the handoff, `--local-dir <checkout>/unraid` for an air-gapped box.

It cannot do two things and does not pretend to: the per-container **Apply**
click (Unraid creates containers through the webGUI; there is no supported CLI)
and the **DNS records** (they live on your resolver, not this box). Full
description: `docs-site/deployment/unraid.md#scripted-setup`.

The rest of this section is the same setup by hand — the reference for what the
script does, and the path for a box with no internet.

1. **Pick a network topology first** — it decides what goes in both DSNs, the
   Meilisearch URL, both Caddy upstreams and the CA's own certificate, so
   changing it later is an edit to every container. Full comparison, worked field
   map and DNS records:
   `docs-site/deployment/unraid.md#step-0-networking`.

   - **Option A, the default:** one shared Docker network, containers address
     each other by NAME. Container-name DNS doesn't work on Unraid's default
     bridge, so a user-defined network is needed — but FIRST enable
     Settings → Docker → (Advanced View) → "Preserve user defined networks",
     which requires stopping the Docker service to change. Without it Unraid
     deletes CLI-created networks on every service restart and the template
     dropdown never lists them. THEN create it once:

             docker network create filearr

     `filearr-caddy` is the exception — it needs a LAN address of its own for
     ports 80/443 *and* the `filearr` network for its upstreams, and Unraid's
     single Network Type dropdown can't express both. A plain
     `docker network connect` is discarded every time Apply or an image update
     re-creates the container; the guide's
     [`#dual-homing`](../docs-site/deployment/unraid.md#dual-homing) section
     gives three ways to make it persist.
   - **Option B, fully bridged:** every container gets `Custom : br0` and its own
     fixed LAN IP, and every reference becomes an IP. No shared network, nothing
     to dual-home, per-container firewall rules — at the cost of IP bookkeeping
     and the macvlan host-isolation gotcha (a `br0` container and the Unraid host
     cannot reach each other until Settings → Docker → *Host access to custom
     networks* is enabled; `ipvlan` does **not** fix that, it fixes the crashes).

2. Manual template install (until published to CA): copy the XML files to
   `/boot/config/plugins/dockerMan/templates-user/` on your Unraid server, then
   add each via Docker tab → Add Container → pick the template.
   **IMPORTANT — delete the copied XMLs after each container is created.** On
   Apply, Unraid saves its own `my-<name>.xml` alongside; if the pristine file
   stays, both claim the same template name and the container's Edit page
   loads the pristine DEFAULTS instead of your saved settings (and re-applying
   then overwrites the saved copy with defaults). One name, one file.

3. Set the same `POSTGRES_PASSWORD` / DSNs and the same `MEILI_MASTER_KEY`
   across containers (templates default to matching values; passwords are masked
   fields you fill once each). At full parity, `FILEARR_PROXY_SHARED_SECRET` must
   be byte-identical on `filearr` and `filearr-caddy`.

4. That's it — on first start the `filearr` container bootstraps the database
   itself (idempotent `scripts/init_db.py`, retrying while Postgres is still
   coming up), once, before either of its child processes starts. No console
   step. Set `FILEARR_AUTO_INIT_DB=false` only if you prefer to run migrations
   manually.

## Notes

- Media is mounted read-only at `/data/media`. Library paths you create in the UI
  are the IN-CONTAINER paths under that mapping.
- Database-backed containers (postgres, meilisearch, step-ca, the agent's SQLite)
  use DIRECT pool paths (`/mnt/cache/appdata/...`), not `/mnt/user/appdata/...`:
  the `/mnt/user` FUSE (shfs) layer has unreliable file locking/mmap, the
  classic Unraid cause of `database is locked` stalls and index corruption.
  Same share, same files — just the path that bypasses FUSE. (On 6.12+ a
  cache-only "exclusive" appdata share makes `/mnt/user` equivalent; the
  `/mnt/cache` default is simply correct everywhere.) The `filearr` container's
  `/config` (thumbnails/caches) is lock-insensitive and stays on `/mnt/user`.
- Port 5432/7700 mappings are intentionally unmapped by default; on the shared
  `filearr` network the stack talks internally and nothing reaches the LAN. That
  guarantee does **not** hold under the fully bridged option — a container with
  its own LAN address publishes every port it listens on at that address,
  regardless of the Port fields, so the app moves to `http://<its-ip>:8000`
  (container port, not the 8484 host mapping) and Postgres/Meilisearch become
  LAN-reachable. Generate real credentials.
- **`filearr-caddy` wants its own IP.** Set Network Type: Custom br0 with a fixed
  address so ports 80/443 do not collide with Unraid's web GUI or an existing
  reverse proxy. This is the expected Unraid pattern for a proxy container — and
  on the shared-network option it is the one container that must be on **two**
  networks, which Unraid's single Network Type dropdown cannot express and Apply
  silently undoes. See `docs-site/deployment/unraid.md#dual-homing`.
- **Three DNS records, at the proxy.** Full parity needs `filearr.<domain>`,
  `agents.<domain>` and `ca.<domain>` all pointing at `filearr-caddy`'s IP —
  including `ca.`, which Caddy raw-passes through on 443. Publish them on your
  LAN resolver (router/firewall host override, Pi-hole, AdGuard, a real DNS
  server); with the `acme` profile the PUBLIC zone needs no A records at all,
  because DNS-01 only ever creates a TXT record. Details and the split-horizon
  caveat: `docs-site/deployment/unraid.md#dns-records`.
- **TLS.** Templates 1–3 serve plain HTTP on 8484. For HTTPS, either put the app
  behind a reverse proxy you already run (SWAG / Nginx Proxy Manager / Unraid's
  built-in — real cert, no per-client CA trust), or install `filearr-caddy`:
  profile `internal` gives you self-signed HTTPS with no domain required, profile
  `acme` gives you a real Let's Encrypt wildcard **plus** the mTLS agent plane.
  Auth is on by default and its session cookie is `Secure`, so set one of these
  up before relying on logins.
- **iGPU thumbnails (optional):** to hardware-accelerate video poster-frames, add
  `--device /dev/dri --group-add $(stat -c '%g' /dev/dri/renderD128)` to the
  **`filearr`** container's Extra Parameters.
  Safe to skip — the pipeline falls back to software automatically.
- Publishing to Community Applications later: submit via ca.unraid.net/submit
  (needs HTTPS PNG icon, support thread, overview — all present; the templates
  point at the real ghcr.io/filearr images and github.com/filearr/filearr URLs).
- Alternative: use the repo's `docker-compose.yml` with the Compose Manager
  plugin. It keeps `app` and `worker` as separate services on purpose and does
  not use the merged `all` mode.
