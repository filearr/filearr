"""job_errors — persisted failure text for Procrastinate jobs (roadmap §18).

procrastinate_events stores only (job_id, type, at); the exception text of a
failed job went exclusively to worker logs, leaving /system/failed-jobs with a
permanently-null ``error``. The joberrors worker middleware now records one
row per failed attempt here (sanitized message + capped traceback). No FK to
procrastinate_jobs (Procrastinate owns that table); both are purged on the
same job_history_retention_days window.

Revision ID: d4f7a2b8c153
Revises: c8d1e5f2a936
Create Date: 2026-07-24
"""

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision = "d4f7a2b8c153"
down_revision = "c8d1e5f2a936"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "job_errors",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column("job_id", sa.BigInteger(), nullable=True),
        sa.Column("task_name", sa.Text(), nullable=False),
        sa.Column("queue", sa.Text(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False, server_default=sa.text("0")),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("traceback", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
    )
    op.create_index("ix_job_errors_job_id", "job_errors", ["job_id"])
    op.create_index("ix_job_errors_created_at", "job_errors", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_job_errors_created_at", table_name="job_errors")
    op.drop_index("ix_job_errors_job_id", table_name="job_errors")
    op.drop_table("job_errors")
