"""Client-IP resolution behind a reverse proxy (filearr/proxy_trust.py).

Live finding 2026-08-19: the security audit showed the Caddy container's IP for
every login. X-Forwarded-For is honoured only via a provable path — the Caddy
X-Filearr-Proxy-Trust header, a trusted-proxy CIDR list, or the legacy flag."""

from __future__ import annotations

import pytest
from pydantic import ValidationError
from starlette.requests import Request

from filearr import proxy_trust
from filearr.config import Settings, get_settings


def _req(peer: str, headers: dict[str, str] | None = None) -> Request:
    raw = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    scope = {
        "type": "http",
        "method": "GET",
        "path": "/",
        "headers": raw,
        "client": (peer, 12345),
        "query_string": b"",
    }
    return Request(scope)


@pytest.fixture
def settings(monkeypatch):
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_ratelimit_trust_forwarded_for", False)
    monkeypatch.setattr(s, "trusted_proxies", None)
    monkeypatch.setattr(s, "proxy_shared_secret", None)
    proxy_trust._parse_networks.cache_clear()
    yield s
    proxy_trust._parse_networks.cache_clear()


def test_untrusted_peer_ignores_xff(settings):
    r = _req("172.18.0.5", {"X-Forwarded-For": "203.0.113.9"})
    assert proxy_trust.client_ip(r) == "172.18.0.5"


def test_no_header_returns_peer(settings):
    assert proxy_trust.client_ip(_req("192.168.1.20")) == "192.168.1.20"


def test_proxy_trust_header_unlocks_xff(settings, monkeypatch):
    monkeypatch.setattr(settings, "proxy_shared_secret", "s3cret")
    r = _req(
        "172.18.0.5",
        {"X-Forwarded-For": "203.0.113.9", "X-Filearr-Proxy-Trust": "s3cret"},
    )
    assert proxy_trust.client_ip(r) == "203.0.113.9"


def test_proxy_trust_header_wrong_secret_is_ignored(settings, monkeypatch):
    monkeypatch.setattr(settings, "proxy_shared_secret", "s3cret")
    r = _req(
        "172.18.0.5",
        {"X-Forwarded-For": "203.0.113.9", "X-Filearr-Proxy-Trust": "nope"},
    )
    assert proxy_trust.client_ip(r) == "172.18.0.5"


def test_proxy_trust_header_with_unset_secret_never_trusts(settings):
    # Empty configured secret + empty header must NOT compare equal.
    r = _req("172.18.0.5", {"X-Forwarded-For": "203.0.113.9", "X-Filearr-Proxy-Trust": ""})
    assert proxy_trust.client_ip(r) == "172.18.0.5"


def test_proxy_trust_takes_rightmost_hop(settings, monkeypatch):
    # A client that prepends junk can't win: Caddy appends the peer it saw LAST.
    monkeypatch.setattr(settings, "proxy_shared_secret", "s3cret")
    r = _req(
        "172.18.0.5",
        {
            "X-Forwarded-For": "1.2.3.4, 203.0.113.9",
            "X-Filearr-Proxy-Trust": "s3cret",
        },
    )
    assert proxy_trust.client_ip(r) == "203.0.113.9"


def test_trusted_proxies_cidr_walks_rightmost_untrusted(settings, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "172.18.0.0/16, 10.0.0.5")
    r = _req(
        "172.18.0.5",
        {"X-Forwarded-For": "198.51.100.7, 10.0.0.5, 172.18.0.2"},
    )
    # 172.18.0.2 and 10.0.0.5 are trusted hops → the client is 198.51.100.7.
    assert proxy_trust.client_ip(r) == "198.51.100.7"


def test_trusted_proxies_peer_outside_list_is_not_trusted(settings, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "172.18.0.0/16")
    r = _req("192.168.1.20", {"X-Forwarded-For": "203.0.113.9"})
    assert proxy_trust.client_ip(r) == "192.168.1.20"


def test_trusted_proxies_all_hops_trusted_falls_back_to_leftmost(settings, monkeypatch):
    monkeypatch.setattr(settings, "trusted_proxies", "172.18.0.0/16")
    r = _req("172.18.0.5", {"X-Forwarded-For": "172.18.0.7"})
    assert proxy_trust.client_ip(r) == "172.18.0.7"


def test_legacy_flag_takes_leftmost(settings, monkeypatch):
    monkeypatch.setattr(settings, "auth_ratelimit_trust_forwarded_for", True)
    r = _req("172.18.0.5", {"X-Forwarded-For": "203.0.113.9, 10.0.0.1"})
    assert proxy_trust.client_ip(r) == "203.0.113.9"


def test_ipv6_bracketed_hop(settings, monkeypatch):
    monkeypatch.setattr(settings, "proxy_shared_secret", "s3cret")
    r = _req(
        "172.18.0.5",
        {"X-Forwarded-For": "[2001:db8::1]", "X-Filearr-Proxy-Trust": "s3cret"},
    )
    assert proxy_trust.client_ip(r) == "[2001:db8::1]"


def test_ratelimit_client_ip_delegates(settings, monkeypatch):
    from filearr import ratelimit

    monkeypatch.setattr(settings, "proxy_shared_secret", "s3cret")
    r = _req(
        "172.18.0.5",
        {"X-Forwarded-For": "203.0.113.9", "X-Filearr-Proxy-Trust": "s3cret"},
    )
    assert ratelimit.client_ip(r) == "203.0.113.9"


def test_settings_validator_rejects_garbage(monkeypatch):
    monkeypatch.setenv("FILEARR_TRUSTED_PROXIES", "not-an-ip")
    with pytest.raises(ValidationError):
        Settings()


def test_settings_validator_accepts_cidrs(monkeypatch):
    monkeypatch.setenv("FILEARR_TRUSTED_PROXIES", "10.0.0.1, 172.18.0.0/16,fd00::/8")
    assert Settings().trusted_proxies == "10.0.0.1, 172.18.0.0/16,fd00::/8"
