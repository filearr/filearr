"""app_logs: unified log stream for the console Logs panel.

App and worker run in separate containers, so a UI log view cannot read any
single process's stdout — both processes write WARNING+ (and filearr INFO)
records here through the fail-open DB log sink (filearr.logsink), and the
Jobs page tails the table. High-churn by design: bigint identity PK (cheap,
keyset-paginated), bounded by the purge_app_logs retention task.

Revision ID: d4b8e1f6c290
Revises: b8d4e6f2a157
Create Date: 2026-08-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "d4b8e1f6c290"
down_revision = "b8d4e6f2a157"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "app_logs",
        sa.Column("id", sa.BigInteger(), sa.Identity(), primary_key=True),
        sa.Column(
            "ts",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("level", sa.Text(), nullable=False),
        sa.Column("levelno", sa.SmallInteger(), nullable=False),
        sa.Column("logger", sa.Text(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("exc", sa.Text(), nullable=True),
    )
    # Reads are keyset on the PK; ts serves only the retention purge cutoff.
    op.create_index("ix_app_logs_ts", "app_logs", ["ts"])


def downgrade() -> None:
    op.drop_index("ix_app_logs_ts", table_name="app_logs")
    op.drop_table("app_logs")
