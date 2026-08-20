"""LLM tool facade (M1): key auth, role gating, scope, tools, prompting.

Real-Postgres integration through the mounted sub-app at /api/llm/v1 plus the
admin mint endpoint at /api/v1/llm-keys. Meilisearch-backed search_files is
not exercised here (no Meili in the suite) — the SQL-backed tools and the
whole authz/handshake surface are.
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
from filearr.api.llm import llm_app
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import ApiKey, Item, Library
from filearr.security import generate_key

pytestmark = pytest.mark.anyio

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)



async def _llm_acct(client) -> str:
    """2026-08-20: LLM keys require a service-account owner (like plain keys)."""
    import uuid as _uuidmod

    r = await client.post(
        "/api/v1/service-accounts", json={"name": f"llm-{_uuidmod.uuid4().hex[:8]}"}
    )
    assert r.status_code == 201, r.text
    return r.json()["id"]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_maker(module_db, monkeypatch):
    # A per-MODULE isolated database (never the shared session DB: this module
    # deletes items/libraries wholesale, which would clobber other modules'
    # seeds). Alembic-upgraded to head — exercising the f2c8b5a9d417 migration.
    uri = module_db.get_uri()
    # alembic/env.py takes its DSN from get_settings().database_url — point it
    # (and the app under test) at the module database.
    monkeypatch.setenv("FILEARR_DATABASE_URL", _psycopg3(uri))
    get_settings.cache_clear()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM api_keys"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    # admin surface open (facade auth is its own, independent of this switch)
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    # The facade is a MOUNTED sub-application: parent overrides do not cascade.
    llm_app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    llm_app.dependency_overrides.clear()


async def _seed(db_maker) -> dict:
    """One library, two items (one text-bearing), three LLM keys + a plain key."""
    async with db_maker() as s:
        lib = Library(name=f"docs-{uuid.uuid4().hex[:6]}", root_path="/data/docs")
        s.add(lib)
        await s.flush()
        video = Item(
            library_id=lib.id,
            path="/data/docs/clips/big.mkv",
            rel_path="clips/big.mkv",
            filename="big.mkv",
            extension="mkv",
            size=5_000_000,
            mtime=datetime(2026, 5, 1, tzinfo=UTC),
            status="active",
            file_category="video",
            file_group="video",
        )
        doc = Item(
            library_id=lib.id,
            path="/data/docs/tax/return-2023.pdf",
            rel_path="tax/return-2023.pdf",
            filename="return-2023.pdf",
            extension="pdf",
            size=120_000,
            mtime=datetime(2026, 4, 1, tzinfo=UTC),
            status="active",
            file_category="document",
            file_group="document",
            metadata_={"body_text": "TAXABLE INCOME 12345 " * 50, "pages": 3},
        )
        s.add_all([video, doc])

        keys = {}
        for role in ("librarian", "analyst", "guest"):
            full, prefix, key_hash = generate_key()
            s.add(
                ApiKey(
                    name=f"t-{role}", prefix=prefix, key_hash=key_hash,
                    scopes=["read"], llm_role=role,
                )
            )
            keys[role] = full
        full, prefix, key_hash = generate_key()
        s.add(ApiKey(name="plain", prefix=prefix, key_hash=key_hash, scopes=["read"]))
        keys["plain"] = full
        await s.commit()
        return {
            "keys": keys,
            "video_id": str(video.id),
            "doc_id": str(doc.id),
            "library": lib.name,
        }


def _h(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


async def test_facade_auth_and_handshake(client, db_maker):
    seed = await _seed(db_maker)

    r = await client.get("/api/llm/v1/capabilities")
    assert r.status_code == 401

    r = await client.get("/api/llm/v1/capabilities", headers=_h("ck_bogus"))
    assert r.status_code == 401

    # a plain (non-LLM) read key is refused with guidance
    r = await client.get("/api/llm/v1/capabilities", headers=_h(seed["keys"]["plain"]))
    assert r.status_code == 403

    r = await client.get(
        "/api/llm/v1/capabilities", headers=_h(seed["keys"]["librarian"])
    )
    assert r.status_code == 200
    caps = r.json()
    assert caps["role"]["name"] == "librarian"
    assert "read_content" not in caps["role"]["tools"]
    assert any(t["function"]["name"] == "where_is" for t in caps["tools_openai"])
    assert caps["system"]["item_count"] == 2

    # system prompt: role-accurate, text/plain, ETagged
    r = await client.get(
        "/api/llm/v1/system-prompt", headers=_h(seed["keys"]["librarian"])
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("text/plain")
    assert r.headers.get("etag")
    assert "librarian assistant" in r.text
    assert "read_content" not in r.text
    r2 = await client.get(
        "/api/llm/v1/system-prompt", headers=_h(seed["keys"]["analyst"])
    )
    assert "read_content" in r2.text


async def test_tools_role_gating_and_data(client, db_maker):
    seed = await _seed(db_maker)
    lib_key = seed["keys"]["librarian"]

    # filter_files via the shared grammar
    r = await client.post(
        "/api/llm/v1/filter_files", headers=_h(lib_key), json={"dsl": "kind:video"}
    )
    assert r.status_code == 200
    rows = r.json()["rows"]
    assert len(rows) == 1 and rows[0]["filename"] == "big.mkv"
    assert rows[0]["citation"] == seed["video_id"]
    assert rows[0]["rel_path"] == "clips/big.mkv"  # librarian sees paths

    # bad DSL surfaces as a 422 the model can read
    r = await client.post(
        "/api/llm/v1/filter_files", headers=_h(lib_key), json={"dsl": "size:banana"}
    )
    assert r.status_code == 422

    # guest: no filter_files tool at all, and no paths from get_file
    r = await client.post(
        "/api/llm/v1/filter_files",
        headers=_h(seed["keys"]["guest"]),
        json={"dsl": "kind:video"},
    )
    assert r.status_code == 403
    r = await client.post(
        "/api/llm/v1/get_file",
        headers=_h(seed["keys"]["guest"]),
        json={"citation": seed["video_id"]},
    )
    assert r.status_code == 200
    assert "rel_path" not in r.json() and "path" not in r.json()

    # where_is: librarian yes (with path), guest refused (no location reveal)
    r = await client.post(
        "/api/llm/v1/where_is", headers=_h(lib_key), json={"citation": seed["video_id"]}
    )
    assert r.status_code == 200
    assert r.json()["locations"][0]["path"] == "/data/docs/clips/big.mkv"
    r = await client.post(
        "/api/llm/v1/where_is",
        headers=_h(seed["keys"]["guest"]),
        json={"citation": seed["video_id"]},
    )
    assert r.status_code == 403

    # read_content: analyst reads body_text; librarian is refused
    r = await client.post(
        "/api/llm/v1/read_content",
        headers=_h(seed["keys"]["analyst"]),
        json={"citation": seed["doc_id"], "max_chars": 100},
    )
    assert r.status_code == 200
    body = r.json()
    assert "TAXABLE INCOME" in body["text"] and len(body["text"]) <= 100
    assert body["pages_total"] > 1
    r = await client.post(
        "/api/llm/v1/read_content",
        headers=_h(lib_key),
        json={"citation": seed["doc_id"]},
    )
    assert r.status_code == 403

    # get_file redacts bulk text behind read_content
    r = await client.post(
        "/api/llm/v1/get_file", headers=_h(lib_key), json={"citation": seed["doc_id"]}
    )
    assert "use read_content" in r.json()["metadata"]["body_text"]

    # aggregate + overview
    r = await client.post(
        "/api/llm/v1/aggregate",
        headers=_h(lib_key),
        json={"group_by": "kind", "metric": "count"},
    )
    assert r.status_code == 200
    agg = {row["key"]: row["count"] for row in r.json()["rows"]}
    assert agg == {"video": 1, "document": 1}

    r = await client.post("/api/llm/v1/catalog_overview", headers=_h(lib_key))
    assert r.status_code == 200
    assert r.json()["total_items"] == 2

    # run_report: canned largest_files
    r = await client.post(
        "/api/llm/v1/run_report",
        headers=_h(lib_key),
        json={"report": "largest_files", "limit": 5},
    )
    assert r.status_code == 200
    assert r.json()["rows"]
    r = await client.post(
        "/api/llm/v1/run_report", headers=_h(lib_key), json={"report": "nope"}
    )
    assert r.status_code == 404


async def test_mint_scope_and_rate_limit(client, db_maker):
    await _seed(db_maker)

    # mint via the admin surface (auth disabled in this suite -> open)
    r = await client.post(
        "/api/v1/llm-keys",
        json={
            "service_account_id": await _llm_acct(client),
            "name": "bot", "role": "librarian", "rate_limit": 3, "expires_days": 7,
        },
    )
    assert r.status_code == 201
    minted = r.json()
    key = minted["key"]
    assert key and minted["role"] == "librarian"

    r = await client.get("/api/v1/llm-keys")
    names = [k["name"] for k in r.json()["keys"]]
    assert "bot" in names
    assert all("key" not in k for k in r.json()["keys"])  # never re-shown

    r = await client.post(
        "/api/v1/llm-keys",
        json={"service_account_id": await _llm_acct(client), "name": "x", "role": "wizard"},
    )
    assert r.status_code == 422

    # library allow-list scoping: a key locked to a nonexistent library sees nothing
    r = await client.post(
        "/api/v1/llm-keys",
        json={
            "service_account_id": await _llm_acct(client),
            "name": "scoped", "role": "librarian", "libraries": [str(uuid.uuid4())],
        },
    )
    scoped_key = r.json()["key"]
    r = await client.post(
        "/api/llm/v1/filter_files", headers=_h(scoped_key), json={"dsl": "kind:video"}
    )
    assert r.status_code == 200 and r.json()["rows"] == []
    r = await client.post("/api/llm/v1/catalog_overview", headers=_h(scoped_key))
    assert r.json()["total_items"] == 0

    # rate limit: burst of 3 allowed, 4th call 429 with Retry-After
    statuses = []
    for _ in range(4):
        resp = await client.post("/api/llm/v1/catalog_overview", headers=_h(key))
        statuses.append(resp.status_code)
    assert statuses[:3] == [200, 200, 200] and statuses[3] == 429

    # revoke -> facade refuses immediately
    kid = minted["id"]
    r = await client.delete(f"/api/v1/llm-keys/{kid}")
    assert r.status_code == 204
    r = await client.post("/api/llm/v1/catalog_overview", headers=_h(key))
    assert r.status_code == 401


async def test_find_similar_tool_gating_and_409(client, db_maker):
    """2026-08-20: find_similar is a read tool; 409s explain semantic-off."""
    seeded = await _seed(db_maker)
    key = seeded["keys"]["librarian"]
    r = await client.post(
        "/api/llm/v1/find_similar", headers=_h(key), json={"citation": "not-a-uuid"}
    )
    # semantic disabled in the unit env -> the 409 fires before citation parsing
    assert r.status_code == 409 and "semantic" in r.json()["detail"]
    # tool exposure: the librarian (read tier) carries it
    r = await client.get("/api/llm/v1/capabilities", headers=_h(key))
    assert r.status_code == 200, r.text
    body = r.json()
    assert "find_similar" in body["role"]["tools"]
    # the new hygiene/health reports ride run_report automatically
    assert {"library_health", "sidecar_hygiene", "empty_files"} <= set(body["reports"])
