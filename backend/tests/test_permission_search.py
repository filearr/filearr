"""2026-08-23 — permission projection onto the search index + per-item ACL view.

* ``summary_for_snapshot``: allow principals + owner, deny excluded, POSIX
  dir_default skipped, world detection (Everyone / S-1-1-0);
* ``build_doc`` carries ``perm_principals`` / ``perm_world`` / ``perm_owner``
  (empty shape without a snapshot);
* ``build_filters``: ``principal`` (repeatable = OR, quoted) + ``world_readable``;
* Meili settings: the three attributes are filterable, ``perm_principals`` is a
  facet-search candidate, ``perm_world`` is a requested facet, and the saved-
  search vocabulary picked up both new params;
* ``GET /items/{id}/permissions``: newest snapshot for an agent-library item,
  and the "not available" explanation for a central-scanned one.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from filearr import search as search_mod
from filearr.api.search import FACETS, SEARCH_PARAM_NAMES, build_filters
from filearr.meili_ops import FACET_SEARCH_CANDIDATES, FILTERABLE_ATTRIBUTES
from filearr.models import Item, ItemStatus, PermissionSnapshot
from filearr.permission_projection import EMPTY_SUMMARY, summary_for_snapshot

from .conftest import psycopg3_uri

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _p(pid: str, name: str, kind: str = "user") -> dict:
    return {"kind": kind, "id": pid, "name": name}


def _ace(pid, name, typ="allow", verbs=("read",), scope="this", inherited=False, kind="user"):
    return {
        "principal": _p(pid, name, kind), "type": typ, "verbs": list(verbs),
        "scope": scope, "inherited": inherited, "source": "local",
    }


def _snap(**over) -> PermissionSnapshot:
    base = dict(
        agent_id=uuid.uuid4(), path="/x", is_dir=False, collected_at=datetime.now(UTC),
        owner=_p("S-1-5-21-1-2-3-1001", "EX\\eric"), group_=None,
        aces=[
            _ace("S-1-5-21-1-2-3-1002", "EX\\bob"),
            _ace("S-1-5-21-1-2-3-1003", "EX\\mallory", typ="deny"),
            _ace("S-1-5-21-1-2-3-1003", "EX\\mallory"),  # allow+deny -> excluded
            _ace("uid:1000", "alice", scope="dir_default"),  # inherit-only, not access
        ],
        posture=None, fidelity="full_native", principals=[], digest="d",
    )
    base.update(over)
    return PermissionSnapshot(**base)


def test_summary_allow_minus_deny_plus_owner():
    s = summary_for_snapshot(_snap())
    assert "EX\\bob" in s["perm_principals"] and "S-1-5-21-1-2-3-1002" in s["perm_principals"]
    assert "EX\\eric" in s["perm_principals"]  # owner always reads
    assert "EX\\mallory" not in s["perm_principals"]  # carries a deny
    assert "alice" not in s["perm_principals"]  # dir_default is not access now
    assert s["perm_world"] is False
    assert s["perm_owner"] == "EX\\eric"


def test_summary_world_readable_via_everyone():
    s = summary_for_snapshot(_snap(aces=[_ace("S-1-1-0", "Everyone", kind="well_known")]))
    assert s["perm_world"] is True
    assert summary_for_snapshot(None) == EMPTY_SUMMARY


def _item() -> Item:
    return Item(
        id=uuid.uuid4(), library_id=uuid.uuid4(), file_category="video", file_group="video",
        path="/data/a.mkv", rel_path="a.mkv", filename="a.mkv", extension="mkv", size=1,
        mtime=datetime.now(UTC), metadata_={}, user_metadata={}, external_ids={}, tags=[],
        status=ItemStatus.active,
    )


def test_build_doc_projects_permission_fields():
    empty = search_mod.build_doc(_item())
    assert empty["perm_principals"] == [] and empty["perm_world"] is False
    assert empty["perm_owner"] is None
    doc = search_mod.build_doc(_item(), perm=summary_for_snapshot(_snap()))
    assert "EX\\bob" in doc["perm_principals"] and doc["perm_owner"] == "EX\\eric"


def test_principal_and_world_filters():
    f = build_filters(status=None, principal=["EX\\bob"])
    assert "perm_principals = 'EX\\\\bob'" in f
    f2 = build_filters(status=None, principal=["a", "b'c"])
    assert "(perm_principals = 'a' OR perm_principals = 'b\\'c')" in f2
    assert "perm_world = true" in build_filters(status=None, world_readable=True)
    assert "perm_world = false" in build_filters(status=None, world_readable=False)
    assert not any("perm_" in c for c in build_filters(status=None))


def test_settings_and_vocabulary_carry_permission_fields():
    for a in ("perm_principals", "perm_world", "perm_owner"):
        assert a in FILTERABLE_ATTRIBUTES
    assert "perm_principals" in FACET_SEARCH_CANDIDATES
    assert "perm_world" in FACETS
    assert {"principal", "world_readable"} <= SEARCH_PARAM_NAMES


# --------------------------------------------------------------------------- #
# GET /items/{id}/permissions                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def wired(pg_uri, monkeypatch):

    from alembic.config import Config

    from alembic import command
    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(psycopg3_uri(pg_uri))
    async with engine.begin() as conn:
        for t in ("permission_snapshots", "items", "libraries", "agents"):
            await conn.execute(text(f"DELETE FROM {t}"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    yield app, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_item_permissions_endpoint(wired):
    from filearr.models import Agent, Library

    app, maker = wired
    async with maker() as s:
        agent = Agent(name="xenon", hostname="xenon", platform="windows",
                      cert_fingerprint="FP:" + uuid.uuid4().hex)
        s.add(agent)
        await s.flush()
        alib = Library(name="xenon:D", root_path="D:\\media", source_agent_id=agent.id,
                       agent_library_ref="D:\\media")
        clib = Library(name="central", root_path="/data")
        s.add_all([alib, clib])
        await s.flush()
        a_item = _item()
        a_item.library_id = alib.id
        c_item = _item()
        c_item.library_id = clib.id
        s.add_all([a_item, c_item])
        await s.flush()
        older = _snap(agent_id=agent.id, item_id=a_item.id, path="D:/media/a.mkv",
                      collected_at=datetime(2026, 8, 1, tzinfo=UTC), aces=[_ace("uid:1", "old")])
        newer = _snap(agent_id=agent.id, item_id=a_item.id, path="D:/media/a.mkv",
                      collected_at=datetime(2026, 8, 20, tzinfo=UTC))
        s.add_all([older, newer])
        await s.commit()
        a_id, c_id = str(a_item.id), str(c_item.id)

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://t") as c:
        r = await c.get(f"/api/v1/items/{a_id}/permissions")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["available"] is True and body["agent_name"] == "xenon"
        assert body["owner"]["name"] == "EX\\eric"
        assert any(a["principal"]["name"] == "EX\\bob" for a in body["aces"])  # the NEWEST
        assert not any(a["principal"]["name"] == "old" for a in body["aces"])
        assert body["summary"]["perm_owner"] == "EX\\eric"

        r = await c.get(f"/api/v1/items/{c_id}/permissions")
        assert r.status_code == 200
        assert r.json()["available"] is False
        assert "agents" in r.json()["reason"]
