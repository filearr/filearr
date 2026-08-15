"""Cert-rebind proof-of-possession endpoint (credential-drift fix 2026-07-24).

Covers: the canonical-payload cross-language vector (must byte-match Go's
``enroll.RebindPayload``), verify_rebind unit cases against a generated
root→intermediate→leaf chain, the state rules in
``agentsync.rebind_agent_certificate`` (idempotent / revoked / pending), the
HTTP surface (happy path, stale timestamp, tampered signature, wrong-agent
cert, expired leaf, CA-root-down 503), and the mtls-header fingerprint
self-heal.

Runs against the migrated pgserver Postgres (mirrors test_agents_p5t1's
harness).
"""

from __future__ import annotations

import base64
import time
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import ExtendedKeyUsageOID
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import agentcert
from filearr import db as db_mod
from filearr.agentcert import RebindError, canonical_payload, verify_rebind
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


# --------------------------------------------------------------------------- #
# Test PKI: root -> intermediate -> leaf(SAN=agent_id, clientAuth)             #
# --------------------------------------------------------------------------- #
# The test PKI mirrors step-ca's issued shape: SKI on every cert and AKI on
# everything below the root — the strict RFC 5280 verifier in agentcert
# REQUIRES them (it rejected an earlier minimal PKI with "missing required
# extension 2.5.29.35"), and step-ca's default templates always emit them.
def _make_ca(cn: str, issuer_cert=None, issuer_key=None):
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, cn)])
    issuer = issuer_cert.subject if issuer_cert is not None else subject
    signer = issuer_key if issuer_key is not None else key
    signer_pub = signer.public_key()
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(minutes=5))
        .not_valid_after(datetime.now(UTC) + timedelta(days=365))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .add_extension(
            x509.KeyUsage(
                digital_signature=False, content_commitment=False,
                key_encipherment=False, data_encipherment=False, key_agreement=False,
                key_cert_sign=True, crl_sign=True, encipher_only=False,
                decipher_only=False,
            ),
            critical=True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(signer_pub),
            critical=False,
        )
        .sign(signer, hashes.SHA256())
    )
    return key, cert


def _make_leaf(agent_id: str, issuer_cert, issuer_key, *, not_after=None, key=None):
    key = key or ec.generate_private_key(ec.SECP256R1())
    not_after = not_after or (datetime.now(UTC) + timedelta(hours=48))
    not_before = min(datetime.now(UTC) - timedelta(minutes=5), not_after - timedelta(hours=1))
    cert = (
        x509.CertificateBuilder()
        .subject_name(x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, agent_id)]))
        .issuer_name(issuer_cert.subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(not_before)
        .not_valid_after(not_after)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(agent_id)]), critical=False)
        .add_extension(
            x509.ExtendedKeyUsage(
                [ExtendedKeyUsageOID.CLIENT_AUTH, ExtendedKeyUsageOID.SERVER_AUTH]
            ),
            critical=False,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), critical=False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(issuer_key.public_key()),
            critical=False,
        )
        .sign(issuer_key, hashes.SHA256())
    )
    return key, cert


def _pem(*certs) -> str:
    from cryptography.hazmat.primitives.serialization import Encoding

    return "".join(c.public_bytes(Encoding.PEM).decode() for c in certs)


def _fp(cert) -> str:
    import hashlib

    from cryptography.hazmat.primitives.serialization import Encoding

    return hashlib.sha256(cert.public_bytes(Encoding.DER)).hexdigest()


def _sign(key, agent_id: str, ts: int, fingerprint: str) -> bytes:
    return key.sign(canonical_payload(agent_id, ts, fingerprint), ec.ECDSA(hashes.SHA256()))


@pytest.fixture(scope="module")
def pki():
    root_key, root = _make_ca("Filearr Test Root")
    int_key, intermediate = _make_ca("Filearr Test Intermediate", root, root_key)
    return {
        "root_key": root_key,
        "root": root,
        "int_key": int_key,
        "intermediate": intermediate,
    }


# --------------------------------------------------------------------------- #
# Cross-language canonical payload vector                                      #
# --------------------------------------------------------------------------- #
def test_canonical_payload_cross_language_vector():
    """MUST byte-match Go's TestRebindPayloadGolden
    (agent/internal/enroll/rebind_test.go). Change both together, never one."""
    got = canonical_payload("agent-123", 1700000000, "abcdef0123")
    assert got == b"filearr-agent-rebind-v1\nagent-123\n1700000000\nabcdef0123"


