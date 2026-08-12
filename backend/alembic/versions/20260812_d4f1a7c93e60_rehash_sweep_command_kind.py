"""rehash_sweep command kind: widen the agent_commands kind CHECK.

QH-T6, the agent-side half of the quick_hash migration. Until 2026-07-18 both
hashers read a fixed 64 KiB head and appended a tail only above 131072 bytes, so
a file in the 65537..131072 band had its middle and tail silently unhashed —
false duplicates, and a mis-keyed move-detection tier. QH-T1 fixed the hashers
and QH-T4's ``rehash_small_files`` sweep converged central's OWN rows
(``still_stale = 0``, verified 2026-08-11).

Agent-owned rows were excluded from that sweep and cannot be reached by it:
central does not host the files, and ``agentsync.apply_batch`` never writes
``policy_version`` for agent rows, so the ``cfg1 -> cfg2`` provenance predicate
that drives it cannot even IDENTIFY a stale agent hash. The agent is the only
writer for those rows, so ``rehash_sweep`` is the agent-scoped command that asks
it to re-read the band and correct them. 98,628 affected rows across seven
libraries on the live fleet when this shipped.

NOT ``rehash_check``, which has been in this CHECK since P10-T1 and is a
different thing entirely: item-scoped, one file, verify-only, writes nothing.

Purely a constraint widening: the kind is agent-scoped, so it reuses the nullable
``item_id`` the self_update revision (e7a4c2d9b168) introduced.

Revision ID: d4f1a7c93e60
Revises: b2e6d048f317
Create Date: 2026-08-12
"""

from __future__ import annotations

from alembic import op

revision = "d4f1a7c93e60"
down_revision = "b2e6d048f317"
branch_labels = None
depends_on = None

_KIND_OLD = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance','reextract')"
)
_KIND_NEW = (
    "kind IN ('stat_check','rehash_check','stage_upload','inventory',"
    "'self_update','suspend','agent_maintenance','reextract','rehash_sweep')"
)


def upgrade() -> None:
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_NEW)


def downgrade() -> None:
    # Drop the rows holding the kind we are about to un-declare FIRST, the way
    # the reextract (c6b1f24d70ae) and self_update (e7a4c2d9b168) revisions do.
    # Postgres validates a new CHECK against existing rows, so a downgrade with a
    # queued or completed 'rehash_sweep' row would otherwise fail outright.
    #
    # Deleting them is safe here in a way it would NOT be for a job whose only
    # record of progress is the command row: this sweep's progress lives in the
    # AGENT's rehash_state cursor, so a re-upgrade and a re-issued command
    # resumes exactly where it stopped. What is lost is the console's history of
    # the run, not the run.
    op.execute("DELETE FROM agent_commands WHERE kind = 'rehash_sweep'")
    op.drop_constraint("agent_commands_kind_valid", "agent_commands", type_="check")
    op.create_check_constraint("agent_commands_kind_valid", "agent_commands", _KIND_OLD)
