"""item_frecency: central frecency (frequency+recency) personal ranking store.

Roadmap §5 P3. One row per (owner, item): ``rank`` accumulates uses, ``last_used``
drives the recency weight — the same zoxide-style shape the agent's local
``history.db`` already uses (agent/internal/history), keyed per principal here
(``owner`` = principal UUID string, or 'anonymous' when auth is off). Rows
cascade away with their item.

Revision ID: a3f1c7e2d940
Revises: e7a4c2d9b168
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "a3f1c7e2d940"
down_revision = "e7a4c2d9b168"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "item_frecency",
        sa.Column("owner", sa.Text(), nullable=False),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("rank", sa.Float(), nullable=False, server_default=sa.text("0")),
        sa.Column("last_used", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("owner", "item_id", name="pk_item_frecency"),
    )
    # The re-rank lookup is always "this owner's rows for these item ids" — the
    # PK covers it. A plain owner index serves the maintenance sweeps.
    op.create_index("ix_item_frecency_owner", "item_frecency", ["owner"])


def downgrade() -> None:
    op.drop_index("ix_item_frecency_owner", table_name="item_frecency")
    op.drop_table("item_frecency")
