# ruff: noqa: E501
"""W6-D3 extensible inventory framework (central-side): capability persistence on
poll, the inventory-results receiver (auth / cap / gzip roundtrip / wrong-agent /
write-if-absent / non-inventory), and an inline inventory command completion.

Runs against the migrated pgserver Postgres (mirrors test_agent_commands' harness).
"""

from __future__ import annotations

import gzip
import json
import uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, AgentCommand, Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM permission_snapshots"))
        await conn.execute(text("DELETE FROM agent_commands"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


async def _seed(maker) -> tuple[uuid.UUID, uuid.UUID, str]:
    fp = "FP:" + uuid.uuid4().hex
    async with maker() as s:
        agent = Agent(name="nas", hostname="nas", platform="linux", cert_fingerprint=fp)
        lib = Library(name="lib-" + uuid.uuid4().hex[:8], root_path="/data")
        s.add_all([agent, lib])
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="video", file_group="video",
            path="/data/x.mkv",
            rel_path="x.mkv",
            filename="x.mkv",
            size=1,
            mtime=datetime.now(UTC),
        )
        s.add(item)
        await s.commit()
        return agent.id, item.id, fp


async def _mk_inventory_command(maker, agent_id, item_id) -> uuid.UUID:
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=agent_id,
            kind="inventory",
            item_id=item_id,
            payload={"preset": "user-documents", "collectors": ["stat"]},
            status="picked_up",
            picked_up_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        return cmd.id


@pytest.fixture
async def client(db_maker, tmp_path, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "inventory_dir", str(tmp_path / "inventory"))
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings, tmp_path
    app.dependency_overrides.clear()


def _auth(fp: str) -> dict:
    return {"Authorization": f"Bearer {fp}"}


# --------------------------------------------------------------------------- #
# Capability persistence on poll                                              #
# --------------------------------------------------------------------------- #
async def test_poll_persists_capabilities(client):
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    caps = {
        "inventory_collectors": ["owner", "perms", "placeholder", "stat"],
        "inventory_version": 1,
    }
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "capabilities": caps},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities == caps

    # A subsequent poll WITHOUT capabilities leaves the stored value untouched.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities == caps


async def test_poll_oversize_capabilities_dropped_not_fatal(client):
    c, maker, settings, _ = client
    agent_id, _, fp = await _seed(maker)
    monkeypatch_cap = 64
    settings.agent_capabilities_max_bytes = monkeypatch_cap
    big = {"inventory_collectors": ["x" * 200], "inventory_version": 1}
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "capabilities": big},
        headers=_auth(fp),
    )
    # The poll still succeeds; the oversize advertisement is simply dropped.
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.capabilities is None


# --------------------------------------------------------------------------- #
# inventory-results receiver                                                   #
# --------------------------------------------------------------------------- #
def _gz(payload: bytes) -> bytes:
    return gzip.compress(payload)


async def test_inventory_results_gzip_roundtrip_and_idempotent(client):
    c, maker, _, tmp_path = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    blob = _gz(b'{"rel":"a.txt","size":1}\n{"rel":"b.txt","size":2}\n')

    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid), "Content-Type": "application/gzip"},
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["result_ref"] == f"inventory/{cid}.ndjson.gz" and body["created"] is True

    stored = tmp_path / "inventory" / f"{cid}.ndjson.gz"
    assert stored.exists()
    assert gzip.decompress(stored.read_bytes()).startswith(b'{"rel":"a.txt"')

    # Write-if-absent: a redelivered upload is a 200 no-op.
    r2 = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid), "Content-Type": "application/gzip"},
    )
    assert r2.status_code == 200
    assert r2.json()["created"] is False


