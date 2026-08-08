# filearr-agent — standalone inventory agent container (Unraid-first, but any
# container host works).
#
# What this image is: the pure-Go agent binary plus a tiny entrypoint that
# (1) enrolls against central on first start (single-use token), (2) runs the
# replication daemon, and (3) rescans the mounted roots on an interval —
# because inotify is unreliable on FUSE mounts like Unraid's /mnt/user shfs,
# interval rescans are the correct mode there (same gotcha as central).
#
# Self-update is DELIBERATELY disabled in this image: no release-signing
# public key is pinned at build time, and an unpinned agent refuses every
# update manifest (fail-closed). Container hosts update by pulling a new
# image — Unraid's CA update flow — not by in-place binary swaps. Do not pass
# UPDATE_PUBLIC_KEY unless you know you want in-container self-updates (then
# your restart policy must relaunch on exit code 20 to complete a swap).
#
# Build (from the repo root; agent/VERSION is the canonical version):
#   docker build -t filearr-agent --build-arg VERSION="$(cat agent/VERSION)" ./agent

FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG VERSION=0.0.0-docker
ARG UPDATE_PUBLIC_KEY=""
# CI passes VERSION=<agent/VERSION>@<full-sha>; shorten the sha to 7 chars and
# join with '-' (e.g. "1.5.0-1a2b3c4") — the SAME stamp shape as the root
# Dockerfile's agentdist stage and build_windows_agent.ps1, so every build
# path reports comparably. The old "<ref>@<sha7> (<date>)" stamp contained a
# space + '@' that central's _SAFE_VERSION rejects, and a branch name that
# orders against nothing (fleet showed "main" / "1.4.0" / "0.0.0-dev"
# simultaneously — live report 2026-08-07). Build date lives in the OCI image
# labels (docker inspect), not the version string.
RUN V="${VERSION}"; case "$V" in *@*) V="${V%%@*}-$(echo "${V#*@}" | cut -c1-7)";; esac; \
    CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X \"main.Version=${V}\" -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=${UPDATE_PUBLIC_KEY}" \
    -o /out/filearr-agent ./cmd/filearr-agent

FROM alpine:3.22
# ffmpeg: optional at runtime (only video poster-frame thumbnails need it) but
# cheap to ship and exactly what a media-heavy Unraid box wants. tzdata: honor
# TZ. su-exec: drop root to PUID/PGID after fixing /config ownership.
RUN apk add --no-cache ca-certificates tzdata ffmpeg su-exec

COPY --from=build /out/filearr-agent /usr/local/bin/filearr-agent
COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

# Everything the agent persists (identity key/cert, SQLite index + outbox,
# scan.json, caches) lives under one dir — bind it to appdata.
ENV FILEARR_AGENT_DATA_DIR=/config
# The image is immutable: updating = pulling a new image, so the in-binary
# self-update subsystem is switched OFF here (also silences the unpinned-key
# warning, which is correct-but-alarming in a container). Override at runtime
# if you truly pinned a key at build time.
ENV FILEARR_AGENT_SELF_UPDATE=false
# Shared log dir: every agent process (daemon, scans, entrypoint) writes its
# own rotating file here, and the web UI Logs tab merges them — that is the
# only way scan-process lines reach the browser (the in-process ring can't
# cross processes). LOG_STDERR keeps `docker logs` carrying every line too.
ENV FILEARR_AGENT_LOG_DIR=/config/logs
ENV FILEARR_AGENT_LOG_STDERR=true
VOLUME /config

# The agent's only listener is a policy-gated LOOPBACK web UI (non-loopback
# binds are refused by design), so there is no EXPOSE: all traffic is
# outbound mTLS to central + step-ca.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD pgrep -f "filearr-agent run" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
