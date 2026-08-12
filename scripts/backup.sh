#!/usr/bin/env bash
# backup.sh — full-deployment backup BUNDLE for a compose-deployed Filearr (BK-T2).
#
# WHY THIS IS NOT JUST A pg_dump (design-backup-completeness.md, 2026-08-12).
# Postgres is the source of truth for the CATALOGUE, and for a long time this
# script and the manual both said "Postgres only". That is correct about data and
# wrong about the deployment, in two ways — one of which fails SILENTLY:
#
#   1. FILEARR_SECRET_KEY is the AES-GCM envelope key for alert-channel secrets
#      and is deliberately held OUTSIDE Postgres (a stolen dump must yield no
#      credentials). So a dump carries the CIPHERTEXT and not the key. Restore
#      onto a fresh box with a newly generated key and every step reports
#      success — pg_restore clean, migrations clean, index rebuilt — while every
#      stored SMTP password / webhook secret / apprise URL is now permanently
#      undecryptable. The operator finds out weeks later, from the alert that
#      never arrived.
#   2. The step-ca volume holds the CA root + intermediate keys. Lose it and
#      step-ca auto-inits a BRAND NEW root on next start: every certificate it
#      ever issued stops validating and every enrolled agent needs full
#      re-enrollment. No pg_restore repairs that.
#
# So this writes a BUNDLE: the dump, the .env, a tar of the CA volume, and a
# MANIFEST.json that records what is inside and what must be restored alongside
# it. The manifest carries key FINGERPRINTS (sha256(value)[:16]) and never key
# values, so `restore` can be CHECKED before it is trusted.
#
# ⚠ THE BUNDLE CONTAINS SECRETS. env.backup is your deployment's .env verbatim
# (database password, Meili master key, FILEARR_SECRET_KEY, the CA provisioner
# JWK) and the step-ca tar contains private CA keys. Treat a bundle exactly like
# the .env itself: 0600, off-box, encrypted at rest if it leaves your control.
#
# Usage:   scripts/backup.sh
# Env:
#   FILEARR_DIR    compose project dir (default /opt/filearr, else script's ..)
#   BACKUP_DIR     output dir (default a `backups/` SIBLING of the compose dir —
#                  deliberately OUTSIDE {config}, see below)
#   BACKUP_KEEP    how many bundles to retain (default 7)
#   PG_SERVICE     compose service name for Postgres (default postgres)
#   PG_USER/PG_DB  role/db (default filearr/filearr)
#   CA_SERVICE     compose service name for step-ca (default step-ca)
#   SKIP_ENV=1     omit env.backup (you archive .env by other means)
#   SKIP_CA=1      omit the step-ca tar
#
# Restore: docs-site/operations.md#backup-and-restore. VERIFY FIRST with
# scripts/verify-backup.sh — that is a numbered step of the restore, not an aside.

set -euo pipefail
set -E
trap 'rc=$?; echo "✗ BACKUP FAILED (exit $rc) at line $LINENO: $BASH_COMMAND" >&2' ERR

SELF_DIR="$(cd "$(dirname "$0")/.." && pwd)"
FILEARR_DIR="${FILEARR_DIR:-$( [[ -f /opt/filearr/docker-compose.yml ]] && echo /opt/filearr || echo "$SELF_DIR" )}"
# Default OUTSIDE {config}. The old default wrote dumps into {config}/backups —
# the very tree the manual told operators they need not back up, so the backups
# were excluded from the backup by their own documentation. A sibling directory
# is still on the same box (see the filesystem warning below) but it is at least
# somewhere an operator's off-box sync will look.
BACKUP_DIR="${BACKUP_DIR:-$(dirname "$FILEARR_DIR")/backups}"
BACKUP_KEEP="${BACKUP_KEEP:-7}"
PG_SERVICE="${PG_SERVICE:-postgres}"
PG_USER="${PG_USER:-filearr}"
PG_DB="${PG_DB:-filearr}"
CA_SERVICE="${CA_SERVICE:-step-ca}"

command -v docker >/dev/null 2>&1 || { echo "docker not found" >&2; exit 1; }
cd "$FILEARR_DIR" || { echo "compose dir not found: $FILEARR_DIR" >&2; exit 1; }

mkdir -p "$BACKUP_DIR"
BACKUP_DIR="$(cd "$BACKUP_DIR" && pwd)"
ts="$(date -u +%Y%m%dT%H%M%SZ)"
bundle="${BACKUP_DIR}/filearr-${ts}"
# Atomic publish: everything is assembled under .partial and renamed at the very
# end, so a crash mid-backup never leaves a half-bundle that looks complete to
# the retention prune (or to a panicking operator at 3am).
work="${bundle}.partial"
rm -rf "$work"
mkdir -p "$work"

