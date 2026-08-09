"""Global maintenance mode + agent suspend/maintenance command kinds.

``maintenance_mode`` is the single-row (id=1) operator switch that suspends
regular work generation (scan scheduling, maintenance tick, report exports)
and advertises back-off to agents (poll header + replication 503). Seeded
inactive so the flag exists from the first read.

The ``agent_commands`` kind CHECK gains two agent-scoped kinds: ``suspend``
(pause/resume the agent's own scan + replication processing) and
``agent_maintenance`` (local index VACUUM, outbox prune, temp cleanup).

Revision ID: a9c4e7f2b581
Revises: e5c7a9d1f483
Create Date: 2026-08-09
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "a9c4e7f2b581"
down_revision = "e5c7a9d1f483"
branch_labels = None
depends_on = None

_KIND_OLD = "kind IN ('stat_check','rehash_check','stage_upload','inventory','self_update')"
_KIND_NEW = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance')"
)


def upgrade() -> None:
    op.create_table(
        "maintenance_mode",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=False),
        sa.Column("active", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.execute("INSERT INTO maintenance_mode (id, active) VALUES (1, false)")

    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_NEW)


def downgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_OLD)
    op.drop_table("maintenance_mode")
