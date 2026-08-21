"""GUI auth-provider configuration (2026-08-20).

Covers the config store + secret encryption + the DB-over-env overlay that every
auth reader now uses, plus the admin API (redaction, source map, secret keep/
clear) — network-free (no LDAP/OIDC server; the test actions are exercised in
the ldap/oidc suites).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import app_settings, authconfig
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM app_settings"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    app_settings.reset_for_tests()
    get_settings.cache_clear()
    # A secret key must be set for secret encryption.
    monkeypatch.setattr(get_settings(), "secret_key", "test-secret-key-000000000000000000000000")
    yield Session
    app_settings.reset_for_tests()
    await engine.dispose()


# --------------------------------------------------------------------------- #
# Store + overlay                                                              #
# --------------------------------------------------------------------------- #
async def test_set_and_effective_overlay(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        await authconfig.set_config(s, "ldap", {
            "ldap_enabled": True,
            "ldap_server": "ldaps://dc.corp:636",
            "ldap_bind_dn": "cn=svc,dc=corp",
            "ldap_bind_password": "s3cr3t",
        }, updated_by=None)
        await s.commit()

    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    # GUI values override env; the secret is decrypted back to plaintext.
    assert eff.ldap_enabled is True
    assert eff.ldap_server == "ldaps://dc.corp:636"
    assert eff.ldap_bind_password == "s3cr3t"


async def test_secret_is_encrypted_at_rest_and_redacted(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr.models import AppSetting

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        await authconfig.set_config(
            s, "oidc",
            {"oidc_enabled": True, "oidc_issuer": "https://idp/", "oidc_client_id": "cid",
             "oidc_client_secret": "topsecret"},
            updated_by=None,
        )
        await s.commit()
        row = await s.get(AppSetting, app_settings.KEY_OIDC_CONFIG)
        stored = (row.value or {})["v"]
    # The ciphertext is NOT the plaintext.
    assert stored["oidc_client_secret"] != "topsecret"
    # The read API redacts it to a has_* flag and never returns the value.
    async with maker() as s:
        cfg = await authconfig.get_config(s, "oidc")
    assert cfg["has_oidc_client_secret"] is True
    assert "oidc_client_secret" not in cfg
    assert cfg["oidc_issuer"] == "https://idp/"
    assert cfg["_source"]["oidc_issuer"] == "gui"
    assert cfg["_source"]["oidc_enabled"] == "gui"


async def test_secret_unchanged_sentinel_keeps_stored(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        await authconfig.set_config(s, "ldap", {"ldap_bind_password": "orig"}, updated_by=None)
        await s.commit()
        # A later save that edits another field but keeps the secret sentinel.
        await authconfig.set_config(
            s, "ldap",
            {"ldap_server": "ldaps://x", "ldap_bind_password": authconfig.SECRET_UNCHANGED},
            updated_by=None,
        )
        await s.commit()
    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    assert eff.ldap_bind_password == "orig" and eff.ldap_server == "ldaps://x"
    # An empty string CLEARS it.
    async with maker() as s:
        await authconfig.set_config(s, "ldap", {"ldap_bind_password": ""}, updated_by=None)
        await s.commit()
    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    assert not eff.ldap_bind_password


async def test_unknown_field_rejected(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        with pytest.raises(authconfig.AuthConfigError):
            await authconfig.set_config(s, "ldap", {"ldap_server": "x", "evil": 1}, updated_by=None)


async def test_directory_endpoint_secrets_encrypted_and_kept(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        await authconfig.set_config(s, "directory", {
            "ldap_directory_sync_enabled": True,
            "ldap_directories": [
                {"server": "ldaps://a:636", "bind_dn": "cn=a", "bind_password": "pa"},
            ],
        }, updated_by=None)
        await s.commit()
    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    # Decrypted for the sync path.
    assert eff.ldap_directories[0]["bind_password"] == "pa"
    # Redacted in the API read.
    async with maker() as s:
        cfg = await authconfig.get_config(s, "directory")
    ep = cfg["ldap_directories"][0]
    assert "bind_password" not in ep and ep["has_bind_password"] is True
    # A resave with the per-endpoint sentinel keeps the stored password.
    async with maker() as s:
        await authconfig.set_config(s, "directory", {
            "ldap_directories": [
                {"server": "ldaps://a:636", "bind_dn": "cn=a",
                 "bind_password": authconfig.SECRET_UNCHANGED},
            ],
        }, updated_by=None)
        await s.commit()
    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    assert eff.ldap_directories[0]["bind_password"] == "pa"


# --------------------------------------------------------------------------- #
# API                                                                          #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _sess():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _sess
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


async def test_api_put_get_roundtrip_redacts_secret(api):
    r = await api.put("/api/v1/auth-config/ldap", json={
        "ldap_enabled": True, "ldap_server": "ldaps://dc:636",
        "ldap_bind_dn": "cn=svc", "ldap_bind_password": "pw",
    })
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["has_ldap_bind_password"] is True and "ldap_bind_password" not in body
    assert body["ldap_server"] == "ldaps://dc:636" and body["_source"]["ldap_server"] == "gui"

    r = await api.get("/api/v1/auth-config/ldap")
    assert r.json()["ldap_enabled"] is True

    # Unknown provider 404, unknown field 422.
    assert (await api.get("/api/v1/auth-config/nope")).status_code == 404
    assert (await api.put("/api/v1/auth-config/ldap", json={"bad": 1})).status_code == 422


async def test_api_oidc_test_requires_issuer(api):
    r = await api.post("/api/v1/auth-config/oidc/test", json={})
    assert r.status_code == 200
    assert r.json()["ok"] is False and "issuer" in r.json()["error"]


# --------------------------------------------------------------------------- #
# CA certificate paste + fetch                                                 #
# --------------------------------------------------------------------------- #
def _self_signed_pem() -> str:
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "corp-root-ca")])
    cert = (
        x509.CertificateBuilder()
        .subject_name(name).issuer_name(name).public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(UTC) - timedelta(days=1))
        .not_valid_after(datetime.now(UTC) + timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    return cert.public_bytes(serialization.Encoding.PEM).decode("ascii")


async def test_pem_ca_paste_validates_and_flows_to_ldapconfig(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr.ldap_auth import LdapConfig

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    pem = _self_signed_pem()
    async with maker() as s:
        await authconfig.set_config(s, "ldap", {
            "ldap_enabled": True, "ldap_server": "ldaps://dc:636",
            "ldap_user_base": "dc=corp", "ldap_tls_ca_cert_pem": pem,
        }, updated_by=None)
        await s.commit()
    async with maker() as s:
        eff = await authconfig.effective_settings(s)
    assert eff.ldap_tls_ca_cert_pem == pem
    # It reaches the ldap3 TLS layer via LdapConfig.tls_ca_cert_data.
    cfg = LdapConfig.from_settings(eff)
    assert cfg.tls_ca_cert_data == pem


async def test_pem_ca_paste_rejects_garbage(maker, monkeypatch):
    from filearr import db as db_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    async with maker() as s:
        with pytest.raises(authconfig.AuthConfigError):
            await authconfig.set_config(
                s, "ldap", {"ldap_tls_ca_cert_pem": "not a certificate"}, updated_by=None
            )


def test_validate_pem_counts_certs():
    pem = _self_signed_pem() + _self_signed_pem()
    assert authconfig.validate_pem_chain(pem) == 2


async def test_fetch_cert_no_server(api):
    r = await api.post("/api/v1/auth-config/ldap/fetch-cert", json={})
    assert r.status_code == 422  # nothing configured to fetch from


async def test_fetch_cert_unreachable_returns_error(api):
    r = await api.post(
        "/api/v1/auth-config/ldap/fetch-cert",
        json={"ldap_server": "ldaps://127.0.0.1:1"},  # nothing listening
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is False and "could not connect" in body["error"]


# --------------------------------------------------------------------------- #
# AIA chain completion (AD DCs commonly present only their leaf)               #
# --------------------------------------------------------------------------- #
def _chain_with_aia() -> dict:
    """leaf(AIA→issuing.p7b) ← intermediate(AIA→root.cer) ← root. The AIA
    payload map serves the intermediate as PKCS#7 DER and the root as bare DER
    — the two formats AD CS actually publishes."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.hazmat.primitives.serialization import pkcs7
    from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

    def _name(cn: str) -> x509.Name:
        return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])

    def _build(subject, issuer, pubkey, signkey, *, ca, aia_url=None):
        b = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer).public_key(pubkey)
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        if aia_url:
            b = b.add_extension(
                x509.AuthorityInformationAccess([
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.CA_ISSUERS,
                        x509.UniformResourceIdentifier(aia_url),
                    )
                ]),
                critical=False,
            )
        return b.sign(signkey, hashes.SHA256())

    keys = [rsa.generate_private_key(public_exponent=65537, key_size=2048) for _ in range(3)]
    root_key, int_key, leaf_key = keys
    root = _build(_name("corp-root"), _name("corp-root"), root_key.public_key(),
                  root_key, ca=True)
    inter = _build(_name("corp-issuing"), _name("corp-root"), int_key.public_key(),
                   root_key, ca=True, aia_url="http://pki.corp/root.cer")
    leaf = _build(_name("dc01.corp"), _name("corp-issuing"), leaf_key.public_key(),
                  int_key, ca=False, aia_url="http://pki.corp/issuing.p7b")
    der = serialization.Encoding.DER
    return {
        "leaf": leaf.public_bytes(der),
        "payloads": {
            "http://pki.corp/issuing.p7b": pkcs7.serialize_certificates([inter], der),
            "http://pki.corp/root.cer": root.public_bytes(der),
        },
    }


