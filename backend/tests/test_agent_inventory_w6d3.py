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
    # 2026-08-23: agent-scoped — a host walk has no item; an item_id is a 422.
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={"kind": "inventory", "item_id": str(item_id), "payload": {"collectors": ["stat"]}},
    )
    assert r.status_code == 422, r.text
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands",
        json={
            "kind": "inventory",
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
def _perm_record(*, owner_id="1000", extra_aces=None, fidelity="full_native", collected_at=None):
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
        # Relative to now (digest excludes it) so the change-report's
        # threshold_days window is not brittle to the wall-clock date.
        "collected_at": collected_at or datetime.now(UTC).isoformat(),
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


async def test_permissions_explicit_outliers_report(client):
    """§4 outliers (2026-08-20): an explicit child ACE that RESTATES the parent
    directory's ACE for the same (principal, type, verbs) is hidden; a deviating
    explicit ACE shows with baseline='deviates'; paths whose parent has no
    snapshot are kept with baseline='unknown'."""
    from filearr import permission_ingest

    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    dev = {"principal": {"kind": "user", "id": "555", "name": "svc"}, "type": "allow",
           "verbs": ["full"], "raw_mask": "0x1f01ff", "inherited": False,
           "scope": "this", "source": "local", "order_index": 3}
    async with maker() as s:
        await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None, entries=[
                {"path": "/data", "is_dir": True, "permissions": _perm_record()},
                {"path": "/data/x.mkv", "permissions": _perm_record(extra_aces=[dev])},
                {"path": "/lone/file", "permissions": _perm_record()},
            ],
        )

    r = await c.get("/api/v1/reports/permissions_explicit_outliers")
    assert r.status_code == 200, r.text
    body = r.json()
    rows = body["rows"] if isinstance(body, dict) else body
    got = {(x["path"], x["principal_id"], x["baseline"]) for x in rows}
    # The deviating grant survives; the restating 1000/100 ACEs on the child do not.
    assert ("/data/x.mkv", "555", "deviates") in got
    assert not any(p == "/data/x.mkv" and pid in ("1000", "100") for (p, pid, _b) in got)
    # No parent snapshot -> kept, honestly marked unknown.
    assert ("/lone/file", "1000", "unknown") in got
    assert ("/data", "1000", "unknown") in got
    # Well-known principals stay hidden (same exclusion as by-principal).
    assert not any(pid == "other" for (_p, pid, _b) in got)


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
    # 2026-08-25: the upload only STORES; the worker task ingests. A malformed
    # line and an entry without a record are skipped, never fatal.
    from filearr.tasks.permissions import ingest_inventory_result

    async with maker() as s:
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot))).scalar_one()
    assert n == 0
    assert (await ingest_inventory_result(str(cid)))["written"] == 1
    async with maker() as s:
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot))).scalar_one()
    assert n == 1


