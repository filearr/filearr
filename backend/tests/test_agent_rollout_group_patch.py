"""PATCH /agents/{id} — re-assigning an ENROLLED agent's policy (rollout) group.

Why this module exists: ``agents.rollout_group`` used to be written exactly once,
at register, from the consumed enrollment token. Nothing else in the backend ever
assigned it, so the MIDDLE policy scope (``policy.resolve_effective_policy``:
``agent:<id>`` > ``group:<rollout_group>`` > ``global``) was frozen for an agent's
whole life — the only way to move a desktop into a ``filers`` policy was to
re-enroll the machine. Meanwhile the grouping the console COULD edit
(``config_group_id``) is an orthogonal dimension policy resolution never reads.

The regression that actually matters is the last test here: an agent moved into a
group that HAS a ``group:`` document must resolve THAT document on its next
effective-policy read — whole-document, no key merging. Everything else (404 /
409 / scope / validation / audit) guards the endpoint around that behaviour.

Harness mirrors ``test_agent_policy_effective`` / ``test_agents_p5t1``: alembic
head on the pgserver Postgres, ASGITransport, auth off (admin gating re-checked
with auth ON in its own test), agents enabled.
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
from filearr import policy as policy_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent, PolicyVersion

pytestmark = pytest.mark.asyncio
BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM security_events"))
        await conn.execute(text("DELETE FROM policy_versions"))
        await conn.execute(text("UPDATE agents SET config_group_id = NULL"))
        await conn.execute(text("DELETE FROM agent_config_groups"))
        await conn.execute(text("DELETE FROM enrollment_tokens"))
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
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


async def _seed_agent(
    maker, *, rollout_group: str = "default", name: str = "box", revoked: bool = False
) -> uuid.UUID:
    async with maker() as s:
        agent = Agent(
            name=name,
            hostname=name,
            platform="linux",
            rollout_group=rollout_group,
            cert_fingerprint="FP:" + uuid.uuid4().hex,
            revoked_at=datetime.now(UTC) if revoked else None,
        )
        s.add(agent)
        await s.commit()
        return agent.id


async def _put_policy(maker, scope_type: str, scope_id: str | None, policy: dict):
    async with maker() as s:
        s.add(
            PolicyVersion(
                scope_type=scope_type,
                scope_id=scope_id,
                version=await policy_mod.next_version(s, scope_type, scope_id),
                policy=policy,
            )
        )
        await s.commit()


async def _events(maker, event_type: str) -> list[dict]:
    async with maker() as s:
        rows = (
            await s.execute(
                text("SELECT details FROM security_events WHERE event_type = :et"),
                {"et": event_type},
            )
        ).all()
    return [r.details for r in rows]


# --------------------------------------------------------------------------- #
# The gap this closes: the group is assignable after enrollment                #
# --------------------------------------------------------------------------- #
async def test_patch_changes_rollout_group_and_reports_new_scope(client):
    """The group moves, and the response says which policy document the agent
    now resolves — the operator should never have to infer that."""
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="default")
    await _put_policy(maker, "global", None, {"extract_enabled": False})
    await _put_policy(maker, "group", "filers", {"extract_enabled": True})

    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["rollout_group"] == "filers"
    assert body["policy_scope"] == "group:filers"
    assert body["policy_version"] == 1

    async with maker() as s:
        agent = await s.get(Agent, aid)
        assert agent.rollout_group == "filers"


async def test_resolved_policy_follows_the_new_group(client):
    """THE regression: after the move, the agent's effective policy IS the target
    group's document — whole-document, no merging with what it had before."""
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="desktops")
    # A global document every agent falls back to...
    await _put_policy(
        maker, "global", None, {"extract_enabled": False, "watch_mode": False}
    )
    # ...a desktops document that inventories but never extracts EXIF/OCR...
    await _put_policy(
        maker,
        "group",
        "desktops",
        {"extract_enabled": True, "extract_exif": False, "extract_ocr": False},
    )
    # ...and a filers document that does both.
    await _put_policy(
        maker,
        "group",
        "filers",
        {"extract_enabled": True, "extract_exif": True, "extract_ocr": True},
    )

    before = await c.get(f"/api/v1/agent-policies/effective/{aid}")
    assert before.status_code == 200, before.text
    assert before.json()["scope"] == "group:desktops"
    assert before.json()["policy"]["extract_exif"] is False

    patched = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert patched.status_code == 200, patched.text
    assert patched.json()["policy_scope"] == "group:filers"

    after = await c.get(f"/api/v1/agent-policies/effective/{aid}")
    assert after.status_code == 200, after.text
    eff = after.json()
    assert eff["scope"] == "group:filers"
    assert eff["policy"]["extract_exif"] is True
    assert eff["policy"]["extract_ocr"] is True
    # Whole-document replacement: the global document's watch_mode key does NOT
    # leak into the winner (the agent falls back to its BUILT-IN default there).
    assert "watch_mode" not in eff["policy"]
    assert eff["source_keys"]["extract_exif"] == "group"
    assert eff["source_keys"]["watch_mode"] == "default"


