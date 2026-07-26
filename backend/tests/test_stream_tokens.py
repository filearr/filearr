"""Unit tests for the single-use SSE stream-token store (roadmap §14 —
`filearr.streamtokens`): scoping, single-use consumption, expiry, and the
opportunistic sweep bound."""

import time

from filearr import streamtokens


def _drain_store():
    streamtokens._store.clear()


def test_mint_and_consume_roundtrip():
    _drain_store()
    token = streamtokens.mint("scan-events", "abc")
    assert token.startswith("st_")
    # Only the hash is stored — the raw token never sits in the store.
    assert token not in streamtokens._store
    assert streamtokens.consume(token, "scan-events", "abc") is True


def test_single_use():
    _drain_store()
    token = streamtokens.mint("scan-events", "abc")
    assert streamtokens.consume(token, "scan-events", "abc") is True
    assert streamtokens.consume(token, "scan-events", "abc") is False


def test_scoped_to_kind_and_resource():
    _drain_store()
    token = streamtokens.mint("scan-events", "abc")
    # Wrong resource: refused AND burned (a mismatched guess consumes it).
    assert streamtokens.consume(token, "scan-events", "other") is False
    assert streamtokens.consume(token, "scan-events", "abc") is False

    token2 = streamtokens.mint("scan-events", "abc")
    assert streamtokens.consume(token2, "transfer-events", "abc") is False


def test_expiry(monkeypatch):
    _drain_store()
    token = streamtokens.mint("scan-events", "abc", ttl=60.0)
    real_time = time.time
    monkeypatch.setattr(streamtokens.time, "time", lambda: real_time() + 61.0)
    assert streamtokens.consume(token, "scan-events", "abc") is False


def test_unknown_token_refused():
    _drain_store()
    assert streamtokens.consume("st_never_minted", "scan-events", "abc") is False


def test_sweep_bounds_store(monkeypatch):
    """Expired entries are swept once the store crosses the threshold, so an
    unauthenticated-mint flood cannot grow it without bound over time."""
    _drain_store()
    real_time = time.time
    for i in range(streamtokens._SWEEP_THRESHOLD + 1):
        streamtokens.mint("scan-events", str(i), ttl=0.001)
    # All entries above are expired; the next mint triggers the sweep.
    monkeypatch.setattr(streamtokens.time, "time", lambda: real_time() + 1.0)
    streamtokens.mint("scan-events", "fresh")
    assert len(streamtokens._store) == 1
