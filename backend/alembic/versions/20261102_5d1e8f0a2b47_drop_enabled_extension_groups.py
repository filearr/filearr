"""drop libraries.enabled_extension_groups (dead since W8-B)

The P2-T3 extension-group refinement was superseded for scan gating by the
taxonomy ``enabled_groups`` (W8-B) and nothing read the column except the
provenance fingerprint. Dropped end to end in P7 (B6); the downgrade restores
the column empty (the stored selections had no effect, so nothing is lost).

Revision ID: 5d1e8f0a2b47
Revises: 3c8a6f1d9e24
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "5d1e8f0a2b47"
down_revision = "3c8a6f1d9e24"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("libraries", "enabled_extension_groups")


def downgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column(
            "enabled_extension_groups",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'"),
        ),
    )
