"""LDAP-T1: AD/LDAP directory sync — enumeration, decoding, reconciliation.

Network-free: ldap3's offline MOCK_SYNC strategy stands in for a real DC (same
pattern as test_ldap_p6t6). The pure SID/GUID decoders are unit-tested against
known binary; the sync/reconcile runs against a migrated Postgres and asserts
that an agent-pushed SID resolves into a principal_aliases row the permission
reports already join through, and that group membership expands.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from ldap3 import MOCK_SYNC, SIMPLE, Connection, Server
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import ldap_directory
from filearr.config import Settings
from filearr.ldap_auth import LdapConfig
from filearr.ldap_directory import decode_guid, decode_sid
from filearr.models import DirectoryObject, PermissionSnapshot, PrincipalAlias

BACKEND_DIR = Path(__file__).resolve().parents[1]

SVC_DN = "cn=svc,dc=corp,dc=example,dc=com"
# S-1-5-21-1001-2002-3003-1104 (alice), -513 (Domain Users group)
ALICE_SID = bytes([1, 5, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0,
                   233, 3, 0, 0, 210, 7, 0, 0, 187, 11, 0, 0, 80, 4, 0, 0])
GROUP_SID = bytes([1, 5, 0, 0, 0, 0, 0, 5, 21, 0, 0, 0,
                   233, 3, 0, 0, 210, 7, 0, 0, 187, 11, 0, 0, 1, 2, 0, 0])
ALICE_SID_STR = decode_sid(ALICE_SID)
GROUP_SID_STR = decode_sid(GROUP_SID)
ALICE_GUID = uuid.UUID("aaaa1111-2222-3333-4444-555566667777")
GROUP_GUID = uuid.UUID("bbbb1111-2222-3333-4444-555566667777")
GROUP_DN = "cn=Media Admins,ou=groups,dc=corp,dc=example,dc=com"


# --------------------------------------------------------------------------- #
# Pure decoders                                                                #
# --------------------------------------------------------------------------- #
def test_decode_sid_known_values():
    assert decode_sid(bytes([1, 1, 0, 0, 0, 0, 0, 5, 18, 0, 0, 0])) == "S-1-5-18"
    assert ALICE_SID_STR == "S-1-5-21-1001-2002-3003-1104"
    assert decode_sid(b"") is None
    assert decode_sid(b"\x01\x99") is None  # truncated sub-authorities


def test_decode_guid_roundtrips_bytes_le():
    assert decode_guid(ALICE_GUID.bytes_le) == str(ALICE_GUID)
    assert decode_guid(b"tooshort") is None


def test_domain_from_dn():
    assert ldap_directory.domain_from_dn("cn=x,dc=corp,dc=example,dc=com") == "CORP"
    assert ldap_directory.domain_from_dn("cn=x,ou=t") is None
    assert ldap_directory.domain_from_dn(None) is None


# --------------------------------------------------------------------------- #
# MOCK_SYNC directory + connector                                             #
# --------------------------------------------------------------------------- #
def _dit() -> dict:
    return {
        SVC_DN: {"userPassword": "svcpw", "objectClass": ["user"]},
        "cn=Alice,ou=people,dc=corp,dc=example,dc=com": {
            "objectClass": ["user", "person"],
            "sAMAccountName": "alice",
            "displayName": "Alice Anderson",
            "userPrincipalName": "alice@corp.example.com",
            "objectSid": ALICE_SID,
            "objectGUID": ALICE_GUID.bytes_le,
            "memberOf": [GROUP_DN],
        },
        GROUP_DN: {
            "objectClass": ["group"],
            "sAMAccountName": "MediaAdmins",
            "displayName": "Media Admins",
            "objectSid": GROUP_SID,
            "objectGUID": GROUP_GUID.bytes_le,
        },
    }


def _connector(dit: dict | None = None):
    dit = dit if dit is not None else _dit()

    def connector(cfg, *, user, password):
        srv = Server("fake", get_info="ALL")
        conn = Connection(srv, user=user, password=password,
                          authentication=SIMPLE, client_strategy=MOCK_SYNC)
        for dn, attrs in dit.items():
            conn.strategy.add_entry(dn, attrs)
        if not conn.bind():
            return None
        return conn

    return connector


def _settings(**over) -> Settings:
    base = dict(
        auth_enabled=True,
        ldap_enabled=True,
        ldap_server="ldaps://dc.corp.example.com",
        ldap_bind_dn=SVC_DN,
        ldap_bind_password="svcpw",
        ldap_user_base="dc=corp,dc=example,dc=com",
        ldap_directory_sync_enabled=True,
        ldap_directory_user_base="ou=people,dc=corp,dc=example,dc=com",
        ldap_directory_group_base="ou=groups,dc=corp,dc=example,dc=com",
    )
    base.update(over)
    return Settings(**base)


def test_enumerate_directory_decodes_users_and_groups():
    s = _settings()
    cfg = LdapConfig.from_settings(s)
    dcfg = ldap_directory.DirectoryConfig.from_settings(s)
    entries = ldap_directory.enumerate_directory(cfg, dcfg, connector=_connector())
    by_sam = {e.sam_account_name: e for e in entries}
    assert set(by_sam) == {"alice", "MediaAdmins"}
    alice = by_sam["alice"]
    assert alice.object_sid == ALICE_SID_STR
    assert alice.object_guid == str(ALICE_GUID)
    assert alice.display_name == "Alice Anderson"
    assert alice.kind == "user" and alice.domain == "CORP"
    assert alice.canonical_id() == "CORP\\alice"
    assert GROUP_DN in alice.member_of_dns
    assert by_sam["MediaAdmins"].kind == "group"


def test_enumerate_requires_service_bind():
    s = _settings(ldap_bind_dn=None, ldap_bind_password=None,
                  ldap_user_dn_template="uid={username},dc=corp,dc=example,dc=com")
    cfg = LdapConfig.from_settings(s)
    with pytest.raises(ldap_directory.LDAPError):
        ldap_directory.enumerate_directory(cfg, connector=_connector())


# --------------------------------------------------------------------------- #
# Reconciliation against a migrated Postgres                                  #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM permission_snapshots"))
        await conn.execute(text("DELETE FROM directory_objects"))
        await conn.execute(text("DELETE FROM principal_aliases"))
        await conn.execute(text("DELETE FROM agents"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    yield Session
    await engine.dispose()


async def _seed_snapshot(session, sid: str):
    """A permission snapshot whose owner ACE names a raw SID (as a non-domain-
    joined agent would push it)."""
    from filearr.models import Agent

    agent = Agent(name="nas", hostname="nas", platform="windows", cert_fingerprint="FPD")
    session.add(agent)
    await session.flush()
    session.add(
        PermissionSnapshot(
            agent_id=agent.id,
            path="/data/share/x",
            is_dir=False,
            owner={"kind": "user", "id": sid, "name": sid},
            group_=None,
            aces=[
                {"principal": {"kind": "user", "id": sid, "name": sid},
                 "type": "allow", "verbs": ["read", "write"], "raw_mask": "0x1f",
                 "inherited": False, "scope": "this", "source": "local", "order_index": 0},
            ],
            fidelity="full_native",
            posture={},
            principals=[sid],
            digest="d1",
            collected_at=datetime.now(UTC),
        )
    )
    await session.commit()


async def test_sync_reconciles_agent_pushed_sid_to_alias(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod
    from filearr.config import get_settings

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    s = _settings()
    monkeypatch.setattr(worker_mod, "get_settings", lambda: s)
    monkeypatch.setattr(ldap_directory, "get_settings", lambda: s)

    async with maker() as session:
        await _seed_snapshot(session, ALICE_SID_STR)

    out = await worker_mod.sync_directory_now(connector=_connector())
    assert out["status"] == "done"
    assert out["objects"] == 2 and out["users"] == 1 and out["groups"] == 1
    assert out["aliases_written"] == 1 and out["unresolved_sids"] == 0

    async with maker() as session:
        alias = (
            await session.execute(
                select(PrincipalAlias).where(PrincipalAlias.alias == ALICE_SID_STR)
            )
        ).scalar_one()
        assert alias.canonical == "CORP\\alice"
        assert alias.display == "Alice Anderson"
        assert alias.source == "ldap"
        # Alice's directory row carries the group SID for expansion.
        alice = (
            await session.execute(
                select(DirectoryObject).where(DirectoryObject.object_sid == ALICE_SID_STR)
            )
        ).scalar_one()
        assert GROUP_SID_STR in alice.member_of_sids


async def test_sync_counts_unresolved_and_tombstones(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    s = _settings()
    monkeypatch.setattr(worker_mod, "get_settings", lambda: s)
    monkeypatch.setattr(ldap_directory, "get_settings", lambda: s)

    async with maker() as session:
        # A SID that is NOT in the directory -> unresolved.
        await _seed_snapshot(session, "S-1-5-21-9-9-9-1234")

    out = await worker_mod.sync_directory_now(connector=_connector())
    assert out["unresolved_sids"] == 1 and out["aliases_written"] == 0

    # Re-sync with alice REMOVED from the directory -> she tombstones.
    dit = _dit()
    del dit["cn=Alice,ou=people,dc=corp,dc=example,dc=com"]
    out2 = await worker_mod.sync_directory_now(connector=_connector(dit))
    assert out2["tombstoned"] == 1
    async with maker() as session:
        alice = (
            await session.execute(
                select(DirectoryObject).where(DirectoryObject.object_sid == ALICE_SID_STR)
            )
        ).scalar_one()
        assert alice.deleted_at is not None


async def test_group_membership_expansion(maker, monkeypatch):
    from filearr import worker as worker_mod

    s = _settings()
    monkeypatch.setattr(worker_mod, "get_settings", lambda: s)
    monkeypatch.setattr(ldap_directory, "get_settings", lambda: s)

    async with maker() as session:
        # Sync the directory so member_of_sids is populated.
        from filearr import db as db_mod
        monkeypatch.setattr(db_mod, "SessionLocal", maker)
        await worker_mod.sync_directory_now(connector=_connector())

    async with maker() as session:
        expanded = await ldap_directory.expand_principals_with_groups(
            session, {ALICE_SID_STR}
        )
    # Alice's own SID plus the group SID she belongs to.
    assert ALICE_SID_STR in expanded and GROUP_SID_STR in expanded


async def test_sync_disabled_is_skipped(maker, monkeypatch):
    from filearr import worker as worker_mod

    s = _settings(ldap_directory_sync_enabled=False)
    monkeypatch.setattr(worker_mod, "get_settings", lambda: s)
    out = await worker_mod.sync_directory_now(connector=_connector())
    assert out["status"] == "skipped" and out["reason"] == "directory_sync_disabled"


async def test_sync_does_not_clobber_a_manual_alias(maker, monkeypatch):
    from filearr import db as db_mod
    from filearr import worker as worker_mod

    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    s = _settings()
    monkeypatch.setattr(worker_mod, "get_settings", lambda: s)
    monkeypatch.setattr(ldap_directory, "get_settings", lambda: s)

    async with maker() as session:
        await _seed_snapshot(session, ALICE_SID_STR)
        # An admin pinned a manual display for alice's SID BEFORE the sync.
        session.add(
            PrincipalAlias(
                alias=ALICE_SID_STR, canonical="CORP\\alice",
                display="Alice (VIP)", source="manual",
            )
        )
        await session.commit()

    await worker_mod.sync_directory_now(connector=_connector())
    async with maker() as session:
        alias = (
            await session.execute(
                select(PrincipalAlias).where(PrincipalAlias.alias == ALICE_SID_STR)
            )
        ).scalar_one()
    # The sync's conflict update is gated on source='ldap', so the manual row stands.
    assert alias.source == "manual" and alias.display == "Alice (VIP)"


# --------------------------------------------------------------------------- #
# API surface                                                                  #
# --------------------------------------------------------------------------- #
@pytest.fixture
async def api(pg_uri, monkeypatch):
    import httpx

    from filearr import db as db_mod
    from filearr.config import get_settings
    from filearr.db import get_session
    from filearr.main import create_app

    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM directory_objects"))
        await conn.execute(text("DELETE FROM principal_aliases"))
    Session = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", Session)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with Session() as sess:
            yield sess

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, Session
    app.dependency_overrides.clear()
    await engine.dispose()


async def test_directory_objects_and_status_endpoints(api):
    c, Session = api
    async with Session() as session:
        session.add_all([
            DirectoryObject(
                object_guid=str(ALICE_GUID), object_sid=ALICE_SID_STR,
                sam_account_name="alice", display_name="Alice Anderson",
                kind="user", domain="CORP", member_of_sids=[GROUP_SID_STR],
            ),
            DirectoryObject(
                object_guid=str(GROUP_GUID), object_sid=GROUP_SID_STR,
                sam_account_name="MediaAdmins", display_name="Media Admins",
                kind="group", domain="CORP",
            ),
        ])
        await session.commit()

    r = await c.get("/api/v1/directory/objects", params={"kind": "user"})
    assert r.status_code == 200
    body = r.json()
    assert body["total"] == 1 and body["objects"][0]["sam_account_name"] == "alice"

    r = await c.get("/api/v1/directory/objects", params={"q": "media"})
    assert {o["kind"] for o in r.json()["objects"]} == {"group"}

    r = await c.get("/api/v1/directory/status")
    st = r.json()
    assert st["users"] == 1 and st["groups"] == 1


async def test_directory_sync_trigger_422_when_disabled(api, monkeypatch):
    from filearr.config import get_settings

    monkeypatch.setattr(get_settings(), "ldap_directory_sync_enabled", False)
    c, _ = api
    r = await c.post("/api/v1/directory/sync")
    assert r.status_code == 422 and "disabled" in r.json()["detail"]
