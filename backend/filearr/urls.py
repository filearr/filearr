"""Outward-facing base-URL derivation for links and scripts served to clients.

uvicorn runs WITHOUT ``--proxy-headers`` (globally trusting forwarded headers
would let direct LAN clients spoof the source IPs the audit log records), so
behind the TLS-terminating Caddy ``request.base_url`` says ``http://…`` — and
everything derived from it (the baked install.ps1/install.sh central URL, the
agent-dist manifest's artifact links, installer sidecar configs) dialed a
scheme/port that isn't even listening (live 2026-08-08: install-agent.ps1
"connection actively refused" on http://). This helper is the one place that
derivation happens, with honest precedence:

1. ``FILEARR_PUBLIC_BASE_URL`` — the operator's explicit statement of the
   outward URL; always wins when set.
2. ``X-Forwarded-Proto`` / ``X-Forwarded-Host`` — set by the bundled Caddy on
   every proxied request. Spoofed values are harmless HERE: the derived URLs
   are self-referential links served back to the same requester (never used
   for auth, redirects to third parties, or stored).
3. ``request.base_url`` — direct connections (LAN-port access, dev).
"""

from __future__ import annotations

from fastapi import Request


def public_base_url(request: Request) -> str:
    """The base URL clients should use to reach this instance (no trailing /)."""
    from filearr.config import get_settings

    configured = (get_settings().public_base_url or "").strip().rstrip("/")
    if configured:
        return configured
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    if proto in ("http", "https"):
        host = (
            (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
            or request.headers.get("host")
            or request.url.netloc
        )
        return f"{proto}://{host}"
    return str(request.base_url).rstrip("/")
