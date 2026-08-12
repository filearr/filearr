"""instance_meta: key/deployment fingerprints so a restore can be CHECKED (BK-T1)

The defect this exists to close is a SILENT one. ``FILEARR_SECRET_KEY`` is the
AES-GCM envelope key for alert-channel secrets and is held outside Postgres on
purpose (a stolen dump must yield no credentials -- alerts/crypto.py). The cost
of that design is that a dump carries the ciphertext but not the key, so a
restore onto a fresh box with a newly generated key reports success at every
step -- pg_restore fine, init_db fine, rebuild-index fine -- while every stored
SMTP password / webhook HMAC secret / apprise URL is now permanently
undecryptable. Nothing anywhere said so. It surfaces weeks later as an alert
that quietly stopped sending.

This table is the missing anchor: the key's fingerprint travels INSIDE the dump,
so the next boot can compare what the environment supplies against what the data
was encrypted under and shout when they differ.

A generic key/value shape rather than a column-per-fact so the next thing worth
pinning (the step-ca root, already stamped here as ``ca_root_fingerprint``) costs
no migration. Values are fingerprints and short opaque strings ONLY -- putting a
secret here would hand back exactly what keeping the key outside Postgres buys.

Revision ID: f3a9d5c81b62
Revises: d4f1a7c93e60
Create Date: 2026-08-12
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "f3a9d5c81b62"
down_revision = "d4f1a7c93e60"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "instance_meta",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )


def downgrade() -> None:
    # Dropping this loses only the fingerprints, which the next boot re-stamps
    # from the live environment. That re-stamp is why a downgrade is safe AND
    # why it is quietly destructive: it re-establishes the baseline against
    # whatever key is present at that moment, so a downgrade/upgrade cycle
    # performed WHILE the key is wrong silently blesses the wrong key.
    op.drop_table("instance_meta")
