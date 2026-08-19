"""P6-T10 (2026-08-19): api_keys.service_account_id + backfill; service_accounts.description.

Revision ID: e0f1a2b3c4d5
Revises: d9e0f1a2b3c4
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "e0f1a2b3c4d5"
down_revision = "d9e0f1a2b3c4"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("service_accounts", sa.Column("description", sa.Text(), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column(
            "service_account_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("service_accounts.principal_id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_api_keys_service_account_id", "api_keys", ["service_account_id"])
    # Backfill: every existing key becomes owned by ONE "Pre-existing keys"
    # service account (a real principal, kind service_account, role viewer --
    # a key's power comes from its scopes, not the owner's role). Only when
    # there are keys to re-home.
    conn = op.get_bind()
    n = conn.execute(
        sa.text("SELECT count(*) FROM api_keys WHERE service_account_id IS NULL")
    ).scalar()
    if n:
        pid = conn.execute(
            sa.text(
                "INSERT INTO principals (kind, global_role) VALUES ('service_account', 'viewer') "
                "RETURNING id"
            )
        ).scalar()
        conn.execute(
            sa.text(
                "INSERT INTO service_accounts (principal_id, name, description) "
                "VALUES (:pid, 'Pre-existing keys', "
                "'API keys that existed before service accounts (backfilled). "
                "Move them by re-minting under a named account.')"
            ),
            {"pid": pid},
        )
        conn.execute(
            sa.text(
                "UPDATE api_keys SET service_account_id = :pid WHERE service_account_id IS NULL"
            ),
            {"pid": pid},
        )


def downgrade() -> None:
    op.drop_index("ix_api_keys_service_account_id", table_name="api_keys")
    op.drop_column("api_keys", "service_account_id")
    op.drop_column("service_accounts", "description")