# --------------------------------------------------------------------------- #
# W7-T9 (2026-08-19): permission drift report + change alert + item link       #
# --------------------------------------------------------------------------- #
async def test_permission_changes_report_and_alert(client):
    from filearr import permission_ingest
    from filearr.alerts import ops
    from filearr.models import AlertEvent, AlertRule, PermissionSnapshot

    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    # make the seeded library AGENT-owned so the path → item link resolves
    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().first()
        lib.source_agent_id = agent_id
        lib.agent_library_ref = "/data"
        await s.commit()
        lib_id = lib.id
    # enable the system rule
    await ops.seed_system_alert_rules(maker)
    async with maker() as s:
        rule = (
            await s.execute(select(AlertRule).where(AlertRule.name == ops.PERMISSION_CHANGE_RULE_NAME))
        ).scalar_one()
        rule.enabled = True
        await s.commit()

    async with maker() as s:
        # first snapshot: no previous → no alert, but the item link is stamped
        out = await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None,
            entries=[{"path": "/data/x.mkv", "permissions": _perm_record()}],
        )
        assert out["written"] == 1
        snap = (await s.execute(select(PermissionSnapshot))).scalars().one()
        assert snap.item_id == item_id
        evs = (await s.execute(select(AlertEvent))).scalars().all()
        assert evs == []

        # second snapshot: owner changes + Everyone gains write → alert
        world_write = {
            "principal": {"kind": "well_known", "id": "other", "name": "other", "well_known": "EVERYONE"},
            "type": "allow", "verbs": ["read", "write"], "raw_mask": "mode:other=06",
            "inherited": False, "scope": "this", "source": "local", "order_index": 3,
        }
        out = await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None,
            entries=[{"path": "/data/x.mkv",
                      "permissions": _perm_record(owner_id="1001", extra_aces=[world_write])}],
        )
        assert out["written"] == 1
        evs = (await s.execute(select(AlertEvent))).scalars().all()
        assert len(evs) == 1
        ev = evs[0]
        assert ev.event_type == ops.PERMISSION_CHANGE_EVENT
        assert ev.item_id == item_id and ev.library_id == lib_id
        assert ev.payload["owner_changed"] is True
        assert ev.payload["added"] >= 1
        assert "other" in ev.payload["summary"] and "write" in ev.payload["summary"]

        # re-ingesting the same record is digest-gated → nothing new
        out = await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None,
            entries=[{"path": "/data/x.mkv",
                      "permissions": _perm_record(owner_id="1001", extra_aces=[world_write])}],
        )
        assert out == {"seen": 1, "written": 0, "unchanged": 1, "skipped": 0}
        n = (await s.execute(select(func.count()).select_from(AlertEvent))).scalar_one()
        assert n == 1

        # fidelity-only change: snapshot written, NO alert (diff is empty)
        out = await permission_ingest.ingest_entries(
            s, agent_id=agent_id, command_id=None,
            entries=[{"path": "/data/x.mkv",
                      "permissions": _perm_record(owner_id="1001", extra_aces=[world_write],
                                                  fidelity="posix_mode_only")}],
        )
        assert out["written"] == 1
        n = (await s.execute(select(func.count()).select_from(AlertEvent))).scalar_one()
        assert n == 1

    # the drift report: two change rows (newest first), the fidelity-only one labelled
    r = await c.get("/api/v1/reports")
    assert "permission_changes" in {x["id"] for x in r.json()["reports"]}
    r = await c.get("/api/v1/reports/permission_changes")
    assert r.status_code == 200, r.text
    rows = r.json()["rows"] if isinstance(r.json(), dict) else r.json()
    assert len(rows) == 2
    assert rows[0]["details"].startswith("fidelity full_native → posix_mode_only")
    assert rows[0]["added"] == 0 and rows[0]["removed"] == 0
    real = rows[1]
    assert real["owner_before"] == "eric" and real["owner_after"] == "eric"  # same display name
    assert real["added"] == 1 and real["modified"] >= 1  # Everyone +write, owner ACE moved
    assert "owner" in real["details"] and "+allow" in real["details"]
    # library filter rides the item link
    r = await c.get(f"/api/v1/reports/permission_changes?library_id={lib_id}")
    assert r.status_code == 200 and len(r.json()["rows"]) == 2
    # threshold: nothing older than a day ago is excluded; 1 day still includes now
    r = await c.get("/api/v1/reports/permission_changes?threshold_days=1")
    assert r.status_code == 200 and len(r.json()["rows"]) == 2
    # csv export works
    r = await c.get("/api/v1/reports/permission_changes?format=csv")
    assert r.status_code == 200 and "details" in r.text.splitlines()[0]