async def test_inventory_results_requires_auth(client):
    c, maker, _, _ = client
    agent_id, item_id, _ = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={"X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 401


async def test_inventory_results_wrong_agent_404(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    # A second agent cannot upload for the first agent's command.
    other_id, _, other_fp = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{other_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={**_auth(other_fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 404


async def test_inventory_results_non_inventory_command_409(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    async with maker() as s:
        cmd = AgentCommand(
            agent_id=agent_id,
            kind="stat_check",
            item_id=item_id,
            status="picked_up",
            picked_up_at=datetime.now(UTC),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        )
        s.add(cmd)
        await s.commit()
        stat_cid = cmd.id
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers={**_auth(fp), "X-Filearr-Command-Id": str(stat_cid)},
    )
    assert r.status_code == 409


async def test_inventory_results_rejects_non_gzip_415(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=b'{"rel":"plain"}\n',  # not gzip
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 415


async def test_inventory_results_size_cap_413(client):
    c, maker, settings, _ = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    settings.agent_inventory_result_max_bytes = 16
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"x" * 1024),
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 413


async def test_inventory_results_missing_command_header_422(client):
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=_gz(b"{}\n"),
        headers=_auth(fp),
    )
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Inline inventory command completion                                          #
# --------------------------------------------------------------------------- #
async def test_inventory_command_enqueue_poll_complete_inline(client):
    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    # Enqueue an inventory command via the EXISTING command-creation endpoint.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={
            "kind": "inventory",
            "item_id": str(item_id),
            "payload": {"preset": "downloads", "collectors": ["stat"], "max_entries": 100},
        },
    )
    assert r.status_code == 201, r.text
    cid = r.json()["id"]
    # Payload passes through untouched.
    assert r.json()["payload"]["preset"] == "downloads"

    # Poll picks it up.
    poll = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert poll.status_code == 200
    assert any(cc["id"] == cid and cc["kind"] == "inventory" for cc in poll.json())

    # Complete with an inline summary + entries.
    result = {
        "summary": {"roots_expanded": 1, "entries": 2, "denied": 0},
        "entries": [{"rel": "a.txt", "size": 1}, {"rel": "b.txt", "size": 2}],
    }
    done = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": result},
        headers=_auth(fp),
    )
    assert done.status_code == 200, done.text
    assert done.json()["status"] == "done"
    assert done.json()["result"]["summary"]["entries"] == 2


async def test_poll_persists_health_and_stamps_auth_mode(client):
    """2026-08-08 fleet health: a poll's ``health`` snapshot is stored VERBATIM
    with an arrival stamp (same additive contract as capabilities — absent
    leaves the stored value untouched), and authenticating the poll records
    the observed transport ('bearer' here — the honest source for the console's
    mTLS badge)."""
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    health = {
        "uptime_s": 4242,
        "outbox_pending": 17,
        "index_items": 120_345,
        "scan": {"status": "finished", "seen": 120_345},
    }
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll",
        json={"max": 5, "health": health, "version": "1.5.0-6f9b984"},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.health == health
        assert agent.health_at is not None
        assert agent.last_auth_mode == "bearer"
        # Version confirmation rides the poll too (container images disable
        # the updater — the historical version channel — so central showed
        # their enrollment-era version forever; live 2026-08-08).
        assert agent.agent_version == "1.5.0-6f9b984"
        first_at = agent.health_at

    # Absent health leaves the stored snapshot (and its stamp) untouched.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/poll", json={"max": 5}, headers=_auth(fp)
    )
    assert r.status_code == 200
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        assert agent.health == health
        assert agent.health_at == first_at


# --------------------------------------------------------------------------- #
# W7-T6/T7 (2026-08-19): permission snapshots + reports                        #
# --------------------------------------------------------------------------- #
def _perm_record(*, owner_id="1000", extra_aces=None, fidelity="full_native"):
    aces = [
        {"principal": {"kind": "user", "id": owner_id, "name": "eric"}, "type": "allow",
         "verbs": ["read", "write"], "raw_mask": "mode:user_obj=06", "inherited": False,
         "scope": "this", "source": "local", "order_index": 0},
        {"principal": {"kind": "group", "id": "100", "name": "users"}, "type": "allow",
         "verbs": ["read"], "raw_mask": "mode:group_obj=04", "inherited": False,
         "scope": "this", "source": "local", "order_index": 1},
        {"principal": {"kind": "well_known", "id": "other", "name": "other", "well_known": "EVERYONE"},
         "type": "allow", "verbs": ["read"], "raw_mask": "mode:other=04", "inherited": False,
         "scope": "this", "source": "local", "order_index": 2},
    ] + (extra_aces or [])
    return {
        "collected_at": "2026-08-19T10:00:00Z",
        "owner": {"kind": "user", "id": owner_id, "name": "eric"},
        "group": {"kind": "group", "id": "100", "name": "users"},
        "posture": {"dacl_present": False, "dacl_canonical": False, "generic_mapping_applied": False},
        "fidelity": fidelity,
        "entries": aces,
    }


