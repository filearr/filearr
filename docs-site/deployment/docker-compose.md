# Docker Compose

The compose stack is the canonical Filearr deployment. This page walks through
the prerequisites, the compose file, the environment variables you must set,
and the two gotchas that bite people.

## Prerequisites

- **Docker Engine with the Compose v2 plugin** (`docker compose`, not the old
  `docker-compose` binary — the stack uses profiles and healthcheck
  conditions). Any current Engine release qualifies.
- **`git`** to fetch the source. The shipped compose file **builds the images
  locally** (`build: .`), so the full source tree is required; pre-built
  multi-arch images also exist at `ghcr.io/pwsh/filearr` /
  `ghcr.io/pwsh/filearr-agent` if you'd rather swap `build:` for `image:`.
- Your media reachable on the host at a stable path (local disk, or an
  SMB/NFS/rclone mount) — mounted **read-only** into the containers.

## Quick start

```bash
git clone https://github.com/pwsh/filearr.git && cd filearr
cp .env.example .env          # then edit the secrets (see below)
docker compose up -d          # builds images on first run, then starts the stack
```

The app container runs the idempotent DB bootstrap itself on start (waiting
for Postgres), so `docker compose up -d` is the whole quick start. Running the
bootstrap explicitly still works — useful for watching migration output — and
`FILEARR_AUTO_INIT_DB=false` on the app service disables the automatic run:

```bash
docker compose run --rm app python scripts/init_db.py    # idempotent bootstrap
```

