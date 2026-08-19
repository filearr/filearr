# Deployment

Filearr ships three supported deployment paths. All of them run the same
container images and the same Postgres + Meilisearch stack — they differ only in
how the host and storage are prepared.

<div class="grid cards" markdown>

- :material-docker: **[Docker Compose](docker-compose.md)**

    The canonical deployment. Works on any Docker host. Every other path wraps
    this compose stack.

- :material-nas: **[Unraid](unraid.md)**

    Community-Applications-format templates. Three containers for the simple
    tier, five for full mutual-TLS agent parity — installed step by step from an
    empty box.

- :material-server-network: **[Proxmox LXC](proxmox.md)**

    A guided wizard builds a Docker-in-LXC container, mounts your network storage
    inside it, and can stand up TLS and the agent CA.

</div>

After any deploy, see [Upgrades & migrations](upgrades.md) for the redeploy and
schema-migration behavior, and [Operations & recovery](../operations.md) for the
runbook.

## What every deployment runs

| Service | Image | Role | Back up? |
|---|---|---|---|
| `app` | `filearr` | REST API + SPA (port 8000 → 8484) | via Postgres |
| `worker` | `filearr` | Procrastinate job worker (scan/extract/index/maintenance) | — |
| `postgres` | `postgres:18.4` | Source of truth **and** job queue | **YES** |
| `meilisearch` | `getmeili/meilisearch:v1.53.0` | Disposable search projection | No (rebuildable) |
| `caddy` *(optional)* | built locally | TLS reverse proxy | No |
| `step-ca` *(optional, `agents` profile)* | `smallstep/step-ca:0.30.2` | Agent certificate authority | volume only |
| `watcher` *(optional)* | `filearr` | Local-disk filesystem watch mode | — |

Only Postgres holds data you cannot recreate. Meilisearch and the thumbnail
cache are disposable projections.

## First run — the same on every path {#first-run}

Once the stack is up (whichever path you chose), the first-run flow is
identical:

1. **Open the web UI** (`http://<host>:8484`, or your HTTPS hostname). Auth is
   on by default, and with zero users the UI shows a one-time **"create the
   administrator account"** screen — set the first admin's username and a
   strong passphrase, then log in. (API equivalent:
   [`POST /api/v1/auth/bootstrap`](../operations.md#enabling-authentication).)
2. **Create your first library**: Libraries → New. Pick the media root
   as the *in-container* path (`/data/media/...` on the standard mappings),
   choose the content presets to include, and save. The folder browser only
   offers allow-listed roots, so a mapping mistake is visible immediately.
3. **Scan it**: the new library's **Scan** button starts a walk with live
   progress. First scans are mtime+size cheap; heavyweight metadata extraction
   queues behind the walk and fills in over the following minutes.
4. **Verify search**: type a filename fragment (typos welcome) into the search
   tab — results should appear as the scan commits batches, before it even
   finishes.

**Then, before you rely on it:**

- **TLS** — put the UI behind HTTPS (per-path notes:
  [compose](docker-compose.md#https-with-the-bundled-caddy),
  [Unraid](unraid.md#https-on-unraid), [Proxmox](proxmox.md) does it in the
  wizard). Sessions send a `Secure` cookie only over HTTPS.
- **Backups** — back up **Postgres** (everything else is rebuildable):
  `scripts/backup.sh` wraps a `pg_dump -Fc` with retention, cron-friendly. See
  [Operations](../operations.md).
- **More sources** — attach other machines with the
  [distributed agents](../agents.md) (one-command installer served by your own
  central), wire up [alerts](../operations.md), and read
  [Upgrades](upgrades.md) before your first version bump.
