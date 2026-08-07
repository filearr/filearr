"""LLM/RAG M2 (doc_chunks + retrieve_passages) and M3 (curator writes) —
plus the audit-FK regression (M1's LLM_TOOL_CALL events were silently dropped
because an ApiKey uuid was passed into the principals FK column).

Real Postgres through the mounted facade; Meili is faked at the module seam
(no Meilisearch in the suite); chunk embedding runs with semantic disabled
(vectorless chunks are valid — keyword retrieval).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.api.llm import llm_app
from filearr.chunking import CHUNKS_FP_KEY, chunk_text, chunks_fingerprint
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import ApiKey, DocChunk, Item, ItemVersion, Library, SecurityEvent
from filearr.security import generate_key

pytestmark = pytest.mark.anyio

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
async def db_maker(module_db, monkeypatch):
    uri = module_db.get_uri()
    monkeypatch.setenv("FILEARR_DATABASE_URL", _psycopg3(uri))
    get_settings.cache_clear()
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM doc_chunks"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM api_keys"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def client(db_maker, monkeypatch):
    import filearr.api.llm as llm_api_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    synced: list[list[str]] = []

    async def _fake_sync(ids):
        synced.append(list(ids))

    monkeypatch.setattr(llm_api_mod, "defer_index_sync", _fake_sync)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    llm_app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        c.synced = synced  # type: ignore[attr-defined]
        yield c
    llm_app.dependency_overrides.clear()


async def _seed(db_maker) -> dict:
    async with db_maker() as s:
        lib = Library(
            name=f"docs-{uuid.uuid4().hex[:6]}",
            root_path="/data/docs",
            chunking_enabled=True,
        )
        s.add(lib)
        await s.flush()
        doc = Item(
            library_id=lib.id,
            path="/data/docs/report.pdf",
            rel_path="report.pdf",
            filename="report.pdf",
            extension="pdf",
            size=100,
            mtime=datetime(2026, 5, 1, tzinfo=UTC),
            status="active",
            file_category="document",
            file_group="pdf",
            metadata_={"body_text": "alpha beta gamma. " * 200},
            tags=["existing"],
        )
        s.add(doc)
        keys = {}
        for role in ("librarian", "analyst", "curator"):
            full, prefix, key_hash = generate_key()
            s.add(
                ApiKey(
                    name=f"t-{role}", prefix=prefix, key_hash=key_hash,
                    scopes=["read"], llm_role=role,
                )
            )
            keys[role] = full
        await s.commit()
        return {"keys": keys, "doc_id": str(doc.id), "library_id": str(lib.id)}


def _h(key: str) -> dict:
    return {"Authorization": f"Bearer {key}"}


# --------------------------------------------------------------------------- #
# Chunker unit behaviour                                                      #
# --------------------------------------------------------------------------- #
def test_chunk_text_windows_and_overlap():
    text_in = ("word " * 600).strip()  # 3000 chars
    parts = chunk_text(text_in, size=1000, overlap=150)
    assert len(parts) >= 3
    assert all(len(p) <= 1000 for p in parts)
    # overlap: consecutive chunks share a suffix/prefix window
    assert parts[1][:50] in parts[0][-400:] + parts[1]
    # whitespace-preferring boundary: no chunk starts/ends mid-"word"
    assert all(not p.startswith("ord") for p in parts)


def test_chunk_text_empty_and_short():
    assert chunk_text("", size=1000, overlap=150) == []
    assert chunk_text("tiny", size=1000, overlap=150) == ["tiny"]


def test_fingerprint_changes_with_text_and_config(monkeypatch):
    get_settings.cache_clear()
    s = get_settings()
    a = chunks_fingerprint("hello", s)
    assert a == chunks_fingerprint("hello", s)
    assert a != chunks_fingerprint("other", s)
    monkeypatch.setattr(s, "chunk_size_chars", 500)
    assert a != chunks_fingerprint("hello", s)


# --------------------------------------------------------------------------- #
# chunk_item task (fake Meili projection seam)                                #
# --------------------------------------------------------------------------- #
async def test_chunk_item_stores_rows_and_stamps(db_maker, monkeypatch):
    from filearr.tasks import chunks as chunks_mod

    seed = await _seed(db_maker)
    monkeypatch.setattr(chunks_mod, "SessionLocal", db_maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "semantic_enabled", False)

    upserted: list[list[dict]] = []
    deleted: list[str] = []

    async def _ensure():
        return None

    async def _upsert(docs):
        upserted.append(list(docs))

    async def _delete(item_id):
        deleted.append(item_id)

    monkeypatch.setattr(chunks_mod, "ensure_chunks_index", _ensure)
    monkeypatch.setattr(chunks_mod, "upsert_chunk_docs", _upsert)
    monkeypatch.setattr(chunks_mod, "delete_item_chunk_docs", _delete)

    n = await chunks_mod.chunk_item(seed["doc_id"])
    assert n >= 3

    async with db_maker() as s:
        rows = (
            (
                await s.execute(
                    select(DocChunk).where(DocChunk.item_id == uuid.UUID(seed["doc_id"]))
                )
            )
            .scalars()
            .all()
        )
        assert len(rows) == n
        assert all(r.embedding is None for r in rows)  # semantic off
        item = await s.get(Item, uuid.UUID(seed["doc_id"]))
        assert CHUNKS_FP_KEY in item.metadata_
    assert deleted == [seed["doc_id"]]
    assert len(upserted) == 1 and upserted[0][0]["item_id"] == seed["doc_id"]
    assert upserted[0][0]["id"] == f"{seed['doc_id']}_0"

    # idempotent: second run no-ops on the fingerprint stamp
    assert await chunks_mod.chunk_item(seed["doc_id"]) == 0


async def test_chunk_item_respects_library_opt_in(db_maker, monkeypatch):
    from filearr.tasks import chunks as chunks_mod

    seed = await _seed(db_maker)
    monkeypatch.setattr(chunks_mod, "SessionLocal", db_maker)
    async with db_maker() as s:
        lib = await s.get(Library, uuid.UUID(seed["library_id"]))
        lib.chunking_enabled = False
        await s.commit()
    assert await chunks_mod.chunk_item(seed["doc_id"]) == 0
    async with db_maker() as s:
        assert (
            await s.execute(select(DocChunk).limit(1))
        ).scalar_one_or_none() is None


# --------------------------------------------------------------------------- #
# Facade: retrieve_passages gating; curator writes; audit regression          #
# --------------------------------------------------------------------------- #
async def test_retrieve_passages_role_gating_and_search(client, db_maker, monkeypatch):
    import filearr.api.llm as llm_api_mod

    seed = await _seed(db_maker)

    # librarian lacks the tool entirely
    r = await client.post(
        "/api/llm/v1/retrieve_passages",
        headers=_h(seed["keys"]["librarian"]),
        json={"query": "alpha"},
    )
    assert r.status_code == 403

    # analyst hits the (faked) chunks index and gets cited passages
    class _FakeIndex:
        def __init__(self):
            self.calls = []

        async def search(self, q, **kwargs):
            self.calls.append((q, kwargs))

            class R:
                hits = [
                    {
                        "item_id": seed["doc_id"],
                        "chunk_no": 0,
                        "filename": "report.pdf",
                        "rel_path": "report.pdf",
                        "text": "alpha beta gamma",
                    }
                ]

            return R()

    fake_index = _FakeIndex()

    class _FakeClient:
        def index(self, uid):
            assert uid.endswith("_chunks")
            return fake_index

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(llm_api_mod, "meili_client", lambda: _FakeClient())
    r = await client.post(
        "/api/llm/v1/retrieve_passages",
        headers=_h(seed["keys"]["analyst"]),
        json={"query": "alpha", "k": 3},
    )
    assert r.status_code == 200
    passages = r.json()["passages"]
    assert passages[0]["citation"] == seed["doc_id"]
    assert passages[0]["text"].startswith("alpha")
    assert fake_index.calls[0][1]["limit"] == 3

    # dsl narrowing that matches nothing short-circuits without a Meili call
    before = len(fake_index.calls)
    r = await client.post(
        "/api/llm/v1/retrieve_passages",
        headers=_h(seed["keys"]["analyst"]),
        json={"query": "alpha", "dsl": "kind:video"},
    )
    assert r.status_code == 200 and r.json()["passages"] == []
    assert len(fake_index.calls) == before


async def test_curator_writes_and_analyst_denied(client, db_maker):
    seed = await _seed(db_maker)
    cur = seed["keys"]["curator"]

    # analyst cannot write
    r = await client.post(
        "/api/llm/v1/tag_files",
        headers=_h(seed["keys"]["analyst"]),
        json={"citations": [seed["doc_id"]], "add": ["x"]},
    )
    assert r.status_code == 403

    # curator tags: PATCH-merge (existing tag kept), audited, synced
    r = await client.post(
        "/api/llm/v1/tag_files",
        headers=_h(cur),
        json={"citations": [seed["doc_id"]], "add": ["tax", "2023"], "remove": ["nope"]},
    )
    assert r.status_code == 200
    assert r.json()["changed"] == 1
    async with db_maker() as s:
        item = await s.get(Item, uuid.UUID(seed["doc_id"]))
        assert set(item.tags) == {"existing", "tax", "2023"}
        versions = (
            (
                await s.execute(
                    select(ItemVersion).where(ItemVersion.item_id == item.id)
                )
            )
            .scalars()
            .all()
        )
        assert any(v.actor == "llm:t-curator" for v in versions)
    assert client.synced and client.synced[0] == [seed["doc_id"]]  # type: ignore[attr-defined]

    # idempotent second call: nothing changes
    r = await client.post(
        "/api/llm/v1/tag_files",
        headers=_h(cur),
        json={"citations": [seed["doc_id"]], "add": ["tax"]},
    )
    assert r.json() == {"changed": 0, "unchanged": 1, "add": ["tax"], "remove": []}

    # annotate sets + clears the note in user_metadata
    r = await client.post(
        "/api/llm/v1/annotate",
        headers=_h(cur),
        json={"citation": seed["doc_id"], "note": "quarterly summary"},
    )
    assert r.status_code == 200 and r.json()["note"] == "quarterly summary"
    async with db_maker() as s:
        item = await s.get(Item, uuid.UUID(seed["doc_id"]))
        assert item.user_metadata["note"] == "quarterly summary"
    r = await client.post(
        "/api/llm/v1/annotate",
        headers=_h(cur),
        json={"citation": seed["doc_id"], "note": ""},
    )
    assert r.json()["cleared"] is True
    async with db_maker() as s:
        item = await s.get(Item, uuid.UUID(seed["doc_id"]))
        assert "note" not in item.user_metadata

    # empty tag_files request is a 422
    r = await client.post(
        "/api/llm/v1/tag_files", headers=_h(cur), json={"citations": [seed["doc_id"]]}
    )
    assert r.status_code == 422


async def test_curator_capabilities_and_prompt(client, db_maker):
    seed = await _seed(db_maker)
    r = await client.get(
        "/api/llm/v1/capabilities", headers=_h(seed["keys"]["curator"])
    )
    tools = r.json()["role"]["tools"]
    assert {"tag_files", "annotate", "retrieve_passages", "read_content"} <= set(tools)
    r = await client.get(
        "/api/llm/v1/system-prompt", headers=_h(seed["keys"]["curator"])
    )
    assert "tag_files" in r.text
    assert "read-only" not in r.text  # write role gets the write-aware denial


async def test_audit_events_persist_and_usage_counts(client, db_maker):
    """Regression: M1 passed the ApiKey uuid into security_events.principal_id
    (an FK to principals) — every facade audit emit failed and was swallowed.
    Now the key rides in details and events actually land; the llm-keys list
    aggregates them into the M3 usage dashboard."""
    seed = await _seed(db_maker)
    for _ in range(3):
        r = await client.post(
            "/api/llm/v1/catalog_overview", headers=_h(seed["keys"]["analyst"])
        )
        assert r.status_code == 200

    async with db_maker() as s:
        events = (
            (
                await s.execute(
                    select(SecurityEvent).where(
                        SecurityEvent.event_type == "LLM_TOOL_CALL"
                    )
                )
            )
            .scalars()
            .all()
        )
    assert len(events) == 3
    assert all(e.principal_id is None for e in events)
    assert all(e.details["tool"] == "catalog_overview" for e in events)

    r = await client.get("/api/v1/llm-keys")
    by_name = {k["name"]: k for k in r.json()["keys"]}
    assert by_name["t-analyst"]["tool_calls"] == 3
    assert by_name["t-analyst"]["last_call_at"] is not None
    assert by_name["t-analyst"]["last_used_at"] is not None  # facade stamps it now
    assert by_name["t-librarian"]["tool_calls"] == 0
