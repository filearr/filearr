"""agents.health / health_at / last_auth_mode: self-reported agent health +
central-observed transport.

``health`` is the compact snapshot the agent attaches to its command poll
(uptime, outbox backlog, index size, scan state — stored VERBATIM,
size-capped, like ``capabilities``); ``health_at`` stamps when it arrived.
``last_auth_mode`` ('bearer' | 'mtls') is CENTRAL's observation of which
auth path the agent's last authenticated request used — the ground truth for
"is this agent actually on mTLS", which the agent itself cannot assert.

Revision ID: e5c7a9d1f483
Revises: d4b8e1f6c290
Create Date: 2026-08-08
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "e5c7a9d1f483"
down_revision = "d4b8e1f6c290"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("agents", sa.Column("health", JSONB(), nullable=True))
    op.add_column(
        "agents", sa.Column("health_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column("agents", sa.Column("last_auth_mode", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("agents", "last_auth_mode")
    op.drop_column("agents", "health_at")
    op.drop_column("agents", "health")
