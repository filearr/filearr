"""maintenance_schedules: runtime overrides for maintenance-task schedules

Revision ID: b3e9a1c7d520
Revises: f2c8b5a9d417
Create Date: 2026-07-29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b3e9a1c7d520"
down_revision = "f2c8b5a9d417"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "maintenance_schedules",
        sa.Column("task_key", sa.Text(), primary_key=True),
        sa.Column("cron", sa.Text(), nullable=True),
        sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
        sa.Column("last_cron_fired_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("maintenance_schedules")
