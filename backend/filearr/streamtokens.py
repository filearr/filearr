"""Short-lived, single-use tokens for browser SSE streams.

``EventSource`` cannot set request headers, so a browser cannot present a
Bearer key on an SSE connection. The original workaround accepted the real API
key as a ``?api_key=`` query parameter — but query strings leak into proxy and
access logs, so a captured log line was a captured credential. The roadmap
(§14) called for replacing that with a scoped one-time stream token minted by
an authenticated POST; this module is that token store.

Properties:
  * **Scoped** — a token is bound to one (kind, resource_id) pair, e.g.
    ("scan-events", "<scan uuid>"). It opens exactly that stream and nothing
    else; it is never a general credential.
  * **Single-use** — consumed on first presentation. A token that leaks into
    an access log is already dead.
  * **Short-lived** — default 60 s between mint and connect.
  * **In-process** — the store is a module-level dict. Filearr runs a single
    API process (compose `app` service), so no shared backend is needed; a
    process restart invalidates outstanding tokens, which only costs the
    client one silent re-mint (the SSE consumers re-mint on every reconnect).

Only the sha256 of the token is stored, so a memory dump of the store cannot
be replayed.
"""

from __future__ import annotations

import hashlib
import secrets
import time
from dataclasses import dataclass

TOKEN_TTL_SECONDS = 60.0

# Opportunistic-cleanup threshold: on mint, once the store holds more entries
# than this, expired rows are swept. Keeps the store bounded without a timer.
_SWEEP_THRESHOLD = 256


@dataclass(frozen=True)
class _Entry:
    kind: str
    resource_id: str
    expires_at: float


_store: dict[str, _Entry] = {}


def _digest(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def mint(kind: str, resource_id: str, *, ttl: float = TOKEN_TTL_SECONDS) -> str:
    """Mint a single-use stream token for (kind, resource_id) and return the
    raw token. Only its hash is retained."""
    if len(_store) > _SWEEP_THRESHOLD:
        now = time.time()
        for key in [k for k, e in _store.items() if e.expires_at < now]:
            _store.pop(key, None)
    token = "st_" + secrets.token_urlsafe(32)
    _store[_digest(token)] = _Entry(kind, resource_id, time.time() + ttl)
    return token


def consume(token: str, kind: str, resource_id: str) -> bool:
    """Validate + consume a token. True only when the token exists, matches the
    (kind, resource_id) it was minted for, and has not expired. The entry is
    removed regardless of outcome (single-use; a mismatched guess burns it)."""
    entry = _store.pop(_digest(token), None)
    if entry is None:
        return False
    if entry.expires_at < time.time():
        return False
    return entry.kind == kind and entry.resource_id == resource_id
