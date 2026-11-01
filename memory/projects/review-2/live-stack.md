# Filearr live review stack

Everything lives under this directory (`$LIVE` =
`/tmp/claude-0/-home-user/6189c784-cfba-5e5e-9be0-ef56dd8b70a2/scratchpad/review2/live`).
No tracked file in `/home/user/repo` was changed (`git status --short` is empty). The only things
written outside `$LIVE` are `frontend/node_modules/axe-core` (installed with `npm i --no-save`),
the Vite dep cache under `frontend/node_modules/.vite`, and swagger assets copied into the
already-present, gitignored `frontend/dist/swagger-ui` by the build plugin. The build plugin
hard-codes that path.

**Status at hand-off: the stack is RUNNING** with auth enabled. Log in as **admin / <review-password>**.

## Start / stop

```bash
$LIVE/start.sh            # idempotent; starts whatever is not already up
AUTH=false $LIVE/start.sh # run the API with FILEARR_AUTH_ENABLED=false (stop first)
$LIVE/stop.sh             # stops everything; data (PG cluster, meili db, thumbnails) is kept
```

Both scripts were tested with a full stop, then start, then stop, then start.

## What runs where

| Component | Port | How it runs |
|---|---|---|
| PostgreSQL **16** | 127.0.0.1:5433 | `$LIVE/pg/data` cluster. Postgres refuses to run as root, and the harness keeps resetting the parent `/tmp/claude-0` to mode 0700, so a `postgres` OS user cannot reach it. The cluster therefore runs under `unshare --user --map-user=1000 --map-group=1000`: postgres sees uid 1000, and the files map to real root. Trust auth. The DB is `filearr`, role `filearr`/`filearr` (superuser). |
| Meilisearch **v1.53.1** | 127.0.0.1:7700 | `install.meilisearch.com` and GitHub releases are blocked (403), and Docker Hub hit its pull rate limit. The **`mirror.gcr.io` registry mirror worked**: the amd64 layers of `getmeili/meilisearch:v1.53.1` (the version in docker-compose) were pulled with curl into `$LIVE/dl/rootfs`. The binary is musl-linked, so it runs through the image's own `ld-musl-x86_64.so.1 --library-path rootfs/lib:rootfs/usr/lib`. Master key `<meili-master-key>`. |
| API (uvicorn `filearr.main:app`) | 127.0.0.1:8000 | Python **3.14.6** venv in `$LIVE/venv`. `uv sync --frozen --no-dev` was run with `UV_PROJECT_ENVIRONMENT=$LIVE/venv`, and uv fetched 3.14 because the project pins `>=3.14,<3.15`. |
| Worker | n/a | `procrastinate --app=filearr.worker.proc_app worker --concurrency 4 --queues ""`, the same flags the compose `worker` service uses. |
| Frontend (production build) | 127.0.0.1:**4173** | `vite build --outDir $LIVE/dist`, served by `vite preview`, which proxies `/api` to :8000. **All captures used this build.** |
| Frontend (dev server) | 127.0.0.1:5173 | `npx vite`, also proxying `/api` to :8000. |

Environment: `$LIVE/env.sh` is sourced by start.sh. Key settings:
`FILEARR_DATABASE_URL=postgresql+psycopg://filearr:filearr@127.0.0.1:5433/filearr`,
`FILEARR_PROCRASTINATE_DSN=postgresql://…5433/filearr`, `FILEARR_MEILI_URL=http://127.0.0.1:7700`,
`FILEARR_CONFIG_DIR=$LIVE/config` (thumbnails go there), `FILEARR_AUTH_ENABLED=true`,
`FILEARR_AGENTS_ENABLED=true`, `FILEARR_SEMANTIC_ENABLED=false`, `FILEARR_UPDATE_CHECK_AUTO=false`,
`FILEARR_CONTENT_SNIFF_ENABLED=false`, `FILEARR_SECRET_KEY=<dummy>`, `FILEARR_DISK_PG_PATH=$LIVE/pg/data`,
`PYTHONPATH=/home/user/repo/backend`, `PYTHONDONTWRITEBYTECODE=1`.
Bootstrap is `python scripts/init_db.py`, run from `backend/`. It is idempotent, and start.sh runs it every time.

