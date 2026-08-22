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

# Build stage runs on the BUILD host's arch and cross-compiles (CGO is off, so
# GOOS/GOARCH is all Go needs). Without --platform=$BUILDPLATFORM the arm64
# half of the multi-arch CI build compiled under QEMU emulation, ~10 minutes
# per push (measured 2026-08-16). Same shape as the root Dockerfile's agentdist
# stage.
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS build
ARG TARGETOS
ARG TARGETARCH
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
    CGO_ENABLED=0 GOOS="$TARGETOS" GOARCH="$TARGETARCH" go build -trimpath \
    -ldflags "-s -w -X \"main.Version=${V}\" -X github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=${UPDATE_PUBLIC_KEY}" \
    -o /out/filearr-agent ./cmd/filearr-agent

FROM alpine:3.22
# ffmpeg: optional at runtime (video poster-frame thumbnails, and ffprobe backs
# the media technical probe in the extraction pass) but cheap to ship and exactly
# what a media-heavy Unraid box wants. poppler-utils: pdfinfo/pdftotext/pdftoppm —
# PDF properties, PDF body text, and page rasterisation; PDFs are the single most
# common document type and their text is what makes semantic search and RAG
# chunking meaningful for agent libraries. exiftool: curated camera/lens/exposure/
# GPS metadata for photo libraries. tesseract-ocr + tesseract-ocr-data-eng: OCR of
# images and — together with pdftoppm above — of scanned PDFs. tzdata: honor TZ.
# su-exec: drop root to PUID/PGID after fixing /config ownership.
#
# The policy here is COMPLETENESS: the container agent ships every tool the
# extraction pass can use, so a containerized host never has a capability gap and
# an operator never has to build a derived image to get one. exiftool (perl) and
# tesseract (language data) are the bulky ones and that cost is accepted, because
# a partially-capable agent is the more expensive problem — it makes policy
# settings look applied when they are not. None of these run unless a policy
# enables extraction, so the only price on an unused install is image size.
#
# Extra OCR LANGUAGES are still a derived image (the -data-* packages are per
# language and shipping them all would be gratuitous):
#   FROM ghcr.io/filearr/filearr-agent:latest
#   RUN apk add --no-cache tesseract-ocr-data-deu
# The console's per-agent capability matrix shows which tools each host actually
# resolved, so a missing one is visible rather than mysterious.
RUN apk add --no-cache ca-certificates tzdata ffmpeg poppler-utils exiftool \
    tesseract-ocr tesseract-ocr-data-eng su-exec

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
# Advertised to central as the `container` capability: the console then FLAGS
# a newer build ("pull the new image") instead of offering the self-update
# swap. Set =0 (with SELF_UPDATE=true) if you deliberately want in-container
# binary swaps — your restart policy must relaunch on exit code 20.
ENV FILEARR_AGENT_CONTAINER=1
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
