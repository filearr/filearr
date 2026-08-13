#!/bin/sh
# Container entrypoint: idempotent DB bootstrap, then either exec the real
# command or (mode `all`) supervise the app AND worker in one container.
#
# ---------------------------------------------------------------------------
# Mode 1 — pass-through (the default, and what compose/Proxmox use)
# ---------------------------------------------------------------------------
# `$1` is the real command; we bootstrap and then `exec "$@"`.
#
# Only the APP command bootstraps ($1 == uvicorn): the worker/watcher containers
# share the image but override the command, and racing two concurrent bootstraps
# would trip Procrastinate's non-idempotent apply_schema (init_db guards it with
# to_regclass, but only within one process). The app always starts first in every
# documented topology (compose depends_on; Unraid install order), so a single
# bootstrapper is safe AND sufficient.
#
# ---------------------------------------------------------------------------
# Mode 2 — merged app+worker (UR-T2, 2026-08-12) — `$1 == "all"`
# ---------------------------------------------------------------------------
# Opt-in, added so an Unraid install is 3 containers instead of 4 (the app and
# worker templates differed ONLY by command, and every other env/volume had to be
# kept byte-identical by hand — a standing drift risk; see unraid/README.md).
# Compose is deliberately NOT switched to it: separate services keep the
# documented `docker compose up -d --scale worker=N` scale-out, which a single
# supervised container cannot express.
#
# The supervisor is ~60 lines of POSIX sh on purpose. supervisord/s6 would add a
# runtime dependency and a second configuration language to an image whose entire
# process model is "two long-lived children"; the parts that actually matter here
# are the three below, and each is a rule we learned the hard way:
#
#   1. Bootstrap runs ONCE, before either child starts. Same gate as mode 1, so
#      the "exactly one process migrates" invariant is preserved by construction
#      rather than by timing.
#   2. SIGTERM is forwarded to BOTH children and we wait up to
#      FILEARR_STOP_GRACE_SECONDS (default 60) before SIGKILL. 60s is not a
#      guess: docker-compose.yml sets `stop_grace_period: 60s` on the worker
#      because Docker's default 10s regularly cut Procrastinate jobs off
#      mid-transaction during redeploys. Unraid gives a container 10s and then
#      kills it, so WE have to be the thing that waits — which is exactly why
#      this default lives here and not only in compose.
#   3. If EITHER child exits, we tear the other down and exit NON-ZERO. A
#      supervisor that limps along on one surviving child hides a dead API behind
#      a container Docker still calls "running". Exiting lets
#      `--restart=unless-stopped` recreate the container, and the bootstrap above
#      re-runs cleanly (it is idempotent).
#
# Anything after `all` is appended to the uvicorn argv, e.g.
#   all --proxy-headers --forwarded-allow-ips '*'
#
# FILEARR_AUTO_INIT_DB=false opts out of the bootstrap in BOTH modes (operators
# who run scripts/init_db.py themselves, e.g. blue/green migrations).
set -e

FILEARR_MODE="${1:-}"

# --- DB bootstrap (shared by mode 1's uvicorn command and mode 2) -------------
if [ "${FILEARR_AUTO_INIT_DB:-true}" != "false" ] &&
   { [ "$FILEARR_MODE" = "uvicorn" ] || [ "$FILEARR_MODE" = "all" ]; }; then
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

if [ "$FILEARR_MODE" != "all" ]; then
  exec "$@"
fi

# ===========================================================================
# Mode 2 continues here: merged app + worker supervisor.
# ===========================================================================
shift   # drop the literal "all"; anything left is extra uvicorn argv

# Liveness probe for one of our own children.
#
# `kill -0` alone is NOT sufficient and the reason is the classic sh-supervisor
# trap: a child that has exited but not yet been reaped is a ZOMBIE, and you can
# still signal a zombie, so `kill -0` keeps reporting success — a crashed uvicorn
# would look alive forever. Two things fix it, belt and braces:
#
#   * /proc/<pid>/status reports `State: Z (zombie)` on Linux, which is the only
#     platform this image runs on. grep is Essential in Debian, unlike awk in a
#     -slim base, so grep is what we use. `-s` swallows the race where the pid
#     disappears between the two checks.
#   * As a fallback anywhere /proc is absent: the `wait` inside _nap blocks in
#     waitpid(-1), which reaps ANY finished child as a side effect, so a real
#     death becomes visible to `kill -0` within one poll interval regardless.
_alive() {
  kill -0 "$1" 2>/dev/null || return 1
  if grep -qs '^State:[[:space:]]*Z' "/proc/$1/status"; then return 1; fi
  return 0
}

