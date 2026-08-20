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
