"""Roles as data, self-service account, session-timeout settings (2026-08-16).

* roles: builtins seeded + undeletable, admin keeps the admin scope, custom role
  create/edit/delete, a user on a custom role gets exactly its scopes/ceiling,
  compare matrix shape;
* account: profile edit (display name / contact / local username), preferences
  round-trip, federated username locked;
* timeouts: global runtime setting overrides env, per-user override wins,
  applied live to an existing session's idle window.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import app_settings, authx
from filearr import db as db_mod
from filearr import roles as roles_registry
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for t in (
            "security_events",
            "auth_rate_limits",
            "sessions",
            "app_settings",
            "path_grants",
            "users",
            "api_keys",
            "service_accounts",
            "principals",
        ):
            await conn.execute(text(f"DELETE FROM {t}"))
        await conn.execute(text("DELETE FROM roles WHERE NOT builtin"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    roles_registry.reset_for_tests()
    app_settings.reset_for_tests()
    yield maker
    await engine.dispose()
    roles_registry.reset_for_tests()
    app_settings.reset_for_tests()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _bootstrap_admin(c):
    r = await c.post(
        "/api/v1/auth/bootstrap", json={"username": "admin", "password": "admin-pw-123"}
    )
    assert r.status_code in (200, 201), r.text
    r = await c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pw-123"})
    assert r.status_code == 200, r.text


async def _login(c, u, p):
    r = await c.post("/api/v1/auth/login", json={"username": u, "password": p})
    assert r.status_code == 200, r.text
    return r


# --------------------------------------------------------------------------- #
# Roles                                                                        #
# --------------------------------------------------------------------------- #
async def test_builtin_roles_seeded_and_protected(client):
    c, _, _ = client
    await _bootstrap_admin(c)
    r = await c.get("/api/v1/rbac/roles")
    assert r.status_code == 200, r.text
    roles = {x["name"]: x for x in r.json()}
    assert set(roles) >= {"admin", "user", "viewer"}
    assert roles["admin"]["builtin"] and roles["admin"]["bypass"]
    assert roles["admin"]["users"] == 1
    assert set(roles["viewer"]["ceiling_actions"]) == {"search_metadata", "search_content"}
    # undeletable
    assert (await c.delete("/api/v1/rbac/roles/viewer")).status_code == 409
    # admin cannot lose the admin scope
    r = await c.patch("/api/v1/rbac/roles/admin", json={"scopes": ["read", "write"]})
    assert r.status_code == 409, r.text
    # but its description can change and its ceiling can be edited
    r = await c.patch("/api/v1/rbac/roles/admin", json={"description": "the boss"})
    assert r.status_code == 200 and r.json()["description"] == "the boss"


async def test_custom_role_lifecycle_and_enforcement(client):
    c, maker, _ = client
    await _bootstrap_admin(c)
    # create: read+write scopes but a viewer-ish ceiling plus download
    r = await c.post(
        "/api/v1/rbac/roles",
        json={
            "name": "curator",
            "display_name": "Curator",
            "description": "edits metadata, no downloads",
            "scopes": ["write"],
            "ceiling_actions": ["search_metadata", "edit_metadata"],
        },
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["scopes"] == ["read", "write"]  # write implies read
    assert body["bypass"] is False and body["users"] == 0
    # bad names / dupes / unknown perms
    assert (
        await c.post("/api/v1/rbac/roles", json={"name": "Bad Name", "display_name": "x"})
    ).status_code == 422
    assert (
        await c.post("/api/v1/rbac/roles", json={"name": "curator", "display_name": "x"})
    ).status_code == 409
    r = await c.post(
        "/api/v1/rbac/roles", json={"name": "zz", "display_name": "x", "scopes": ["root"]}
    )
    assert r.status_code == 422
    # assign a user to it
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "cara", "password": "cara-pw-1234", "global_role": "curator"},
    )
    assert r.status_code == 201, r.text
    # unknown role refused
    r = await c.post(
        "/api/v1/auth/users",
        json={"username": "nope", "password": "nope-pw-1234", "global_role": "ghost"},
    )
    assert r.status_code == 422
    # role in use cannot be deleted
    assert (await c.delete("/api/v1/rbac/roles/curator")).status_code == 409
    # the custom role's scopes are what the auth gate sees
    await c.post("/api/v1/auth/logout")
    await _login(c, "cara", "cara-pw-1234")
    assert (await c.get("/api/v1/libraries")).status_code == 200  # read
    assert (await c.get("/api/v1/auth/users")).status_code == 403  # no admin
    me = (await c.get("/api/v1/auth/me")).json()
    assert me["global_role"] == "curator"
    # tighten the role live: drop write -> a write endpoint 403s on the next request
    await c.post("/api/v1/auth/logout")
    await _login(c, "admin", "admin-pw-123")
    r = await c.patch("/api/v1/rbac/roles/curator", json={"scopes": ["read"]})
    assert r.status_code == 200 and r.json()["scopes"] == ["read"]
    await c.post("/api/v1/auth/logout")
    await _login(c, "cara", "cara-pw-1234")
    r = await c.post("/api/v1/scans", json={})  # any write-scoped route
    assert r.status_code in (403, 404, 405, 422)
    if r.status_code != 403:
        # find a definitely write-gated route: PATCH a library that does not exist
        r = await c.patch(
            "/api/v1/libraries/00000000-0000-0000-0000-000000000000", json={"name": "x"}
        )
        assert r.status_code == 403, r.text
    # reassign + delete
    await c.post("/api/v1/auth/logout")
    await _login(c, "admin", "admin-pw-123")
    users = {u["username"]: u for u in (await c.get("/api/v1/auth/users")).json()}
    r = await c.patch(f"/api/v1/auth/users/{users['cara']['id']}", json={"global_role": "viewer"})
    assert r.status_code == 200 and r.json()["global_role"] == "viewer"
    assert (await c.delete("/api/v1/rbac/roles/curator")).status_code == 204
    assert authx.scopes_for_role("curator") == frozenset()  # registry bumped


async def test_compare_matrix_shape(client):
    c, _, _ = client
    await _bootstrap_admin(c)
    await c.post(
        "/api/v1/rbac/roles",
        json={"name": "auditor", "display_name": "Auditor", "clone_from": "viewer"},
    )
    r = await c.get("/api/v1/rbac/roles/compare")
    assert r.status_code == 200, r.text
    b = r.json()
    assert set(b["scopes"]) == {"read", "write", "admin"}
    assert "search_metadata" in b["actions"] and b["action_help"]["download"]
    names = {x["name"] for x in b["roles"]}
    assert {"admin", "user", "viewer", "auditor"} <= names
    assert b["matrix"]["auditor"]["action:search_metadata"] is True
    assert b["matrix"]["auditor"]["scope:admin"] is False
    assert b["matrix"]["admin"]["scope:admin"] is True
    assert b["users_by_role"]["admin"] == ["admin"]


# --------------------------------------------------------------------------- #
# Account                                                                      #
# --------------------------------------------------------------------------- #
async def test_profile_and_preferences_roundtrip(client):
    c, _, _ = client
    await _bootstrap_admin(c)
    r = await c.patch(
        "/api/v1/auth/me",
        json={"display_name": "Ada L.", "email": "ada@example.com", "phone": "+1 555 0100"},
    )
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["display_name"] == "Ada L." and b["phone"] == "+1 555 0100"
    # username change (local account) + normalisation
    r = await c.patch("/api/v1/auth/me", json={"username": "ADA"})
    assert r.status_code == 200 and r.json()["username"] == "ada"
    # /me reflects it; old name no longer logs in, new one does
    assert (await c.get("/api/v1/auth/me")).json()["username"] == "ada"
    await c.post("/api/v1/auth/logout")
    assert (
        await c.post("/api/v1/auth/login", json={"username": "admin", "password": "admin-pw-123"})
    ).status_code == 401
    await _login(c, "ada", "admin-pw-123")
    # preferences
    r = await c.put(
        "/api/v1/auth/me/preferences", json={"theme": {"mode": "dark", "accent": "#00aa88"}}
    )
    assert r.status_code == 200 and r.json()["preferences"]["theme"]["mode"] == "dark"
    assert (await c.get("/api/v1/auth/me")).json()["preferences"]["theme"]["accent"] == "#00aa88"
    # too large
    r = await c.put("/api/v1/auth/me/preferences", json={"blob": "x" * 9000})
    assert r.status_code == 422
    # username clash
    await c.post("/api/v1/auth/users", json={"username": "bob", "password": "bob-pw-12345"})
    assert (await c.patch("/api/v1/auth/me", json={"username": "bob"})).status_code == 409


async def test_federated_username_locked(client):
    c, maker, _ = client
    await _bootstrap_admin(c)
    async with maker() as s:
        await s.execute(text("UPDATE users SET auth_provider='oidc' WHERE username='admin'"))
        await s.commit()
    r = await c.patch("/api/v1/auth/me", json={"username": "other"})
    assert r.status_code == 409
    # display name still editable
    assert (await c.patch("/api/v1/auth/me", json={"display_name": "A"})).status_code == 200


# --------------------------------------------------------------------------- #
# Session timeouts                                                             #
# --------------------------------------------------------------------------- #
async def test_global_and_per_user_timeouts(client):
    c, maker, settings = client
    await _bootstrap_admin(c)
    r = await c.get("/api/v1/auth/settings/session")
    assert r.status_code == 200, r.text
    b = r.json()
    assert b["inactivity_source"] == "env" and b["inactivity_hours"] == float(
        settings.session_inactivity_hours
    )
    # set a global idle window of 1 hour
    r = await c.patch("/api/v1/auth/settings/session", json={"inactivity_hours": 1})
    assert r.status_code == 200, r.text
    assert r.json()["inactivity_hours"] == 1 and r.json()["inactivity_source"] == "global"
    # out of band
    assert (
        await c.patch("/api/v1/auth/settings/session", json={"ttl_hours": 0.001})
    ).status_code == 422
    # my effective timeouts
    me = (await c.get("/api/v1/auth/me/session-timeouts")).json()
    assert me["inactivity_hours"] == 1 and me["inactivity_source"] == "global"
    # per-user override on a second user
    r = await c.post("/api/v1/auth/users", json={"username": "kiosk", "password": "kiosk-pw-123"})
    kid = r.json()["id"]
    r = await c.patch(
        f"/api/v1/auth/users/{kid}", json={"session_inactivity_hours": 2, "session_ttl_hours": 3}
    )
    assert r.status_code == 200, r.text
    assert r.json()["session_inactivity_hours"] == 2 and r.json()["session_ttl_hours"] == 3
    # log in as kiosk: absolute expiry = 3h, idle = 2h
    await c.post("/api/v1/auth/logout")
    await _login(c, "kiosk", "kiosk-pw-123")
    me = (await c.get("/api/v1/auth/me/session-timeouts")).json()
    assert me["inactivity_hours"] == 2 and me["inactivity_source"] == "user"
    assert me["ttl_hours"] == 3 and me["ttl_source"] == "user"
    async with maker() as s:
        row = (await s.execute(text("SELECT expires_absolute, created_at FROM sessions"))).one()
        assert timedelta(hours=2.9) < (row[0] - row[1]) < timedelta(hours=3.1)
        # age the session 2.5h idle -> beyond the 2h per-user window
        await s.execute(
            text("UPDATE sessions SET last_seen_at = :t"),
            {"t": datetime.now(UTC) - timedelta(hours=2.5)},
        )
        await s.commit()
    assert (await c.get("/api/v1/auth/me")).status_code == 401
    # clear the override (0) -> back to global 1h; global cleared -> env
    await _login(c, "admin", "admin-pw-123")
    r = await c.patch(f"/api/v1/auth/users/{kid}", json={"session_inactivity_hours": 0})
    assert r.json()["session_inactivity_hours"] is None
    r = await c.patch("/api/v1/auth/settings/session", json={"inactivity_hours": 0})
    assert r.json()["inactivity_source"] == "env"
