"""LDAP-T1 multi-forest: directory_objects.source_directory.

Which configured directory endpoint (forest/domain) produced a row, so a
per-endpoint sync tombstones only its OWN objects — an unreachable forest never
tombstones another's. Nullable metadata-only ADD COLUMN.

Revision ID: c7e2b91f4a63
Revises: a3f6c1d84b29
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "c7e2b91f4a63"
down_revision = "a3f6c1d84b29"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "directory_objects", sa.Column("source_directory", sa.Text(), nullable=True)
    )
    op.create_index(
        "ix_directory_objects_source", "directory_objects", ["source_directory"]
    )


def downgrade() -> None:
    op.drop_index("ix_directory_objects_source", table_name="directory_objects")
    op.drop_column("directory_objects", "source_directory")
