# Backup & restore (Postgres)

> **This page is now the Postgres-specific half of a larger picture.** The
> canonical, complete procedure — including the two pieces of state that are NOT
> in a Postgres dump and whose loss is otherwise silent — is
> `docs-site/operations.md#backup-and-restore` (BK-T2/T5, 2026-08-12). Read the
> state inventory there before you rely on anything below.
>
> The two gaps, in one paragraph: **`FILEARR_SECRET_KEY`** is the AES-GCM
> envelope key for alert-channel secrets and lives outside Postgres by design, so
> a dump carries the ciphertext and not the key — restore under a fresh key and
> every step reports success while every stored SMTP password / webhook secret /
> apprise URL becomes permanently undecryptable. And the **`stepca_data`**
> volume holds the CA root: lose it and step-ca auto-inits a NEW root, every
> certificate it ever issued stops validating, and every enrolled agent must
> re-enroll. `scripts/backup.sh` now takes all three (dump + `.env` + CA tar) as
> one bundle with a `MANIFEST.json`, and `scripts/verify-backup.sh` checks a
> bundle before you trust it.

Postgres is the **source of truth** for the catalogue. Everything a user cannot
get back by re-scanning lives there, so Postgres is what must be backed up *as
data*. Meilisearch and the thumbnail cache are **disposable projections** —
rebuilt from Postgres on demand — and are deliberately NOT part of the backup.

## What's irreplaceable vs. rebuildable

| Data | Store | Backed up? | How it comes back |
|---|---|---|---|
| `user_metadata` edits, tags, custom-field values | Postgres | **YES** | restore only |
| Saved searches, alert channels/rules, alert history | Postgres | **YES** | restore only |
| Provenance / attributed audit trail | Postgres | **YES** | restore only |
| Libraries, scan_paths, schedules, presets | Postgres | **YES** | restore only |
| Extracted `metadata` (ffprobe/exif/…) | Postgres | YES (via dump) | or re-scan the files |
| Job queue (procrastinate_*) | Postgres | YES (via dump) | re-enqueued by scans |
| Search index | Meilisearch | **NO** (by design) | `rebuild-index` from Postgres |
| Thumbnails / poster frames | thumbnail cache volume | **NO** (by design) | regenerated lazily on serve |

The media files themselves are read-only source data on your NAS/shares and are
never touched by Filearr — back them up with your existing NAS strategy, not here.

## Back up (one-liner)

Run on the host where the stack lives (Proxmox: inside the CT; Unraid: on the
server), from the compose project directory (`/opt/filearr` on the Proxmox
deploy):

```bash
cd /opt/filearr
docker compose exec -T postgres pg_dump -U filearr -Fc filearr > filearr-$(date -u +%Y%m%dT%H%M%SZ).dump
```

- `-Fc` = compressed **custom format** → selective/parallel restore with
  `pg_restore`. For a plain-SQL dump you can eyeball, use `-Fp` and redirect to
  `.sql`.
- `-T` (no TTY) is required when piping through `docker compose exec`.
- The dump lands on the host filesystem; copy it off-box (another host, NAS, or
  object storage) — a backup on the same disk as the database is not a backup.

### Helper script + retention

`scripts/backup.sh` does more than the above (rewritten for BK-T2, 2026-08-12):
it writes a timestamped **bundle** — the `-Fc` dump, a 0600 copy of `.env`, a tar
of the step-ca volume, and a `MANIFEST.json` carrying key *fingerprints*
(`sha256(value)[:16]`, never values) plus restore notes. Output goes to a
`backups/` directory **beside** the compose project (override `BACKUP_DIR`), no
longer into `<compose-dir>/config/backups/` — that was the one tree the manual
told operators they need not back up, so the backups were excluded from the
backup by their own documentation. Retention is still the newest **7**
(`BACKUP_KEEP`), the write is still atomic (`.partial` → rename), and it is still
`set -euo pipefail` with an ERR trap, safe to run unattended.

⚠ **A bundle contains secrets** (`.env` verbatim, CA private keys). Treat it like
the `.env` itself. `SKIP_ENV=1` / `SKIP_CA=1` omit either half.

