"""Outward base-URL derivation (filearr.urls.public_base_url) — live 2026-08-08:
behind the TLS-terminating Caddy the raw request.base_url said http://, so the
baked install.ps1 dialed a scheme/port that was not listening ("connection
actively refused"). Precedence: FILEARR_PUBLIC_BASE_URL -> X-Forwarded-* ->
request.base_url."""

from __future__ import annotations

from types import SimpleNamespace

from filearr.config import get_settings
from filearr.urls import public_base_url


def _req(headers: dict[str, str], base: str = "http://filearr.example.com/"):
    return SimpleNamespace(
        headers=headers,
        base_url=base,
        url=SimpleNamespace(netloc=base.split("//", 1)[1].rstrip("/")),
    )


async def test_configured_public_base_url_wins(monkeypatch):
    monkeypatch.setattr(
        get_settings(), "public_base_url", "https://filearr.example.com/"
    )
    req = _req({"x-forwarded-proto": "http", "host": "wrong.example"})
    assert public_base_url(req) == "https://filearr.example.com"


async def test_forwarded_headers_fix_the_scheme(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", None)
    # Caddy terminates TLS and forwards plain http to uvicorn: the request
    # says http://, the forwarded headers carry the truth.
    req = _req({"x-forwarded-proto": "https", "host": "filearr.example.com"})
    assert public_base_url(req) == "https://filearr.example.com"
    # X-Forwarded-Host (possibly a list) outranks Host when present.
    req = _req(
        {
            "x-forwarded-proto": "https, http",
            "x-forwarded-host": "outer.example.com, inner",
            "host": "filearr.example.com",
        }
    )
    assert public_base_url(req) == "https://outer.example.com"


async def test_direct_connection_uses_request_base_url(monkeypatch):
    monkeypatch.setattr(get_settings(), "public_base_url", None)
    req = _req({})  # no proxy involved (LAN-port access / dev)
    assert public_base_url(req) == "http://filearr.example.com"
    # Garbage in the proto header falls through rather than building nonsense.
    req = _req({"x-forwarded-proto": "gopher"})
    assert public_base_url(req) == "http://filearr.example.com"
