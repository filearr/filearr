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

### Why four containers and not one {#why-four-containers}

Four containers is more install friction than one, and on Unraid — where an app
is a template you click — that friction is real. It is a deliberate trade, argued
for a **homelab**, not borrowed from enterprise practice:

1. **A bundled Postgres welds its major version to the app image.** The day that
   pin moves from 18 to 19, every existing data directory needs `pg_upgrade` run
   with *both* majors present — which a single image that ships exactly one major
   cannot do. Separate containers mean **you** choose when Postgres moves, and
   you can stay on 18 through as many Filearr releases as you like.
2. **Updating Filearr does not cycle your database.** Pull the app image, restart
   two containers, done. Postgres and Meilisearch keep running with their caches
   warm. A single container restarts everything for a UI fix.
3. **Memory isolation.** Meilisearch's indexing flush can spike past 6 GiB on a
   large catalogue (see [Console unresponsive, host CPU pegged](../operations.md#console-unresponsive-high-cpu)).
   In its own container that spike kills Meilisearch alone, and the index is
   rebuildable. In a shared container it takes the database down with it.
4. **Per-container logs and health are Unraid's primary debugging affordance.**
   "Which one is unhealthy" is a glance at the Docker tab. Inside one container it
   is a log-file archaeology exercise.

The install friction is being reduced by other means rather than by bundling:
merging the app and worker into one container, publishing the CA template, and
better defaults so fewer fields need touching. Collapsing the database into the
app image is not on that list — it would trade a one-time setup cost for a
permanent upgrade cliff.

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

## Backup and restore {#backup-and-restore}

Everything else in the manual takes backups through `docker compose`, which
Unraid does not have. These are the native equivalents. Read the
[state inventory](../operations.md#state-inventory) first — it lists what exists
besides the database and what losing each piece costs.

### The three things to back up

1. **The database** — `filearr-postgres`. Everything you cannot re-derive.
2. **`FILEARR_SECRET_KEY`** — set on the `filearr` container. It is the envelope
   key for alert-channel secrets, and it is **not inside the dump**. Restore a
   dump under a different key and everything reports success while every stored
   SMTP password / webhook secret / apprise URL becomes permanently
   undecryptable, with no error anywhere. Copy it into your password manager the
   day you set it.
3. **`/mnt/user/appdata/filearr/agent-releases`** — only if you have uploaded a
   custom signed agent build. It is the one `/config` subtree nothing can
   regenerate. Everything else under `/config` (thumbnails, models, exports,
   inventory) rebuilds itself.

There is no step-ca container in the Unraid templates, so the CA volume is not a
concern here unless you also run the compose stack.

### Take a backup

From the Unraid terminal (or **Docker → filearr-postgres → Console**):

```bash
mkdir -p /mnt/user/backups/filearr
docker exec filearr-postgres pg_dump -U filearr -Fc filearr \
  > /mnt/user/backups/filearr/filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

`-Fc` is the compressed custom format `pg_restore` reads. Note there is **no
`-T`** here: that flag is a `docker compose exec` requirement, and plain
`docker exec` does not allocate a TTY unless you ask for one — adding `-t` would
corrupt the binary dump.

Write to a share that is **not** on the same pool as `/mnt/cache/appdata`, and
have your existing off-box sync pick it up. A backup that dies with the cache
pool is not a backup.

### Schedule it (User Scripts plugin) {#scheduling-with-user-scripts}

Install **User Scripts** from Community Applications, then *Add New Script* →
name it `filearr-backup` → *Edit Script*:

```bash
#!/bin/bash
# Nightly Filearr backup. Keeps the newest 7 dumps.
set -euo pipefail
DEST=/mnt/user/backups/filearr
KEEP=7

mkdir -p "$DEST"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
out="$DEST/filearr-$ts.dump"

# Refuse rather than write a truncated file if the database is not up.
docker exec filearr-postgres pg_isready -U filearr >/dev/null

# Write to .partial and rename, so a crash never leaves a half dump that the
# prune below would count as a good one.
docker exec filearr-postgres pg_dump -U filearr -Fc filearr > "$out.partial"
mv "$out.partial" "$out"
echo "wrote $out ($(du -h "$out" | cut -f1))"

# Prune to the newest $KEEP.
ls -1t "$DEST"/filearr-*.dump | tail -n +$((KEEP + 1)) | while read -r old; do
  echo "removing $old"; rm -f "$old"
done
```

Set the schedule to **Scheduled Daily** (or *Custom* with a cron expression such
as `30 3 * * *`). Use *Run Script* once by hand and read the output before you
trust the schedule.

!!! warning "Record the secret key alongside the dumps"
    The script above backs up the database and nothing else, because that is all
    a shell on the Unraid host can reach without stopping containers. Store
    `FILEARR_SECRET_KEY` (and your `POSTGRES_PASSWORD` / `MEILI_MASTER_KEY`)
    somewhere safe **now**. The console's Jobs page shows the key's fingerprint
    on the About page, which lets you *check* a key but never recover one.

### How the "Backup/Restore Appdata" plugin differs

The Community Applications **Backup/Restore Appdata** plugin is a valid backup
model, but a *different* one — and mixing the two up is how people end up with a
corrupt database in an archive:

- It is a **cold copy**: it stops the containers, tars the appdata paths, and
  starts them again. That is safe precisely *because* Postgres is stopped.
- `pg_dump` is a **live logical** backup: consistent by construction, restorable
  into a different Postgres major, and it takes no downtime.

Either works. If you use the plugin, **check that it sweeps both locations** —
the Filearr stack deliberately splits them:

| Container | Appdata path | Why |
|---|---|---|
| `filearr`, `filearr-worker` | `/mnt/user/appdata/filearr` | `/config` is lock-insensitive, so the FUSE path is fine |
| `filearr-postgres`, `filearr-meilisearch` | `/mnt/cache/appdata/...` | direct pool path — `/mnt/user`'s shfs layer has unreliable file locking/mmap, the classic Unraid cause of database corruption |

A plugin configuration that only sweeps `/mnt/user/appdata` therefore backs up
the thumbnails and misses the database entirely. Add both, or use `pg_dump` for
the database and let the plugin handle the rest.

### Restore

Follow the order. Steps 1 and 2 are not optional.

```bash
# 1. VERIFY FIRST — restore into a throwaway container and count rows. Do this
#    before you touch the live stack.
docker run -d --rm --name pgverify \
  -e POSTGRES_USER=filearr -e POSTGRES_PASSWORD=verify -e POSTGRES_DB=filearr \
  postgres:18.4
sleep 10
docker exec -i pgverify pg_restore -U filearr -d filearr --no-owner --clean --if-exists \
  < /mnt/user/backups/filearr/filearr-YYYYmmddTHHMMSSZ.dump
docker exec pgverify psql -U filearr -d filearr -tAc 'SELECT count(*) FROM items'
docker exec pgverify psql -U filearr -d filearr -tAc \
  "SELECT value FROM instance_meta WHERE key = 'secret_key_fingerprint'"
docker rm -f pgverify
```

Compare that last value against `sha256(FILEARR_SECRET_KEY)` truncated to 16 hex
— it is the fingerprint the app shows on its About page. **If they differ, the
encrypted alert-channel secrets in this dump cannot be read by this deployment.**

```bash
# 2. SECRETS BEFORE DATA. On the `filearr` and `filearr-worker` containers, set
#    FILEARR_SECRET_KEY back to its ORIGINAL value (Docker tab → Edit → Apply),
#    along with POSTGRES_PASSWORD and MEILI_MASTER_KEY.

# 3. Stop the app and worker so nothing writes during the load.
docker stop filearr filearr-worker

# 4. Load the dump.
docker exec -i filearr-postgres pg_restore -U filearr -d filearr \
  --clean --if-exists --no-owner < /mnt/user/backups/filearr/filearr-YYYYmmddTHHMMSSZ.dump

# 5. Start the app. It runs the idempotent scripts/init_db.py bootstrap itself,
#    which is the only thing that can bring an arbitrary prior schema to head.
docker start filearr filearr-worker

# 6. Rebuild the search index (Meilisearch was never backed up — it is a
#    projection). Also available as "Rebuild search index" on the Jobs page.
curl -X POST http://<tower-ip>:8484/api/v1/system/rebuild-index
```

Thumbnails regenerate lazily on first view. Finally, **open the console and check
the About page**: if `FILEARR_SECRET_KEY` does not match what the restored
database was encrypted under, you get a red row there, a banner on the Admin
dashboard, and an error in the log. A clean About page is what "the restore
worked" looks like.

### Without a shell: the in-app backup

The Jobs page has a **Back up now** button that writes a dump into `/config` and
offers it for download — no terminal needed. It is honest about its limits: a
container cannot read the host's environment or another container's volume, so it
cannot include `FILEARR_SECRET_KEY`. Treat it as a convenient database snapshot,
not as your whole backup. See
[in-app backup](../operations.md#in-app-backup).

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
Then set up **backups** — see [Backup and restore](#backup-and-restore) above for
the native commands, a User Scripts schedule, and the restore sequence. The
database is the bulk of it, but it is **not** the only thing you must keep:
`FILEARR_SECRET_KEY` lives outside the dump and losing it costs you every
encrypted alert-channel secret, silently. To
feed this catalog from other machines, see
[distributed agents](../agents.md) — this box can also *be* an agent for a
central running elsewhere via the `filearr-agent` template.
