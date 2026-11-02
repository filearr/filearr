"""Session-token rotation over real HTTP (2026-11-02 spurious-logout fix).

Regression set for "login session times out quickly": rotation used to ride the
request transaction (rolled back by endpoints that never commit, e.g. GET
/auth/me) and the new cookie rode FastAPI's injected Response (dropped whenever
an endpoint returns its own Response or raises). Now:

* rotation is a conditional UPDATE committed in its own transaction;
* the rotated cookie is appended by ``SessionCookieMiddleware`` to EVERY
  response (model, 204, 404, 403, streaming);
* the previous token stays valid until the new one is first presented (then
  only for the short in-flight grace), and a lost cookie is re-issued;
* concurrent rotations: exactly one wins, nobody 401s;
* cookie Max-Age follows the session's effective (per-user) TTL.

Cookies are sent explicitly (the client jar is cleared around every call) so
each assertion controls exactly which token a request carries.
"""

from __future__ import annotations

import asyncio
import uuid
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
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Principal, User

BACKEND_DIR = Path(__file__).resolve().parent.parent
COOKIE = "filearr_session"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        for t in (
            "security_events",
            "auth_rate_limits",
            "sessions",
            "users",
            "api_keys",
            "app_settings",
            "principals",
        ):
            await conn.execute(text(f"DELETE FROM {t}"))
    app_settings.reset_for_tests()
    yield async_sessionmaker(engine, expire_on_commit=False)
    app_settings.reset_for_tests()
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    app = create_app()

    async def _test_session():
        async with db_maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        yield c, settings
    app.dependency_overrides.clear()


async def _seed_user(maker, username, password, role="admin", ttl_hours=None):
    async with maker() as s:
        p = Principal(kind="user", global_role=role, session_ttl_hours=ttl_hours)
        s.add(p)
        await s.flush()
        s.add(
            User(
                principal_id=p.id,
                username=username,
                password_hash=authx.hash_password(password),
                auth_provider="local",
            )
        )
        await s.commit()
        return str(p.id)


def _session_cookie(r: httpx.Response) -> tuple[str, int] | None:
    """(value, max_age) of the filearr_session Set-Cookie, or None."""
    found = [h for h in r.headers.get_list("set-cookie") if h.startswith(f"{COOKIE}=")]
    assert len(found) <= 1, found
    if not found:
        return None
    parts = [p.strip() for p in found[0].split(";")]
    value = parts[0].split("=", 1)[1]
    max_age = next(int(p.split("=", 1)[1]) for p in parts if p.lower().startswith("max-age="))
    lowered = [p.lower() for p in parts]
    assert "httponly" in lowered and "path=/" in lowered
    return value, max_age


async def _call(c, method, url, token, **kw):
    c.cookies.clear()
    r = await c.request(method, url, headers={"cookie": f"{COOKIE}={token}"}, **kw)
    c.cookies.clear()
    return r


async def _login(c, username, password) -> str:
    c.cookies.clear()
    r = await c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    got = _session_cookie(r)
    assert got is not None
    c.cookies.clear()
    return got[0]


async def _age(maker, **cols):
    """Push timestamp columns of every session row into the past."""
    sets = ", ".join(f"{k} = :{k}" for k in cols)
    async with maker() as s:
        await s.execute(text(f"UPDATE sessions SET {sets}"), cols)
        await s.commit()


async def _age_rotation(maker):
    await _age(maker, rotated_at=datetime.now(UTC) - timedelta(minutes=11))


async def _row(maker):
    async with maker() as s:
        return (
            await s.execute(
                text(
                    "SELECT session_hash, prev_session_hash, prev_valid_until, rotated_at "
                    "FROM sessions"
                )
            )
        ).one()


