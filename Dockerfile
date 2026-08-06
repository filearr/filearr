# ---- frontend build ----
FROM node:24-slim AS frontend
WORKDIR /build
COPY frontend/package.json frontend/package-lock.json* ./
RUN npm install
COPY frontend/ .
RUN npm run build

# ---- agent distribution build ----
# Cross-compile the Go agent for every supported install target so central can
# serve first-install binaries itself (/api/v1/agent-dist — no GitHub, no ghcr
# needed on the operator's LAN). Static (CGO off), same -trimpath/-ldflags as
# agent/Dockerfile; VERSION/UPDATE_PUBLIC_KEY mirror that image's build args.
# Go cross-compilation is host-arch-independent, so this stage is pinned to the
# BUILD platform: on a multi-arch buildx run it compiles once natively instead
# of once more under qemu (the output binaries are identical either way).
FROM --platform=$BUILDPLATFORM golang:1.26-alpine AS agentdist
WORKDIR /src
COPY agent/go.mod agent/go.sum ./
RUN go mod download
COPY agent/ .
ARG VERSION=0.0.0-dev
ARG UPDATE_PUBLIC_KEY=
RUN set -eu; \
    V="${VERSION%%@*}"; SHA="${VERSION#*@}"; \
    if [ "$SHA" != "$VERSION" ]; then V="$V ($(echo "$SHA" | cut -c1-7))"; fi; \
    mkdir -p /out; \
    for target in windows/amd64 linux/amd64 linux/arm64 darwin/amd64 darwin/arm64; do \
      GOOS="${target%/*}"; GOARCH="${target#*/}"; ext=""; \
      [ "$GOOS" = "windows" ] && ext=".exe"; \
      CGO_ENABLED=0 GOOS="$GOOS" GOARCH="$GOARCH" go build -trimpath \
        -ldflags "-s -w -X \"main.Version=$V\" -X \"github.com/filearr/filearr/agent/internal/update.PublicKeyBase64=$UPDATE_PUBLIC_KEY\"" \
        -o "/out/filearr-agent-$GOOS-$GOARCH$ext" ./cmd/filearr-agent; \
    done; \
    printf '%s\n' "$V" > /out/VERSION

# ---- backend runtime ----
# python:3.14.6-slim: every pinned C-extension dep ships cp314 wheels
# (verified against PyPI 2026-07-27); patch-pinned like the rest of the stack.
FROM python:3.14.6-slim AS runtime
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

# libmagic + libmediainfo for python-magic / pymediainfo; ffmpeg provides ffprobe.
# P3-T6 OCR: tesseract-ocr (engine) + poppler-utils (pdftoppm, scanned-PDF raster).
# P3-T11 EXIF/GPS: libimage-exiftool-perl (the real exiftool binary; subprocess
# only, never linked in-process). These binaries are runtime-only — the OCR pass is
# per-library opt-in (default OFF) and the EXIF pass runs for images.
# P12-T5 PDF thumbnails: NO apt package needed -- the pypdfium2 wheel bundles
# libpdfium (the PDFium C library) inside the wheel, so page-1 render works with
# the pip install alone (deliberately unlike libvips/poppler, which would each add
# an apt dependency + an independent CVE surface).
RUN apt-get update && apt-get install -y --no-install-recommends \
      libmagic1 libmediainfo0v5 ffmpeg \
      tesseract-ocr poppler-utils libimage-exiftool-perl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY backend/pyproject.toml ./
RUN uv pip install --system -r pyproject.toml
COPY backend/ .
COPY --from=frontend /build/dist ./static
# First-install agent binaries served by /api/v1/agent-dist (FILEARR_AGENT_DIST_DIR
# overrides; the API 404s gracefully when the directory is absent in dev).
COPY --from=agentdist /out ./agent-dist

ENV PYTHONUNBUFFERED=1 PYTHONPATH=/app
EXPOSE 8000
# Entrypoint auto-runs the idempotent scripts/init_db.py bootstrap for the APP
# command only (waits for Postgres; FILEARR_AUTO_INIT_DB=false opts out) — an
# Unraid/compose first start needs no manual console step. Worker/watcher
# command overrides skip it and pass straight through to exec.
RUN chmod +x scripts/docker-entrypoint.sh
ENTRYPOINT ["/app/scripts/docker-entrypoint.sh"]
# app:   uvicorn filearr.main:app
# worker: procrastinate --app=filearr.worker.proc_app worker
CMD ["uvicorn", "filearr.main:app", "--host", "0.0.0.0", "--port", "8000"]