```bash
# manual
bash /opt/filearr/scripts/backup.sh

# nightly at 03:30 via the Proxmox host's crontab (runs inside the CT):
30 3 * * *  pct exec 300 -- bash /opt/filearr/scripts/backup.sh >> /var/log/filearr-backup.log 2>&1
```

**Retention suggestion:** keep 7 daily dumps on-box (the default) and copy at
least one weekly dump off-box. Dumps are small — they hold metadata, not media —
so a few weeks of history costs little.

## Restore

A restore rebuilds Postgres, then the disposable projections are regenerated —
you do NOT need a Meili or thumbnail backup.

1. **Bring up a fresh stack** (empty volumes) with the SAME `.env` — in
   particular the same `POSTGRES_PASSWORD`, `FILEARR_DATABASE_URL`, and
   `MEILI_MASTER_KEY`:
   ```bash
   cd /opt/filearr
   docker compose up -d postgres
   docker compose exec -T postgres pg_isready -U filearr   # wait for ready
   ```

2. **Restore the dump** into the (empty) database. `--clean --if-exists` makes it
   safe to re-run over an existing schema:
   ```bash
   docker compose exec -T postgres \
     pg_restore -U filearr -d filearr --clean --if-exists --no-owner \
     < filearr-YYYYmmddTHHMMSSZ.dump
   ```

3. **Stamp / migrate** — bring the schema to head. `init_db.py` is idempotent:
   it detects a pre-Alembic DB and stamps the baseline, otherwise runs
   `alembic upgrade head`, applies the procrastinate schema, and ensures the
   Meili index exists (see docs/migrations.md):
   ```bash
   docker compose run --rm app python scripts/init_db.py
   ```

4. **Start the rest of the stack and rebuild the search index** (Meili was never
   backed up — it is rebuilt from the restored Postgres rows):
   ```bash
   docker compose up -d
   curl -X POST http://localhost:8484/api/v1/system/rebuild-index
   ```

5. **Thumbnails regenerate lazily** — the first grid/detail view of an item with
   no cached thumbnail re-queues generation (video posters via the worker). No
   manual step; the cache refills over normal use. To pre-warm, re-run a scan
   (the extract ride-along pregenerates grid tiers).

## Verify a backup (scratch round-trip)

**Do this FIRST, as step 1 of any restore — not afterwards as a nice-to-have.**
`scripts/verify-backup.sh <bundle-or-dump>` automates everything below and adds
the check that matters most: it compares the secret-key fingerprint recorded
*inside* the dump against this deployment's `.env`, so you learn that a restore
would orphan your encrypted alert-channel secrets before you perform it rather
than weeks later.

```bash
bash /opt/filearr/scripts/verify-backup.sh /path/to/backups/filearr-YYYYmmddTHHMMSSZ
```

The manual equivalent (throwaway DB, never production):

```bash
# spin a scratch postgres, load the dump, count the irreplaceable rows
docker run -d --name pg-scratch -e POSTGRES_PASSWORD=x -e POSTGRES_USER=filearr -e POSTGRES_DB=filearr postgres:18.4
sleep 8
docker exec -i pg-scratch pg_restore -U filearr -d filearr --clean --if-exists --no-owner < filearr-YYYYmmddTHHMMSSZ.dump
docker exec -it pg-scratch psql -U filearr -d filearr -c "select count(*) from items;"
docker rm -f pg-scratch
```

## Downgrades / schema rollbacks

Alembic downgrades exist but are best-effort and can lose recycle-bin data
(see docs/migrations.md). **Always take a fresh dump before downgrading.** Meili
never needs a backup — rebuild it after any schema change that touches indexed
fields.

## Related: trusting the LAN TLS CA (OPS-T1)

Not a backup, but the other file operators reach for post-deploy. Caddy's
self-signed root CA lives on the `caddy_data` volume. Export it to trust the
HTTPS UI on client machines (removes the browser warning):

```bash
cd /opt/filearr
docker compose cp caddy:/data/caddy/pki/authorities/local/root.crt ./filearr-root-ca.crt
```

Import `filearr-root-ca.crt` into each client's OS/browser trust store. The cert
persists across restarts because `caddy_data` is a named volume — losing it just
means clients must re-trust a freshly minted root.