async def test_fetch_cert_completes_leaf_only_chain_via_aia(api, monkeypatch):
    from filearr.api import authconfig as api_mod

    fx = _chain_with_aia()
    monkeypatch.setattr(api_mod, "_fetch_chain", lambda host, port, timeout: [fx["leaf"]])
    monkeypatch.setattr(api_mod, "_http_get", lambda url, timeout: fx["payloads"][url])
    r = await api.post(
        "/api/v1/auth-config/ldap/fetch-cert", json={"ldap_server": "ldaps://dc01.corp:636"}
    )
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True, body
    assert [c["via_aia"] for c in body["chain"]] == [False, True, True]
    assert body["chain"][1]["subject"] == "CN=corp-issuing"
    assert body["chain"][2]["is_self_signed"] is True
    # Suggested anchor = intermediate + root — the leaf is excluded.
    assert authconfig.validate_pem_chain(body["suggested_ca_pem"]) == 2
    assert "AIA" in body["note"]


async def test_fetch_cert_leaf_only_unreachable_aia_falls_back_to_leaf(api, monkeypatch):
    from filearr.api import authconfig as api_mod

    fx = _chain_with_aia()
    monkeypatch.setattr(api_mod, "_fetch_chain", lambda host, port, timeout: [fx["leaf"]])

    def _unreachable(url: str, timeout: float) -> bytes:
        raise ValueError("no route to pki host")

    monkeypatch.setattr(api_mod, "_http_get", _unreachable)
    r = await api.post(
        "/api/v1/auth-config/ldap/fetch-cert", json={"ldap_server": "ldaps://dc01.corp:636"}
    )
    body = r.json()
    assert body["ok"] is True and len(body["chain"]) == 1
    assert authconfig.validate_pem_chain(body["suggested_ca_pem"]) == 1
    assert "leaf" in body["note"]