# --------------------------------------------------------------------------- #
# Same-filesystem warning                                                      #
# --------------------------------------------------------------------------- #
# A backup on the disk it is protecting is not a backup. We cannot see inside a
# named volume from here, but we CAN compare the filesystem device backing the
# backup destination with the one backing the Docker data-root, which is where
# `pgdata` lives on a default install. Advisory only — never fatal.
warn_same_fs() {
  local docker_root dev_backup dev_docker
  docker_root="$(docker info --format '{{.DockerRootDir}}' 2>/dev/null || true)"
  [[ -n "$docker_root" && -d "$docker_root" ]] || return 0
  dev_backup="$(df -P "$BACKUP_DIR" 2>/dev/null | awk 'NR==2 {print $1}')"
  dev_docker="$(df -P "$docker_root" 2>/dev/null | awk 'NR==2 {print $1}')"
  [[ -n "$dev_backup" && "$dev_backup" == "$dev_docker" ]] || return 0
  cat >&2 <<EOF
⚠ ${BACKUP_DIR} is on the SAME filesystem (${dev_backup}) as the Docker data
  root (${docker_root}), which is where the Postgres volume lives. A disk
  failure or a full disk takes the database and this backup together. Set
  BACKUP_DIR to another device, or sync the bundle off-box after every run.
EOF
}
warn_same_fs

# --------------------------------------------------------------------------- #
# 1. Postgres dump                                                             #
# --------------------------------------------------------------------------- #
echo "==> checking Postgres is ready"
docker compose exec -T "$PG_SERVICE" pg_isready -U "$PG_USER" >/dev/null

dump_name="filearr-${ts}.dump"
echo "==> dumping ${PG_DB} -> ${dump_name}"
# -Fc: compressed custom format (parallel/selective restore via pg_restore).
docker compose exec -T "$PG_SERVICE" pg_dump -U "$PG_USER" -Fc "$PG_DB" \
  > "${work}/${dump_name}"
dump_bytes="$(wc -c < "${work}/${dump_name}" | tr -d ' ')"
echo "    ${dump_name} (${dump_bytes} bytes)"

# Schema revision + item count, read from the SAME database we just dumped, so
# verify-backup.sh has something to compare a restored copy against.
psql_q() {
  docker compose exec -T "$PG_SERVICE" \
    psql -U "$PG_USER" -d "$PG_DB" -tAc "$1" 2>/dev/null | tr -d '\r' || true
}
alembic_head="$(psql_q 'SELECT version_num FROM alembic_version LIMIT 1')"
item_count="$(psql_q "SELECT count(*) FROM items")"
server_version="$(psql_q 'SHOW server_version')"

# --------------------------------------------------------------------------- #
# 2. .env — the half of the deployment Postgres cannot hold                    #
# --------------------------------------------------------------------------- #
env_included=false
if [[ "${SKIP_ENV:-0}" != "1" && -f "${FILEARR_DIR}/.env" ]]; then
  # umask BEFORE the copy, not chmod after: a 0644 window on a file containing
  # the database password and the envelope key is a real window.
  ( umask 077; cp "${FILEARR_DIR}/.env" "${work}/env.backup" )
  chmod 600 "${work}/env.backup"
  env_included=true
  echo "==> env.backup written (mode 0600) — ⚠ CONTAINS SECRETS"
elif [[ "${SKIP_ENV:-0}" == "1" ]]; then
  echo "==> skipping .env (SKIP_ENV=1)"
else
  echo "⚠ no ${FILEARR_DIR}/.env found — this bundle CANNOT restore your secrets" >&2
fi

