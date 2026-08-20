"""Webhook SSRF guard (Phase 8, brief §7.1).

LIVE: ``alerts.dispatch.send_webhook`` calls :func:`check_webhook_url` before
every POST, so this is an enforced control, not a plan. (This header said "inert
scaffolding" until 2026-08-10.) A webhook channel lets an admin supply an
arbitrary URL that Filearr's **own server** will POST to — a textbook SSRF
surface (brief §7.1). This module is the pure classification + verdict core:

* :func:`classify_ip` — bucket an IP literal (v4 or v6, including IPv4-mapped
  IPv6 and IPv6 ULA) into an :class:`IpClass`.
* :func:`check_webhook_url` — parse a URL, **resolve then validate** every A/AAAA
  record via an **injectable resolver**, and deny non-public targets unless the
  operator opts in (R5).

The resolver is injected so tests run with a fake (no network) and the
implementing task (P8-T2) wires a real one that re-validates the IP **at
socket-connect time**, closing the DNS-rebinding TOCTOU gap the brief calls out.
Two distinct opt-ins, deliberately different in blast radius:

* ``allow_private`` (``FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS``, R5) is the blunt
  switch: it flips **only** the ``private`` class (RFC 1918 / ULA LAN ranges).
  Loopback, link-local (cloud metadata 169.254.169.254 / fd00:ec2::254),
  reserved and unspecified stay denied under it.
* ``allowed_cidrs`` (``FILEARR_WEBHOOK_ALLOWED_CIDRS``, 2026-08-19) is the finer
  **explicit per-target allowlist**: an IP matching a listed CIDR is admitted
  regardless of class — *including* loopback and link-local — with the sole
  exception of the unspecified address (0.0.0.0 / ::), which never passes. This
  is intentional (an operator's "my ntfy on 127.0.0.1:8080" needs it) and is
  the behaviour enforced in :func:`check_webhook_url`.

  **Footgun:** because it admits any listed class, adding a link-local range
  such as ``169.254.0.0/16`` re-opens the cloud-metadata SSRF this guard exists
  to stop. List the narrowest CIDR you actually need (``127.0.0.1/32``, a
  specific ``/32`` on the LAN), never a broad link-local or metadata range.
"""

from __future__ import annotations

import ipaddress
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from urllib.parse import urlsplit

# A resolver maps a hostname to the list of IP strings it resolves to (all A and
# AAAA records). Injected for testability; the real one uses the stdlib/DNS.
Resolver = Callable[[str], list[str]]

_ALLOWED_SCHEMES = frozenset({"http", "https"})


class IpClass(Enum):
    """SSRF-relevant buckets for a resolved IP."""

    LOOPBACK = "loopback"          # 127.0.0.0/8, ::1
    LINK_LOCAL = "link_local"      # 169.254.0.0/16 (incl. metadata), fe80::/10
    PRIVATE = "private"            # RFC1918, IPv6 ULA fc00::/7
    RESERVED = "reserved"          # multicast, reserved, otherwise-not-global
    UNSPECIFIED = "unspecified"    # 0.0.0.0, ::
    PUBLIC = "public"              # routable, safe to dial


# The classes an outbound webhook may target. Everything else is denied.
_PUBLIC_OK: frozenset[IpClass] = frozenset({IpClass.PUBLIC})
# ...plus this one when the operator sets FILEARR_WEBHOOK_ALLOW_PRIVATE_CIDRS.
_PRIVATE_OK: frozenset[IpClass] = frozenset({IpClass.PUBLIC, IpClass.PRIVATE})