async def test_permission_snapshots_ingest_and_reports(client):
    from filearr.models import PermissionSnapshot

    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)

    world_write = {"principal": {"kind": "well_known", "id": "other", "name": "other", "well_known": "EVERYONE"},
                   "type": "allow", "verbs": ["read", "write"], "raw_mask": "mode:other=06",
                   "inherited": False, "scope": "this", "source": "local", "order_index": 3}
    entries = [
        {"path": "/data/x.mkv", "rel": "x.mkv", "permissions": _perm_record()},
        {"path": "/data/pub", "rel": "pub", "is_dir": True,
         "permissions": _perm_record(extra_aces=[world_write], fidelity="posix_mode_only")},
        {"path": "/data/nothing", "rel": "nothing"},  # no permissions record -> ignored
    ]
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"summary": {"entries": 3}, "entries": entries}},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        rows = (await s.execute(select(PermissionSnapshot).order_by(PermissionSnapshot.path))).scalars().all()
    assert [x.path for x in rows] == ["/data/pub", "/data/x.mkv"]
    assert rows[0].is_dir is True and rows[0].fidelity == "posix_mode_only"
    assert set(rows[1].principals) == {"1000", "100", "other"}
    assert rows[1].command_id == cid

    # unchanged re-collection (same digest) writes nothing; a changed one adds a row
    from filearr import permission_ingest
    async with maker() as s:
        out = await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None, entries=entries[:1]
        )
        assert out == {"seen": 1, "written": 0, "unchanged": 1, "skipped": 0}
        changed = [{"path": "/data/x.mkv", "permissions": _perm_record(owner_id="1001")}]
        out = await permission_ingest.ingest_entries(s, agent_id=agent_id, command_id=None, entries=changed, retain=2)
        assert out["written"] == 1
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot).where(PermissionSnapshot.path == "/data/x.mkv"))).scalar_one()
        assert n == 2
        # retention: a third change with retain=2 keeps 2
        out = await permission_ingest.ingest_entries(s, agent_id=agent_id, command_id=None, entries=[{"path": "/data/x.mkv", "permissions": _perm_record(owner_id="1002")}], retain=2)
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot).where(PermissionSnapshot.path == "/data/x.mkv"))).scalar_one()
        assert n == 2

    # reports: by-principal hides the well-known 'other'; broad-access finds /data/pub
    r = await c.get("/api/v1/reports")
    ids = {x["id"] for x in r.json()["reports"]}
    assert {"permissions_by_principal", "permissions_broad_access"} <= ids
    r = await c.get("/api/v1/reports/permissions_by_principal")
    assert r.status_code == 200, r.text
    rows_ = r.json()["rows"] if isinstance(r.json(), dict) else r.json()
    principals = {(x["path"], x["principal_id"]) for x in rows_}
    assert ("/data/x.mkv", "1002") in principals and ("/data/x.mkv", "100") in principals
    assert not any(x["principal_id"] == "other" for x in rows_)
    r = await c.get("/api/v1/reports/permissions_broad_access")
    assert r.status_code == 200, r.text
    rows_ = r.json()["rows"] if isinstance(r.json(), dict) else r.json()
    assert [x["path"] for x in rows_] == ["/data/pub"] and "write" in rows_[0]["verbs"]


async def test_permission_snapshots_from_ndjson_upload(client):
    from filearr.models import PermissionSnapshot

    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    line = json.dumps({"path": "/data/x.mkv", "rel": "x.mkv", "permissions": _perm_record()})
    blob = _gz((line + "\n" + '{"rel":"noperm"}\n' + "garbage\n").encode())
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "X-Filearr-Command-Id": str(cid), "Content-Type": "application/gzip"},
    )
    assert r.status_code == 201, r.text
    async with maker() as s:
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot))).scalar_one()
    assert n == 1
