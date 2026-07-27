# Unraid templates for the Filearr stack

Five Community Applications–format templates (`Container version="2"`):

| Install order | Template | Image |
|---|---|---|
| 1 | `filearr-postgres.xml` | postgres:18.4 (source of truth + job queue — back this up) |
| 2 | `filearr-meilisearch.xml` | getmeili/meilisearch:v1.49.0 (disposable, rebuildable index) |
| 3 | `filearr.xml` | ghcr.io/pwsh/filearr (web UI + API, port 8484) |
| 4 | `filearr-worker.xml` | same image, Post Arguments run the Procrastinate worker |
| — | `filearr-agent.xml` | ghcr.io/pwsh/filearr-agent (standalone inventory agent — standalone install, needs only a central URL + enrollment token) |

`filearr-agent` is independent of the stack above: install it when this Unraid
box should *feed* a central Filearr running elsewhere (it inventories
`/mnt/user` read-only and replicates outbound over mTLS). It needs no
Postgres/Meilisearch/network setup here — just a token minted from the central
console. Full runbook: `docs/ops/agents.md` §12.

## One-time setup

1. Create the shared Docker network (container-name DNS doesn't work on Unraid's
   default bridge):

       docker network create filearr

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
   fields you fill once each).

4. After first start, initialise the schema once:
   Docker tab → filearr → Console →  `python scripts/init_db.py`

## Notes

- Media is mounted read-only (`/data/media`) in both app and worker — identical
  mapping in both is required so paths in the catalog match.
- Database-backed containers (postgres, meilisearch, the agent's SQLite) use
  DIRECT pool paths (`/mnt/cache/appdata/...`), not `/mnt/user/appdata/...`:
  the `/mnt/user` FUSE (shfs) layer has unreliable file locking/mmap, the
  classic Unraid cause of `database is locked` stalls and index corruption.
  Same share, same files — just the path that bypasses FUSE. (On 6.12+ a
  cache-only "exclusive" appdata share makes `/mnt/user` equivalent; the
  `/mnt/cache` default is simply correct everywhere.) The `filearr` app/worker
  `/config` (thumbnails/caches) is lock-insensitive and stays on `/mnt/user`.
- Port 5432/7700 mappings are intentionally unmapped by default; the stack talks
  over the `filearr` network internally.
- Publishing to Community Applications later: submit via ca.unraid.net/submit
  (needs HTTPS PNG icon, support thread, overview — all present; the templates
  point at the real ghcr.io/pwsh images and github.com/pwsh/filearr URLs).
- **TLS (OPS-T1):** these CA templates ship the app over plain HTTP on port 8484.
  For HTTPS, either (a) put the app behind Unraid's built-in reverse proxy /
  SWAG / Nginx-Proxy-Manager (recommended on Unraid — real cert, no per-client
  CA trust), or (b) use the repo `docker-compose.yml` which includes the Caddy
  TLS sidecar (self-signed LAN CA, https on 8443). A standalone `filearr-caddy`
  CA template is NOT provided yet — the reverse-proxy route is the Unraid-native
  path. Wave 4 login will require HTTPS (Secure cookies), so set this up before
  enabling auth.
- **iGPU thumbnails (optional, P12/OPS-T7):** to hardware-accelerate video
  poster-frames, add `--device=/dev/dri` and the render group to the
  `filearr-worker` container (Extra Parameters: `--device /dev/dri --group-add
  $(stat -c '%g' /dev/dri/renderD128)`). Safe to skip — the pipeline falls back
  to software automatically when the device is absent.
- Alternative: use the repo's `docker-compose.yml` with the Compose Manager plugin.
