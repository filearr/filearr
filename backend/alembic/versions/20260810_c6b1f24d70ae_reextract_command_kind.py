"""reextract command kind: widen the agent_commands kind CHECK.

Extraction parity phase 3. Agent-side extraction only runs over the files a
scan reports as new or changed, so items catalogued before ``extract_enabled``
was turned on — or before the agent host gained ffprobe/exiftool/poppler/
tesseract — are never enriched. ``reextract`` is the agent-scoped command that
sweeps the agent's existing local index and re-emits those items through the
normal replication path.

Purely a constraint widening: the kind is agent-scoped, so it reuses the
nullable ``item_id`` the self_update revision (e7a4c2d9b168) introduced.

Revision ID: c6b1f24d70ae
Revises: a9c4e7f2b581
Create Date: 2026-08-10
"""

from __future__ import annotations

from alembic import op

revision = "c6b1f24d70ae"
down_revision = "a9c4e7f2b581"
branch_labels = None
depends_on = None

_KIND_OLD = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance')"
)
_KIND_NEW = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance','reextract')"
)


def upgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_NEW)


def downgrade() -> None:
    # Drop the rows holding the kind we are about to un-declare FIRST, the way
    # the self_update revision (e7a4c2d9b168) does. Postgres validates a new
    # CHECK against existing rows, so a downgrade with a queued/completed
    # 'reextract' row would otherwise fail outright — the maintenance-mode
    # revision (a9c4e7f2b581) omitted this step and is only survivable because
    # its two kinds are usually swept before anyone downgrades. Command rows are
    # transient by construction (TTL + the expiry sweep) and the sweep itself is
    # resumable from the agent's own cursor, so nothing durable is lost.
    op.execute("DELETE FROM agent_commands WHERE kind = 'reextract'")
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_OLD)