def classify_ip(ip: str) -> IpClass:
    """Classify an IP literal into an :class:`IpClass`.

    Handles IPv4-mapped IPv6 (``::ffff:10.0.0.1`` classifies as the embedded v4
    address, defeating a mapped-address bypass) and IPv6 ULA (``fc00::/7`` →
    ``PRIVATE``). Order matters: the most-specific dangerous buckets
    (unspecified/loopback/link-local) are checked before the broad ``is_private``
    test, which itself covers RFC1918 + ULA.
    """
    addr = ipaddress.ip_address(ip)
    # Unwrap IPv4-mapped IPv6 so ::ffff:10.x is judged as 10.x, not "public v6".
    mapped = getattr(addr, "ipv4_mapped", None)
    if mapped is not None:
        addr = mapped

    if addr.is_unspecified:
        return IpClass.UNSPECIFIED
    if addr.is_loopback:
        return IpClass.LOOPBACK
    if addr.is_link_local:
        return IpClass.LINK_LOCAL
    if addr.is_multicast or addr.is_reserved:
        return IpClass.RESERVED
    if addr.is_private:
        return IpClass.PRIVATE
    return IpClass.PUBLIC


@dataclass(frozen=True)
class UrlVerdict:
    """Result of vetting a webhook URL.

    ``allowed`` is the go/no-go; ``reason`` is a short, non-sensitive tag safe to
    log/surface; ``resolved`` records every ``(ip, class)`` considered (empty for
    a pre-DNS rejection like a bad scheme).
    """

    allowed: bool
    reason: str
    resolved: tuple[tuple[str, IpClass], ...] = ()


def parse_allowed_cidrs(
    raw: str | None,
) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """``FILEARR_WEBHOOK_ALLOWED_CIDRS`` ("10.0.0.5/32, 192.168.1.0/24") -> networks.
    Malformed entries are dropped (config validation already rejects them)."""
    out = []
    for part in (raw or "").split(","):
        part = part.strip()
        if not part:
            continue
        try:
            out.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            continue
    return tuple(out)


def _in_allowlist(ip: str, nets) -> bool:
    if not nets:
        return False
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in n for n in nets)


def check_webhook_url(
    url: str,
    resolver: Resolver,
    allow_private: bool = False,
    allowed_cidrs=(),
) -> UrlVerdict:
    """Vet ``url`` for outbound webhook dispatch (resolve-then-validate, §7.1).

    Steps: scheme must be http/https; host must be present; port (if any) must be
    1..65535; resolve the host via ``resolver`` (IP literals skip DNS and are
    classified directly); require **at least one** record; then require **every**
    resolved IP to be acceptable — a single disallowed record fails the whole URL
    (this is what defeats a rebinding record set mixing a public and a private
    answer). ``allow_private`` widens the allow-set to include ``PRIVATE`` only.
    """
    parts = urlsplit(url)
    scheme = parts.scheme.lower()
    if scheme not in _ALLOWED_SCHEMES:
        return UrlVerdict(False, "scheme-not-allowed")

    host = parts.hostname
    if not host:
        return UrlVerdict(False, "no-host")

    try:
        port = parts.port  # raises ValueError on a malformed port
    except ValueError:
        return UrlVerdict(False, "bad-port")
    if port is not None and not (1 <= port <= 65535):
        return UrlVerdict(False, "bad-port")

    # An IP literal host needs no DNS; a name is resolved via the injected hook.
    try:
        ipaddress.ip_address(host)
        ips = [host]
    except ValueError:
        ips = resolver(host)

    if not ips:
        return UrlVerdict(False, "no-dns-records")

    allowed_classes = _PRIVATE_OK if allow_private else _PUBLIC_OK
    resolved = tuple((ip, classify_ip(ip)) for ip in ips)
    for ip, cls in resolved:
        if cls in allowed_classes:
            continue
        # Roadmap §6 polish (2026-08-19): an explicit operator CIDR allowlist
        # (FILEARR_WEBHOOK_ALLOWED_CIDRS) admits a specific target regardless of
        # class -- "my ntfy on 127.0.0.1:8080" / "10.0.0.5/32" -- without the
        # blunt private-class switch. Unspecified (0.0.0.0 / ::) never passes.
        if cls is not IpClass.UNSPECIFIED and _in_allowlist(ip, allowed_cidrs):
            continue
        return UrlVerdict(False, f"blocked:{cls.value}", resolved)
    return UrlVerdict(True, "ok", resolved)
