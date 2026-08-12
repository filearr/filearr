"""BK-T1 — the key-fingerprint guard.

The defect: restoring a Postgres dump onto a box with a freshly generated
``FILEARR_SECRET_KEY`` succeeds at every observable step while leaving every
encrypted alert-channel secret permanently undecryptable, and NOTHING says so.
These tests pin the four states the guard distinguishes, the two surfaces it
reports through (``/stats`` ``degraded`` and ``/system/about``), and the
non-negotiables: never store or emit a key value, never refuse to boot, never
raise out of the check.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import keyguard
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent

KEY_A = "unit-test-secret-key-AAAA"
KEY_B = "unit-test-secret-key-BBBB"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def ctx(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        # Each test starts from "this database has never been stamped", which is
        # exactly the fresh-restore state the guard has to handle.
        await conn.execute(text("DELETE FROM instance_meta"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "secret_key", KEY_A)
    monkeypatch.setattr(settings, "ca_fingerprint", "")
    # The process-local cache is module state; a leaked value from another test
    # would make `mismatches()` answer about the wrong run.
    monkeypatch.setattr(keyguard, "_last", None)
    yield maker, settings, monkeypatch
    await engine.dispose()


# --------------------------------------------------------------------------- #
# The fingerprint itself                                                       #
# --------------------------------------------------------------------------- #


def test_fingerprint_is_truncated_sha256_and_never_the_value():
    fp = keyguard.fingerprint(KEY_A)
    assert fp == hashlib.sha256(KEY_A.encode()).hexdigest()[:16]
    assert len(fp) == 16
    assert all(c in "0123456789abcdef" for c in fp)
    # The guard's whole safety argument is that this leaks nothing: the value
    # must not appear in, or be recoverable from, the fingerprint.
    assert KEY_A not in fp
    assert keyguard.fingerprint(KEY_B) != fp


# --------------------------------------------------------------------------- #
# The three states                                                             #
# --------------------------------------------------------------------------- #


async def test_fresh_database_stamps_and_is_silent(ctx):
    """State 1: no stored fingerprint -> record it. First run, or an instance
    that predates the guard. Must not warn: there is nothing wrong."""
    maker, settings, _ = ctx
    async with maker() as session:
        result = await keyguard.check_all(session, settings)
    assert result["secret_key"]["state"] == "stamped"
    assert result["secret_key"]["recorded"] == keyguard.fingerprint(KEY_A)
    assert keyguard.mismatches(result) == {}

    # And the stamp is durable — it is what rides the next pg_dump.
    async with maker() as session:
        row = await session.get(
            __import__("filearr.models", fromlist=["InstanceMeta"]).InstanceMeta,
            keyguard.SECRET_KEY_FP,
        )
    assert row is not None and row.value == keyguard.fingerprint(KEY_A)
    assert KEY_A not in row.value


async def test_matching_key_is_silent(ctx):
    """State 2: stored == current. The overwhelmingly common case; it must
    produce no log noise, no banner and no degraded entry."""
    maker, settings, _ = ctx
    async with maker() as session:
        await keyguard.check_all(session, settings)
        result = await keyguard.check_all(session, settings)
    assert result["secret_key"]["state"] == "match"
    assert result["secret_key"]["recorded_at"]  # the original stamp survives
    assert keyguard.mismatches(result) == {}


async def test_changed_key_reports_mismatch_and_does_not_overwrite(ctx):
    """State 3: the silent failure, made loud.

    Also pins the non-obvious half: a mismatch must NOT re-stamp. The recorded
    fingerprint is the evidence of what the ciphertext was actually written
    under, and a self-clearing warning is no warning at all."""
    maker, settings, monkeypatch = ctx
    async with maker() as session:
        await keyguard.check_all(session, settings)
    monkeypatch.setattr(settings, "secret_key", KEY_B)
    async with maker() as session:
        result = await keyguard.check_all(session, settings)
    sub = result["secret_key"]
    assert sub["state"] == "mismatch"
    assert sub["recorded"] == keyguard.fingerprint(KEY_A)
    assert sub["current"] == keyguard.fingerprint(KEY_B)
    assert "no longer be decrypted" in sub["reason"]
    assert keyguard.mismatches(result) == {"secret_key": sub["reason"]}

    # Second look: still a mismatch, still remembering key A.
    async with maker() as session:
        again = await keyguard.check_all(session, settings)
    assert again["secret_key"]["state"] == "mismatch"
    assert again["secret_key"]["recorded"] == keyguard.fingerprint(KEY_A)


async def test_missing_key_does_not_crash_and_is_reported(ctx):
    """A key that is simply absent must never raise. Unstamped -> "unset"
    (silent: a stack with no alert channels needs no key). Stamped -> "missing",
    which breaks exactly what a wrong key breaks and so is equally loud."""
    maker, settings, monkeypatch = ctx
    monkeypatch.setattr(settings, "secret_key", None)
    async with maker() as session:
        result = await keyguard.check_all(session, settings)
    assert result["secret_key"]["state"] == "unset"
    assert keyguard.mismatches(result) == {}

    monkeypatch.setattr(settings, "secret_key", KEY_A)
    async with maker() as session:
        await keyguard.check_all(session, settings)
    monkeypatch.setattr(settings, "secret_key", None)
    async with maker() as session:
        result = await keyguard.check_all(session, settings)
    assert result["secret_key"]["state"] == "missing"
    assert "FILEARR_SECRET_KEY is not set" in result["secret_key"]["reason"]
    assert set(keyguard.mismatches(result)) == {"secret_key"}


# --------------------------------------------------------------------------- #
# The CA half                                                                  #
# --------------------------------------------------------------------------- #


async def test_ca_root_stamps_normalised_and_reports_replacement(ctx):
    """The step-ca root pin gets the same treatment: losing ``stepca_data``
    makes step-ca mint a NEW root and every issued agent cert stops validating.
    Operators paste the pin with colons and in mixed case, so it is normalised
    before comparison — a formatting difference must never read as a new CA."""
    maker, settings, monkeypatch = ctx
    monkeypatch.setattr(settings, "ca_fingerprint", "AB:CD:EF01" + "23456789" * 6)
    async with maker() as session:
        first = await keyguard.check_all(session, settings)
    assert first["ca_root"]["state"] == "stamped"
    assert first["ca_root"]["recorded"] == "abcdef0123456789"

    monkeypatch.setattr(settings, "ca_fingerprint", "abcdef0123456789" + "0" * 48)
    async with maker() as session:
        same = await keyguard.check_all(session, settings)
    assert same["ca_root"]["state"] == "match"  # same first 16 hex, no colons

    monkeypatch.setattr(settings, "ca_fingerprint", "f" * 64)
    async with maker() as session:
        changed = await keyguard.check_all(session, settings)
    assert changed["ca_root"]["state"] == "mismatch"
    assert "re-enrollment" in changed["ca_root"]["reason"]


async def test_disabling_agents_does_not_warn_about_the_ca(ctx):
    """Turning the agents profile off is legitimate and reversible. Shouting
    "your CA vanished" at that operator would teach them to ignore this whole
    surface, so an unconfigured CA is `unset` even when one was recorded."""
    maker, settings, monkeypatch = ctx
    monkeypatch.setattr(settings, "ca_fingerprint", "a" * 64)
    async with maker() as session:
        await keyguard.check_all(session, settings)
    monkeypatch.setattr(settings, "ca_fingerprint", "")
    async with maker() as session:
        result = await keyguard.check_all(session, settings)
    assert result["ca_root"]["state"] == "unset"
    assert keyguard.mismatches(result) == {}


# --------------------------------------------------------------------------- #
# Totality: the check may never take the app down                              #
# --------------------------------------------------------------------------- #


async def test_check_is_total_when_the_table_is_missing(ctx):
    """The deploy window where a new image boots before alembic has run. The
    guard must degrade to `unknown`, not raise — it is a monitoring aid.

    The DROP is deliberately left UNCOMMITTED and the check runs on the same
    session: Postgres DDL is transactional, and the guard's own error path
    rolls back, which both proves the recovery works and leaves the shared test
    database intact. Committing the drop would poison every later test in the
    session (alembic is already at head, so it would not recreate the table)."""
    maker, settings, _ = ctx
    async with maker() as session:
        await session.execute(text("DROP TABLE instance_meta"))
        result = await keyguard.check_all(session, settings)
        assert result["secret_key"]["state"] == "unknown"
        assert result["secret_key"]["reason"]
        assert keyguard.mismatches(result) == {}
    # The guard rolled the failed transaction back, so the table is still there.
    async with maker() as session:
        assert (
            await session.execute(text("SELECT to_regclass('instance_meta')"))
        ).scalar() is not None


async def test_startup_check_never_raises_without_a_database(monkeypatch):
    """``run_startup_check`` runs in the app lifespan with its own session. A
    dead database must produce a warning and a boot, not a crash-loop."""
    class _Boom:
        def __call__(self):
            raise RuntimeError("no database here")

    monkeypatch.setattr(db_mod, "SessionLocal", _Boom())
    monkeypatch.setattr(keyguard, "_last", None)
    result = await keyguard.run_startup_check()
    assert result["secret_key"]["state"] == "unknown"
    assert keyguard.last_result() is result


# --------------------------------------------------------------------------- #
# The surfaces an operator actually looks at                                   #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(ctx):
    maker, settings, monkeypatch = ctx
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings, monkeypatch
    app.dependency_overrides.clear()


async def test_stats_and_about_surface_the_mismatch(client):
    """Both operator surfaces, in one test, because the requirement is that the
    condition is impossible to miss WITHOUT reading logs."""
    c, maker, settings, monkeypatch = client
    async with maker() as session:
        await keyguard.check_all(session, settings)  # stamp under key A
    monkeypatch.setattr(settings, "secret_key", KEY_B)

    stats = (await c.get("/api/v1/stats")).json()
    assert "secret_key" in stats["degraded"]
    assert "no longer be decrypted" in stats["degraded"]["secret_key"]
    assert stats["key_fingerprints"]["secret_key"]["state"] == "mismatch"

    about = (await c.get("/api/v1/system/about")).json()
    fp = about["application"]["key_fingerprints"]["secret_key"]
    assert fp["state"] == "mismatch"
    assert fp["recorded"] == keyguard.fingerprint(KEY_A)
    # Neither surface may ever carry a key value.
    assert KEY_A not in (await c.get("/api/v1/system/about")).text
    assert KEY_B not in (await c.get("/api/v1/stats")).text


async def test_healthy_instance_reports_nothing_degraded(client):
    """The silent case, end to end: a matching key adds no entry to `degraded`,
    so the dashboard banner stays off."""
    c, maker, settings, _ = client
    async with maker() as session:
        await keyguard.check_all(session, settings)
    stats = (await c.get("/api/v1/stats")).json()
    assert "secret_key" not in stats["degraded"]
    assert stats["key_fingerprints"]["secret_key"]["state"] == "match"