# Fingerprints, computed WITHOUT the value ever reaching stdout, a log, or a
# process argument. Same function as the app's (filearr.keyguard.fingerprint):
# sha256 of the UTF-8 value, first 16 hex. Matching these against a target box's
# environment is the check that catches the silent-restore failure.
fp_of() {  # fp_of <VAR_NAME> ; prints 16 hex, or nothing
  local name="$1" line value
  [[ -f "${FILEARR_DIR}/.env" ]] || return 0
  line="$(grep -m1 -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "${FILEARR_DIR}/.env" || true)"
  [[ -n "$line" ]] || return 0
  value="${line#*=}"
  # Strip one layer of surrounding quotes the way a shell/compose parser would.
  value="${value%$'\r'}"
  [[ "$value" == \"*\" ]] && value="${value:1:${#value}-2}"
  [[ "$value" == \'*\' ]] && value="${value:1:${#value}-2}"
  [[ -n "$value" ]] || return 0
  printf '%s' "$value" | sha256sum | cut -c1-16
}
fp_secret="$(fp_of FILEARR_SECRET_KEY)"
# The CA pin is PUBLIC material, so the manifest records its own first 16 hex
# rather than a hash of it — that is what the app stores too, and it is directly
# comparable to `step certificate fingerprint` output.
ca_pin="$(grep -m1 -E '^[[:space:]]*(export[[:space:]]+)?FILEARR_CA_FINGERPRINT=' \
  "${FILEARR_DIR}/.env" 2>/dev/null | sed -E 's/^[^=]*=//; s/^["'"'"']//; s/["'"'"']$//' \
  | tr -d ':\r' | tr 'A-Z' 'a-z' | cut -c1-16 || true)"

# --------------------------------------------------------------------------- #
# 3. step-ca volume — fleet-wide agent trust                                   #
# --------------------------------------------------------------------------- #
# Resolved from the RUNNING container's mounts when possible (exact), else from
# the compose project name (best effort). A stack with no CA — the agents
# profile is opt-in — skips cleanly with a printed note; that is a normal
# configuration, not a failure.
ca_included=false
ca_bytes=0
# Distinguishes "there is no CA to back up" (a normal agents-off stack) from
# "there is one and we skipped it" — only the latter makes the bundle partial.
ca_expected=false
resolve_ca_volume() {
  local cid vol project
  cid="$(docker compose ps -q "$CA_SERVICE" 2>/dev/null || true)"
  if [[ -n "$cid" ]]; then
    vol="$(docker inspect -f '{{range .Mounts}}{{if eq .Destination "/home/step"}}{{.Name}}{{end}}{{end}}' "$cid" 2>/dev/null || true)"
    [[ -n "$vol" ]] && { printf '%s' "$vol"; return 0; }
  fi
  project="${COMPOSE_PROJECT_NAME:-$(basename "$FILEARR_DIR" | tr 'A-Z' 'a-z' | tr -cd 'a-z0-9_-')}"
  vol="${project}_stepca_data"
  docker volume inspect "$vol" >/dev/null 2>&1 && { printf '%s' "$vol"; return 0; }
  return 1
}
if [[ "${SKIP_CA:-0}" == "1" ]]; then
  ca_expected=true
  echo "⚠ skipping step-ca volume (SKIP_CA=1) — back it up separately or this" >&2
  echo "  bundle cannot restore agent trust" >&2
elif ca_vol="$(resolve_ca_volume)"; then
  ca_expected=true
  echo "==> archiving step-ca volume ${ca_vol} -> stepca/stepca-data.tar.gz"
  mkdir -p "${work}/stepca"
  # Read-only mount of the CA volume into a throwaway container: this needs no
  # running step-ca and works while the stack is down.
  docker run --rm -v "${ca_vol}:/ca:ro" alpine:3.21 \
    tar -cz -C /ca . > "${work}/stepca/stepca-data.tar.gz"
  chmod 600 "${work}/stepca/stepca-data.tar.gz"
  ca_bytes="$(wc -c < "${work}/stepca/stepca-data.tar.gz" | tr -d ' ')"
  ca_included=true
  echo "    stepca-data.tar.gz (${ca_bytes} bytes, mode 0600) — ⚠ CONTAINS CA PRIVATE KEYS"
else
  echo "==> no step-ca volume found — this stack runs without the agents CA; skipping"
fi

# --------------------------------------------------------------------------- #
# 4. MANIFEST.json                                                             #
# --------------------------------------------------------------------------- #
app_version="$(docker compose exec -T app python -c \
  'import filearr; print(filearr.__version__)' 2>/dev/null | tr -d '\r' || true)"

json_str() {  # emit a JSON string, or null when empty
  # Backslash FIRST, then quote — the other order double-escapes. These values
  # come from psql/docker output, not from an operator, but a malformed
  # MANIFEST.json would break the verifier at exactly the wrong moment.
  local v="${1:-}" bs='\'
  if [[ -z "$v" ]]; then printf 'null'; return; fi
  # The pattern must come from a VARIABLE: a literal `${v//\\/...}` does not
  # match a backslash under `set -euo pipefail` in every bash we support
  # (verified 2026-08-12) and would silently emit invalid JSON.
  v="${v//"$bs"/"$bs$bs"}"
  printf '"%s"' "${v//\"/\\\"}"
}

{
  printf '{\n'
  printf '  "bundle_version": 1,\n'
  printf '  "created_at": "%s",\n' "$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  printf '  "app_version": %s,\n' "$(json_str "$app_version")"
  printf '  "alembic_head": %s,\n' "$(json_str "$alembic_head")"
  printf '  "postgres_server_version": %s,\n' "$(json_str "$server_version")"
  printf '  "item_count": %s,\n' "${item_count:-null}"
  printf '  "contents": {\n'
  printf '    "dump": {"file": "%s", "bytes": %s},\n' "$dump_name" "${dump_bytes:-0}"
  printf '    "env": {"file": %s, "included": %s},\n' \
    "$( [[ "$env_included" == true ]] && printf '"env.backup"' || printf 'null')" "$env_included"
  printf '    "stepca": {"file": %s, "included": %s, "bytes": %s}\n' \
    "$( [[ "$ca_included" == true ]] && printf '"stepca/stepca-data.tar.gz"' || printf 'null')" \
    "$ca_included" "${ca_bytes:-0}"
  printf '  },\n'
  # Machine-readable completeness, mirroring the in-app bundle's flag so one
  # verifier reads both. TRUE only when everything this deployment HAS was
  # captured: an agents-off stack with no CA is complete without a CA archive;
  # a stack that has one and skipped it is not.
  complete=false
  if [[ "$env_included" == true ]] && { [[ "$ca_expected" == false ]] || [[ "$ca_included" == true ]]; }; then
    complete=true
  fi
  printf '  "complete": %s,\n' "$complete"
  printf '  "fingerprints": {\n'
  printf '    "_note": "sha256(value)[:16] hex. NEVER a value. Compare against the target deployment BEFORE trusting a restore.",\n'
  printf '    "secret_key_fingerprint": %s,\n' "$(json_str "$fp_secret")"
  printf '    "ca_root_fingerprint": %s\n' "$(json_str "$ca_pin")"
  printf '  },\n'
  printf '  "restore_notes": [\n'
  printf '    "Run scripts/verify-backup.sh on this bundle BEFORE relying on it. It is a numbered step of the restore, not an optional aside.",\n'
  printf '    "Restore env.backup as the .env of the target deployment FIRST. FILEARR_SECRET_KEY must be the ORIGINAL value: a fresh key restores cleanly and leaves every encrypted alert-channel secret permanently undecryptable, with no error anywhere.",\n'
  printf '    "Compare secret_key_fingerprint above against the target box after it boots (console About page, or the startup log). A mismatch is reported as an error there.",\n'
  printf '    "Restore stepca/stepca-data.tar.gz into the step-ca volume BEFORE first start. An empty volume makes step-ca generate a NEW root; every certificate it previously issued stops validating and every enrolled agent needs re-enrollment.",\n'
  printf '    "Load order is dump, THEN scripts/init_db.py (it is the only thing that can stamp/upgrade from an arbitrary prior schema), THEN start the stack, THEN POST /api/v1/system/rebuild-index.",\n'
  printf '    "Meilisearch, the thumbnail cache and {config}/exports are NOT in this bundle by design: all are rebuildable. {config}/agent-releases is NOT rebuildable if you uploaded a custom signed agent build — copy it separately."\n'
  printf '  ]\n'
  printf '}\n'
} > "${work}/MANIFEST.json"

# The manifest names files but not secrets; the bundle DIRECTORY still holds
# env.backup and the CA keys, so the whole thing is 0700.
chmod 700 "$work"
mv "$work" "$bundle"
echo "✓ bundle complete: ${bundle}"
[[ -n "$fp_secret" ]] && echo "    FILEARR_SECRET_KEY fingerprint: ${fp_secret}"
echo "    NEXT: scripts/verify-backup.sh ${bundle}"

# --------------------------------------------------------------------------- #
# 5. Prune                                                                     #
# --------------------------------------------------------------------------- #
echo "==> pruning to newest ${BACKUP_KEEP} bundle(s)"
# Newest-first; delete everything past the keep count. Our own names are
# timestamped and contain no spaces/newlines, so plain ls is fine. Leftover
# *.partial directories from a crashed run are swept too — they are never
# valid backups and their presence only ever misleads.
mapfile -t bundles < <(ls -1dt "${BACKUP_DIR}"/filearr-*/ 2>/dev/null | sed 's:/*$::' | grep -v '\.partial$' || true)
if (( ${#bundles[@]} > BACKUP_KEEP )); then
  for old in "${bundles[@]:BACKUP_KEEP}"; do
    echo "    removing old bundle: $old"
    rm -rf "$old"
  done
fi
for stale in "${BACKUP_DIR}"/filearr-*.partial; do
  [[ -d "$stale" && "$stale" != "$work" ]] || continue
  echo "    removing abandoned partial: $stale"
  rm -rf "$stale"
done
