"""Client-IP resolution behind a reverse proxy.

Filearr normally sits behind the Caddy TLS sidecar (or an operator's own nginx /
Traefik), so ``request.client.host`` is the PROXY's address — every security
event, session row and rate-limit bucket would otherwise record the container
IP instead of the user's. ``X-Forwarded-For`` carries the real address, but it
is client-supplied and MUST NOT be trusted blindly: the app port (8484) is also
published directly, so an attacker hitting it could forge the header to dodge
the per-IP brute-force bucket or plant a fake address in the audit log.

The header is therefore honoured only when the request demonstrably transited a
trusted proxy, by any ONE of:

1. ``X-Filearr-Proxy-Trust`` matches ``FILEARR_PROXY_SHARED_SECRET`` — the
   shipped Caddyfiles stamp it on every ``reverse_proxy`` block, so the managed
   deployments (compose / Proxmox / Unraid full tier) get it for free with no
   IP bookkeeping. A distinct header from ``X-Filearr-Proxy-Auth`` on purpose:
   that one switches the AGENT plane into mTLS-header auth and stamping it on
   the web vhost would break bearer agents in ``both`` mode.
2. the socket peer is inside ``FILEARR_TRUSTED_PROXIES`` (comma-separated IPs /
   CIDRs) — for third-party proxies. The chain is walked from the RIGHT,
   skipping trusted hops, and the first untrusted address is the client
   (the standard rightmost-untrusted algorithm; immune to a client prepending
   junk).
3. the legacy ``FILEARR_AUTH_RATELIMIT_TRUST_FORWARDED_FOR=true`` — trusts the
   LEFTMOST entry unconditionally. Kept for deployments that set it; the two
   options above are preferred.
"""

from __future__ import annotations

import ipaddress
import secrets
from functools import lru_cache

from fastapi import Request

from filearr.config import get_settings

HDR_PROXY_TRUST = "x-filearr-proxy-trust"

_Net = ipaddress.IPv4Network | ipaddress.IPv6Network


@lru_cache(maxsize=8)
def _parse_networks(raw: str) -> tuple[_Net, ...]:
    out: list[_Net] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            # A bad entry must never widen trust; drop it (config validator
            # already rejects these at startup — this is the belt).
            continue
    return tuple(out)


def _ip(value: str) -> ipaddress.IPv4Address | ipaddress.IPv6Address | None:
    value = value.strip()
    if value.startswith("[") and value.endswith("]"):
        value = value[1:-1]
    try:
        return ipaddress.ip_address(value)
    except ValueError:
        return None


def peer_is_trusted(peer: str | None, networks: tuple[_Net, ...]) -> bool:
    if not peer or not networks:
        return False
    addr = _ip(peer)
    if addr is None:
        return False
    return any(addr in n for n in networks)


def _proxy_header_ok(request: Request) -> bool:
    secret = get_settings().proxy_shared_secret or ""
    provided = request.headers.get(HDR_PROXY_TRUST) or ""
    # Compare as BYTES: Starlette decodes headers as latin-1, so a byte > 0x7F
    # yields a non-ASCII str and secrets.compare_digest raises TypeError on it
    # (→ an unhandled 500 instead of a clean reject). Encoding first keeps the
    # comparison constant-time and makes a non-matching header return False.
    return (
        bool(secret)
        and bool(provided)
        and secrets.compare_digest(provided.encode("utf-8"), secret.encode("utf-8"))
    )


def forwarded_client(xff: str, *, trusted: tuple[_Net, ...]) -> str | None:
    """Rightmost-untrusted walk over an ``X-Forwarded-For`` chain. With no
    trusted networks the rightmost entry is taken (it is the one the trusted
    proxy that brought us here appended — the proxy is trusted by the header
    check, not by address)."""
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if not hops:
        return None
    if not trusted:
        return hops[-1]
    for hop in reversed(hops):
        if not peer_is_trusted(hop, trusted):
            return hop
    # Every hop is a trusted proxy — the leftmost is the best we have.
    return hops[0]


def client_ip(request: Request, *, trust_forwarded: bool | None = None) -> str | None:
    """The caller's source IP (see module docstring for the trust rules).
    ``trust_forwarded`` forces the legacy unconditional-leftmost behaviour on/off
    (tests and the rate limiter's explicit callers)."""
    settings = get_settings()
    peer = request.client.host if request.client else None
    xff = request.headers.get("x-forwarded-for") or ""

    legacy = (
        settings.auth_ratelimit_trust_forwarded_for if trust_forwarded is None else trust_forwarded
    )
    if legacy:
        first = xff.split(",")[0].strip() if xff else ""
        return first or peer

    if not xff:
        return peer
    networks = _parse_networks(settings.trusted_proxies or "")
    if _proxy_header_ok(request):
        # Our own proxy vouched for the request. It appended the peer it saw
        # (the real client, or an upstream hop we were told to trust).
        return forwarded_client(xff, trusted=networks) or peer
    if peer_is_trusted(peer, networks):
        return forwarded_client(xff, trusted=networks) or peer
    return peer