## Seed data

- `$LIVE/seed_media.py` creates `$LIVE/media`: 39 files. They cover 8 PNG/JPEG images plus a 4000×3000 JPEG, md/txt/csv/json documents, unicode folders (`Família & Amigos`, `日本語フォルダ`, `Q3 résumé – final.md`), an `.srt`, 3 WAVs, a 190-character filename, `100% done #1 & more.txt`, a zero-byte file, a 5 MB random `.bin`, an extensionless file, a dotfile and 4-level nesting.
- The library **"Sample Media"** has id **`70219235-bc4e-486e-9537-12d298d6e4f9`** (also stored in `$LIVE/library_id`).
- Scan result: 38 new, 1 excluded (the dotfile). Extraction finished 38/38 with no errors. 38/38 items were indexed into Meili with drift 0. 9 thumbnails were made, all for images.
- The admin user was created through the UI's first-run bootstrap form. Shots: `shots/login-bootstrap-*`, `shots/flow-bootstrap-*`.
- Side effects of the capture run: about 24 agent enrollment tokens were minted by the `agents-mint-token` state, one saved search "Review saved search" was created, and the library was renamed and then renamed back. There was 1 extra rescan, and audit and login events were recorded.

## What works and what is degraded

- **Everything core works**: auth, bootstrap, login and logout, scan, extract, thumbnails, the Meili index, typo-tolerant search, facets, browse, reports, jobs, agents (with the flag on) and admin.
- **PG16 caveat**: `init_db.py` runs `SELECT uuidv7()` as a guard. uuidv7 is native only in PG18, and migrations use it as a server default. A shim was created before `init_db`, the same one the backend test suite uses (`tests/conftest.py UUIDV7_SHIM`): `CREATE FUNCTION uuidv7() RETURNS uuid AS 'SELECT gen_random_uuid()' LANGUAGE sql`. With the shim, all alembic migrations apply cleanly on 16. IDs are v4, so they are not time-ordered.
- **Semantic/hybrid search is off** (`FILEARR_SEMANTIC_ENABLED=false`) to avoid downloading the model. The About page shows the model as not downloaded.
- **Missing extractors**: `exiftool` is absent, so EXIF/XMP enrichment is skipped. `tesseract` is absent, so OCR is unavailable; it is off by default anyway. `ffprobe`, `ffmpeg`, `pdftoppm` and `pdftotext` are present.
- There is no TLS. The UI therefore shows its plain-http warning, and the login responses carry a `warning` field.
- Service workers were blocked in Playwright (`serviceWorkers: 'block'`) so every capture saw fresh network traffic. That is the origin of the "Service Worker registration blocked by Playwright" warning in every console file, which is a harness artifact.

## Capture artifacts

- `capture.mjs` is the single capture script. Usage: `node capture.mjs shots <phone|tablet|desktop> [routeFilter]`, then `node capture.mjs merge`, `node capture.mjs keyboard` and `node capture.mjs flows`.
- `shots/`: 353 PNGs, named `<route>-<state>-<viewport>-<theme>.png`, plus `flow-*` and `keyboard-*`.
- `console/<route>.json` (20 files): per viewport and theme, it holds console errors and warnings, pageerrors, failed requests (status ≥400 or network failures) and every `/api` call with its timing.
- `perf/<route>.json` (20 files): navigation timing, FCP, LCP, CLS, long tasks, JS sizes (largest chunks), and per-state DOM node count, img count, broken images, pending spinners and document vs viewport width.
- `axe/*.json`: 100 scans, plus `axe/summary.md`.
- `keyboard.md`, `flows.md` and `flows.json`.
- `digest.json` is a machine digest of console and perf data across routes.
- `REPORT.md` is the reviewer report.
- `sheets/` holds the contact sheets I used while reviewing.

Total time: about 25 minutes of wall-clock from the first command to captures done, plus report writing.