async def test_principal_alias_canonicalises_reports(client):
    """W7-T8 (2026-08-20): an alias folds host-local ids into one canonical
    identity in the by-principal report; raw ids stay for forensics."""
    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    entries = [{"path": "/data/x.mkv", "rel": "x.mkv", "permissions": _perm_record()}]
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"summary": {"entries": 1}, "entries": entries}},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text

    # map uid 1000 -> the org identity
    r = await c.put("/api/v1/principal-aliases", json=[
        {"alias": "1000", "canonical": "org:eric", "display": "Eric H"},
    ])
    assert r.status_code == 200 and r.json() == {"upserted": 1, "skipped": 0}
    r = await c.get("/api/v1/principal-aliases")
    assert r.json()["aliases"][0]["canonical"] == "org:eric"
    assert r.json()["aliases"][0]["source"] == "manual"

    r = await c.get("/api/v1/reports/permissions_by_principal")
    rows = r.json()["rows"]
    eric = [x for x in rows if x["principal_id"] == "1000"]
    assert eric and all(x["principal"] == "Eric H" and x["canonical_id"] == "org:eric" for x in eric)
    other = [x for x in rows if x["principal_id"] == "100"]
    assert other and all(x["canonical_id"] is None and x["principal"] == "users" for x in other)

    # delete restores the raw resolution
    assert (await c.delete("/api/v1/principal-aliases/1000")).status_code == 204
    r = await c.get("/api/v1/reports/permissions_by_principal")
    eric = [x for x in r.json()["rows"] if x["principal_id"] == "1000"]
    assert all(x["principal"] == "eric" for x in eric)
    assert (await c.delete("/api/v1/principal-aliases/1000")).status_code == 404


async def test_effective_access_endpoint(client):
    """W7-T10 (2026-08-20): the inspection endpoint over the newest snapshot."""
    c, maker, _, _tmp = client
    agent_id, item_id, fp = await _seed(maker)
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    entries = [{"path": "/data/x.mkv", "rel": "x.mkv", "permissions": _perm_record()}]
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"summary": {"entries": 1}, "entries": entries}},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    r = await c.get(
        "/api/v1/permissions/effective-access",
        params={"agent_id": str(agent_id), "path": "/data/x.mkv", "principal": ["1000"]},
    )
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["verbs"] == ["read", "write"] and out["matched_aces"] >= 1
    # unknown path -> 404; no principal -> 422
    assert (
        await c.get(
            "/api/v1/permissions/effective-access",
            params={"agent_id": str(agent_id), "path": "/nope", "principal": ["1000"]},
        )
    ).status_code == 404
    assert (
        await c.get(
            "/api/v1/permissions/effective-access",
            params={"agent_id": str(agent_id), "path": "/data/x.mkv"},
        )
    ).status_code == 422


# --------------------------------------------------------------------------- #
# 2026-08-23: run-it-now endpoint + the wire-shape regression                  #
# --------------------------------------------------------------------------- #
async def _seed_global_inventory(maker, inventory: dict) -> None:
    """Publish ``inventory`` on the permanent Global group (created here when
    the migrated schema has none) so the agent's effective config carries it."""
    from filearr.models import AgentConfigGroup, AgentConfigGroupVersion

    async with maker() as s:
        g = (
            await s.execute(select(AgentConfigGroup).where(AgentConfigGroup.is_system.is_(True)))
        ).scalars().first()
        if g is None:
            g = AgentConfigGroup(name="Global", is_system=True, priority=0, current_version=0)
            s.add(g)
            await s.flush()
        nxt = (g.current_version or 0) + 1
        s.add(
            AgentConfigGroupVersion(
                group_id=g.id, version=nxt, settings={"inventory": inventory}, policy={}
            )
        )
        g.current_version = nxt
        await s.commit()


