"""W7-T8 (2026-08-20): principal_aliases — canonical identity across hosts.

Revision ID: 229726a6e7a9
Revises: 5d1e9c0db889
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "229726a6e7a9"
down_revision = "5d1e9c0db889"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "principal_aliases",
        sa.Column("alias", sa.Text(), primary_key=True),
        sa.Column("canonical", sa.Text(), nullable=False),
        sa.Column("display", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    op.create_index("ix_principal_aliases_canonical", "principal_aliases", ["canonical"])


def downgrade() -> None:
    op.drop_index("ix_principal_aliases_canonical", table_name="principal_aliases")
    op.drop_table("principal_aliases")
