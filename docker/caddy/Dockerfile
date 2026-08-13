# Filearr — custom Caddy image (OPS-T1 acme-dns rework / P5-T6 agent-plane mTLS).
#
# The stock caddy:2.11.4 image has neither the Cloudflare DNS-01 solver nor the
# layer4 (raw-TCP/SNI) app, both of which this deploy needs:
#   * caddy-dns/cloudflare — wildcard cert for *.<domain> via DNS-01 (no inbound
#     :80 / HTTP-01; works behind NAT, no public reachability on 80 required).
#   * mholt/caddy-l4        — SNI-based L4 passthrough so the CA hostname's TLS is
#     forwarded RAW to step-ca (it keeps its own TLS; mTLS /renew must NOT be
#     L7-terminated — docs/ops/agents.md §7.4). Coexists with the HTTP app on 443
#     via the `layer4` listener wrapper (see Caddyfile.acme).
#
# Pins (re-verify BOTH plugin versions AND the caddy CVE list on any bump, same
# discipline as the compose caddy/meili/step-ca pins):
#   caddy                        v2.11.4   (matches the runtime base image below)
#   github.com/caddy-dns/cloudflare  v0.2.4   (proxy.golang.org @latest 2026-07-17)
#   github.com/mholt/caddy-l4        v0.1.2   (proxy.golang.org @latest 2026-07-17)
FROM caddy:2.11.4-builder AS builder
RUN xcaddy build v2.11.4 \
    --with github.com/caddy-dns/cloudflare@v0.2.4 \
    --with github.com/mholt/caddy-l4@v0.1.2

FROM caddy:2.11.4
COPY --from=builder /usr/bin/caddy /usr/bin/caddy

# UR-T1 (2026-08-12): this image is now PUBLISHED as ghcr.io/pwsh/filearr-caddy
# so an Unraid install can reach mTLS parity without a compose file. compose
# still BUILDS it locally and bind-mounts ./docker/caddy over /etc/caddy — the
# local build stays the source of truth for the Proxmox path — so everything
# below has to be inert under compose. It is: the bind mount shadows the copied
# Caddyfiles with the identical files, and the CMD is overridden by the service's
# own `command:`.
#
# Both profiles are baked because a template-only host has nowhere else to put a
# config file. See entrypoint.sh for the FILEARR_CADDY_PROFILE selection and why
# it is CMD rather than ENTRYPOINT.
#
# The build context is the REPO ROOT (docker-compose.yml sets `context: .` with
# `dockerfile: docker/caddy/Dockerfile`, and build.yml mirrors that), so these
# paths are repo-relative, not Dockerfile-relative.
COPY docker/caddy/Caddyfile.acme docker/caddy/Caddyfile.internal /etc/caddy/
COPY docker/caddy/entrypoint.sh /usr/local/bin/filearr-caddy-entrypoint.sh
RUN chmod +x /usr/local/bin/filearr-caddy-entrypoint.sh

# No ENTRYPOINT on purpose (upstream caddy sets none either): a `command:`
# override in compose must bypass the profile selector entirely.
CMD ["/usr/local/bin/filearr-caddy-entrypoint.sh"]