async def test_inventory_now_uses_effective_group_settings(client):
    c, maker, _, _ = client
    agent_id, _, _fp = await _seed(maker)
    # Pin the starting state: the db fixture does not clear config groups, so a
    # Global group published by another test file could otherwise leak in.
    await _seed_global_inventory(maker, {"enabled": False, "collectors": []})
    # Nothing authored anywhere -> 422 that says what is missing, not a 500.
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 422, r.text
    assert "collectors" in r.text
    # Collectors but no paths/preset and no libraries -> 422 naming that.
    await _seed_global_inventory(maker, {"enabled": True, "collectors": ["permissions"]})
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 422, r.text
    assert "nothing to walk" in r.text
    # ...and once the agent has a library, its root is the default walk.
    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        lib.source_agent_id = agent_id
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 201, r.text
    assert r.json()["payload"]["paths"] == ["/data"]
    async with maker() as s:
        cmd = (await s.execute(select(AgentCommand))).scalars().one()
        await s.delete(cmd)
        await s.commit()
    await _seed_global_inventory(
        maker,
        {
            "enabled": False,  # the master switch gates the SCHEDULE, not a manual run
            "collectors": ["stat", "permissions"],
            "paths": ["D:\\"],
            "preset": "user-documents",
        },
    )
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "inventory" and body["item_id"] is None
    # Byte-for-byte the schedule's payload, except the cursor marker.
    assert body["payload"] == {
        "scheduled": False,
        "collectors": ["stat", "permissions"],
        "paths": ["D:\\"],
        "preset": "user-documents",
        "origin": "console",
    }
    # A second request while the first is queued is a 409, not a pile-up.
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 409, r.text
    # Overrides replace the group's fields for one run.
    async with maker() as s:
        cmd = (await s.execute(select(AgentCommand))).scalars().one()
        cmd.status = "done"
        await s.commit()
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory",
        json={"collectors": ["permissions"], "paths": ["/srv/share"], "preset": ""},
    )
    assert r.status_code == 201, r.text
    assert r.json()["payload"] == {
        "scheduled": False,
        "collectors": ["permissions"],
        "paths": ["/srv/share"],
        "origin": "console",
    }


async def test_permission_ingest_accepts_legacy_record_key(client):
    """Agent builds <= 1.5.3 emitted the permissions record under ``record``
    (the walker merges collector maps flat into the entry) while central read
    ``permissions`` -- so nothing was ever ingested. Both keys ingest now, and
    Go's nil-slice ``"entries": null`` counts as an empty ACE list."""
    from filearr.models import PermissionSnapshot

    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    async with maker() as s:
        # Linking only considers the agent's OWN libraries (source_agent_id).
        lib = (await s.execute(select(Library))).scalars().one()
        lib.source_agent_id = agent_id
        await s.commit()
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    legacy = _perm_record()
    empty = {**_perm_record(owner_id="7"), "entries": None}
    entries = [
        {"path": "/data/x.mkv", "rel": "x.mkv", "size": 1, "is_dir": False, "record": legacy},
        {"path": "/data/pub", "rel": "pub", "is_dir": True, "permissions": empty},
        {"path": "/data/none", "rel": "none", "record": {"not": "a record"}},
    ]
    r = await c.post(
        f"/api/v1/agents/{agent_id}/commands/{cid}/complete",
        json={"ok": True, "result": {"summary": {"entries": 3}, "entries": entries}},
        headers=_auth(fp),
    )
    assert r.status_code == 200, r.text
    async with maker() as s:
        rows = (
            await s.execute(select(PermissionSnapshot).order_by(PermissionSnapshot.path))
        ).scalars().all()
    assert [x.path for x in rows] == ["/data/pub", "/data/x.mkv"]
    assert rows[0].aces == [] and rows[0].owner["id"] == "7"
    assert rows[1].item_id == item_id  # linked through the library root


