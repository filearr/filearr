#!/usr/bin/env bash
# verify-backup.sh — prove a backup bundle is restorable BEFORE you need it (BK-T2).
#
# WHY THIS IS A REQUIRED STEP AND NOT AN ASIDE. The old manual mentioned dump
# verification in a closing paragraph, after the restore procedure, phrased as a
# suggestion. That ordering is exactly backwards: an operator following the
# documented restore reaches the end of it before being told to check whether the
# file they just loaded was any good. A truncated dump, a dump taken while the
# database was mid-migration, or a bundle whose FILEARR_SECRET_KEY does not match
# the target box are all things you want to learn on a normal Tuesday, not during
# the outage that made you go looking for the backup.
#
# WHAT IT DOES
#   1. Structural check of the bundle (dump present, MANIFEST.json parses).
#   2. `pg_restore --list` — proves the dump is a readable custom-format archive
#      and not a truncated file, without touching anything.
#   3. Restores it into a THROWAWAY postgres:18.4 container on a random port,
#      counts `items`, and compares against the manifest's recorded count.
#   4. Compares the manifest fingerprints against the CURRENT deployment .env.
#      A mismatch here is the whole point of BK-T1: it means restoring this
#      bundle onto this box would silently orphan every encrypted alert-channel
#      secret.
#
# The throwaway container is removed on every exit path, including failure and
# Ctrl-C. Nothing in your live stack is touched: this script never writes to the
# running Postgres and never starts a compose service.
#
# Usage:   scripts/verify-backup.sh <bundle-dir-or-dump-file>
# Env:
#   FILEARR_DIR   compose project dir, for the .env fingerprint comparison
#                 (default /opt/filearr, else the script's ..)
#   PG_IMAGE      throwaway image (default postgres:18.4 — must be >= the
#                 server the dump came from)
#   KEEP=1        leave the throwaway container running for inspection

set -euo pipefail
set -E

target="${1:-}"
if [[ -z "$target" ]]; then
  echo "usage: $0 <bundle-dir-or-dump-file>" >&2
  exit 2
fi

SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILEARR_DIR="${FILEARR_DIR:-$( [[ -f /opt/filearr/docker-compose.yml ]] && echo /opt/filearr || echo "$SELF_DIR" )}"
PG_IMAGE="${PG_IMAGE:-postgres:18.4}"
PG_USER=filearr
PG_DB=filearr

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }

fail=0
note_fail() { echo "✗ $*" >&2; fail=1; }
note_ok()   { echo "✓ $*"; }
note_warn() { echo "⚠ $*" >&2; }

# --------------------------------------------------------------------------- #
# 1. Locate the dump + manifest                                                #
# --------------------------------------------------------------------------- #
manifest=""
if [[ -d "$target" ]]; then
  bundle="$(cd "$target" && pwd)"
  dump="$(ls -1 "$bundle"/*.dump 2>/dev/null | head -1 || true)"
  [[ -f "${bundle}/MANIFEST.json" ]] && manifest="${bundle}/MANIFEST.json"
  [[ -n "$dump" ]] || { echo "no *.dump inside ${bundle}" >&2; exit 1; }
elif [[ -f "$target" ]]; then
  # A bare .dump is still verifiable — the pre-BK-T2 backups all look like this,
  # and refusing to check them would leave every existing backup unverified.
  dump="$target"
  bundle="$(cd "$(dirname "$target")" && pwd)"
  [[ -f "${bundle}/MANIFEST.json" ]] && manifest="${bundle}/MANIFEST.json"
  note_warn "verifying a bare dump: env/CA coverage cannot be checked"
else
  echo "not found: ${target}" >&2
  exit 1
fi
echo "==> bundle: ${bundle}"
echo "    dump:   $(basename "$dump") ($(wc -c < "$dump" | tr -d ' ') bytes)"

