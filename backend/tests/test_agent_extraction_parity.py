"""Agent extraction parity, central half (2026-08-09,
``archive/docs/agent-parity-design.md``).

The agent now runs its OWN extraction pass and attaches the result to a
replication event as the additive ``extracted`` object; central folds it into
``Item.metadata_`` (the EXTRACTED column — invariant 2, never ``user_metadata``).
What is pinned here:

* a batch WITH ``extracted`` lands ``meta`` + ``body_text`` + the provenance
  stamps on the item, and reaches the same column the central extractors write;
* an OVERSIZE payload is DROPPED but the event still applies — enrichment must
  never wedge replication (the whole point of not 413-ing the batch);
* central-owned bookkeeping keys survive, and an agent cannot forge them;
* the stale ``_embedding``/``_embedding_fp``/``_chunks_fp`` fingerprints are
  dropped when the source text changes, so ``embed_missing`` / ``chunk_missing``
  re-select the item (and are NOT dropped when it doesn't);
* an OLDER agent (no ``extracted`` field at all) is byte-for-byte unaffected;
* the four new policy keys round-trip through ``PUT /agent-policies/{scope}`` and
  show up in the admin effective-policy view.

Harness mirrors ``test_replication_p5t4`` / ``test_agent_policy_effective``
(alembic head on the pgserver Postgres, ASGITransport, auth off, agents enabled).
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
from filearr.agentsync import (
    AgentEvent,
    ExtractedPayload,
    ReplicationBatch,
    apply_batch,
    extracted_json_len,
    merge_extracted_metadata,
)
from filearr.api import agent_commands as agent_commands_mod
from filearr.chunking import CHUNKS_FP_KEY
from filearr.config import get_settings
from filearr.db import get_session
from filearr.embed import EMBEDDING_KEY, FINGERPRINT_KEY
from filearr.main import create_app
from filearr.models import Agent, Item, ItemStatus

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM policy_versions"))
        await conn.execute(text("DELETE FROM agent_replication_log"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
        await conn.execute(text("DELETE FROM agents"))
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

    # The post-commit defers open the real Procrastinate connector — stub them
    # exactly as test_replication_p5t4 does (invariant 5 is pinned there).
    async def _noop(_ids):
        return None

    monkeypatch.setattr(agent_commands_mod, "defer_index_sync", _noop)
    monkeypatch.setattr(agent_commands_mod, "defer_agent_associate", _noop)

    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(maker, *, seq: int = 0) -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
            last_contiguous_seq_no=seq,
        )
        s.add(agent)
        await s.commit()
        return agent.id


def _ev(seq: int, rel_path: str, *, etype: str = "created", extracted=None) -> AgentEvent:
    return AgentEvent(
        seq_no=seq,
        event_type=etype,
        library_ref="/srv/docs",
        rel_path=rel_path,
        size=100,
        mtime=1_700_000_000.0,
        quick_hash="q",
        extracted=extracted,
    )


async def _apply(maker, agent_id, *events) -> dict:
    async with maker() as s:
        agent = await s.get(Agent, agent_id)
        return await apply_batch(
            s, agent, ReplicationBatch(agent_id=str(agent_id), entries=list(events))
        )


async def _item(maker, rel_path: str) -> Item:
    async with maker() as s:
        return (
            await s.execute(select(Item).where(Item.rel_path == rel_path))
        ).scalar_one()


# --------------------------------------------------------------------------- #
# The happy path: extracted -> metadata_ (+ provenance)                        #
# --------------------------------------------------------------------------- #
async def test_extracted_lands_in_metadata_with_provenance(db_maker):
    agent_id = await _seed_agent(db_maker)
    payload = {
        "schema": 1,
        "meta": {"pages": 12, "title": "Q3 report", "author": "ops"},
        "body_text": "revenue was up",
        "body_text_truncated": True,
    }
    res = await _apply(db_maker, agent_id, _ev(1, "reports/q3.pdf", extracted=payload))
    assert res["upserted"] == 1

    item = await _item(db_maker, "reports/q3.pdf")
    meta = item.metadata_
    assert meta["pages"] == 12
    assert meta["title"] == "Q3 report"
    assert meta["author"] == "ops"
    assert meta["body_text"] == "revenue was up"
    assert meta["body_text_truncated"] is True
    # Provenance — an operator (and a future re-extract) must be able to tell an
    # agent extraction from a central one.
    assert meta["_extracted_by"] == "agent"
    assert meta["_extract_schema"] == 1
    assert meta["_extracted_at"]
    # Invariant 2: the EXTRACTED column only; user edits are untouched.
    assert item.user_metadata == {}


async def test_modified_event_merges_into_existing_metadata(db_maker):
    """An update path event merges rather than replaces, and the reassignment
    actually flushes (metadata_ is a plain JSONB dict — an in-place mutation
    would silently not persist)."""
    agent_id = await _seed_agent(db_maker)
    await _apply(
        db_maker,
        agent_id,
        _ev(1, "a.docx", extracted={"schema": 1, "meta": {"pages": 2}}),
    )
    await _apply(
        db_maker,
        agent_id,
        _ev(
            2,
            "a.docx",
            etype="modified",
            extracted={"schema": 1, "meta": {"author": "bo"}, "body_text": "hi"},
        ),
    )
    meta = (await _item(db_maker, "a.docx")).metadata_
    assert meta["pages"] == 2  # preserved from the earlier extraction
    assert meta["author"] == "bo"
    assert meta["body_text"] == "hi"


async def test_central_bookkeeping_keys_survive_and_cannot_be_forged(db_maker):
    """The ``_``-prefixed namespace is central's. An agent extraction must not be
    able to clear ``_extract_error`` or plant an embedding fingerprint."""
    agent_id = await _seed_agent(db_maker)
    await _apply(db_maker, agent_id, _ev(1, "b.pdf"))
    async with db_maker() as s:
        row = (await s.execute(select(Item).where(Item.rel_path == "b.pdf"))).scalar_one()
        row.metadata_ = {"_extract_error": "boom", "_sniffed_mime": "application/pdf"}
        await s.commit()

    await _apply(
        db_maker,
        agent_id,
        _ev(
            2,
            "b.pdf",
            etype="modified",
            extracted={
                "schema": 1,
                "meta": {
                    "pages": 3,
                    "_extract_error": None,  # attempted clear
                    FINGERPRINT_KEY: "forged",  # attempted forgery
                },
            },
        ),
    )
    meta = (await _item(db_maker, "b.pdf")).metadata_
    assert meta["_extract_error"] == "boom"
    assert meta["_sniffed_mime"] == "application/pdf"
    assert FINGERPRINT_KEY not in meta
    assert meta["pages"] == 3


# --------------------------------------------------------------------------- #
# Stale derivation fingerprints                                                #
# --------------------------------------------------------------------------- #
async def _seed_with_derivations(db_maker, agent_id, rel_path: str) -> None:
    await _apply(
        db_maker,
        agent_id,
        _ev(1, rel_path, extracted={"schema": 1, "body_text": "first text"}),
    )
    async with db_maker() as s:
        row = (
            await s.execute(select(Item).where(Item.rel_path == rel_path))
        ).scalar_one()
        row.metadata_ = {
            **row.metadata_,
            EMBEDDING_KEY: [0.1, 0.2],
            FINGERPRINT_KEY: "embfp",
            CHUNKS_FP_KEY: "chunkfp",
        }
        await s.commit()


async def test_changed_body_text_drops_stale_embedding_and_chunk_stamps(db_maker):
    """``embed_missing`` selects on a missing/mismatched ``_embedding_fp`` and
    ``chunk_missing`` on a missing ``_chunks_fp`` — dropping them is exactly what
    makes the backfills re-derive from the NEW text."""
    agent_id = await _seed_agent(db_maker)
    await _seed_with_derivations(db_maker, agent_id, "c.docx")

    await _apply(
        db_maker,
        agent_id,
        _ev(
            2,
            "c.docx",
            etype="modified",
            extracted={"schema": 1, "body_text": "completely different text"},
        ),
    )
    meta = (await _item(db_maker, "c.docx")).metadata_
    assert meta["body_text"] == "completely different text"
    assert EMBEDDING_KEY not in meta
    assert FINGERPRINT_KEY not in meta
    assert CHUNKS_FP_KEY not in meta


async def test_unchanged_body_text_keeps_the_fingerprints(db_maker):
    """The converse guard: a metadata-only re-extraction must not force a whole
    library to re-embed and re-chunk."""
    agent_id = await _seed_agent(db_maker)
    await _seed_with_derivations(db_maker, agent_id, "d.docx")

    await _apply(
        db_maker,
        agent_id,
        _ev(
            2,
            "d.docx",
            etype="modified",
            extracted={
                "schema": 1,
                "meta": {"author": "later"},
                "body_text": "first text",  # identical
            },
        ),
    )
    meta = (await _item(db_maker, "d.docx")).metadata_
    assert meta[FINGERPRINT_KEY] == "embfp"
    assert meta[CHUNKS_FP_KEY] == "chunkfp"
    assert meta["author"] == "later"


# --------------------------------------------------------------------------- #
# Oversize payload: dropped, never fatal                                        #
# --------------------------------------------------------------------------- #
async def test_oversize_extracted_is_dropped_but_the_event_still_applies(
    db_maker, monkeypatch, caplog
):
    agent_id = await _seed_agent(db_maker)
    monkeypatch.setattr(get_settings(), "agent_extracted_max_bytes", 256)
    big = {"schema": 1, "meta": {"pages": 1}, "body_text": "x" * 5000}
    assert extracted_json_len(ExtractedPayload(**big)) > 256

    with caplog.at_level("WARNING", logger="filearr.agentsync"):
        res = await _apply(db_maker, agent_id, _ev(1, "huge.pdf", extracted=big))

    assert res["upserted"] == 1  # replication is NOT wedged
    item = await _item(db_maker, "huge.pdf")
    assert item.metadata_ in ({}, None)  # nothing extracted was applied
    assert item.size == 100 and item.quick_hash == "q"  # identity applied normally
    assert any("oversize extracted payload" in r.getMessage() for r in caplog.records)


async def test_oversize_on_an_update_leaves_prior_metadata_intact(db_maker, monkeypatch):
    agent_id = await _seed_agent(db_maker)
    await _apply(
        db_maker,
        agent_id,
        _ev(1, "e.pdf", extracted={"schema": 1, "meta": {"pages": 9}}),
    )
    monkeypatch.setattr(get_settings(), "agent_extracted_max_bytes", 64)
    await _apply(
        db_maker,
        agent_id,
        _ev(
            2,
            "e.pdf",
            etype="modified",
            extracted={"schema": 1, "body_text": "y" * 5000},
        ),
    )
    meta = (await _item(db_maker, "e.pdf")).metadata_
    assert meta["pages"] == 9
    assert "body_text" not in meta


# --------------------------------------------------------------------------- #
# Regression guard: an older agent is unaffected                                #
# --------------------------------------------------------------------------- #
async def test_older_agent_without_the_field_is_unchanged(db_maker):
    """``extracted`` is purely additive. An agent build that predates it sends no
    such key; the item must come out exactly as it did before this feature."""
    agent_id = await _seed_agent(db_maker)
    ev = AgentEvent.model_validate(
        {
            "seq_no": 1,
            "event_type": "created",
            "library_ref": "/srv/docs",
            "rel_path": "legacy.mkv",
            "size": 42,
            "mtime": 1_700_000_000.0,
            "quick_hash": "q",
        }
    )
    assert ev.extracted is None
    await _apply(db_maker, agent_id, ev)

    item = await _item(db_maker, "legacy.mkv")
    assert item.metadata_ in ({}, None)
    assert item.size == 42
    assert item.status == ItemStatus.active


async def test_replication_endpoint_accepts_and_applies_extracted(client):
    """End to end over the wire, since the payload has to survive request parsing
    (``schema`` is an alias for the model's ``schema_version`` attribute)."""
    c, maker, _ = client
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        agent_id, fp = agent.id, agent.cert_fingerprint

    r = await c.post(
        f"/api/v1/agents/{agent_id}/replication-batch",
        headers={"Authorization": f"Bearer {fp}"},
        json={
            "agent_id": str(agent_id),
            "entries": [
                {
                    "seq_no": 1,
                    "event_type": "created",
                    "library_ref": "/srv/docs",
                    "rel_path": "wire.docx",
                    "size": 10,
                    "mtime": 1_700_000_000.0,
                    "extracted": {
                        "schema": 1,
                        "meta": {"pages": 4, "ocr_text": "scanned words"},
                        "body_text": "hello wire",
                    },
                }
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["upserted"] == 1

    meta = (await _item(maker, "wire.docx")).metadata_
    assert meta["pages"] == 4
    assert meta["ocr_text"] == "scanned words"
    assert meta["body_text"] == "hello wire"
    assert meta["_extracted_by"] == "agent"


# --------------------------------------------------------------------------- #
# The four policy keys                                                          #
# --------------------------------------------------------------------------- #
EXTRACT_POLICY = {
    "extract_enabled": True,
    "extract_body_text": True,
    "extract_ocr": False,
    "extract_max_bytes": 33_554_432,
}


async def test_extraction_policy_keys_round_trip(client):
    c, maker, _ = client
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        agent_id = agent.id

    r = await c.put("/api/v1/agent-policies/global", json={"policy": EXTRACT_POLICY})
    assert r.status_code == 200, r.text
    assert r.json()["policy"] == EXTRACT_POLICY

    body = (await c.get(f"/api/v1/agent-policies/effective/{agent_id}")).json()
    assert body["policy"] == EXTRACT_POLICY
    for key in EXTRACT_POLICY:
        assert body["source_keys"][key] == "global"


async def test_extraction_policy_keys_are_known_defaults_when_absent(client):
    """They must be MODELLED keys (reported as ``default``), not forward-compat
    unknowns — the console renders a field per known key."""
    c, maker, _ = client
    async with maker() as s:
        agent = Agent(
            name="nas",
            hostname="nas",
            platform="linux",
            cert_fingerprint="FP:" + uuid.uuid4().hex,
        )
        s.add(agent)
        await s.commit()
        agent_id = agent.id

    body = (await c.get(f"/api/v1/agent-policies/effective/{agent_id}")).json()
    for key in EXTRACT_POLICY:
        assert body["source_keys"][key] == "default"


async def test_extract_max_bytes_rejects_a_negative_bound(client):
    c, _, _ = client
    r = await c.put(
        "/api/v1/agent-policies/global", json={"policy": {"extract_max_bytes": -1}}
    )
    assert r.status_code == 422
    assert "extract_max_bytes" in r.json()["detail"]


# --------------------------------------------------------------------------- #
# archive_members projection                                                   #
# --------------------------------------------------------------------------- #
def _payload(meta: dict) -> ExtractedPayload:
    return ExtractedPayload.model_validate({"schema": 1, "meta": meta})


def test_archive_members_derived_for_the_search_projection():
    """The agent ships only the structured ``archive`` object, but the Meili
    projection indexes the flat ``archive_members`` string — central derives it
    so an agent-extracted archive's contents are actually searchable."""
    now = datetime.now(UTC)
    merged = merge_extracted_metadata(
        None,
        _payload(
            {
                "archive": {
                    "member_count": 2,
                    "format": "zip",
                    "members": [{"name": "a/b.txt", "size": 1}, {"name": "c.md", "size": 2}],
                }
            }
        ),
        now=now,
    )
    assert merged["archive_members"] == "a/b.txt\nc.md"


def test_archive_members_respects_an_agent_supplied_string_and_odd_shapes():
    now = datetime.now(UTC)
    # An agent that DOES send the flat string wins — no double derivation.
    merged = merge_extracted_metadata(
        None,
        _payload({"archive": {"members": [{"name": "x"}]}, "archive_members": "kept"}),
        now=now,
    )
    assert merged["archive_members"] == "kept"

    # Non-archive extractions gain nothing, and malformed shapes never raise.
    assert "archive_members" not in merge_extracted_metadata(
        None, _payload({"title": "t"}), now=now
    )
    for bad in ({"members": "nope"}, {"members": []}, {"members": [{"size": 1}]}, {}):
        merge_extracted_metadata(None, _payload({"archive": bad}), now=now)


def test_archive_members_is_capped_to_the_index_ceiling(monkeypatch):
    from filearr.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "archive_members_index_chars", 10)
    merged = merge_extracted_metadata(
        None,
        _payload({"archive": {"members": [{"name": "n" * 50}]}}),
        now=datetime.now(UTC),
    )
    assert len(merged["archive_members"]) == 10