def _ad_leaf_and_root(aia_url: str | None) -> dict:
    """An AD-style pair: self-signed root named CN=CORP-ATOM-CA,DC=corp,
    DC=example and a leaf signed by it, optionally carrying an AIA pointer.
    Mirrors a stock AD CS PKI where the DC presents only the leaf and the AIA
    is an ldap:/// URI (or absent)."""
    from datetime import UTC, datetime, timedelta

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import AuthorityInformationAccessOID, NameOID

    ca_name = x509.Name([
        # DER order: most-significant DC first (example → corp.example).
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "example"),
        x509.NameAttribute(NameOID.DOMAIN_COMPONENT, "corp"),
        x509.NameAttribute(NameOID.COMMON_NAME, "CORP-ATOM-CA"),
    ])
    leaf_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "dc01.corp.example")])
    root_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    leaf_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _build(subject, issuer, pubkey, signkey, *, ca, aia=None):
        b = (
            x509.CertificateBuilder()
            .subject_name(subject).issuer_name(issuer).public_key(pubkey)
            .serial_number(x509.random_serial_number())
            .not_valid_before(datetime.now(UTC) - timedelta(days=1))
            .not_valid_after(datetime.now(UTC) + timedelta(days=365))
            .add_extension(x509.BasicConstraints(ca=ca, path_length=None), critical=True)
        )
        if aia:
            b = b.add_extension(
                x509.AuthorityInformationAccess([
                    x509.AccessDescription(
                        AuthorityInformationAccessOID.CA_ISSUERS,
                        x509.UniformResourceIdentifier(aia),
                    )
                ]),
                critical=False,
            )
        return b.sign(signkey, hashes.SHA256())

    root = _build(ca_name, ca_name, root_key.public_key(), root_key, ca=True)
    leaf = _build(leaf_name, ca_name, leaf_key.public_key(), root_key, ca=False,
                  aia=aia_url)
    der = serialization.Encoding.DER
    return {"leaf": leaf.public_bytes(der), "root": root.public_bytes(der)}


