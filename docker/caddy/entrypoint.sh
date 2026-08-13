#!/bin/sh
# filearr-caddy default command (UR-T1, 2026-08-12).
#
# Why this exists: the compose stack MOUNTS ./docker/caddy over /etc/caddy and
# picks its config in the service `command:`, so it never needs this script. The
# PUBLISHED image (ghcr.io/pwsh/filearr-caddy, used by unraid/filearr-caddy.xml)
# has no bind mount and no compose file to hold that logic — an Unraid template
# is env vars and nothing else. So both Caddyfiles are baked into the image and
# selected here by FILEARR_CADDY_PROFILE.
#
#   internal (DEFAULT) — Caddy's built-in self-signed CA. Needs no DNS
#                        credentials and no public DNS, so it is the profile
#                        that cannot fail on a fresh install.
#   acme               — Let's Encrypt wildcard via Cloudflare DNS-01, PLUS the
#                        agents.<domain> mTLS site and the ca.<domain> raw-L4
#                        passthrough. Required for
#                        FILEARR_AGENT_AUTH_MODE=mtls-header.
#
# This is wired as CMD, NOT ENTRYPOINT, and that is deliberate: docker-compose.yml
# overrides `command:` with a full `caddy run ...` line, which would otherwise be
# handed to this script as arguments. Leaving ENTRYPOINT unset (as the upstream
# caddy image does) means a `command:` override bypasses this file entirely and
# the compose path is provably untouched.
set -e

profile="${FILEARR_CADDY_PROFILE:-internal}"
case "$profile" in
  internal|acme) ;;
  *)
    echo "filearr-caddy: FILEARR_CADDY_PROFILE must be 'internal' or 'acme' (got '${profile}')" >&2
    exit 64
    ;;
esac

config="/etc/caddy/Caddyfile.${profile}"
[ -f "$config" ] || { echo "filearr-caddy: missing ${config}" >&2; exit 66; }

# Fail fast and legibly rather than crash-looping on a half-filled template.
# Caddy substitutes {$VAR} at adapt time and an unset var becomes the EMPTY
# STRING, so an acme profile without a domain silently adapts to a site address
# of "*." and then misbehaves at request time instead of at boot.
if [ "$profile" = "acme" ]; then
  missing=""
  [ -n "${FILEARR_TLS_DOMAIN:-}" ] || missing="$missing FILEARR_TLS_DOMAIN"
  [ -n "${CLOUDFLARE_API_TOKEN:-}" ] || missing="$missing CLOUDFLARE_API_TOKEN"
  [ -n "${FILEARR_ACME_EMAIL:-}" ] || missing="$missing FILEARR_ACME_EMAIL"
  if [ -n "$missing" ]; then
    echo "filearr-caddy: profile 'acme' needs:${missing}" >&2
    exit 78
  fi
  # Not fatal — a deployment can run the acme profile purely for public TLS and
  # leave the agent plane unused — but mtls-header fails CLOSED without it, and
  # an empty header value is far harder to diagnose than this line.
  if [ -z "${FILEARR_PROXY_SHARED_SECRET:-}" ]; then
    echo "filearr-caddy: WARNING FILEARR_PROXY_SHARED_SECRET is empty — the agents.${FILEARR_TLS_DOMAIN} site will forward a blank X-Filearr-Proxy-Auth and every mtls-header agent request will be rejected" >&2
  fi
fi

echo "filearr-caddy: profile=${profile} config=${config}" >&2
exec caddy run --adapter caddyfile --config "$config"
