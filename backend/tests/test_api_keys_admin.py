# ruff: noqa: E501
"""Admin-minted ordinary API keys (2026-08-17): mint with scopes/expiry, the
minted key authorises exactly its scopes on the main API, listing hides LLM
keys and never shows key material, revoke kills it immediately, non-admins get
403."""

from __future__ import annotations

import pytest

from .test_roles_account_settings import _bootstrap_admin, client, db_maker  # noqa: F401, F811

pytestmark = pytest.mark.asyncio


async def _mk_account(c, name="sonarr-box"):
    r = await c.post("/api/v1/service-accounts", json={"name": name})
    assert r.status_code == 201, r.text
    return r.json()["id"]


async def test_mint_list_use_revoke(client):  # noqa: F811
    c, maker, _ = client
    await _bootstrap_admin(c)
    acct = await _mk_account(c)

    r = await c.get("/api/v1/api-keys/scopes")
    assert r.status_code == 200
    assert [s["name"] for s in r.json()["scopes"]] == ["read", "write", "admin"]

    r = await c.post(
        "/api/v1/api-keys",
        json={"name": "sonarr", "scopes": ["write", "read", "read"], "expires_days": 30,
              "service_account_id": acct},
    )
    assert r.status_code == 201, r.text
    minted = r.json()
    assert minted["service_account"] == "sonarr-box"
    # a key without an owning service account is refused (no orphan keys)
    assert (await c.post("/api/v1/api-keys", json={"name": "x", "scopes": ["read"]})).status_code == 422
    assert minted["scopes"] == ["read", "write"]  # deduped, canonical order
    assert minted["expires_at"] and not minted["expired"]
    key = minted["key"]
    assert key.startswith(minted["prefix"])

    # unknown scope -> 422
    r = await c.post("/api/v1/api-keys", json={"name": "x", "scopes": ["root"], "service_account_id": acct})
    assert r.status_code == 422

    # Listing: present, no key material, LLM keys excluded.
    r = await c.post("/api/v1/llm-keys", json={"name": "llm", "role": "librarian"})
    assert r.status_code == 201, r.text
    r = await c.get("/api/v1/api-keys")
    rows = r.json()["keys"]
    assert [k["name"] for k in rows] == ["sonarr"]
    assert "key" not in rows[0]

    # The minted key works as a bearer for read + write, not admin.
    hdr = {"Authorization": f"Bearer {key}"}
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 200
    r = await c.get("/api/v1/api-keys", headers=hdr)  # admin-only
    assert r.status_code == 403

    # Revoke -> 204, then the key is dead and a second revoke is 404.
    r = await c.delete(f"/api/v1/api-keys/{minted['id']}")
    assert r.status_code == 204
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 401
    assert (await c.delete(f"/api/v1/api-keys/{minted['id']}")).status_code == 404


async def test_admin_scoped_key_and_non_admin_refused(client):  # noqa: F811
    c, maker, _ = client
    await _bootstrap_admin(c)
    acct = await _mk_account(c, "ops")
    r = await c.post("/api/v1/api-keys", json={"name": "ops", "scopes": ["admin"], "service_account_id": acct})
    assert r.status_code == 201, r.text
    key = r.json()["key"]
    hdr = {"Authorization": f"Bearer {key}"}
    # admin implies everything, including minting further keys
    r = await c.post("/api/v1/api-keys", json={"name": "child", "scopes": ["read"], "service_account_id": acct}, headers=hdr)
    assert r.status_code == 201
    child = r.json()["key"]
    # a read key cannot list or mint
    ch = {"Authorization": f"Bearer {child}"}
    assert (await c.get("/api/v1/api-keys", headers=ch)).status_code == 403
    assert (await c.post("/api/v1/api-keys", json={"name": "n", "service_account_id": acct}, headers=ch)).status_code == 403
    # revoking an LLM key through this router is a 404 (wrong family)
    r = await c.post("/api/v1/llm-keys", json={"name": "llm", "role": "librarian"})
    assert (await c.delete(f"/api/v1/api-keys/{r.json()['id']}")).status_code == 404


async def test_service_account_disable_and_delete_take_keys_along(client):  # noqa: F811
    c, maker, _ = client
    await _bootstrap_admin(c)
    acct = await _mk_account(c, "grafana")
    assert (await c.post("/api/v1/service-accounts", json={"name": "Grafana"})).status_code == 409
    r = await c.post("/api/v1/api-keys", json={"name": "k", "scopes": ["read"], "service_account_id": acct})
    key = r.json()["key"]
    hdr = {"Authorization": f"Bearer {key}"}
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 200
    r = await c.get("/api/v1/service-accounts")
    row = next(a for a in r.json()["service_accounts"] if a["id"] == acct)
    assert row["key_count"] == 1 and row["disabled"] is False
    # disable -> key refused immediately; minting under it refused
    r = await c.patch(f"/api/v1/service-accounts/{acct}", json={"disabled": True})
    assert r.status_code == 200 and r.json()["disabled"] is True
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 401
    assert (await c.post("/api/v1/api-keys", json={"name": "k2", "scopes": ["read"], "service_account_id": acct})).status_code == 409
    r = await c.patch(f"/api/v1/service-accounts/{acct}", json={"disabled": False, "name": "grafana-2"})
    assert r.json()["name"] == "grafana-2"
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 200
    # delete -> keys revoked
    assert (await c.delete(f"/api/v1/service-accounts/{acct}")).status_code == 204
    assert (await c.get("/api/v1/libraries", headers=hdr)).status_code == 401
    assert (await c.get("/api/v1/api-keys")).json()["keys"] == []
