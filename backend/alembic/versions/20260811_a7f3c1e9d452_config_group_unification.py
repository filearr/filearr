"""unify agent policy + config groups into priority-layered config groups (P13)

Revision ID: a7f3c1e9d452
Revises: c6b1f24d70ae
Create Date: 2026-08-11

The schema half of ``archive/docs/design-config-group-unification.md``. Filearr
carried THREE overlapping agent groupings — ``agents.rollout_group`` (which was
simultaneously the middle policy scope and the release-canary selector),
``agents.config_group_id`` (the W6-D2 remote-configuration group, which policy
resolution never consulted), and the ``policy_versions`` scope ladder. This
revision collapses them into ONE: a config group with a ``priority``, two
document sections, versioned snapshots and phased rollouts.

STRUCTURE

  ``agent_config_groups`` gains ``priority`` (ascending merge order; Global = 0),
  ``is_system`` (the permanent Global row), ``policy`` (the second document
  section) and ``current_version``.

  NEW ``agent_config_group_members`` — explicit many-to-many membership. Global
  is deliberately NOT represented here: its membership is implicit, so enrolling
  an agent writes no row and an agent with zero rows is still fully configured.

  NEW ``agent_config_group_versions`` — immutable snapshots. ``seq`` is a BIGINT
  identity and is the GLOBAL generation counter delivered to agents as the wire
  ``version``; ``version`` is the per-group human counter.

  NEW ``agent_config_rollouts`` — phased publication of one group version through
  ≤5 percent/delay tiers, one live rollout per group (partial unique index).

  ``agents`` — ``rollout_group`` and ``config_group_id`` are dropped;
  ``policy_version_applied`` is renamed to ``config_generation_applied`` and
  widened to BIGINT.

  ``enrollment_tokens`` — ``rollout_group`` is replaced by a
  ``config_group_names`` JSONB list.

  ``agent_releases`` — ``stage`` / ``promoted_at`` (the canary staging pair) are
  dropped; every release is fleet-visible on upload and ``auto_update`` is the
  brake.

  ``policy_versions`` is DROPPED. Its own docstring earmarked the table for reuse
  "so nobody invents a second policy-versioning scheme" — the config-group
  snapshots ARE that single scheme, so keeping the table would have created
  exactly the duplication it warned about.

DATA MIGRATION (runs BEFORE the drops, against a live fleet)

  1. Seed/adopt the ``Global`` group; its ``policy`` = the current ``global``
     scope document (or ``{}``). Built-in defaults are NOT materialised —
     resolution still falls back to code defaults below the merge, so an unset
     key stays honestly unset.
  2. Every distinct ``group:<name>`` scope becomes (or updates) a config group of
     that name; an already-existing group of the same name KEEPS its ``settings``
     and only gains the policy document.
  3. Every ``agent:<uuid>`` scope with a live agent becomes a ``host-<hostname>``
     group at priority 1000+ (so a per-machine override still outranks
     everything) with that one agent as its only member.
  4. Membership: an agent joins the group matching its ``rollout_group`` IF such
     a group exists after step 2 (a bare rollout group with no policy document
     and no config group of that name creates nothing — inventing empty groups
     for it would clutter the console with rows that configure nothing), and the
     group its ``config_group_id`` pointed at.
  5. Every group is snapshotted as version 1 (``actor='migration'``). Version
     counters RESTART at 1: the old whole-document history does not translate
     into layered per-key semantics, so carrying the numbers forward would
     attach real-looking version labels to documents that never existed in this
     scheme.
  6. ``config_generation_applied`` is NULLed — the old numbers are per-scope
     policy versions on a completely different scale, and leaving them would
     make every agent read as permanently behind.

DOWNGRADE is structural only (repo norm for a cutover): the dropped columns and
``policy_versions`` come back EMPTY, and the group/membership/version/rollout
data is destroyed. There is no way to reverse step 1-5 — one layered document
cannot be split back into the scope ladder that produced it.
"""

from __future__ import annotations