async def test_fetch_cert_resolves_ldap_aia_with_service_bind(api, monkeypatch):
    from filearr.api import authconfig as api_mod

    fx = _ad_leaf_and_root(
        "ldap:///CN=CORP-ATOM-CA,CN=AIA,CN=Public%20Key%20Services,CN=Services,"
        "CN=Configuration,DC=corp,DC=example?cACertificate?base"
        "?objectClass=certificationAuthority"
    )
    monkeypatch.setattr(api_mod, "_fetch_chain", lambda host, port, timeout: [fx["leaf"]])
    calls: list[tuple] = []

    def _ldap_seam(host, dn, timeout, bind_dn, bind_password):
        calls.append((host, dn, bind_dn, bind_password))
        return [fx["root"]]

    monkeypatch.setattr(api_mod, "_ldap_get_ca_certs", _ldap_seam)

    def _no_http(url, timeout):
        raise ValueError("no http route")

    monkeypatch.setattr(api_mod, "_http_get", _no_http)
    r = await api.post("/api/v1/auth-config/ldap/fetch-cert", json={
        "ldap_server": "ldaps://dc01.corp.example:636",
        "ldap_bind_dn": "cn=svc,dc=corp,dc=example",
        "ldap_bind_password": "pw",
    })
    body = r.json()
    assert body["ok"] is True, body
    assert [c["via_aia"] for c in body["chain"]] == [False, True]
    assert body["chain"][1]["is_self_signed"] is True
    host, dn, bind_dn, bind_password = calls[0]
    # The hostless ldap:/// URI resolves against the DC we fetched from, with
    # the service bind, and the DN is percent-decoded.
    assert host == "dc01.corp.example"
    assert dn.startswith("CN=CORP-ATOM-CA,CN=AIA,CN=Public Key Services,")
    assert (bind_dn, bind_password) == ("cn=svc,dc=corp,dc=example", "pw")
    assert authconfig.validate_pem_chain(body["suggested_ca_pem"]) == 1


async def test_fetch_cert_certenroll_fallback_no_credentials(api, monkeypatch):
    from filearr.api import authconfig as api_mod

    fx = _ad_leaf_and_root(None)  # no AIA at all — CertEnroll guess is the only path
    monkeypatch.setattr(api_mod, "_fetch_chain", lambda host, port, timeout: [fx["leaf"]])
    hits: list[str] = []

    def _http_seam(url, timeout):
        hits.append(url)
        if url == "http://atom.corp.example/CertEnroll/atom.corp.example_CORP-ATOM-CA.crt":
            return fx["root"]
        raise ValueError("404")

    monkeypatch.setattr(api_mod, "_http_get", _http_seam)
    r = await api.post(
        "/api/v1/auth-config/ldap/fetch-cert", json={"ldap_server": "ldaps://dc01.corp.example"}
    )
    body = r.json()
    assert body["ok"] is True, body
    assert [c["via_aia"] for c in body["chain"]] == [False, True]
    # Host candidates derive from the issuer CN tokens + DC-components domain.
    assert "http://corp.corp.example/CertEnroll/corp.corp.example_CORP-ATOM-CA.crt" in hits
    assert authconfig.validate_pem_chain(body["suggested_ca_pem"]) == 1


async def test_fetch_cert_ldap_aia_without_creds_notes_bind_needed(api, monkeypatch):
    from filearr.api import authconfig as api_mod

    fx = _ad_leaf_and_root("ldap:///CN=CORP-ATOM-CA,CN=AIA,DC=corp,DC=example?cACertificate")
    monkeypatch.setattr(api_mod, "_fetch_chain", lambda host, port, timeout: [fx["leaf"]])

    def _fail(*a, **k):
        raise ValueError("unreachable")

    monkeypatch.setattr(api_mod, "_http_get", _fail)
    monkeypatch.setattr(api_mod, "_ldap_get_ca_certs", _fail)
    r = await api.post(
        "/api/v1/auth-config/ldap/fetch-cert", json={"ldap_server": "ldaps://dc01.corp.example"}
    )
    body = r.json()
    assert body["ok"] is True and len(body["chain"]) == 1
    assert "bind DN" in body["note"]