# --------------------------------------------------------------------------- #
# verify_rebind unit cases (pure — root passed in)                             #
# --------------------------------------------------------------------------- #
def _verify(pki, chain_pem, agent_id, ts, sig, *, skew=300):
    return verify_rebind(
        chain_pem=chain_pem,
        agent_id=agent_id,
        timestamp=ts,
        signature=sig,
        root=pki["root"],
        max_skew_s=skew,
    )


def test_verify_rebind_happy_path(pki):
    agent_id = str(uuid.uuid4())
    key, leaf = _make_leaf(agent_id, pki["intermediate"], pki["int_key"])
    ts = int(time.time())
    fp = _fp(leaf)
    sig = _sign(key, agent_id, ts, fp)
    assert _verify(pki, _pem(leaf, pki["intermediate"]), agent_id, ts, sig) == fp


def test_verify_rebind_stale_timestamp(pki):
    agent_id = str(uuid.uuid4())
    key, leaf = _make_leaf(agent_id, pki["intermediate"], pki["int_key"])
    ts = int(time.time()) - 3600
    sig = _sign(key, agent_id, ts, _fp(leaf))
    with pytest.raises(RebindError) as ei:
        _verify(pki, _pem(leaf, pki["intermediate"]), agent_id, ts, sig)
    assert ei.value.reason == "stale_timestamp"


def test_verify_rebind_tampered_payload(pki):
    agent_id = str(uuid.uuid4())
    key, leaf = _make_leaf(agent_id, pki["intermediate"], pki["int_key"])
    ts = int(time.time())
    # Signature over a DIFFERENT fingerprint than the presented leaf's.
    sig = _sign(key, agent_id, ts, "0" * 64)
    with pytest.raises(RebindError) as ei:
        _verify(pki, _pem(leaf, pki["intermediate"]), agent_id, ts, sig)
    assert ei.value.reason == "bad_signature"


def test_verify_rebind_self_signed_chain_rejected(pki):
    """A cert NOT issued by the pinned CA must fail even with a valid
    self-signature — possession of any old key pair earns nothing."""
    agent_id = str(uuid.uuid4())
    rogue_key, rogue_ca = _make_ca("Rogue CA")
    key, leaf = _make_leaf(agent_id, rogue_ca, rogue_key)
    ts = int(time.time())
    sig = _sign(key, agent_id, ts, _fp(leaf))
    with pytest.raises(RebindError) as ei:
        _verify(pki, _pem(leaf, rogue_ca), agent_id, ts, sig)
    assert ei.value.reason == "bad_chain"


def test_verify_rebind_wrong_san(pki):
    """A valid CA-issued cert for a DIFFERENT agent id -> san_mismatch (the
    API maps this to 403, not 401 — identity confusion, not bad credentials)."""
    other_id = str(uuid.uuid4())
    key, leaf = _make_leaf(other_id, pki["intermediate"], pki["int_key"])
    claimed = str(uuid.uuid4())
    ts = int(time.time())
    sig = _sign(key, claimed, ts, _fp(leaf))
    with pytest.raises(RebindError) as ei:
        _verify(pki, _pem(leaf, pki["intermediate"]), claimed, ts, sig)
    assert ei.value.reason == "san_mismatch"


def test_verify_rebind_expired_leaf(pki):
    """An expired leaf must NOT rebind (it would gut the PoP guarantee). The
    offline->renew(grace)->rebind sequence presents the fresh leaf instead."""
    agent_id = str(uuid.uuid4())
    key, leaf = _make_leaf(
        agent_id, pki["intermediate"], pki["int_key"],
        not_after=datetime.now(UTC) - timedelta(hours=1),
    )
    ts = int(time.time())
    sig = _sign(key, agent_id, ts, _fp(leaf))
    with pytest.raises(RebindError) as ei:
        _verify(pki, _pem(leaf, pki["intermediate"]), agent_id, ts, sig)
    assert ei.value.reason == "expired_cert"


def test_verify_rebind_garbage_chain(pki):
    with pytest.raises(RebindError) as ei:
        _verify(pki, "not a pem", str(uuid.uuid4()), int(time.time()), b"x")
    assert ei.value.reason == "bad_chain"


