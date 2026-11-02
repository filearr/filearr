"""libraries.description — optional free-text note shown in the console

Operator-facing only: not part of the provenance fingerprint and not projected
into the search index. Nullable, no default — existing rows read as "no
description".

Revision ID: 7e2c4b9a1d36
Revises: 5d1e8f0a2b47
Create Date: 2026-11-02
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "7e2c4b9a1d36"
down_revision = "5d1e8f0a2b47"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("libraries", sa.Column("description", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("libraries", "description")
