"""Shared helpers for the P13 configuration-group tests.

The integration suite runs against ONE session-shared Postgres, and the
permanent **Global** group is seeded by the migration rather than created per
test. That makes it exactly the kind of state a test can quietly leak: a test
that publishes a policy into Global changes what every later test's agents
resolve. :func:`reset_config_groups` puts it back — and deliberately does NOT
delete the Global row, which is undeletable by design and whose absence would
break every subsequent module rather than just the next test.
"""

from __future__ import annotations

from typing import Any

from sqlalchemy import text

#: Statements that return the config-group world to "freshly migrated": no
#: bespoke groups, no memberships, no rollouts, and a Global group whose two
#: documents are empty at version 1.
_RESET_SQL = (
    "DELETE FROM agent_config_rollouts",
    "DELETE FROM agent_config_group_members",
    "DELETE FROM agent_config_groups WHERE NOT is_system",
    "UPDATE agent_config_groups SET settings = '{}'::jsonb, "
    "  policy = '{}'::jsonb, current_version = 1 WHERE is_system",
    "DELETE FROM agent_config_group_versions WHERE version > 1",
    "UPDATE agent_config_group_versions "
    "  SET settings = '{}'::jsonb, policy = '{}'::jsonb",
)


async def reset_config_groups(conn) -> None:
    """Reset config-group state on an ``engine.begin()`` connection."""
    for stmt in _RESET_SQL:
        await conn.execute(text(stmt))


async def global_group_id(client) -> str:
    """The seeded Global group's id, found by ``is_system`` (never by name — the
    name is for humans; ``is_system`` is the contract)."""
    groups = (await client.get("/api/v1/agents/config-groups")).json()
    return next(g["id"] for g in groups if g["is_system"])


async def set_global(
    client,
    *,
    policy: dict[str, Any] | None = None,
    settings: dict[str, Any] | None = None,
) -> Any:
    """Publish a new Global version carrying ``policy`` / ``settings``."""
    gid = await global_group_id(client)
    body: dict[str, Any] = {}
    if policy is not None:
        body["policy"] = policy
    if settings is not None:
        body["settings"] = settings
    return await client.patch(f"/api/v1/agents/config-groups/{gid}", json=body)


async def make_group(
    client,
    name: str,
    *,
    priority: int = 100,
    settings: dict[str, Any] | None = None,
    policy: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Create a config group and return its row."""
    r = await client.post(
        "/api/v1/agents/config-groups",
        json={
            "name": name,
            "priority": priority,
            "settings": settings or {},
            "policy": policy or {},
        },
    )
    assert r.status_code == 201, r.text
    return r.json()


async def join(client, agent_id, group_ids: list[str]) -> Any:
    """Replace an agent's explicit group membership."""
    return await client.put(
        f"/api/v1/agents/{agent_id}/config-groups", json={"group_ids": group_ids}
    )


async def effective(client, agent_id) -> dict[str, Any]:
    """The admin effective-config view for one agent."""
    r = await client.get(f"/api/v1/agents/{agent_id}/effective-config")
    assert r.status_code == 200, r.text
    return r.json()
