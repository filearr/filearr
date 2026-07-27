#!/bin/sh
# filearr-agent container entrypoint.
#
# Lifecycle: fix /config ownership -> enroll once (retrying while central is
# unreachable) -> start the replication daemon + an interval rescan loop as
# two children of this shell (the documented "scan + run side by side"
# operating model), reap them, and forward SIGTERM so `docker stop` unwinds
# both cleanly. This shell stays PID 1 on purpose: `exec`ing the daemon would
# orphan the scan loop's children onto a process that never reaps them.
set -u

PUID="${PUID:-99}"    # Unraid default: 99 (nobody)
PGID="${PGID:-100}"   # Unraid default: 100 (users)
DATA_DIR="${FILEARR_AGENT_DATA_DIR:-/config}"
SCAN_ROOTS="${FILEARR_AGENT_SCAN_ROOTS:-}"
SCAN_INTERVAL="${FILEARR_AGENT_SCAN_INTERVAL:-6h}"
SCAN_ON_START="${FILEARR_AGENT_SCAN_ON_START:-true}"

export FILEARR_AGENT_DATA_DIR="$DATA_DIR"
export HOME="$DATA_DIR"

# Entrypoint lines go to the container log AND (when a log dir is set) to a
# file the web UI Logs tab can merge; the size guard caps the file at ~1 MiB
# (no lumberjack out here — it only ever accumulates a few lines per scan).
ENTRYPOINT_LOG=""
if [ -n "${FILEARR_AGENT_LOG_DIR:-}" ]; then
    ENTRYPOINT_LOG="$FILEARR_AGENT_LOG_DIR/filearr-agent-entrypoint.log"
    # Own the log dir as PUID even when the /config chown below is skipped
    # (existing install adding the dir): the agent processes run as PUID and
    # must be able to create their files here.
    mkdir -p "$FILEARR_AGENT_LOG_DIR"
    chown "$PUID:$PGID" "$FILEARR_AGENT_LOG_DIR" 2>/dev/null || true
fi
log() {
    line="$(date -u +%Y-%m-%dT%H:%M:%SZ) [entrypoint] $*"
    echo "$line"
    if [ -n "$ENTRYPOINT_LOG" ]; then
        if [ -f "$ENTRYPOINT_LOG" ] && [ "$(wc -c < "$ENTRYPOINT_LOG")" -gt 1048576 ]; then
            tail -c 524288 "$ENTRYPOINT_LOG" > "$ENTRYPOINT_LOG.tmp" && mv "$ENTRYPOINT_LOG.tmp" "$ENTRYPOINT_LOG"
        fi
        echo "$line" >> "$ENTRYPOINT_LOG" 2>/dev/null || true
    fi
}

mkdir -p "$DATA_DIR"
if [ "$(stat -c '%u:%g' "$DATA_DIR")" != "$PUID:$PGID" ]; then
    log "fixing ownership of $DATA_DIR -> $PUID:$PGID"
    chown -R "$PUID:$PGID" "$DATA_DIR"
fi

run_as() { su-exec "$PUID:$PGID" "$@"; }

# --------------------------------------------------------------------------- #
# First start: enroll. The token is SINGLE-USE — central consumes it on
# success, and every later start skips this block because the identity
# (state.json + key/cert) already sits in /config. Retries cover "central not
# up yet"; a genuinely bad/expired token keeps failing here, so read the
# error in the container log and mint a fresh token.
# --------------------------------------------------------------------------- #
if [ ! -f "$DATA_DIR/state.json" ]; then
    if [ -z "${FILEARR_AGENT_CENTRAL_URL:-}" ] || [ -z "${FILEARR_AGENT_TOKEN:-}" ]; then
        log "ERROR: first start needs FILEARR_AGENT_CENTRAL_URL and FILEARR_AGENT_TOKEN"
        log "       (mint a token in the Filearr console: Agents -> Mint enrollment token)"
        exit 1
    fi
    log "no identity in $DATA_DIR — enrolling against $FILEARR_AGENT_CENTRAL_URL"
    until run_as filearr-agent enroll; do
        log "enrollment failed; retrying in 30s (bad token? central down? see error above)"
        sleep 30
    done
    log "enrolled."
fi

# --------------------------------------------------------------------------- #
# Interval rescans. /mnt/user on Unraid is a FUSE (shfs) mount where inotify
# is unreliable, so watch mode is wrong there — a periodic full walk is the
# supported cadence (the walk is mtime+size cheap; unchanged files cost a
# stat). Roots are comma-separated; they merge into scan.json, so removing a
# root from the env does NOT remove it from scan.json — edit
# /config/scan.json for that (see docs).
# --------------------------------------------------------------------------- #
scan_once() {
    set --
    OLD_IFS=$IFS
    IFS=','
    for root in $SCAN_ROOTS; do
        IFS=$OLD_IFS
        [ -n "$root" ] && set -- "$@" -root "$root"
    done
    IFS=$OLD_IFS
    run_as filearr-agent scan "$@"
}

scan_loop() {
    if [ "$SCAN_ON_START" != "true" ]; then
        sleep "$SCAN_INTERVAL"
    fi
    while :; do
        log "scan starting (roots: ${SCAN_ROOTS:-from scan.json})"
        scan_once || log "scan failed (will retry next interval)"
        sleep "$SCAN_INTERVAL"
    done
}

if [ -z "$SCAN_ROOTS" ] && [ ! -f "$DATA_DIR/scan.json" ]; then
    log "WARNING: FILEARR_AGENT_SCAN_ROOTS is empty and no scan.json exists —"
    log "         nothing will be inventoried until you set it (e.g. /mnt/user)"
    SCAN_PID=""
else
    scan_loop &
    SCAN_PID=$!
fi

run_as filearr-agent run &
RUN_PID=$!

term() {
    log "shutting down"
    kill -TERM "$RUN_PID" ${SCAN_PID:+"$SCAN_PID"} 2>/dev/null
}
trap term TERM INT

wait "$RUN_PID"
CODE=$?
[ -n "${SCAN_PID:-}" ] && kill -TERM "$SCAN_PID" 2>/dev/null
wait 2>/dev/null
exit "$CODE"
