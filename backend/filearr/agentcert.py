"""Agent certificate rebind — proof-of-possession verification (fix 2026-07-24).

The credential-drift bug this cures: in ``fingerprint`` auth mode the agent's
bearer token IS the SHA-256 of its current step-ca leaf, but central only ever
learned that fingerprint once (at enrollment). The renewal daemon rotates the
leaf at ~2/3 of its 24-72h lifetime, so ~32h after enroll every request starts
401ing — and ``mtls-header`` mode 403s the same way, because Caddy forwards the
fp header unconditionally and central compares it against the stale binding.

The cure is a rebind endpoint (api/agents.py) authenticated NOT by the (now
useless) bearer but by proof of possession of a CA-issued cert for that agent:

  1. the presented chain verifies leaf → intermediate → the PINNED step-ca root
     (fetched once via the same fetch-and-pin bootstrap the agent itself uses),
  2. the leaf's SAN equals the agent id (only step-ca can issue that — OTT
     SANs are scoped at mint time),
  3. an ECDSA signature over a domain-separated canonical payload proves the
     caller holds the leaf's private key (which SURVIVES renewal — the agent
     re-keys never, re-certs often),
  4. a timestamp bounds replay; a replay inside the window is idempotent (same
     cert) or a transient rollback to another still-valid same-agent cert —
     no privilege is ever gained, so no server-side nonce state is warranted.

Isolated in this module so x509 concerns never leak into agentsync/api code.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from datetime import UTC, datetime

log = logging.getLogger("filearr.agentcert")

# Domain separation: this exact prefix is baked into the Go signer
# (agent/internal/enroll/rebind.go) — the leaf key's signatures can never be
# confused with any future protocol reusing the same key. Bump the suffix on
# any payload change.
REBIND_CONTEXT = b"filearr-agent-rebind-v1"

# Sanity ceilings on the presented chain (leaf + intermediate is the step-ca
# shape; +2 headroom for a future cross-sign).
MAX_CHAIN_CERTS = 4


class RebindError(Exception):
    """A rebind request central refuses. ``reason`` is a stable machine string
    the API maps onto an HTTP status: ``bad_chain`` / ``expired_cert`` /
    ``bad_signature`` / ``stale_timestamp`` (→ 401), ``san_mismatch`` (→ 403),
    ``ca_root_unavailable`` (→ 503), ``bad_request`` (→ 400)."""

    def __init__(self, reason: str, message: str | None = None) -> None:
        self.reason = reason
        super().__init__(message or reason)


def canonical_payload(agent_id: str, timestamp: int, fingerprint: str) -> bytes:
    """The exact bytes both sides sign/verify. Mirrored in Go
    (``enroll.RebindPayload``) and pinned by a cross-language vector test on
    each side — change NEITHER without the other."""
    return b"\n".join(
        (REBIND_CONTEXT, agent_id.encode(), str(timestamp).encode(), fingerprint.encode())
    )


def _norm_fp(fp: str) -> str:
    return fp.replace(":", "").strip().lower()


# ---------------------------------------------------------------------------
# CA root fetch-and-pin
# ---------------------------------------------------------------------------
# Central holds ONLY the root fingerprint (public pinning material, config
# ca_fingerprint); the root cert itself lives in the step-ca/Caddy containers.
# Fetch it from step-ca's bootstrap endpoint exactly the way the agent does:
# TLS unverified (chicken-and-egg) but AUTHENTICATED by the pin —
# sha256(root.der) must equal the configured fingerprint.
_root_cache: dict[tuple[str, str], object] = {}
_root_lock = asyncio.Lock()


async def get_ca_root(settings):
    """Fetch, pin-verify, and cache the step-ca root certificate.

    Raises ``RebindError("ca_root_unavailable")`` when the CA is unreachable or
    the served root does not match the pin (the latter is also logged loudly —
    it means the CA identity changed underneath the deployment)."""
    from cryptography import x509

    if not settings.ca_url or not settings.ca_fingerprint:
        raise RebindError("ca_root_unavailable", "ca_url/ca_fingerprint not configured")
    key = (settings.ca_url, _norm_fp(settings.ca_fingerprint))
    cached = _root_cache.get(key)
    if cached is not None:
        return cached
    async with _root_lock:
        cached = _root_cache.get(key)
        if cached is not None:
            return cached
        import httpx

        url = f"{settings.ca_url.rstrip('/')}/root/{key[1]}"
        try:
            async with httpx.AsyncClient(verify=False, timeout=10.0) as client:
                resp = await client.get(url)
                resp.raise_for_status()
                pem = resp.json().get("ca", "")
        except Exception as exc:
            raise RebindError("ca_root_unavailable", f"CA root fetch failed: {exc}") from exc
        if not pem or len(pem) > 65536:
            raise RebindError("ca_root_unavailable", "CA returned no usable root")
        try:
            root = x509.load_pem_x509_certificate(pem.encode())
        except Exception as exc:
            raise RebindError("ca_root_unavailable", "CA root did not parse") from exc
        der_fp = hashlib.sha256(root.public_bytes(_der_encoding())).hexdigest()
        if der_fp != key[1]:
            log.error(
                "step-ca root fingerprint MISMATCH: served %s, pinned %s", der_fp, key[1]
            )
            raise RebindError(
                "ca_root_unavailable", "CA root does not match the pinned fingerprint"
            )
        _root_cache[key] = root
        return root


def _der_encoding():
    from cryptography.hazmat.primitives.serialization import Encoding

    return Encoding.DER


# ---------------------------------------------------------------------------
# Rebind verification
# ---------------------------------------------------------------------------
def verify_rebind(
    *,
    chain_pem: str,
    agent_id: str,
    timestamp: int,
    signature: bytes,
    root,
    now: datetime | None = None,
    max_skew_s: int,
) -> str:
    """Verify a rebind request end to end; return the leaf's sha256 fingerprint.

    Checks, in order: timestamp freshness → chain to the pinned root (validity
    window included) → leaf SAN == agent_id → ECDSA-SHA256 signature over the
    canonical payload with the leaf's public key. Any failure raises
    :class:`RebindError` with a stable reason. Synchronous + pure (root passed
    in) so it is trivially unit-testable."""
    from cryptography import x509
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.verification import PolicyBuilder, Store

    now = now or datetime.now(UTC)

    if abs(now.timestamp() - timestamp) > max_skew_s:
        raise RebindError("stale_timestamp", "rebind timestamp outside the accepted window")

    try:
        certs = x509.load_pem_x509_certificates(chain_pem.encode())
    except Exception as exc:
        raise RebindError("bad_chain", "certificate chain did not parse") from exc
    if not certs or len(certs) > MAX_CHAIN_CERTS:
        raise RebindError("bad_chain", "certificate chain empty or oversized")
    leaf, intermediates = certs[0], certs[1:]

    # Chain + validity. The client verifier also enforces the clientAuth EKU —
    # exactly what a step-ca agent leaf carries (Caddy's require_and_verify
    # validates the same certs in mtls mode, so the profile is proven live).
    try:
        verifier = PolicyBuilder().store(Store([root])).time(now).build_client_verifier()
        verifier.verify(leaf, intermediates)
    except Exception as exc:
        # An expired leaf is the interesting sub-case for operators (offline
        # > TTL agent must renew FIRST, then rebind) — surface it distinctly.
        if leaf.not_valid_after_utc < now or leaf.not_valid_before_utc > now:
            raise RebindError(
                "expired_cert", "leaf certificate outside its validity window"
            ) from exc
        raise RebindError("bad_chain", "chain does not verify to the pinned CA root") from exc

    try:
        sans = leaf.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value.get_values_for_type(x509.DNSName)
    except x509.ExtensionNotFound:
        sans = []
    if agent_id not in sans:
        raise RebindError("san_mismatch", "certificate SAN does not name this agent")

    fingerprint = hashlib.sha256(leaf.public_bytes(_der_encoding())).hexdigest()
    payload = canonical_payload(agent_id, timestamp, fingerprint)
    public_key = leaf.public_key()
    if not isinstance(public_key, ec.EllipticCurvePublicKey):
        # step-ca issues EC P-256 here; anything else is not ours.
        raise RebindError("bad_signature", "unsupported leaf key type")
    try:
        public_key.verify(signature, payload, ec.ECDSA(hashes.SHA256()))
    except InvalidSignature as exc:
        raise RebindError("bad_signature", "signature does not verify") from exc

    return fingerprint