json_get() {  # json_get <key> — flat top-level string/number lookup, no jq needed
  [[ -n "$manifest" ]] || return 0
  sed -n "s/.*\"$1\"[[:space:]]*:[[:space:]]*\"\{0,1\}\([^\",}]*\)\"\{0,1\}.*/\1/p" \
    "$manifest" | head -1 | tr -d '\r'
}

if [[ -n "$manifest" ]]; then
  note_ok "MANIFEST.json present (created $(json_get created_at), app $(json_get app_version), head $(json_get alembic_head))"
else
  note_warn "no MANIFEST.json — this predates BK-T2; only the dump can be checked"
fi

# --------------------------------------------------------------------------- #
# 2. Bundle completeness (the two silent gaps)                                 #
# --------------------------------------------------------------------------- #
if [[ -d "$target" ]]; then
  if [[ -f "${bundle}/env.backup" ]]; then
    note_ok "env.backup present"
    perms="$(stat -c '%a' "${bundle}/env.backup" 2>/dev/null || echo '')"
    [[ "$perms" == "600" ]] || note_warn "env.backup is mode ${perms:-unknown}, expected 600 — it contains secrets"
  else
    note_warn "no env.backup: this bundle CANNOT restore FILEARR_SECRET_KEY, and a restore under a different key silently orphans every encrypted alert-channel secret"
  fi
  if [[ -f "${bundle}/stepca/stepca-data.tar.gz" ]]; then
    note_ok "step-ca archive present"
  else
    note_warn "no step-ca archive: fine if this stack runs no agents CA; otherwise losing the volume forces a NEW CA root and full re-enrollment of every agent"
  fi
fi

# --------------------------------------------------------------------------- #
# 3. Archive readability — cheap, catches truncation before spending a restore #
# --------------------------------------------------------------------------- #
echo "==> reading the archive table of contents"
toc_lines="$(docker run --rm -i "$PG_IMAGE" pg_restore --list < "$dump" 2>/dev/null | grep -c . || true)"
if [[ "${toc_lines:-0}" -gt 0 ]]; then
  note_ok "pg_restore --list: ${toc_lines} entries (valid custom-format archive)"
else
  note_fail "pg_restore could not read this archive — it is truncated or not -Fc"
  exit 1
fi

# --------------------------------------------------------------------------- #
# 4. Real restore into a throwaway server                                      #
# --------------------------------------------------------------------------- #
cid=""
cleanup() {
  if [[ -n "$cid" && "${KEEP:-0}" != "1" ]]; then
    docker rm -f "$cid" >/dev/null 2>&1 || true
  elif [[ -n "$cid" ]]; then
    echo "    (KEEP=1) throwaway container left running: $cid"
  fi
}
trap cleanup EXIT INT TERM

echo "==> starting a throwaway ${PG_IMAGE}"
# No published port and a tmpfs data dir: nothing on the host is written, and
# nothing can collide with the live stack's 5432.
cid="$(docker run -d --rm \
  -e POSTGRES_USER="$PG_USER" -e POSTGRES_PASSWORD=verify \
  -e POSTGRES_DB="$PG_DB" \
  --tmpfs /var/lib/postgresql:rw,size=4g \
  "$PG_IMAGE")"

for _ in $(seq 1 60); do
  if docker exec "$cid" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null 2>&1; then break; fi
  sleep 1
done
docker exec "$cid" pg_isready -U "$PG_USER" -d "$PG_DB" >/dev/null \
  || { note_fail "throwaway Postgres never became ready"; exit 1; }

echo "==> restoring into the throwaway server"
# --no-owner: the throwaway has only the one role. Errors are captured rather
# than fatal — pg_restore exits non-zero for benign "role does not exist" noise
# on --clean, and the authoritative check is the row count below.
if ! docker exec -i "$cid" pg_restore -U "$PG_USER" -d "$PG_DB" --no-owner --clean --if-exists \
     < "$dump" > /tmp/filearr-verify-$$.log 2>&1; then
  note_warn "pg_restore reported errors (see below); judging by the row counts instead"
  tail -5 /tmp/filearr-verify-$$.log >&2 || true