# Interruptible sleep. POSIX defers a trapped signal until the current FOREGROUND
# command finishes — `sleep 1` would therefore delay shutdown by up to a second
# per iteration and, worse, dash will not run the handler at all until it
# returns. `wait` is the one builtin POSIX specifies as interruptible, so
# backgrounding the sleep and waiting on it is the standard way to make a sh
# poll loop signal-responsive. (There is no portable `wait -n`: it is a bash/ksh
# extension and /bin/sh in this image is dash.)
_nap() {
  sleep "$1" &
  wait "$!" 2>/dev/null || true
}

# Set by the TERM/INT trap. Distinguishes "the orchestrator asked us to stop"
# (clean, exit 0) from "a child died on us" (exit non-zero, see rule 3 above).
FILEARR_STOPPING=0
_on_signal() { FILEARR_STOPPING=1; }
trap _on_signal TERM INT

# Graceful teardown: SIGTERM both, poll for up to the grace budget, then SIGKILL
# whatever is left. Never fails — this runs on the way out.
_stop_children() {
  _grace="${FILEARR_STOP_GRACE_SECONDS:-60}"
  echo "filearr: stopping children (grace ${_grace}s)" >&2
  for _pid in "$FILEARR_API_PID" "$FILEARR_WORKER_PID"; do
    if _alive "$_pid"; then kill -TERM "$_pid" 2>/dev/null || true; fi
  done

  _waited=0
  while [ "$_waited" -lt "$_grace" ]; do
    if ! _alive "$FILEARR_API_PID" && ! _alive "$FILEARR_WORKER_PID"; then
      return 0
    fi
    _nap 1
    _waited=$((_waited + 1))
  done

  # Budget spent. The worker is the one that legitimately takes time here (it
  # finishes in-flight jobs on SIGTERM); past the grace we stop being polite.
  for _pid in "$FILEARR_API_PID" "$FILEARR_WORKER_PID"; do
    if _alive "$_pid"; then
      echo "filearr: pid ${_pid} still running after ${_grace}s — SIGKILL" >&2
      kill -KILL "$_pid" 2>/dev/null || true
    fi
  done
}

# --- start the children ------------------------------------------------------
# uvicorn: same argv as the image CMD, plus anything passed after `all`.
uvicorn filearr.main:app --host 0.0.0.0 --port 8000 "$@" &
FILEARR_API_PID=$!

# procrastinate: the FILEARR_* -> worker-flag mapping is copied verbatim from
# docker-compose.yml's worker service so the two run modes cannot drift.
# Procrastinate reads its OWN PROCRASTINATE_WORKER_* envvars, so we translate to
# keep every tunable under the single FILEARR_ prefix. An empty --queues means
# "all queues", which is the compose default too. PYTHONPATH=/app is set in the
# image; do not drop it.
procrastinate --app=filearr.worker.proc_app worker \
  --concurrency "${FILEARR_WORKER_CONCURRENCY:-4}" \
  --queues "${FILEARR_WORKER_QUEUES:-}" &
FILEARR_WORKER_PID=$!

echo "filearr: merged mode — api pid ${FILEARR_API_PID}, worker pid ${FILEARR_WORKER_PID}" >&2

# --- supervise ---------------------------------------------------------------
# Poll rather than block in `wait`: a plain `wait` returns only when ALL children
# have exited, which is precisely the "limping along on one child" state rule 3
# forbids, and `wait -n` is not POSIX (see _nap).
FILEARR_DEAD_NAME=""
while [ "$FILEARR_STOPPING" -eq 0 ]; do
  if ! _alive "$FILEARR_API_PID"; then FILEARR_DEAD_NAME="api"; break; fi
  if ! _alive "$FILEARR_WORKER_PID"; then FILEARR_DEAD_NAME="worker"; break; fi
  _nap 1
done

if [ -n "$FILEARR_DEAD_NAME" ]; then
  # Reap the corpse to learn its status. `wait <pid>` on an already-exited child
  # returns its exit code; on a signalled child it returns 128+signum.
  FILEARR_DEAD_STATUS=0
  if [ "$FILEARR_DEAD_NAME" = "api" ]; then
    wait "$FILEARR_API_PID" 2>/dev/null || FILEARR_DEAD_STATUS=$?
  else
    wait "$FILEARR_WORKER_PID" 2>/dev/null || FILEARR_DEAD_STATUS=$?
  fi
  echo "filearr: ${FILEARR_DEAD_NAME} exited (status ${FILEARR_DEAD_STATUS}) — shutting the container down" >&2
  _stop_children
  # Always non-zero, even when the child exited 0: neither child is supposed to
  # return, so a clean exit is still a container-level failure.
  [ "$FILEARR_DEAD_STATUS" -ne 0 ] || FILEARR_DEAD_STATUS=1
  exit "$FILEARR_DEAD_STATUS"
fi

# Signalled: normal stop/restart. Exit 0 so `docker stop` and Unraid's restart
# do not log a spurious failure.
_stop_children
echo "filearr: merged mode stopped" >&2
exit 0