async def test_patch_falls_back_to_global_when_target_group_has_no_document(client):
    """Moving into a group with no ``group:`` row is legal and reported honestly
    as ``global`` — the operator sees they still have a document to author."""
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="default")
    await _put_policy(maker, "global", None, {"extract_enabled": False})

    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "laptops"})
    assert r.status_code == 200, r.text
    assert r.json()["policy_scope"] == "global"


async def test_agent_scope_still_outranks_the_new_group(client):
    """Precedence is untouched: a per-agent document keeps winning after a group
    move (the endpoint changes the middle scope, not the ordering)."""
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="default")
    await _put_policy(maker, "group", "filers", {"extract_ocr": True})
    await _put_policy(maker, "agent", str(aid), {"extract_ocr": False})

    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert r.status_code == 200, r.text
    assert r.json()["policy_scope"] == f"agent:{aid}"


# --------------------------------------------------------------------------- #
# The second job of rollout_group: release-canary membership                   #
# --------------------------------------------------------------------------- #
async def test_canary_membership_is_reported(client):
    """One field, two jobs. Moving an agent in/out of the configured canary group
    silently changes which builds it is offered, so the response says so."""
    c, maker, settings = client
    aid = await _seed_agent(maker, rollout_group="default")

    into = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "canary"})
    assert into.status_code == 200, into.text
    assert into.json()["canary_releases"] is True
    assert settings.agent_canary_group == "canary"

    out = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert out.status_code == 200, out.text
    assert out.json()["canary_releases"] is False


# --------------------------------------------------------------------------- #
# Rename (the other mutable field)                                             #
# --------------------------------------------------------------------------- #
async def test_patch_renames_without_touching_the_group(client):
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="filers", name="old-name")

    r = await c.patch(f"/api/v1/agents/{aid}", json={"name": "reception-pc"})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "reception-pc"
    assert r.json()["rollout_group"] == "filers"
    async with maker() as s:
        agent = await s.get(Agent, aid)
        assert agent.name == "reception-pc"
        assert agent.hostname == "old-name"  # the machine fact is never rewritten


# --------------------------------------------------------------------------- #
# Errors: 404 / 409 / validation / scope                                       #
# --------------------------------------------------------------------------- #
async def test_unknown_agent_404s(client):
    c, _, _ = client
    r = await c.patch(
        f"/api/v1/agents/{uuid.uuid4()}", json={"rollout_group": "filers"}
    )
    assert r.status_code == 404


async def test_revoked_agent_409s(client):
    """A revoked agent is denylisted on every policy/replication request, so
    re-grouping it would promise something that can never be delivered."""
    c, maker, _ = client
    aid = await _seed_agent(maker, revoked=True)
    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert r.status_code == 409
    async with maker() as s:
        assert (await s.get(Agent, aid)).rollout_group == "default"


@pytest.mark.parametrize(
    "body",
    [
        {"rollout_group": ""},          # empty
        {"rollout_group": "   "},       # whitespace-only (would be a phantom scope)
        {"rollout_group": "g" * 129},   # over the 128-char TokenMintIn limit
        {"name": ""},
        {"name": "n" * 256},
        {},                             # changes nothing — a typo'd field name
        {"hostname": "nope"},           # not mutable; body carries no real change
    ],
)
async def test_validation_rejects(client, body):
    c, maker, _ = client
    aid = await _seed_agent(maker)
    r = await c.patch(f"/api/v1/agents/{aid}", json=body)
    assert r.status_code == 422, r.text
    async with maker() as s:
        assert (await s.get(Agent, aid)).rollout_group == "default"