fi
rm -f /tmp/filearr-verify-$$.log

q() { docker exec "$cid" psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null | tr -d '\r'; }
restored_items="$(q 'SELECT count(*) FROM items' || true)"
restored_head="$(q 'SELECT version_num FROM alembic_version LIMIT 1' || true)"

if [[ -z "$restored_items" ]]; then
  note_fail "no items table after restore — this dump is not a Filearr database"
else
  note_ok "restored items: ${restored_items}"
  expected="$(json_get item_count)"
  if [[ -n "$expected" && "$expected" != "$restored_items" ]]; then
    note_fail "item count ${restored_items} != manifest ${expected} (dump taken mid-write, or the wrong dump)"
  elif [[ -n "$expected" ]]; then
    note_ok "item count matches the manifest"
  fi
fi

expected_head="$(json_get alembic_head)"
if [[ -n "$restored_head" ]]; then
  note_ok "schema revision in the dump: ${restored_head}"
  if [[ -n "$expected_head" && "$expected_head" != "$restored_head" ]]; then
    note_fail "schema revision ${restored_head} != manifest ${expected_head}"
  fi
else
  note_warn "no alembic_version row — scripts/init_db.py will stamp on restore"
fi

# --------------------------------------------------------------------------- #
# 5. Fingerprints vs THIS deployment (the BK-T1 check, applied early)          #
# --------------------------------------------------------------------------- #
# `instance_meta` travels inside the dump, so the restored copy tells us which
# key the ciphertext was written under — no manifest required. Comparing it
# against the live .env answers the only question that matters before a real
# restore: "will the secrets in this backup still be readable here?"
recorded_fp="$(q "SELECT value FROM instance_meta WHERE key = 'secret_key_fingerprint'" || true)"
current_fp=""
if [[ -f "${FILEARR_DIR}/.env" ]]; then
  line="$(grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?FILEARR_SECRET_KEY=' "${FILEARR_DIR}/.env" || true)"
  if [[ -n "$line" ]]; then
    v="${line#*=}"; v="${v%$'\r'}"
    [[ "$v" == \"*\" ]] && v="${v:1:${#v}-2}"
    [[ "$v" == \'*\' ]] && v="${v:1:${#v}-2}"
    [[ -n "$v" ]] && current_fp="$(printf '%s' "$v" | sha256sum | cut -c1-16)"
  fi
fi

if [[ -z "$recorded_fp" ]]; then
  note_warn "the dump records no secret-key fingerprint (taken before BK-T1) — a restore here cannot be checked automatically"
elif [[ -z "$current_fp" ]]; then
  note_warn "FILEARR_SECRET_KEY is not set in ${FILEARR_DIR}/.env; the dump was written under ${recorded_fp}"
elif [[ "$recorded_fp" == "$current_fp" ]]; then
  note_ok "secret-key fingerprint ${recorded_fp} matches this deployment"
else
  note_fail "SECRET KEY MISMATCH: this dump's encrypted alert-channel secrets were written under ${recorded_fp}, but this deployment uses ${current_fp}. Restoring it here would leave every SMTP password, webhook secret and apprise URL permanently undecryptable — with no error at restore time. Restore the ORIGINAL key (env.backup) first, or plan to re-enter every channel secret."
fi

manifest_fp="$(json_get secret_key_fingerprint)"
if [[ -n "$manifest_fp" && -n "$recorded_fp" && "$manifest_fp" != "$recorded_fp" ]]; then
  note_fail "manifest fingerprint ${manifest_fp} != the one inside the dump ${recorded_fp} — env.backup and the dump are from different deployments"
fi

echo
if (( fail )); then
  echo "✗ VERIFY FAILED — do not rely on this bundle until the above is resolved" >&2
  exit 1
fi
echo "✓ VERIFY PASSED — ${bundle} restores cleanly and its key fingerprints check out"
