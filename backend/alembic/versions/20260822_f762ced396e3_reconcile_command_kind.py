"""`reconcile` agent-command kind.

The console-triggered full-manifest consistency sweep (2026-08-22). Extends
``agent_commands_kind_valid`` the same way reextract (c6b1f24d70ae) and
rehash_sweep (d4f1a7c93e60) did.

Revision ID: f762ced396e3
Revises: 72d54be1b149
"""

from __future__ import annotations

from alembic import op

revision = "f762ced396e3"
down_revision = "72d54be1b149"
branch_labels = None
depends_on = None

_KIND_OLD = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance','reextract','rehash_sweep')"
)
_KIND_NEW = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance','reextract','rehash_sweep',"
    "'reconcile')"
)


def upgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_NEW)


def downgrade() -> None:
    # Drop the rows holding the kind we are about to un-declare FIRST (the
    # reextract/rehash_sweep revisions' pattern).
    op.execute("DELETE FROM agent_commands WHERE kind = 'reconcile'")
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_OLD)
