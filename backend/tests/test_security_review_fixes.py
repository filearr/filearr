"""Regression tests for the 2026-08-20 security review fixes (pure/unit level).

The DB-backed auth-path fixes (LLM keys refused on the main API, the transfer
SSE 401, the LLM run_report / where_is scope confinement) are exercised in the
facade/transfer integration suites; this file pins the pure helpers so a future
edit that reintroduces a bypass fails fast without a Postgres.
"""

from __future__ import annotations

from filearr.api.oidc import _safe_return_to
from filearr.api.search import meili_quote


# --- OIDC open-redirect guard (finding 8) ----------------------------------- #
def test_safe_return_to_blocks_backslash_and_protocol_relative():
    # Local paths pass through unchanged.
    assert _safe_return_to("/admin") == "/admin"
    assert _safe_return_to("/search?q=a:b") == "/search?q=a:b"  # in-path colon ok
    assert _safe_return_to("/") == "/"
    # Off-site redirect vectors all collapse to "/".
    assert _safe_return_to("//evil.example") == "/"
    assert _safe_return_to("/\\evil.example") == "/"  # browsers normalise \ -> /
    assert _safe_return_to("\\\\evil.example") == "/"
    assert _safe_return_to("https://evil.example") == "/"
    assert _safe_return_to("/ok\r\nSet-Cookie: x") == "/"  # control char refused
    assert _safe_return_to(None) == "/"
    assert _safe_return_to("") == "/"


# --- Meili filter-literal escaping (findings 1 & 4) ------------------------- #
def test_meili_quote_escapes_backslash_then_quote():
    assert meili_quote("plain") == "plain"
    assert meili_quote("it's") == "it\\'s"
    # Backslash escaped FIRST so it cannot neutralise the quote escape.
    assert meili_quote("a\\b") == "a\\\\b"
    assert meili_quote("x'; y") == "x\\'; y"
    # The bypass payload becomes an inert literal (no un-escaped quote survives).
    escaped = meili_quote("active' OR status = 'active")
    assert "' OR" not in escaped.replace("\\'", "")
