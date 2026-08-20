"""LDAP-T1 (2026-08-20): AD/LDAP directory sync — directory_objects + alias source.

``directory_objects`` is the synced AD directory of record central resolves
agent-pushed permission SIDs against. ``principal_aliases.source`` records
whether an alias was created by an admin ('manual') or the directory sync
('ldap'), so a resync never clobbers a manual override.

Revision ID: a3f6c1d84b29
Revises: 8f3b6a1d92c7
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import ARRAY

from alembic import op

revision = "a3f6c1d84b29"
down_revision = "8f3b6a1d92c7"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "principal_aliases",
        sa.Column("source", sa.Text(), server_default=sa.text("'manual'"), nullable=False),
    )
    op.create_table(
        "directory_objects",
        sa.Column("id", sa.Uuid(), server_default=sa.text("uuidv7()"), nullable=False),
        sa.Column("object_guid", sa.Text(), nullable=False),
        sa.Column("object_sid", sa.Text(), nullable=True),
        sa.Column("sam_account_name", sa.Text(), nullable=True),
        sa.Column("display_name", sa.Text(), nullable=True),
        sa.Column("user_principal_name", sa.Text(), nullable=True),
        sa.Column("distinguished_name", sa.Text(), nullable=True),
        sa.Column("kind", sa.Text(), server_default=sa.text("'other'"), nullable=False),
        sa.Column("domain", sa.Text(), nullable=True),
        sa.Column(
            "member_of_sids", ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column("disabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "last_synced_at", sa.DateTime(timezone=True), server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("object_guid", name="uq_directory_objects_guid"),
    )
    op.create_index("ix_directory_objects_sid", "directory_objects", ["object_sid"])
    op.create_index("ix_directory_objects_sam", "directory_objects", ["sam_account_name"])
    op.create_index("ix_directory_objects_kind", "directory_objects", ["kind"])
    op.create_index(
        "ix_directory_objects_member_of",
        "directory_objects",
        ["member_of_sids"],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_directory_objects_member_of", table_name="directory_objects")
    op.drop_index("ix_directory_objects_kind", table_name="directory_objects")
    op.drop_index("ix_directory_objects_sam", table_name="directory_objects")
    op.drop_index("ix_directory_objects_sid", table_name="directory_objects")
    op.drop_table("directory_objects")
    op.drop_column("principal_aliases", "source")
