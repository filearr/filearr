"""agents.capabilities_at: when the agent's capability advertisement arrived.

``agents.capabilities`` has been stored verbatim since W6-D3 with no arrival
stamp, which was fine while it only gated which inventory collectors the console
offered. The per-agent About view (2026-08-11) reads the same column for a build
report an operator makes decisions from — host-tool versions, resolved paths, Go
module list — and every one of those facts is only meaningful with an "as of"
beside it. An agent that has been offline for a week still has capabilities in
this column, and without a timestamp the console would present a week-old tool
matrix as current.

Its own column rather than reusing ``health_at``: capabilities and health ride
the same poll but are stored under INDEPENDENT size caps, so an oversize
capabilities body is dropped while health is kept. A shared timestamp would then
date the capability report to a poll that did not update it — the one shape of
staleness bug that looks exactly like fresh data.

Nullable with no backfill. Existing rows genuinely do not know when their stored
advertisement arrived, and inventing ``now()`` for them would be a fabricated
observation; NULL renders as "not reported" and the next poll (≤60s) fills it in
honestly.

Revision ID: b2e6d048f317
Revises: a7f3c1e9d452
Create Date: 2026-08-11
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "b2e6d048f317"
down_revision = "a7f3c1e9d452"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "agents", sa.Column("capabilities_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("agents", "capabilities_at")