The web UI is then at `http://localhost:8484` (first visit shows the one-time
**create-the-administrator-account** screen — see
[First run](index.md#first-run)) and the interactive API docs at
`http://localhost:8484/api/docs`.

## The compose file, service by service

- **`app`** — the FastAPI application (REST API + the built SPA), listening on
  `8000` in-container and published as `8484` on the host. It mounts your media
  **read-only** at `/data/media` and a `./config` volume for caches/thumbnails.
- **`worker`** — the Procrastinate worker. Runs the scan, extract, index and
  maintenance jobs. Concurrency and which queues it serves are env-driven:
    - `FILEARR_WORKER_CONCURRENCY` — parallel jobs per worker (default 4).
    - `FILEARR_WORKER_QUEUES` — comma-separated queues, or empty for all.
    - Scale out extraction with `docker compose up -d --scale worker=3`, or pin a
      dedicated worker to the `extract` queue (see the comments in
      `docker-compose.yml`). Extract jobs run at a lower priority than scan
      control, so a freshly triggered scan is never stuck behind a big extract
      backlog.
- **`postgres`** — `postgres:18.4`. The source of truth and the job queue.
- **`meilisearch`** — `getmeili/meilisearch:v1.49.0`, analytics disabled, master
  key from `.env`. Its LMDB store lives on a **local** named volume, never on the
  media mount.
- **`caddy`** *(optional TLS front)* and **`step-ca`** *(optional `agents`
  profile)* — see [Proxmox](proxmox.md) and [Distributed agents](../agents.md).
- **`watcher`** *(optional)* — filesystem watch mode. Watch is **local-disk
  only**; inotify is unreliable over SMB/NFS, so scheduled polling scans are the
  default for network mounts.

### The media bind uses `rslave` propagation

The media bind is a long-form bind with `bind.propagation: rslave`. This is
deliberate: if the underlying FUSE/SMB mount is remounted on the host (for
example after an rclone restart), a running container sees the **new** mount
instead of a dead endpoint. Without it you get `OSError: EIO` after the mount
flaps. Keep it.

## Environment variables

Start from `.env.example` and change at least these (generate the two
passwords with `openssl rand -hex 24` or
`python -c "import secrets; print(secrets.token_urlsafe(32))"` — they are
service credentials nobody types):

```bash
POSTGRES_PASSWORD=change-me-too
MEILI_MASTER_KEY=change-me

FILEARR_DATABASE_URL=postgresql+psycopg://filearr:change-me-too@postgres:5432/filearr
FILEARR_PROCRASTINATE_DSN=postgresql://filearr:change-me-too@postgres:5432/filearr
FILEARR_MEILI_URL=http://meilisearch:7700
FILEARR_MEILI_MASTER_KEY=change-me
FILEARR_AUTH_ENABLED=true

# Host path to your media, mounted read-only at /data/media
MEDIA_PATH=/mnt/user/data/media
```

A few more you will likely want:

- `FILEARR_SECRET_KEY` — the envelope key used to encrypt alert-channel secrets
  (AES-GCM). **Required** to create alert channels; when unset the alert-channels
  API returns 503 rather than storing plaintext. Generate one with
  `python -c "import secrets; print(secrets.token_urlsafe(48))"`. It is **never
  rotated automatically** (rotating orphans already-encrypted secrets).
- `FILEARR_SOURCE_URL` — the AGPL section 13 "Source" link shown in the UI footer.
  Point it at *your* source if you run a fork.
- `FILEARR_AUTH_ENABLED=false` — turns authentication off (handy for a first
  look; do not run open on an untrusted network).

The full, grouped list is in the [Configuration reference](../reference/configuration.md).

!!! danger "Secrets never belong in a committed file"
    `FILEARR_SECRET_KEY`, `MEILI_MASTER_KEY`, `POSTGRES_PASSWORD`, and (for the
    agent CA) `FILEARR_CA_PROVISIONER_JWK` / `FILEARR_PROXY_SHARED_SECRET` are
    secrets. Keep them in `.env` (the compose `env_file`), never in the committed
    compose file or in a deploy config that gets checked in.

## The bootstrap: `init_db.py`

The app container **runs this automatically on every start** (retrying while
Postgres comes up; `FILEARR_AUTO_INIT_DB=false` opts out) — invoke it manually
only when you want to watch migration output or manage migrations yourself:

```bash
docker compose run --rm app python scripts/init_db.py
```

This is **idempotent** and safe to re-run. It:

1. Creates or migrates the schema. On a brand-new database it stamps the Alembic
   baseline and runs migrations to head; on a pre-Alembic database it detects
   that and stamps the baseline before migrating.
2. Applies the Procrastinate job-queue schema (checking first — the Procrastinate
   `apply_schema` step is *not* itself idempotent, so Filearr guards it).
3. Ensures the Meilisearch index exists.

## Two gotchas that will bite you

!!! bug "PostgreSQL 18 mounts at `/var/lib/postgresql`, not `.../data`"
    The Postgres 18 Docker image changed the volume convention: mount the
    **parent** directory `/var/lib/postgresql`, **not** `/var/lib/postgresql/data`.
    The shipped compose file already does this. If you copy an older compose file
    from elsewhere, fix this or Postgres will not persist correctly.

!!! bug "`PYTHONPATH=/app` is required"
    Scripts and the Procrastinate CLI in the image need `PYTHONPATH=/app`. It is
    set in the image's `ENV`; do not drop it if you customize the entrypoint or
    the worker command.

## HTTPS with the bundled Caddy {#https-with-the-bundled-caddy}

The compose file ships an optional **`caddy`** TLS front (a custom build:
Caddy + the Cloudflare DNS and layer-4 plugins) publishing 443/8443. Two
modes, chosen by which Caddyfile the service loads:

- **Internal CA (LAN, the default `Caddyfile.internal`):** runs as part of the
  normal `docker compose up -d` and serves `https://<host>:8443` with
  certificates from Caddy's own local CA; each client browser must trust that
  CA root once (from the `caddy_data` volume under
  `pki/authorities/local`) or click through the warning.
- **Real certificates (DNS-01 via Cloudflare):** set in `.env`
  `FILEARR_CADDYFILE=Caddyfile.acme`, `FILEARR_TLS_DOMAIN=<your.zone>`,
  `FILEARR_ACME_EMAIL=<you@example.com>`, and a `CLOUDFLARE_API_TOKEN` scoped
  to DNS edits for the zone, then `docker compose up -d caddy` to reload.
  DNS-01 issuance needs no inbound port from the internet — LAN-only
  deployments get real certificates.

Any other reverse proxy you already run (SWAG, NPM, Traefik…) works exactly
as well — point it at `app:8000` / host port 8484 and forward
`X-Forwarded-*`. Serve HTTPS before relying on logins: the session cookie is
`Secure`-only.

## Verifying it works

```bash
docker compose ps                                  # every service Up; postgres healthy
docker compose logs app --tail 20                  # look for the init_db bootstrap + uvicorn startup
curl http://localhost:8484/api/v1/health           # -> 200 {"status":"ok"}
```

Then do the [first-run flow](index.md#first-run) in the browser: create the
admin account, add a library, scan it, and search. The equivalent API calls
need credentials once the admin exists (auth is on by default) — a session
cookie from login, or `FILEARR_AUTH_ENABLED=false` in a throwaway dev stack:

```bash
# dev stack with auth disabled ONLY:
curl -X POST http://localhost:8484/api/v1/libraries \
  -H 'Content-Type: application/json' \
  -d '{"name":"media","root_path":"/data/media"}'
curl -X POST http://localhost:8484/api/v1/libraries/<id>/scan
```

Results appear in search as the scan commits batches; heavier metadata
extraction back-fills over the following minutes.

## After it's up

Follow the shared [first-run guide](index.md#first-run) — first admin, first
library, TLS, **Postgres backups** (`scripts/backup.sh`), and where to go next
(agents, alerts, [upgrades](upgrades.md)).
