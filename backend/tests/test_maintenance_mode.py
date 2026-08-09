"""Global maintenance mode (2026-08-09): state API, scheduler gates, agent
advertisement (poll header + replication 503), manual-scan 409, and the new
agent-scoped suspend / agent_maintenance command endpoints.

Runs against the migrated pgserver Postgres (mirrors test_agent_inventory's
harness).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr import maintmode
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
        await conn.execute(
            text("UPDATE maintenance_mode SET active = false, reason = NULL")
        )
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _seed_agent(maker) -> tuple[uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        s.add(agent)
        await s.commit()
        return agent.id, fp


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


# --------------------------------------------------------------------------- #
# State API                                                                    #
# --------------------------------------------------------------------------- #
async def test_mode_toggle_roundtrip(client):
    c, _ = client
    r = await c.get("/api/v1/system/maintenance-mode")
    assert r.status_code == 200
    assert r.json() == {"active": False, "reason": None, "started_at": None}

    r = await c.post(
        "/api/v1/system/maintenance-mode",
        json={"active": True, "reason": "pg_dump"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["active"] is True
    assert body["reason"] == "pg_dump"
    assert body["started_at"] is not None

    # Idempotent re-activation keeps the original edge stamp.
    started = body["started_at"]
    r = await c.post(
        "/api/v1/system/maintenance-mode", json={"active": True, "reason": "pg_dump"}
    )
    assert r.json()["started_at"] == started

    r = await c.post("/api/v1/system/maintenance-mode", json={"active": False})
    assert r.json() == {"active": False, "reason": None, "started_at": None}


@pytest.mark.skipif(
    not hasattr(__import__("os"), "statvfs"),
    reason="jobs summary's disk probe is POSIX-only (Windows baseline)",
)
async def test_jobs_summary_carries_maintenance(client):
    c, _ = client
    await c.post(
        "/api/v1/system/maintenance-mode", json={"active": True, "reason": "backup"}
    )
    r = await c.get("/api/v1/system/jobs/summary")
    assert r.status_code == 200
    m = r.json()["maintenance"]
    assert m["active"] is True and m["reason"] == "backup"


# --------------------------------------------------------------------------- #
# Scheduler gates                                                              #
# --------------------------------------------------------------------------- #
async def test_scheduler_gates_skip_while_active(client, monkeypatch):
    c, maker = client
    from filearr import maintenance as maint_tasks
    from filearr import worker

    async with maker() as s:
        lib = Library(
            name="lib-" + uuid.uuid4().hex[:8],
            root_path="/data",
            scan_cron="* * * * *",
        )
        s.add(lib)
        await s.commit()

    await c.post("/api/v1/system/maintenance-mode", json={"active": True})
    tick = datetime.now(UTC)
    assert await worker._defer_due_scans(tick) == []
    assert await maint_tasks.run_maintenance_tick(tick) == []
    assert await worker._defer_scan_if_idle("whatever") is None

    # Occurrences were NOT consumed: last_cron_fired_at stays untouched.
    async with maker() as s:
        row = (
            await s.execute(text("SELECT last_cron_fired_at FROM libraries"))
        ).scalar()
        assert row is None


async def test_manual_scan_trigger_409_while_active(client):
    c, maker = client
    async with maker() as s:
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add(lib)
        await s.commit()
        lib_id = lib.id
    await c.post("/api/v1/system/maintenance-mode", json={"active": True, "reason": "x"})
    r = await c.post(f"/api/v1/libraries/{lib_id}/scan")
    assert r.status_code == 409
    assert "maintenance mode" in r.json()["detail"]
    r = await c.post(
        f"/api/v1/libraries/{lib_id}/scan/targeted", json={"path": "", "recursive": True}
    )
    assert r.status_code == 409


# --------------------------------------------------------------------------- #
# Agent advertisement                                                          #
# --------------------------------------------------------------------------- #
async def test_poll_header_advertises_maintenance(client):
    c, maker = client
    agent_id, fp = await _seed_agent(maker)

    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert r.status_code == 200
    assert "X-Filearr-Maintenance" not in r.headers

    await c.post("/api/v1/system/maintenance-mode", json={"active": True})
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert r.status_code == 200
    assert r.headers.get("X-Filearr-Maintenance") == "1"


async def test_replication_batch_503_while_active(client):
    c, maker = client
    agent_id, fp = await _seed_agent(maker)
    await c.post("/api/v1/system/maintenance-mode", json={"active": True})
    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        json={"agent_id": str(agent_id), "entries": []},
        headers=_auth(fp),
    )
    assert r.status_code == 503
    assert r.json()["reason"] == "maintenance"
    assert r.headers.get("Retry-After") == str(maintmode.RETRY_AFTER_SECONDS)


# --------------------------------------------------------------------------- #
# Agent-scoped commands                                                        #
# --------------------------------------------------------------------------- #
async def test_suspend_endpoint_enqueues_and_collapses(client):
    c, maker = client
    agent_id, _ = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/suspend", json={"suspended": True})
    assert r.status_code == 201, r.text
    first = r.json()
    assert first["kind"] == "suspend"
    assert first["payload"] == {"suspended": True}
    assert first["item_id"] is None

    # A second desire while still pending COLLAPSES onto the same command row.
    r = await c.post(f"/api/v1/agents/{agent_id}/suspend", json={"suspended": False})
    assert r.status_code == 201
    second = r.json()
    assert second["id"] == first["id"]
    assert second["payload"] == {"suspended": False}
    async with maker() as s:
        n = (
            await s.execute(
                text("SELECT COUNT(*) FROM agent_commands WHERE kind = 'suspend'")
            )
        ).scalar()
        assert n == 1


async def test_agent_maintenance_endpoint_409_while_in_flight(client):
    c, maker = client
    agent_id, _ = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/maintenance")
    assert r.status_code == 201, r.text
    assert r.json()["kind"] == "agent_maintenance"

    r = await c.post(f"/api/v1/agents/{agent_id}/maintenance")
    assert r.status_code == 409

    # Completing the first frees the gate.
    async with maker() as s:
        await s.execute(
            text(
                "UPDATE agent_commands SET status = 'done', completed_at = now()"
                " WHERE kind = 'agent_maintenance'"
            )
        )
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/maintenance")
    assert r.status_code == 201


async def test_poll_persists_suspended_health(client):
    c, maker = client
    agent_id, fp = await _seed_agent(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "health": {"uptime_s": 5, "suspended": True}},
        headers=_auth(fp),
    )
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.health["suspended"] is True
