"""The `reconcile` agent command (2026-08-22).

The console-triggered full-manifest sweep — exists because the agent's own
triggers (24h uptime ticker, >24h-outage reconnect, cursor dead-end) never
fire on a desktop-pattern machine (live: agent XENON, "Last reconcile:
never"). Covers the enqueue contract: agent-scoped, force_reset round trip,
in-flight 409, long TTL, kind constraint.
"""

from __future__ import annotations

import uuid
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

BACKEND_DIR = Path(__file__).resolve().parents[1]


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def client(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
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
    await engine.dispose()


async def _seed_agent(maker) -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name="xenon", hostname="xenon", platform="windows",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def test_reconcile_enqueues_agent_scoped_command(client):
    c, maker = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reconcile")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "reconcile"
    assert body["status"] == "pending"
    assert body["item_id"] is None  # agent-scoped
    assert body["payload"] == {"force_reset": False}

    # The long TTL: a mismatch streams a whole index; the 1h default would
    # falsely expire a faithfully-heartbeating sweep mid-run.
    async with maker() as s:
        secs = (
            await s.execute(text(
                "SELECT EXTRACT(EPOCH FROM (expires_at - created_at)) "
                "FROM agent_commands WHERE kind = 'reconcile'"
            ))
        ).scalar()
    assert int(secs) == 21_600


async def test_reconcile_force_reset_round_trip_and_409_in_flight(client):
    c, maker = client
    agent_id = await _seed_agent(maker)

    r = await c.post(f"/api/v1/agents/{agent_id}/reconcile", json={"force_reset": True})
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {"force_reset": True}

    # A second enqueue while one is pending is a 409, not a duplicate job —
    # two sweeps would race the per-root reconcile sessions.
    r = await c.post(f"/api/v1/agents/{agent_id}/reconcile")
    assert r.status_code == 409

    # Once the first is terminal, a new one enqueues normally.
    async with maker() as s:
        await s.execute(text(
            "UPDATE agent_commands SET status = 'done' WHERE kind = 'reconcile'"
        ))
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/reconcile")
    assert r.status_code == 201


async def test_reconcile_unknown_agent_404_and_kind_constraint(client):
    c, maker = client
    r = await c.post(f"/api/v1/agents/{uuid.uuid4()}/reconcile")
    assert r.status_code == 404

    # The DB constraint accepts the new kind (migration f762ced396e3).
    agent_id = await _seed_agent(maker)
    async with maker() as s:
        await s.execute(text(
            "INSERT INTO agent_commands (agent_id, kind, payload, status, expires_at) "
            "VALUES (:a, 'reconcile', '{}', 'pending', now() + interval '1 hour')"
        ), {"a": str(agent_id)})
        await s.commit()
