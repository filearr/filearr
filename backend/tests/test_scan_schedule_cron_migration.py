"""2026-11-02 — revision ``3c8a6f1d9e24`` folds the legacy config-group settings
key ``scan_schedule_cron`` into the policy key ``scan_cron``.

Two halves: the pure planner (imported from the migration module by path, since
migrations deliberately copy their logic rather than import app code), and the
real upgrade against a throwaway database seeded at the previous revision — the
same pattern as ``test_config_group_migration_p13``.
"""

from __future__ import annotations

import importlib.util
import json
import uuid
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import create_engine, text

from alembic import command
from filearr.config import get_settings

BACKEND_DIR = Path(__file__).resolve().parent.parent
MIG_PATH = (
    BACKEND_DIR / "alembic" / "versions" / "20261102_3c8a6f1d9e24_migrate_scan_schedule_cron.py"
)
PREV = "9b4e7d2c1a55"
REV = "3c8a6f1d9e24"


def _load_migration():
    spec = importlib.util.spec_from_file_location("mig_3c8a6f1d9e24", MIG_PATH)
    assert spec and spec.loader
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


mig = _load_migration()


def _g(gid, *, settings=None, policy=None, system=False):
    return {
        "id": gid,
        "name": gid,
        "is_system": system,
        "settings": settings or {},
        "policy": policy or {},
    }


# --------------------------------------------------------------------------- #
# Pure planner                                                                 #
# --------------------------------------------------------------------------- #
def test_plan_moves_a_live_legacy_value():
    groups = [
        _g("global", system=True),
        _g("nas", settings={"scan_schedule_cron": "0 3 * * *", "log_level": "info"}),
    ]
    out = mig.plan(groups, {"a1": {"nas"}, "a2": set()})
    assert out == {"nas": ({"log_level": "info"}, {"scan_cron": "0 3 * * *"})}


def test_plan_keeps_an_existing_policy_scan_cron_in_the_same_group():
    groups = [
        _g(
            "global",
            system=True,
            settings={"scan_schedule_cron": "0 3 * * *"},
            policy={"scan_cron": "0 5 * * *"},
        )
    ]
    out = mig.plan(groups, {"a1": set()})
    assert out == {"global": ({}, {"scan_cron": "0 5 * * *"})}


def test_plan_moves_when_own_policy_scan_cron_is_null():
    # A null policy scan_cron fell through to the legacy key on the agent.
    groups = [
        _g(
            "global",
            system=True,
            settings={"scan_schedule_cron": "0 3 * * *"},
            policy={"scan_cron": None},
        )
    ]
    out = mig.plan(groups, {"a1": set()})
    assert out == {"global": ({}, {"scan_cron": "0 3 * * *"})}


def test_plan_drops_a_legacy_value_shadowed_by_a_lower_priority_policy():
    # Global's policy scan_cron outranks a higher-priority group's LEGACY key
    # today; moving it would newly win the per-key merge, so it is dropped.
    groups = [
        _g("global", system=True, policy={"scan_cron": "0 1 * * *"}),
        _g("nas", settings={"scan_schedule_cron": "0 3 * * *"}),
    ]
    out = mig.plan(groups, {"a1": {"nas"}})
    assert out == {"nas": ({}, {})}


def test_plan_moves_for_a_group_without_members():
    groups = [_g("global", system=True), _g("spare", settings={"scan_schedule_cron": "0 3 * * *"})]
    out = mig.plan(groups, {"a1": set()})
    assert out["spare"] == ({}, {"scan_cron": "0 3 * * *"})


def test_plan_refuses_when_a_group_is_live_for_some_members_only():
    groups = [
        _g("global", system=True),
        _g("p", policy={"scan_cron": "0 1 * * *"}),
        _g("z", settings={"scan_schedule_cron": "0 3 * * *"}),
    ]
    # a1 sees z live; a2 has p's policy shadowing it — no per-group document
    # can express both, so the migration must refuse rather than guess.
    with pytest.raises(RuntimeError, match="a2"):
        mig.plan(groups, {"a1": {"z"}, "a2": {"p", "z"}})


def test_plan_later_legacy_group_still_wins():
    groups = [
        _g("global", system=True, settings={"scan_schedule_cron": "0 1 * * *"}),
        _g("nas", settings={"scan_schedule_cron": "0 3 * * *"}),
    ]
    out = mig.plan(groups, {"a1": {"nas"}, "a2": set()})
    assert out["global"] == ({}, {"scan_cron": "0 1 * * *"})
    assert out["nas"] == ({}, {"scan_cron": "0 3 * * *"})


# --------------------------------------------------------------------------- #
# Real upgrade                                                                 #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    if uri.startswith("postgresql://"):
        return uri.replace("postgresql://", "postgresql+psycopg://", 1)
    return uri


