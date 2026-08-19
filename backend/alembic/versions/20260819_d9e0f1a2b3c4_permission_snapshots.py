"""permission_snapshots -- W7-T6 (2026-08-19).

Revision ID: d9e0f1a2b3c4
Revises: c8d9e0f1a2b3
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "d9e0f1a2b3c4"
down_revision = "c8d9e0f1a2b3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "permission_snapshots",
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "agent_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "item_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "command_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("agent_commands.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("is_dir", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column(
            "collected_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("owner", postgresql.JSONB(), nullable=True),
        sa.Column("group", postgresql.JSONB(), nullable=True),
        sa.Column(
            "aces", postgresql.JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")
        ),
        sa.Column("posture", postgresql.JSONB(), nullable=True),
        sa.Column("fidelity", sa.Text(), nullable=False),
        sa.Column(
            "principals",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
        sa.Column("digest", sa.Text(), nullable=False),
    )
    op.create_index(
        "ix_permission_snapshots_agent_path_time",
        "permission_snapshots",
        ["agent_id", "path", sa.text("collected_at DESC")],
    )
    op.create_index(
        "ix_permission_snapshots_principals",
        "permission_snapshots",
        ["principals"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_permission_snapshots_principals", table_name="permission_snapshots")
    op.drop_index("ix_permission_snapshots_agent_path_time", table_name="permission_snapshots")
    op.drop_table("permission_snapshots")
