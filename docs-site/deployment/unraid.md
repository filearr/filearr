# Unraid

Filearr ships four Community-Applications-format templates (one per container).
Until they are published to Community Applications, install them manually.

## The four templates

| Install order | Template | Image | Role |
|---|---|---|---|
| 1 | `filearr-postgres.xml` | `postgres:18.4` | Source of truth + job queue — **back this up** |
| 2 | `filearr-meilisearch.xml` | `getmeili/meilisearch:v1.49.0` | Disposable, rebuildable index |
| 3 | `filearr.xml` | Filearr app image | Web UI + API (port 8484) |
| 4 | `filearr-worker.xml` | same image | Post-arguments run the Procrastinate worker |

## One-time setup

1. **Create the shared Docker network.** Container-name DNS does not work on
   Unraid's default bridge, so the containers need a user-defined network:

    ```bash
    docker network create filearr
    ```

2. **Install the templates.** Fetch the four XML files from the repo's
   [`unraid/` folder](https://github.com/pwsh/filearr/tree/main/unraid) (e.g.
   `wget https://raw.githubusercontent.com/pwsh/filearr/main/unraid/filearr.xml`
   and siblings), copy them to
   `/boot/config/plugins/dockerMan/templates-user/` on the server, then in the
   Docker tab choose **Add Container** and pick each template. **Delete the
   copied XMLs after each container is created** — Unraid saves its own
   `my-<name>.xml` on Apply, and a leftover pristine copy shadows your saved
   settings.

3. **Set matching secrets across containers.** Use the same `POSTGRES_PASSWORD`
   and DSNs, and the same `MEILI_MASTER_KEY`, everywhere they appear. These are
   masked fields you fill once each — generate values with
   `openssl rand -hex 24` in the Unraid terminal. If you plan to use alert
   channels, also set `FILEARR_SECRET_KEY` on the `filearr` container
   (`openssl rand -hex 32`; the alerts API returns 503 without it, and it must
   never be rotated once channels exist).

4. **Start the containers** in install order. The `filearr` app bootstraps the
   database itself on first start (idempotent `scripts/init_db.py`, retrying
   while Postgres finishes coming up) — there is no console step. Set
   `FILEARR_AUTO_INIT_DB=false` on the app container only if you prefer to run
   migrations manually.

## Notes and conventions

- **Media is read-only** at `/data/media` in **both** the app and worker, with
  identical mappings — the catalog paths must match between the two.
- **Data volumes belong on the cache pool**, not the array: put Postgres and
  Meilisearch data in appdata.
- **Ports 5432 / 7700 stay unmapped** by default — the stack talks over the
  `filearr` network internally. Only the app's 8484 needs to be reachable.
- **Optional features are declared explicitly.** Both the `filearr` and
  `filearr-worker` templates expose every [optional feature
  knob](../reference/configuration.md#optional-features) —
  `FILEARR_SEMANTIC_ENABLED`, `FILEARR_CONTENT_SNIFF_ENABLED`,
  `FILEARR_UPDATE_CHECK_AUTO`, `FILEARR_THUMBNAIL_BUDGET_GB`,
  `FILEARR_LOG_DB_ENABLED`, `FILEARR_AGENTS_ENABLED` — as **Advanced View**
  variables pre-filled with their safe defaults, so you can see what exists
  without editing files. Set the same value on **both** containers: the worker
  is what loads the semantic model and runs the content-sniff pass.

## HTTPS on Unraid

The CA templates serve the app over **plain HTTP on 8484**. For HTTPS, the
Unraid-native path is to put the app behind a reverse proxy:

- **Recommended:** Unraid's built-in reverse proxy, SWAG, or Nginx Proxy Manager
  — you get a real certificate and no per-client CA trust to manage.
- **Alternative:** use the repo's `docker-compose.yml` (via the Compose Manager
  plugin), which includes the Caddy TLS sidecar (self-signed LAN CA, HTTPS on
  8443).

!!! note "Set up HTTPS early — login needs it"
    Auth is **on by default** and session cookies are marked `Secure`, so the
    login flow requires HTTPS anywhere beyond plain-HTTP LAN testing. Put the
    reverse proxy in place before relying on logins (or set
    `FILEARR_AUTH_ENABLED=false` while evaluating on a trusted LAN).

## Optional: hardware-accelerated video thumbnails

To use an Intel iGPU for video poster frames, add the render device and group to
the **`filearr-worker`** container's Extra Parameters:

```text
--device /dev/dri --group-add $(stat -c '%g' /dev/dri/renderD128)
```

Safe to skip — the thumbnail pipeline falls back to software decoding
automatically when no render device is present.

## After it's up

Open `http://<tower>:8484` and follow the shared
[first-run guide](index.md#first-run): the one-time create-admin screen, your
first library (`/data/media/...` paths as mapped above), a scan, and a search.
Then set up **Postgres backups** — the `filearr-postgres` appdata is the only
unrecoverable state (`pg_dump -Fc` from the container console, or the repo's
`scripts/backup.sh` pattern; Meilisearch and thumbnails are rebuildable). To
feed this catalog from other machines, see
[distributed agents](../agents.md) — this box can also *be* an agent for a
central running elsewhere via the `filearr-agent` template.
