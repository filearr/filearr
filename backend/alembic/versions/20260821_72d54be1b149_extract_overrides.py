"""Per-library extraction-limit overrides.

``libraries.extract_overrides``: a small JSONB map of allow-listed Settings
keys (config.EXTRACT_OVERRIDE_KEYS — extraction timeouts and size/
decompression ceilings) overlaid onto the global config for that library's
extract jobs. Nullable metadata-only ADD COLUMN; NULL = global settings.

Revision ID: 72d54be1b149
Revises: c7e2b91f4a63
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB

from alembic import op

revision = "72d54be1b149"
down_revision = "c7e2b91f4a63"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "libraries",
        sa.Column("extract_overrides", JSONB(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("libraries", "extract_overrides")