async def _assert_rotation_lifecycle(c, maker, method, url, expect_status, old, **kw):
    """Shared body: a rotating request on ``url`` delivers the new cookie; old and
    new both work; first use of the new token shrinks the old one to the grace;
    after the grace the old token is dead and the new one keeps working."""
    await _age_rotation(maker)
    r = await _call(c, method, url, old, **kw)
    assert r.status_code == expect_status, r.text
    got = _session_cookie(r)
    assert got is not None, f"{method} {url} dropped the rotated cookie"
    new = got[0]
    assert new != old
    # Persisted (NOT rolled back with the request) and the old token is on the
    # long cap, not the 60 s grace.
    row = await _row(maker)
    assert row.session_hash == authx.hash_session_token(new)
    assert row.prev_session_hash == authx.hash_session_token(old)
    assert row.prev_valid_until > datetime.now(UTC) + timedelta(hours=11)

    # Old token: still good, never rotates again, no cookie.
    r_old = await _call(c, "GET", "/api/v1/auth/me", old)
    assert r_old.status_code == 200 and _session_cookie(r_old) is None
    # New token: good, no cookie; this first use confirms it.
    r_new = await _call(c, "GET", "/api/v1/auth/me", new)
    assert r_new.status_code == 200 and _session_cookie(r_new) is None
    row = await _row(maker)
    assert row.prev_valid_until <= datetime.now(UTC) + timedelta(seconds=61)
    # Siblings already in flight with the old cookie survive the grace...
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 200
    # ...and once it lapses the old token is dead; the new one lives on.
    await _age(maker, prev_valid_until=datetime.now(UTC) - timedelta(seconds=1))
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 401
    assert (await _call(c, "GET", "/api/v1/auth/me", new)).status_code == 200
    return new


# --------------------------------------------------------------------------- #
# Each response shape delivers the cookie and the rotation sticks             #
# --------------------------------------------------------------------------- #
async def test_rotation_on_get_me_which_never_commits(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "ann", "ann-pw-12345")
    old = await _login(c, "ann", "ann-pw-12345")
    await _assert_rotation_lifecycle(c, db_maker, "GET", "/api/v1/auth/me", 200, old)


@pytest.mark.parametrize("url", ["/api/v1/auth/sessions", "/api/v1/auth/me/session-timeouts"])
async def test_rotation_on_other_read_only_auth_endpoints(client, db_maker, url):
    c, _ = client
    await _seed_user(db_maker, "bea", "bea-pw-12345")
    old = await _login(c, "bea", "bea-pw-12345")
    await _assert_rotation_lifecycle(c, db_maker, "GET", url, 200, old)


async def test_rotation_on_bare_204_response(client, db_maker, monkeypatch):
    c, settings = client
    # Frecency off → touch returns its own bare Response(204) (the shape whose
    # injected-Response headers FastAPI used to drop).
    monkeypatch.setattr(settings, "frecency_enabled", False)
    await _seed_user(db_maker, "cat", "cat-pw-12345")
    old = await _login(c, "cat", "cat-pw-12345")
    await _assert_rotation_lifecycle(
        c, db_maker, "POST", f"/api/v1/items/{uuid.uuid4()}/touch", 204, old
    )


async def test_rotation_on_404_error_response(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "dan", "dan-pw-12345")
    old = await _login(c, "dan", "dan-pw-12345")
    await _assert_rotation_lifecycle(
        c, db_maker, "POST", f"/api/v1/items/{uuid.uuid4()}/touch", 404, old
    )


async def test_rotation_on_403_error_response(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "vic", "vic-pw-12345", role="viewer")
    old = await _login(c, "vic", "vic-pw-12345")
    # A viewer hitting an admin-scoped endpoint: the rotation already committed,
    # so even the refusal must carry the new cookie.
    await _assert_rotation_lifecycle(c, db_maker, "GET", "/api/v1/auth/users", 403, old)


