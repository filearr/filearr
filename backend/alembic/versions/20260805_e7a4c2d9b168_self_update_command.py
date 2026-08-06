"""self_update command kind: widen the kind CHECK, make item_id nullable.

A fleet-management command targets the AGENT, not an item — item_id becomes
nullable (the API layer still requires it for the item-verify/retrieve kinds).

Revision ID: e7a4c2d9b168
Revises: b3e9a1c7d520
Create Date: 2026-08-05
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "e7a4c2d9b168"
down_revision = "b3e9a1c7d520"
branch_labels = None
depends_on = None

_KINDS_NEW = "kind IN ('stat_check','rehash_check','stage_upload','inventory','self_update')"
_KINDS_OLD = "kind IN ('stat_check','rehash_check','stage_upload','inventory')"


def upgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KINDS_NEW)
    op.alter_column(
        "agent_commands", "item_id", existing_type=sa.dialects.postgresql.UUID(), nullable=True
    )


def downgrade() -> None:
    op.execute("DELETE FROM agent_commands WHERE kind = 'self_update'")
    op.alter_column(
        "agent_commands", "item_id", existing_type=sa.dialects.postgresql.UUID(), nullable=False
    )
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KINDS_OLD)
