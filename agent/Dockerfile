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
# Build (from the repo root):
#   docker build -t filearr-agent --build-arg VERSION=v1.4.0 ./agent

FROM golang:1.26-alpine AS build
WORKDIR /src
COPY go.mod go.sum ./
RUN go mod download
COPY . .
ARG VERSION=0.0.0-docker
ARG UPDATE_PUBLIC_KEY=""
RUN CGO_ENABLED=0 go build -trimpath \
    -ldflags "-s -w -X main.Version=${VERSION} -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=${UPDATE_PUBLIC_KEY}" \
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
VOLUME /config

# The agent's only listener is a policy-gated LOOPBACK web UI (non-loopback
# binds are refused by design), so there is no EXPOSE: all traffic is
# outbound mTLS to central + step-ca.
HEALTHCHECK --interval=60s --timeout=5s --start-period=30s \
  CMD pgrep -f "filearr-agent run" >/dev/null || exit 1

ENTRYPOINT ["/entrypoint.sh"]