async def test_rotation_on_streaming_download(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "eve", "eve-pw-12345")
    old = await _login(c, "eve", "eve-pw-12345")
    await _age_rotation(db_maker)
    r = await _call(c, "GET", "/api/v1/reports/largest_files?format=csv", old)
    assert r.status_code == 200, r.text
    assert r.headers["content-type"].startswith("text/csv")
    assert r.text  # body streamed through the middleware untouched (header row)
    got = _session_cookie(r)
    assert got is not None
    assert (await _call(c, "GET", "/api/v1/auth/me", got[0])).status_code == 200
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 200


# --------------------------------------------------------------------------- #
# Lost cookie / concurrency                                                   #
# --------------------------------------------------------------------------- #
async def test_lost_cookie_is_reissued_from_previous_token(client, db_maker):
    """The rotating response never reached the browser (client abort): it keeps
    presenting the old token. Past the in-flight grace that re-issues a fresh
    cookie instead of drifting toward a logout."""
    c, _ = client
    await _seed_user(db_maker, "fay", "fay-pw-12345")
    old = await _login(c, "fay", "fay-pw-12345")
    await _age_rotation(db_maker)
    r = await _call(c, "GET", "/api/v1/auth/me", old)
    lost = _session_cookie(r)[0]  # "lost": the browser never stores it
    # Inside the grace: old works, no re-issue (siblings in flight).
    r = await _call(c, "GET", "/api/v1/auth/me", old)
    assert r.status_code == 200 and _session_cookie(r) is None
    # Past the grace, still unconfirmed → re-issue.
    await _age(db_maker, rotated_at=datetime.now(UTC) - timedelta(minutes=2))
    r = await _call(c, "GET", "/api/v1/auth/me", old)
    assert r.status_code == 200
    reissued = _session_cookie(r)[0]
    assert reissued not in (old, lost)
    assert (await _call(c, "GET", "/api/v1/auth/me", reissued)).status_code == 200
    # The never-delivered token was replaced; the old one stays prev until the
    # reissued one was used (it just was → grace only).
    assert (await _call(c, "GET", "/api/v1/auth/me", lost)).status_code == 401
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 200


async def test_previous_token_valid_past_old_grace_until_new_is_used(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "gus", "gus-pw-12345")
    old = await _login(c, "gus", "gus-pw-12345")
    await _age_rotation(db_maker)
    assert _session_cookie(await _call(c, "GET", "/api/v1/auth/me", old)) is not None
    # Minutes later (way past the old fixed 60 s grace) the new token was
    # never presented: the old one must still authenticate.
    await _age(db_maker, rotated_at=datetime.now(UTC) - timedelta(minutes=5))
    r = await _call(c, "GET", "/api/v1/auth/me", old)
    assert r.status_code == 200
    assert _session_cookie(r) is not None  # (re-issued, see test above)


async def test_concurrent_rotation_single_winner_nobody_401s(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "hal", "hal-pw-12345")
    old = await _login(c, "hal", "hal-pw-12345")
    await _age_rotation(db_maker)
    c.cookies.clear()
    hdr = {"cookie": f"{COOKIE}={old}"}
    responses = await asyncio.gather(*(c.get("/api/v1/auth/me", headers=hdr) for _ in range(6)))
    c.cookies.clear()
    assert [r.status_code for r in responses] == [200] * 6
    issued = [got[0] for r in responses if (got := _session_cookie(r)) is not None]
    assert len(issued) == 1, issued  # exactly one rotation won
    new = issued[0]
    row = await _row(db_maker)
    assert row.session_hash == authx.hash_session_token(new)
    assert row.prev_session_hash == authx.hash_session_token(old)
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 200
    assert (await _call(c, "GET", "/api/v1/auth/me", new)).status_code == 200


