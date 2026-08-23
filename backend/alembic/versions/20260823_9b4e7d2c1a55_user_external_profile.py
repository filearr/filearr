"""users.external_profile — identity-provider details for the admin Users view

Revision ID: 9b4e7d2c1a55
Revises: f762ced396e3
Create Date: 2026-08-23
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "9b4e7d2c1a55"
down_revision = "f762ced396e3"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "users",
        sa.Column("external_profile", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("users", "external_profile")