async def test_upload_defers_ingest_to_worker_task(client):
    """2026-08-25: the upload stores the blob and acks; permission_snapshots
    are written by the worker task (inline ingest of a 100k-entry blob took
    minutes and the agent's HTTP client timed out waiting for this ack)."""
    from filearr.models import PermissionSnapshot
    from filearr.tasks.permissions import ingest_inventory_result

    c, maker, _, _ = client
    agent_id, item_id, fp = await _seed(maker)
    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        lib.source_agent_id = agent_id
        await s.commit()
    cid = await _mk_inventory_command(maker, agent_id, item_id)
    lines = [
        json.dumps({"path": "/data/x.mkv", "rel": "x.mkv", "permissions": _perm_record()}),
        json.dumps({"path": "/data/pub", "rel": "pub", "is_dir": True, "record": _perm_record()}),
    ]
    blob = gzip.compress(("\n".join(lines) + "\n").encode())
    r = await c.post(
        f"/api/v1/agents/{agent_id}/inventory-results",
        content=blob,
        headers={**_auth(fp), "Content-Type": "application/gzip", "X-Filearr-Command-Id": str(cid)},
    )
    assert r.status_code == 201, r.text
    async with maker() as s:
        n = (await s.execute(select(func.count()).select_from(PermissionSnapshot))).scalar_one()
    assert n == 0  # nothing inline any more
    out = await ingest_inventory_result(str(cid))
    assert out["written"] == 2
    async with maker() as s:
        rows = (
            await s.execute(select(PermissionSnapshot).order_by(PermissionSnapshot.path))
        ).scalars().all()
    assert [x.path for x in rows] == ["/data/pub", "/data/x.mkv"]
    assert rows[1].item_id == item_id
    # Re-running the task over the same blob is a no-op (digest gate).
    assert (await ingest_inventory_result(str(cid)))["written"] == 0


async def test_inventory_now_inherits_scan_paths(client):
    c, maker, _, _ = client
    agent_id, _, _fp = await _seed(maker)
    async with maker() as s:
        lib = (await s.execute(select(Library))).scalars().one()
        lib.source_agent_id = agent_id
        await s.commit()
    await _seed_global_inventory(
        maker,
        {
            "enabled": True,
            "collectors": ["permissions"],
            "paths": ["/extra"],
            "inherit_scan_paths": True,
            "max_entries": 250000,
        },
    )
    async with maker() as s:
        from filearr.models import AgentConfigGroup, AgentConfigGroupVersion

        g = (
            await s.execute(select(AgentConfigGroup).where(AgentConfigGroup.is_system.is_(True)))
        ).scalars().one()
        v = (
            await s.execute(
                select(AgentConfigGroupVersion).where(
                    AgentConfigGroupVersion.group_id == g.id,
                    AgentConfigGroupVersion.version == g.current_version,
                )
            )
        ).scalars().one()
        v.settings = {
            **v.settings,
            "scan_selections": [
                {"paths": ["%USERPROFILE%/Documents"], "enabled": True},
                {"paths": ["/disabled"], "enabled": False},
            ],
        }
        await s.commit()
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory")
    assert r.status_code == 201, r.text
    payload = r.json()["payload"]
    assert payload["paths"] == ["/extra", "/data", "%USERPROFILE%/Documents"]
    assert payload["max_entries"] == 250000


async def test_agent_can_request_its_own_inventory_run(client):
    """2026-08-25: the agent-plane request endpoint (local web UI button)."""
    c, maker, _, _ = client
    agent_id, _, fp = await _seed(maker)
    other_id, _, _other_fp = await _seed(maker)
    async with maker() as s:
        for lib in (await s.execute(select(Library))).scalars().all():
            lib.source_agent_id = agent_id
        await s.commit()
    await _seed_global_inventory(maker, {"enabled": True, "collectors": ["permissions"]})
    # No credential -> 401/403, never a queued command.
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory/request")
    assert r.status_code in (401, 403), r.text
    # Another agent's credential cannot queue a run for this one.
    r = await c.post(f"/api/v1/agents/{other_id}/inventory/request", headers=_auth(fp))
    assert r.status_code in (401, 403, 404), r.text
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory/request", headers=_auth(fp))
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["kind"] == "inventory" and body["requested_by"] is None
    assert body["payload"]["origin"] == "agent-local"
    assert body["payload"]["collectors"] == ["permissions"]
    r = await c.post(f"/api/v1/agents/{agent_id}/inventory/request", headers=_auth(fp))
    assert r.status_code == 409, r.text