async def test_concurrent_mixed_endpoints_during_rotation(client, db_maker, monkeypatch):
    c, settings = client
    monkeypatch.setattr(settings, "frecency_enabled", False)
    await _seed_user(db_maker, "ivy", "ivy-pw-12345")
    old = await _login(c, "ivy", "ivy-pw-12345")
    await _age_rotation(db_maker)
    c.cookies.clear()
    hdr = {"cookie": f"{COOKIE}={old}"}
    rs = await asyncio.gather(
        c.get("/api/v1/auth/me", headers=hdr),
        c.post(f"/api/v1/items/{uuid.uuid4()}/touch", headers=hdr),
        c.get("/api/v1/auth/sessions", headers=hdr),
        c.get("/api/v1/reports/largest_files?format=csv", headers=hdr),
    )
    c.cookies.clear()
    assert [r.status_code for r in rs] == [200, 204, 200, 200]
    issued = [got[0] for r in rs if (got := _session_cookie(r)) is not None]
    assert len(issued) == 1
    assert (await _call(c, "GET", "/api/v1/auth/me", issued[0])).status_code == 200


# --------------------------------------------------------------------------- #
# Max-Age / logout / override validation                                      #
# --------------------------------------------------------------------------- #
async def test_cookie_max_age_follows_per_user_ttl_override(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "jo", "jo-pw-123456", ttl_hours=3)
    c.cookies.clear()
    r = await c.post("/api/v1/auth/login", json={"username": "jo", "password": "jo-pw-123456"})
    c.cookies.clear()
    tok, max_age = _session_cookie(r)
    assert 3 * 3600 - 30 <= max_age <= 3 * 3600  # not the 30-day env default
    await _age_rotation(db_maker)
    _, rotated_age = _session_cookie(await _call(c, "GET", "/api/v1/auth/me", tok))
    assert 3 * 3600 - 30 <= rotated_age <= 3 * 3600


async def test_cookie_max_age_follows_global_ttl_setting(client, db_maker):
    c, _ = client
    async with db_maker() as s:
        await app_settings.set_value(s, app_settings.KEY_SESSION_TTL_HOURS, 5, updated_by=None)
        await s.commit()
    app_settings.reset_for_tests()
    await _seed_user(db_maker, "kim", "kim-pw-12345")
    c.cookies.clear()
    r = await c.post("/api/v1/auth/login", json={"username": "kim", "password": "kim-pw-12345"})
    c.cookies.clear()
    _, max_age = _session_cookie(r)
    assert 5 * 3600 - 30 <= max_age <= 5 * 3600


async def test_logout_with_previous_token_revokes_and_clears(client, db_maker):
    c, _ = client
    await _seed_user(db_maker, "lu", "lu-pw-123456")
    old = await _login(c, "lu", "lu-pw-123456")
    await _age_rotation(db_maker)
    new = _session_cookie(await _call(c, "GET", "/api/v1/auth/me", old))[0]
    r = await _call(c, "POST", "/api/v1/auth/logout", old)
    assert r.status_code == 204
    cleared = _session_cookie(r)
    assert cleared is not None and cleared[1] == 0  # delete_cookie actually sent
    assert (await _call(c, "GET", "/api/v1/auth/me", new)).status_code == 401
    assert (await _call(c, "GET", "/api/v1/auth/me", old)).status_code == 401


@pytest.mark.parametrize("field", ["session_ttl_hours", "session_inactivity_hours"])
async def test_override_rounding_to_zero_is_rejected(client, db_maker, field):
    c, _ = client
    await _seed_user(db_maker, "root", "root-pw-1234")
    kid = await _seed_user(db_maker, "kid", "kid-pw-12345", role="user")
    tok = await _login(c, "root", "root-pw-1234")
    r = await _call(c, "PATCH", f"/api/v1/auth/users/{kid}", tok, json={field: 0.3})
    assert r.status_code == 422, r.text
    r = await _call(c, "PATCH", f"/api/v1/auth/users/{kid}", tok, json={field: 0.6})
    assert r.status_code == 200 and r.json()[field] == 1
    r = await _call(c, "PATCH", f"/api/v1/auth/users/{kid}", tok, json={field: 0})
    assert r.status_code == 200 and r.json()[field] is None
