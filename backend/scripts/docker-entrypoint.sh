#!/bin/sh
# Container entrypoint: idempotent DB bootstrap, then exec the real command.
#
# Only the APP container auto-inits (argv[0] == uvicorn): the worker/watcher
# containers share the image but override the command, and racing two
# concurrent bootstraps would trip Procrastinate's non-idempotent apply_schema
# (init_db guards it with to_regclass, but only within one process). The app
# always starts first in every documented topology (compose depends_on;
# Unraid install order), so a single bootstrapper is safe AND sufficient.
#
# FILEARR_AUTO_INIT_DB=false opts out (operators who run scripts/init_db.py
# themselves, e.g. blue/green migrations).
set -e

if [ "${FILEARR_AUTO_INIT_DB:-true}" != "false" ] && [ "${1:-}" = "uvicorn" ]; then
  attempt=0
  # Postgres may still be starting (Unraid brings containers up in parallel
  # after a reboot) — retry the whole idempotent bootstrap, not just a ping.
  until python scripts/init_db.py; do
    attempt=$((attempt + 1))
    if [ "$attempt" -ge 30 ]; then
      echo "filearr: init_db failed after ${attempt} attempts — check FILEARR_DATABASE_URL / postgres logs" >&2
      exit 1
    fi
    echo "filearr: database not ready (attempt ${attempt}/30); retrying in 2s" >&2
    sleep 2
  done
fi

exec "$@"
