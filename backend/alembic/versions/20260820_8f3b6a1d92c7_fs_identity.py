"""Roadmap §27 (2026-08-20): filesystem identity — hardlinks & symlinks.

Four nullable columns captured from the scan walk's existing lstat:
``nlink``/``inode``/``dev`` (hardlink-group identity; ``inode`` is the
signed-wrapped st_ino) and ``symlink_target`` (non-NULL marks a symlink,
which is catalogued but never hashed/extracted). Nullable with no default =
metadata-only ADD COLUMN — instant on a million-row table, no rewrite.
Backfill is the next full scan of each library. The partial index only
covers rows that can belong to a hardlink group.

Revision ID: 8f3b6a1d92c7
Revises: 229726a6e7a9
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "8f3b6a1d92c7"
down_revision = "229726a6e7a9"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("nlink", sa.Integer(), nullable=True))
    op.add_column("items", sa.Column("inode", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("dev", sa.BigInteger(), nullable=True))
    op.add_column("items", sa.Column("symlink_target", sa.Text(), nullable=True))
    op.create_index(
        "ix_items_hardlink",
        "items",
        ["dev", "inode"],
        postgresql_where=sa.text("nlink > 1"),
    )


def downgrade() -> None:
    op.drop_index("ix_items_hardlink", table_name="items")
    op.drop_column("items", "symlink_target")
    op.drop_column("items", "dev")
    op.drop_column("items", "inode")
    op.drop_column("items", "nlink")
