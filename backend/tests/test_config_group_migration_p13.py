"""P13 — the config-group unification MIGRATION, run against a real pre-P13
database with real data in it.

This is the one test that exercises ``a7f3c1e9d452``'s data half. It seeds the
OLD shape at the previous revision (``c6b1f24d70ae``) — agents with
``rollout_group`` values, a config group assignment, and ``policy_versions`` rows
in all three scopes — then upgrades one step and asserts the composition the
design specifies: a seeded Global carrying the global document, a group per
``group:`` scope, a ``host-<hostname>`` group per ``agent:`` scope, membership
derived from both old grouping columns, and a version-1 snapshot for everything.

It runs on its own throwaway database rather than the session-shared one, because
it must control the schema version — every other integration module expects to
find ``head``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text

from alembic import command
from filearr.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent

PREV = "c6b1f24d70ae"
P13 = "a7f3c1e9d452"


def _psycopg3(uri: str) -> str:
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return uri


@pytest.fixture
def old_shape_db(pg_uri, _pg_provider, monkeypatch):
    """A FRESH database migrated to the revision BEFORE P13, pointed at by the
    alembic env (which reads ``FILEARR_DATABASE_URL`` through ``get_settings``).

    Fresh per test rather than per module: each case here asserts an absolute
    composition ("exactly one group", "these two memberships"), so they must not
    inherit one another's seeded rows.

    ``pg_uri`` is requested purely for ordering: it is the fixture that stamps
    the session-wide ``FILEARR_DATABASE_URL``, so depending on it guarantees
    monkeypatch's undo restores THAT value rather than whatever was in the
    environment before the session started."""
    uri = _pg_provider.new_database("filearr_p13mig")
    monkeypatch.setenv("FILEARR_DATABASE_URL", uri)
    get_settings.cache_clear()
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), PREV)
    yield uri
    get_settings.cache_clear()


def _cfg() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


def _columns(engine, table) -> set[str]:
    return {c["name"] for c in inspect(engine).get_columns(table)}


def _tables(engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def test_migration_composes_groups_from_the_old_shape(old_shape_db):
    engine = create_engine(_psycopg3(old_shape_db))
    try:
        # --- seed the OLD world -------------------------------------------- #
        with engine.begin() as conn:
            grp_id = conn.execute(
                text(
                    "INSERT INTO agent_config_groups (name, settings) "
                    "VALUES ('filers', '{\"log_level\": \"debug\"}'::jsonb) RETURNING id"
                )
            ).scalar()
            # An agent in rollout_group 'filers' (which also has a policy scope),
            # assigned to the SAME config group by the orthogonal column.
            conn.execute(
                text(
                    "INSERT INTO agents (name, hostname, platform, rollout_group, "
                    "  config_group_id, policy_version_applied) "
                    "VALUES ('nas', 'nas', 'linux', 'filers', :g, 7) RETURNING id"
                ),
                {"g": grp_id},
            )
            # An agent in a rollout group nothing else knows about, with its own
            # agent-scoped policy document.
            a2 = conn.execute(
                text(
                    "INSERT INTO agents (name, hostname, platform, rollout_group) "
                    "VALUES ('desk', 'desk-01', 'windows', 'ghosts') RETURNING id"
                )
            ).scalar()
            for scope_type, scope_id, version, policy in (
                ("global", None, 1, '{"watch_mode": false}'),
                ("global", None, 2, '{"watch_mode": true, "poll_interval_seconds": 300}'),
                ("group", "filers", 1, '{"extract_enabled": true}'),
                ("agent", str(a2), 1, '{"log_level_probe": "agent-scope"}'),
            ):
                conn.execute(
                    text(
                        "INSERT INTO policy_versions "
                        "  (scope_type, scope_id, version, policy) "
                        "VALUES (:st, :si, :v, CAST(:p AS jsonb))"
                    ),
                    {"st": scope_type, "si": scope_id, "v": version, "p": policy},
                )
            conn.execute(
                text(
                    "INSERT INTO enrollment_tokens (token_hash, rollout_group, expires_at) "
                    "VALUES ('deadbeef', 'filers', now() + interval '1 hour')"
                )
            )
            conn.execute(
                text(
                    "INSERT INTO agent_releases (version, stage, manifest, promoted_at) "
                    "VALUES ('1.4.0', 'canary', '{}'::jsonb, NULL)"
                )
            )

        # --- migrate -------------------------------------------------------- #
        command.upgrade(_cfg(), P13)

        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar() == P13

            groups = {
                r.name: r
                for r in conn.execute(
                    text(
                        "SELECT id, name, priority, is_system, policy, settings, "
                        "       current_version FROM agent_config_groups"
                    )
                ).all()
            }

            # 1. Global is seeded, system, priority 0, carrying the MAX-version
            #    global document (v2, not v1).
            g = groups["Global"]
            assert g.is_system is True and g.priority == 0
            assert g.policy == {"watch_mode": True, "poll_interval_seconds": 300}
            assert g.current_version == 1  # counters RESTART at 1

            # 2. The group: scope landed on the EXISTING config group of that
            #    name, keeping its settings and gaining the policy half.
            filers = groups["filers"]
            assert filers.is_system is False
            assert filers.settings == {"log_level": "debug"}
            assert filers.policy == {"extract_enabled": True}
            assert filers.priority == 100

            # 3. The agent: scope became a host-<hostname> group above the rest.
            host = groups["host-desk-01"]
            assert host.policy == {"log_level_probe": "agent-scope"}
            assert host.priority >= 1000

            # 4. Membership: a1 joined 'filers' (via BOTH old columns, deduped to
            #    one row); a2 joined only its host group — 'ghosts' created
            #    nothing, because a bare rollout group with no policy document
            #    and no config group of that name configures nothing.
            members = conn.execute(
                text(
                    "SELECT a.hostname, g.name FROM agent_config_group_members m "
                    "JOIN agents a ON a.id = m.agent_id "
                    "JOIN agent_config_groups g ON g.id = m.group_id"
                )
            ).all()
            assert sorted((m.hostname, m.name) for m in members) == [
                ("desk-01", "host-desk-01"),
                ("nas", "filers"),
            ]
            assert "ghosts" not in groups

            # 5. Every group has a version-1 snapshot matching its documents.
            snaps = conn.execute(
                text(
                    "SELECT g.name, v.version, v.settings, v.policy, v.actor "
                    "FROM agent_config_group_versions v "
                    "JOIN agent_config_groups g ON g.id = v.group_id"
                )
            ).all()
            assert {s.name for s in snaps} == set(groups)
            assert {s.version for s in snaps} == {1}
            assert {s.actor for s in snaps} == {"migration"}
            by_name = {s.name: s for s in snaps}
            assert by_name["filers"].policy == {"extract_enabled": True}
            assert by_name["filers"].settings == {"log_level": "debug"}

            # 6. The applied watermark is renamed, widened and NULLed (the old
            #    number was a policy version, a different scale entirely).
            agents_cols = _columns(engine, "agents")
            assert "config_generation_applied" in agents_cols
            assert "policy_version_applied" not in agents_cols
            assert "rollout_group" not in agents_cols
            assert "config_group_id" not in agents_cols
            assert conn.execute(
                text("SELECT count(*) FROM agents WHERE config_generation_applied IS NOT NULL")
            ).scalar() == 0

            # 7. Token grouping moved to the JSONB name list.
            tok_cols = _columns(engine, "enrollment_tokens")
            assert "config_group_names" in tok_cols and "rollout_group" not in tok_cols

            # 8. Release staging is gone and the stored release survived.
            rel_cols = _columns(engine, "agent_releases")
            assert not ({"stage", "promoted_at"} & rel_cols)
            assert conn.execute(
                text("SELECT count(*) FROM agent_releases WHERE version = '1.4.0'")
            ).scalar() == 1

            # 9. The scope ladder is gone.
            assert "policy_versions" not in _tables(engine)
            assert {
                "agent_config_group_members",
                "agent_config_group_versions",
                "agent_config_rollouts",
            } <= _tables(engine)
    finally:
        engine.dispose()


def test_migration_is_reversible_structurally(old_shape_db):
    """The downgrade is a STRUCTURAL revert (documented data loss): the old
    columns and ``policy_versions`` come back empty, and a re-upgrade still
    works — which is what the DB-backed suite relies on every run."""
    engine = create_engine(_psycopg3(old_shape_db))
    try:
        command.upgrade(_cfg(), P13)
        assert "policy_versions" not in _tables(engine)
        command.downgrade(_cfg(), PREV)
        assert "policy_versions" in _tables(engine)
        cols = _columns(engine, "agents")
        assert {"rollout_group", "config_group_id", "policy_version_applied"} <= cols
        assert "config_generation_applied" not in cols
        assert {"stage", "promoted_at"} <= _columns(engine, "agent_releases")
        assert "agent_config_group_members" not in _tables(engine)
        command.upgrade(_cfg(), P13)
        assert "agent_config_rollouts" in _tables(engine)
        # Re-upgrading ADOPTS the Global row left behind by the previous upgrade
        # rather than colliding with its UNIQUE name.
        with engine.connect() as conn:
            assert conn.execute(
                text("SELECT count(*) FROM agent_config_groups WHERE is_system")
            ).scalar() == 1
    finally:
        engine.dispose()


def test_migration_on_an_empty_database_seeds_only_global(old_shape_db):
    """The path every DB-backed test module takes: no agents, no policies. It
    must be fast and produce exactly one group."""
    engine = create_engine(_psycopg3(old_shape_db))
    try:
        command.upgrade(_cfg(), P13)
        with engine.connect() as conn:
            rows = conn.execute(
                text("SELECT name, is_system, priority FROM agent_config_groups")
            ).all()
            assert [(r.name, r.is_system, r.priority) for r in rows] == [
                ("Global", True, 0)
            ]
            assert conn.execute(
                text("SELECT count(*) FROM agent_config_group_versions")
            ).scalar() == 1
    finally:
        engine.dispose()