def _cfg() -> Config:
    return Config(str(BACKEND_DIR / "alembic.ini"))


@pytest.fixture
def prev_db(pg_uri, _pg_provider, monkeypatch):
    uri = _pg_provider.new_database("filearr_cronmig")
    monkeypatch.setenv("FILEARR_DATABASE_URL", uri)
    get_settings.cache_clear()
    command.upgrade(_cfg(), PREV)
    yield uri
    get_settings.cache_clear()


def _add_group(conn, name, *, priority, settings, policy, system=False):
    gid = conn.execute(
        text(
            "INSERT INTO agent_config_groups (name, priority, is_system, settings, "
            "  policy, current_version) "
            "VALUES (:n, :p, :sys, CAST(:s AS jsonb), CAST(:po AS jsonb), 1) RETURNING id"
        ),
        {
            "n": name,
            "p": priority,
            "sys": system,
            "s": json.dumps(settings),
            "po": json.dumps(policy),
        },
    ).scalar()
    conn.execute(
        text(
            "INSERT INTO agent_config_group_versions (group_id, version, settings, policy) "
            "VALUES (:g, 1, CAST(:s AS jsonb), CAST(:po AS jsonb))"
        ),
        {"g": gid, "s": json.dumps(settings), "po": json.dumps(policy)},
    )
    return gid


def test_upgrade_moves_the_key_and_publishes_a_new_version(prev_db):
    engine = create_engine(_psycopg3(prev_db))
    try:
        with engine.begin() as conn:
            # Global already seeded by the P13 migration on an empty DB.
            conn.execute(text("DELETE FROM agent_config_groups"))
            _add_group(
                conn,
                "Global",
                priority=0,
                system=True,
                settings={"log_level": "info"},
                policy={"watch_mode": True},
            )
            nas = _add_group(
                conn,
                "nas",
                priority=100,
                settings={"scan_schedule_cron": "0 3 * * *", "log_level": "debug"},
                policy={"poll_interval_seconds": 120},
            )
            agent = conn.execute(
                text(
                    "INSERT INTO agents (name, hostname, platform) "
                    "VALUES ('n1', 'n1', 'linux') RETURNING id"
                )
            ).scalar()
            conn.execute(
                text("INSERT INTO agent_config_group_members (agent_id, group_id) VALUES (:a, :g)"),
                {"a": agent, "g": nas},
            )
            seq_before = conn.execute(
                text("SELECT max(seq) FROM agent_config_group_versions")
            ).scalar()

        command.upgrade(_cfg(), REV)

        with engine.connect() as conn:
            g = conn.execute(
                text(
                    "SELECT settings, policy, current_version FROM agent_config_groups "
                    "WHERE id = :g"
                ),
                {"g": nas},
            ).one()
            assert g.settings == {"log_level": "debug"}
            assert g.policy == {"poll_interval_seconds": 120, "scan_cron": "0 3 * * *"}
            assert g.current_version == 2
            v2 = conn.execute(
                text(
                    "SELECT seq, settings, policy, actor FROM agent_config_group_versions "
                    "WHERE group_id = :g AND version = 2"
                ),
                {"g": nas},
            ).one()
            assert v2.settings == g.settings and v2.policy == g.policy
            assert v2.actor == "migration"
            # A new global generation -> every member's ETag changes.
            assert v2.seq > seq_before
            # History is immutable: v1 still holds the original document.
            v1 = conn.execute(
                text(
                    "SELECT settings FROM agent_config_group_versions "
                    "WHERE group_id = :g AND version = 1"
                ),
                {"g": nas},
            ).scalar()
            assert v1["scan_schedule_cron"] == "0 3 * * *"
            # Untouched group: no new version.
            assert (
                conn.execute(
                    text("SELECT current_version FROM agent_config_groups WHERE name = 'Global'")
                ).scalar()
                == 1
            )
    finally:
        engine.dispose()


def test_upgrade_refuses_while_a_rollout_is_live(prev_db):
    engine = create_engine(_psycopg3(prev_db))
    try:
        with engine.begin() as conn:
            gid = _add_group(
                conn,
                "nas",
                priority=100,
                settings={"scan_schedule_cron": "0 3 * * *"},
                policy={},
            )
            conn.execute(
                text(
                    "INSERT INTO agent_config_rollouts "
                    "  (id, group_id, target_version, tiers, status) "
                    "VALUES (:i, :g, 1, '[]'::jsonb, 'running')"
                ),
                {"i": uuid.uuid4(), "g": gid},
            )
        with pytest.raises(RuntimeError, match="rollout"):
            command.upgrade(_cfg(), REV)
    finally:
        engine.dispose()
