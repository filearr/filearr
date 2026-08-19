"""Roadmap §6 polish (2026-08-19): alert rule inhibition (inhibited_by + inhibit_window_s).

Revision ID: 5d1e9c0db889
Revises: e0f1a2b3c4d5
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5d1e9c0db889"
down_revision = "e0f1a2b3c4d5"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "alert_rules",
        sa.Column("inhibited_by", postgresql.ARRAY(postgresql.UUID(as_uuid=True)), nullable=True),
    )
    op.add_column(
        "alert_rules",
        sa.Column("inhibit_window_s", sa.Integer(), server_default=sa.text("900"), nullable=False),
    )


def downgrade() -> None:
    op.drop_column("alert_rules", "inhibit_window_s")
    op.drop_column("alert_rules", "inhibited_by")