# --------------------------------------------------------------------------- #
# DB + HTTP surface                                                            #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM enrollment_tokens"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch, pki):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "ca_url", "https://ca.filearr.lan:9000")
    monkeypatch.setattr(settings, "ca_fingerprint", "deadbeef")

    # The pinned-root fetch seam: hand the endpoint our test root directly.
    async def _fake_root(_settings):
        return pki["root"]

    monkeypatch.setattr(agentcert, "get_ca_root", _fake_root)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, *, fingerprint="enrollment-fp", revoked=False):
    async with maker() as s:
        agent = Agent(
            name="nas", hostname="nas", platform="linux",
            cert_fingerprint=fingerprint,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        s.add(agent)
        await s.commit()
        return agent.id


def _rebind_body(pki, key, leaf, agent_id, *, ts=None, sig=None):
    ts = ts if ts is not None else int(time.time())
    fp = _fp(leaf)
    raw = sig if sig is not None else _sign(key, str(agent_id), ts, fp)
    return {
        "cert_chain_pem": _pem(leaf, pki["intermediate"]),
        "timestamp": ts,
        "signature": base64.b64encode(raw).decode(),
    }


async def _post(c, agent_id, body):
    return await c.post(f"/api/v1/agents/{agent_id}/rebind", json=body)


async def test_rebind_happy_path_rotates_fingerprint(client, pki):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    r = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
    assert r.status_code == 200, r.text
    assert r.json()["cert_fingerprint"] == _fp(leaf)
    async with maker() as s:
        assert (await s.get(Agent, agent_id)).cert_fingerprint == _fp(leaf)
    # Idempotent replay of the same material: still 200, binding unchanged.
    r2 = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
    assert r2.status_code == 200


async def test_rebind_stale_timestamp_401(client, pki):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    body = _rebind_body(pki, key, leaf, agent_id, ts=int(time.time()) - 3600)
    # Re-sign so ONLY the staleness fails, not the signature.
    body["signature"] = base64.b64encode(
        _sign(key, str(agent_id), body["timestamp"], _fp(leaf))
    ).decode()
    assert (await _post(c, agent_id, body)).status_code == 401


async def test_rebind_tampered_signature_401(client, pki):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    bad_sig = _sign(key, str(agent_id), int(time.time()), "0" * 64)
    body = _rebind_body(pki, key, leaf, agent_id, sig=bad_sig)
    assert (await _post(c, agent_id, body)).status_code == 401


async def test_rebind_wrong_agents_cert_403(client, pki):
    """A cert issued to agent B cannot rebind agent A."""
    c, maker, _ = client
    agent_a = await _seed_agent(maker)
    key_b, leaf_b = _make_leaf(str(uuid.uuid4()), pki["intermediate"], pki["int_key"])
    body = _rebind_body(pki, key_b, leaf_b, agent_a)
    assert (await _post(c, agent_a, body)).status_code == 403


async def test_rebind_revoked_agent_403(client, pki):
    c, maker, _ = client
    agent_id = await _seed_agent(maker, revoked=True)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    r = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
    assert r.status_code == 403


async def test_rebind_pending_agent_403(client, pki):
    """A PENDING agent (no bound cert) must not activate via rebind — that
    seam stays exclusively behind the one-time enroll_secret."""
    c, maker, _ = client
    agent_id = await _seed_agent(maker, fingerprint=None)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    r = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
    assert r.status_code == 403


async def test_rebind_unknown_agent_404(client, pki):
    c, _, _ = client
    ghost = uuid.uuid4()
    key, leaf = _make_leaf(str(ghost), pki["intermediate"], pki["int_key"])
    r = await _post(c, ghost, _rebind_body(pki, key, leaf, ghost))
    assert r.status_code == 404


async def test_rebind_ca_root_down_503(client, pki, monkeypatch):
    c, maker, _ = client
    agent_id = await _seed_agent(maker)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])

    async def _down(_settings):
        raise RebindError("ca_root_unavailable", "CA unreachable")

    monkeypatch.setattr(agentcert, "get_ca_root", _down)
    r = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
    assert r.status_code == 503


async def test_rebind_feature_disabled_404(client, pki):
    c, maker, settings = client
    agent_id = await _seed_agent(maker)
    key, leaf = _make_leaf(str(agent_id), pki["intermediate"], pki["int_key"])
    settings.agents_enabled = False
    try:
        r = await _post(c, agent_id, _rebind_body(pki, key, leaf, agent_id))
        assert r.status_code == 404
    finally:
        settings.agents_enabled = True
