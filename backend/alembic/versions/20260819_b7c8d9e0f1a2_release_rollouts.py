"""agent_release_rollouts -- phased binary rollouts on the config tier engine
(roadmap §23, 2026-08-19).

Revision ID: b7c8d9e0f1a2
Revises: a1b2c3d4e5f6
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "b7c8d9e0f1a2"
down_revision = "a1b2c3d4e5f6"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "agent_release_rollouts",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("release_version", sa.Text(), nullable=False),
        sa.Column("tiers", postgresql.JSONB(), nullable=False),
        sa.Column("status", sa.Text(), nullable=False, server_default=sa.text("'scheduled'")),
        sa.Column("current_tier", sa.Integer(), nullable=False, server_default=sa.text("-1")),
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
            name="agent_release_rollouts_status_valid",
        ),
    )
    op.create_index(
        "uq_agent_release_rollouts_live",
        "agent_release_rollouts",
        ["release_version"],
        unique=True,
        postgresql_where=sa.text("status IN ('scheduled','running')"),
    )


def downgrade() -> None:
    op.drop_index("uq_agent_release_rollouts_live", table_name="agent_release_rollouts")
    op.drop_table("agent_release_rollouts")