async def test_group_name_is_stripped(client):
    """A copy-pasted trailing space would resolve to a DIFFERENT scope string than
    the ``group:filers`` document an operator authored — normalise, don't debug."""
    c, maker, _ = client
    aid = await _seed_agent(maker)
    await _put_policy(maker, "group", "filers", {"extract_ocr": True})
    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "  filers  "})
    assert r.status_code == 200, r.text
    assert r.json()["rollout_group"] == "filers"
    assert r.json()["policy_scope"] == "group:filers"


async def test_admin_scope_enforced(db_maker, monkeypatch):
    """Same gate as every other agent mutation — admin, not write; the endpoint
    must not widen the surface."""
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    aid = await _seed_agent(maker)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
        assert r.status_code == 401
        assert (await c.get("/api/v1/agents/rollout-groups")).status_code == 401
    app.dependency_overrides.clear()


async def test_feature_gate_404s(db_maker, monkeypatch):
    """FILEARR_AGENTS_ENABLED off → the whole surface is 404, this route included."""
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", False)
    aid = await _seed_agent(maker)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
        assert r.status_code == 404
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Audit trail                                                                  #
# --------------------------------------------------------------------------- #
async def test_audit_event_records_old_and_new(client):
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="default", name="box")
    r = await c.patch(
        f"/api/v1/agents/{aid}",
        json={"rollout_group": "filers", "name": "nas-01"},
    )
    assert r.status_code == 200, r.text
    events = await _events(maker, "agent_updated")
    assert len(events) == 1
    assert events[0]["agent_id"] == str(aid)
    assert events[0]["changes"]["rollout_group"] == {"old": "default", "new": "filers"}
    assert events[0]["changes"]["name"] == {"old": "box", "new": "nas-01"}


async def test_no_op_patch_emits_no_event(client):
    """Re-submitting the current value is a successful 200 (idempotent) but must
    not pollute the trail with a change that did not happen."""
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="filers")
    r = await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    assert r.status_code == 200, r.text
    assert await _events(maker, "agent_updated") == []


# --------------------------------------------------------------------------- #
# GET /agents/rollout-groups — the member count the console cannot derive      #
# --------------------------------------------------------------------------- #
async def test_rollout_groups_lists_counts_and_flags_canary(client):
    """Counts come from ONE grouped query over the whole table: the agents list
    pages server-side, so a client-side tally would under-report a big fleet."""
    c, maker, _ = client
    await _seed_agent(maker, rollout_group="filers", name="nas1")
    await _seed_agent(maker, rollout_group="filers", name="nas2")
    await _seed_agent(maker, rollout_group="desktops", name="pc1")
    await _seed_agent(maker, rollout_group="canary", name="lab1")

    r = await c.get("/api/v1/agents/rollout-groups")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canary_group"] == "canary"
    rows = {g["name"]: g for g in body["groups"]}
    assert rows["filers"]["agent_count"] == 2
    assert rows["desktops"]["agent_count"] == 1
    assert rows["filers"]["canary"] is False
    assert rows["canary"]["canary"] is True
    # The literal path must beat /agents/{agent_id}: a UUID-typed path param
    # would 422 on "rollout-groups" if the routes were declared the other way.
    assert [g["name"] for g in body["groups"]] == sorted(rows)


async def test_canary_group_row_exists_even_when_empty(client):
    """The "canary matches nobody" trap: name every group desktops/filers and the
    default canary group has no members — the console can only warn if the row
    (agent_count 0) comes back anyway."""
    c, maker, _ = client
    await _seed_agent(maker, rollout_group="filers")
    body = (await c.get("/api/v1/agents/rollout-groups")).json()
    rows = {g["name"]: g["agent_count"] for g in body["groups"]}
    assert rows == {"canary": 0, "filers": 1}


async def test_rollout_groups_follows_a_patch(client):
    c, maker, _ = client
    aid = await _seed_agent(maker, rollout_group="desktops")
    await c.patch(f"/api/v1/agents/{aid}", json={"rollout_group": "filers"})
    rows = {
        g["name"]: g["agent_count"]
        for g in (await c.get("/api/v1/agents/rollout-groups")).json()["groups"]
    }
    assert rows == {"canary": 0, "filers": 1}
