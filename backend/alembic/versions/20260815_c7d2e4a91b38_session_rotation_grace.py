"""sessions: honour the previous token briefly after rotation

Session tokens rotate every ``session_rotation_minutes``; until now the old
hash died the instant the row was re-keyed. The SPA sends requests in
parallel (a POST followed by a fan-out refresh, polls, SSE reconnects), so the
first request across the boundary rotated the row and every sibling already
in flight -- still carrying the old cookie -- came back 401 "Missing bearer
token". Observed live 2026-08-15 adding a second library.

The fix keeps the previous hash on the row for ``session_rotation_grace_seconds``
(default 60). A request matched via the previous hash resolves the session but
does not rotate again.

Revision ID: c7d2e4a91b38
Revises: f3a9d5c81b62
Create Date: 2026-08-15
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c7d2e4a91b38"
down_revision = "f3a9d5c81b62"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("sessions", sa.Column("prev_session_hash", sa.Text(), nullable=True))
    op.add_column(
        "sessions", sa.Column("prev_valid_until", sa.DateTime(timezone=True), nullable=True)
    )
    # The grace lookup is by previous hash; keep it as cheap as the primary one.
    op.create_index("ix_sessions_prev_hash", "sessions", ["prev_session_hash"])


def downgrade() -> None:
    op.drop_index("ix_sessions_prev_hash", table_name="sessions")
    op.drop_column("sessions", "prev_valid_until")
    op.drop_column("sessions", "prev_session_hash")