import json
from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "a7f3c1e9d452"
down_revision: str | None = "c6b1f24d70ae"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: The seeded permanent group. Name is part of the contract (the API refuses to
#: rename it and the resolver looks it up by ``is_system``, not by name — the
#: name is for humans).
GLOBAL_GROUP_NAME = "Global"


def upgrade() -> None:
    conn = op.get_bind()

    # --- 1. widen agent_config_groups ---------------------------------------
    op.add_column(
        "agent_config_groups",
        sa.Column(
            "priority", sa.Integer(), nullable=False, server_default=sa.text("100")
        ),
    )
    op.add_column(
        "agent_config_groups",
        sa.Column(
            "is_system", sa.Boolean(), nullable=False, server_default=sa.text("false")
        ),
    )
    op.add_column(
        "agent_config_groups",
        sa.Column(
            "policy",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.add_column(
        "agent_config_groups",
        sa.Column(
            "current_version",
            sa.Integer(),
            nullable=False,
            server_default=sa.text("1"),
        ),
    )
    op.create_index(
        "ix_agent_config_groups_priority", "agent_config_groups", ["priority"]
    )

    # --- 2. new tables -------------------------------------------------------
    op.create_table(
        "agent_config_group_members",
        sa.Column("agent_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["agent_id"],
            ["agents.id"],
            name="fk_agent_config_group_members_agent_id_agents",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["agent_config_groups.id"],
            name="fk_agent_config_group_members_group_id_agent_config_groups",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("agent_id", "group_id"),
    )
    op.create_index(
        "ix_agent_config_group_members_group",
        "agent_config_group_members",
        ["group_id"],
    )

    op.create_table(
        "agent_config_group_versions",
        # GENERATED BY DEFAULT (not ALWAYS): the generation is the wire version
        # and a future repair/import path may need to write an explicit seq.
        sa.Column("seq", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column(
            "settings", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column("note", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["agent_config_groups.id"],
            name="fk_agent_config_group_versions_group_id_agent_config_groups",
            ondelete="CASCADE",
        ),
        sa.UniqueConstraint(
            "group_id", "version", name="uq_agent_config_group_versions_group_version"
        ),
    )
    op.execute(
        "CREATE INDEX ix_agent_config_group_versions_group_version "
        "ON agent_config_group_versions (group_id, version DESC)"
    )

    op.create_table(
        "agent_config_rollouts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("group_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("target_version", sa.Integer(), nullable=False),
        sa.Column("tiers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default=sa.text("'scheduled'")
        ),
        sa.Column(
            "current_tier", sa.Integer(), nullable=False, server_default=sa.text("-1")
        ),
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("tier_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "status IN ('scheduled','running','completed','cancelled')",
            name="agent_config_rollouts_status_valid",
        ),
        sa.ForeignKeyConstraint(
            ["group_id"],
            ["agent_config_groups.id"],
            name="fk_agent_config_rollouts_group_id_agent_config_groups",
            ondelete="CASCADE",
        ),
    )
    # One LIVE rollout per group: two overlapping rollouts would each define a
    # different "active version" for the same agent.
    op.create_index(
        "uq_agent_config_rollouts_live",
        "agent_config_rollouts",
        ["group_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('scheduled','running')"),
    )

    # --- 3. enrollment_tokens.config_group_names ------------------------------
    op.add_column(
        "enrollment_tokens",
        sa.Column(
            "config_group_names",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
    )

    # --- 4. DATA MIGRATION (before any drop) ----------------------------------
    _migrate_data(conn)

    # --- 5. agents: rename the applied watermark, drop the old groupings ------
    op.alter_column(
        "agents",
        "policy_version_applied",
        new_column_name="config_generation_applied",
        existing_type=sa.Integer(),
        type_=sa.BigInteger(),
        existing_nullable=True,
    )
    # Old values are per-scope POLICY versions; the new column holds generations
    # (a global identity sequence). Keeping them would make every agent look
    # permanently behind until its next poll re-stamps the real number.
    op.execute("UPDATE agents SET config_generation_applied = NULL")

    op.drop_index("ix_agents_rollout_group", table_name="agents")
    op.drop_column("agents", "rollout_group")
    op.drop_index("ix_agents_config_group_id", table_name="agents")
    op.drop_constraint("fk_agents_config_group", "agents", type_="foreignkey")
    op.drop_column("agents", "config_group_id")

    op.drop_column("enrollment_tokens", "rollout_group")

    # --- 6. agent_releases: drop the canary staging pair ----------------------
    # No data step needed: dropping ``stage`` makes every stored release visible
    # to the whole fleet, which is exactly the intended post-canary semantics.
    op.drop_index("ix_agent_releases_stage_created", table_name="agent_releases")
    op.drop_constraint(
        "agent_releases_stage_valid", "agent_releases", type_="check"
    )
    op.drop_column("agent_releases", "stage")
    op.drop_column("agent_releases", "promoted_at")

    # --- 7. the scope ladder is gone ------------------------------------------
    op.drop_table("policy_versions")


def _migrate_data(conn) -> None:
    """Compose config groups from the old policy scopes + groupings (steps 1-5).

    Written as explicit statements rather than one clever CTE because a live
    fleet is the only place this ever runs with rows in it, and a migration that
    is hard to read is a migration nobody can audit before running it there."""
    # --- 1. Global -----------------------------------------------------------
    # Adopt an existing group of that name rather than colliding with the UNIQUE
    # constraint (an operator may well have made one called "Global" already).
    conn.execute(
        sa.text(
            "UPDATE agent_config_groups SET is_system = true, priority = 0 "
            "WHERE name = :name"
        ),
        {"name": GLOBAL_GROUP_NAME},
    )
    conn.execute(
        sa.text(
            "INSERT INTO agent_config_groups "
            "  (name, description, settings, policy, priority, is_system, "
            "   current_version) "
            "SELECT :name, :descr, '{}'::jsonb, '{}'::jsonb, 0, true, 1 "
            "WHERE NOT EXISTS (SELECT 1 FROM agent_config_groups WHERE name = :name)"
        ),
        {
            "name": GLOBAL_GROUP_NAME,
            "descr": (
                "Every agent is a member. Lowest priority, so any other group "
                "overrides the keys it sets."
            ),
        },
    )
    conn.execute(
        sa.text(
            "UPDATE agent_config_groups SET policy = COALESCE(("
            "  SELECT pv.policy FROM policy_versions pv "
            "  WHERE pv.scope_type = 'global' ORDER BY pv.version DESC LIMIT 1"
            "), '{}'::jsonb) "
            "WHERE is_system AND policy = '{}'::jsonb"
        )
    )

    # --- 2. group:<name> scopes ----------------------------------------------
    current_group_scopes = sa.text(
        "SELECT DISTINCT ON (scope_id) scope_id AS name, policy "
        "FROM policy_versions "
        "WHERE scope_type = 'group' AND scope_id IS NOT NULL "
        "ORDER BY scope_id, version DESC"
    )
    scopes = list(conn.execute(current_group_scopes).mappings())
    for step, row in enumerate(sorted(scopes, key=lambda r: r["name"])):
        conn.execute(
            sa.text(
                "INSERT INTO agent_config_groups "
                "  (name, description, settings, policy, priority, is_system, "
                "   current_version) "
                "SELECT :name, :descr, '{}'::jsonb, CAST(:policy AS jsonb), "
                "       :priority, false, 1 "
                "WHERE NOT EXISTS "
                "  (SELECT 1 FROM agent_config_groups WHERE name = :name)"
            ),
            {
                "name": row["name"],
                "descr": f"Migrated from policy scope group:{row['name']}",
                "policy": json.dumps(row["policy"] or {}),
                "priority": 100 + step,
            },
        )
        # An already-existing config group of that name keeps its settings and
        # merely gains the policy half (the two halves were authored separately
        # before this revision, so both are real operator intent).
        conn.execute(
            sa.text(
                "UPDATE agent_config_groups SET policy = CAST(:policy AS jsonb) "
                "WHERE name = :name AND NOT is_system AND policy = '{}'::jsonb"
            ),
            {"name": row["name"], "policy": json.dumps(row["policy"] or {})},
        )

    # --- 3. agent:<uuid> scopes -> per-host groups ---------------------------
    agent_scopes = list(
        conn.execute(
            sa.text(
                "SELECT DISTINCT ON (pv.scope_id) pv.scope_id AS agent_id, "
                "       pv.policy, a.hostname "
                "FROM policy_versions pv "
                "JOIN agents a ON a.id = CAST(pv.scope_id AS uuid) "
                "WHERE pv.scope_type = 'agent' AND pv.scope_id IS NOT NULL "
                "ORDER BY pv.scope_id, pv.version DESC"
            )
        ).mappings()
    )
    taken = {
        r[0]
        for r in conn.execute(sa.text("SELECT name FROM agent_config_groups")).all()
    }
    for step, row in enumerate(sorted(agent_scopes, key=lambda r: str(r["agent_id"]))):
        base = f"host-{row['hostname']}"[:128]
        name = base
        suffix = 2
        while name in taken:
            name = f"{base}-{suffix}"[:128]
            suffix += 1
        taken.add(name)
        gid = conn.execute(
            sa.text(
                "INSERT INTO agent_config_groups "
                "  (name, description, settings, policy, priority, is_system, "
                "   current_version) "
                "VALUES (:name, :descr, '{}'::jsonb, CAST(:policy AS jsonb), "
                "        :priority, false, 1) "
                "RETURNING id"
            ),
            {
                "name": name,
                "descr": f"Migrated from policy scope agent:{row['agent_id']}",
                "policy": json.dumps(row["policy"] or {}),
                # Above every migrated group scope, mirroring the old
                # agent > group > global precedence for this one machine.
                "priority": 1000 + step,
            },
        ).scalar_one()
        conn.execute(
            sa.text(
                "INSERT INTO agent_config_group_members (agent_id, group_id) "
                "VALUES (CAST(:agent_id AS uuid), :group_id) "
                "ON CONFLICT DO NOTHING"
            ),
            {"agent_id": str(row["agent_id"]), "group_id": gid},
        )

    # --- 4. membership from the two old grouping columns ---------------------
    conn.execute(
        sa.text(
            "INSERT INTO agent_config_group_members (agent_id, group_id) "
            "SELECT a.id, g.id FROM agents a "
            "JOIN agent_config_groups g ON g.name = a.rollout_group AND NOT g.is_system "
            "ON CONFLICT DO NOTHING"
        )
    )
    conn.execute(
        sa.text(
            "INSERT INTO agent_config_group_members (agent_id, group_id) "
            "SELECT a.id, a.config_group_id FROM agents a "
            "WHERE a.config_group_id IS NOT NULL "
            "ON CONFLICT DO NOTHING"
        )
    )

    # --- 5. snapshot everything as version 1 ---------------------------------
    conn.execute(
        sa.text(
            "INSERT INTO agent_config_group_versions "
            "  (group_id, version, settings, policy, actor, note) "
            "SELECT id, 1, settings, policy, 'migration', :note "
            "FROM agent_config_groups ORDER BY priority, name, id"
        ),
        {"note": "migrated from the pre-P13 policy scopes / config-group settings"},
    )
    conn.execute(sa.text("UPDATE agent_config_groups SET current_version = 1"))


def downgrade() -> None:
    """STRUCTURAL revert only — the layered documents are NOT split back into the
    scope ladder (they cannot be: a merged per-key document has no scope). The
    recreated ``policy_versions`` / ``rollout_group`` / ``config_group_id`` /
    ``stage`` are EMPTY-or-default, and every group/membership/version/rollout
    row is destroyed."""
    op.create_table(
        "policy_versions",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("scope_type", sa.Text(), nullable=False),
        sa.Column("scope_id", sa.Text(), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("policy", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("actor", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.CheckConstraint(
            "scope_type IN ('global','group','agent')",
            name="policy_versions_scope_type_valid",
        ),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "version",
            name="uq_policy_versions_scope_version",
            postgresql_nulls_not_distinct=True,
        ),
    )
    op.execute(
        "CREATE INDEX ix_policy_versions_scope_version "
        "ON policy_versions (scope_type, scope_id, version DESC)"
    )

    op.add_column(
        "agent_releases",
        sa.Column("promoted_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.add_column(
        "agent_releases",
        sa.Column(
            "stage", sa.Text(), nullable=False, server_default=sa.text("'canary'")
        ),
    )
    op.create_check_constraint(
        "agent_releases_stage_valid", "agent_releases", "stage IN ('canary','general')"
    )
    op.execute(
        "CREATE INDEX ix_agent_releases_stage_created "
        "ON agent_releases (stage, created_at DESC)"
    )

    op.add_column(
        "enrollment_tokens",
        sa.Column(
            "rollout_group",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
    )
    op.drop_column("enrollment_tokens", "config_group_names")

    op.add_column(
        "agents",
        sa.Column("config_group_id", postgresql.UUID(as_uuid=True), nullable=True),
    )
    op.create_foreign_key(
        "fk_agents_config_group",
        "agents",
        "agent_config_groups",
        ["config_group_id"],
        ["id"],
        ondelete="SET NULL",
    )
    op.create_index("ix_agents_config_group_id", "agents", ["config_group_id"])
    op.add_column(
        "agents",
        sa.Column(
            "rollout_group",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'default'"),
        ),
    )
    op.create_index("ix_agents_rollout_group", "agents", ["rollout_group"])
    op.alter_column(
        "agents",
        "config_generation_applied",
        new_column_name="policy_version_applied",
        existing_type=sa.BigInteger(),
        type_=sa.Integer(),
        existing_nullable=True,
    )
    op.execute("UPDATE agents SET policy_version_applied = NULL")

    op.drop_index("uq_agent_config_rollouts_live", table_name="agent_config_rollouts")
    op.drop_table("agent_config_rollouts")
    op.drop_index(
        "ix_agent_config_group_versions_group_version",
        table_name="agent_config_group_versions",
    )
    op.drop_table("agent_config_group_versions")
    op.drop_index(
        "ix_agent_config_group_members_group",
        table_name="agent_config_group_members",
    )
    op.drop_table("agent_config_group_members")
    op.drop_index("ix_agent_config_groups_priority", table_name="agent_config_groups")
    op.drop_column("agent_config_groups", "current_version")
    op.drop_column("agent_config_groups", "policy")
    op.drop_column("agent_config_groups", "is_system")
    op.drop_column("agent_config_groups", "priority")
