"""LLM-grade API keys: role, path/library scope, content + path flags, rate limit.

M1 of the LLM/RAG integration (docs/research/llm-rag-integration.md §5).
All columns nullable: NULL llm_role = an ordinary (non-LLM) key, and the
per-key flags NULL = inherit the role's defaults — fully backward compatible.

Revision ID: f2c8b5a9d417
Revises: d4f7a2b8c153
Create Date: 2026-07-28
"""

import sqlalchemy as sa

from alembic import op
from filearr.models import LtreeCompat

revision = "f2c8b5a9d417"
down_revision = "d4f7a2b8c153"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("api_keys", sa.Column("llm_role", sa.Text(), nullable=True))
    # LtreeCompat, NEVER Text: psycopg renders ::VARCHAR bind casts for Text
    # params, fatal (42804) against a live ltree column (see models.LtreeCompat).
    op.add_column("api_keys", sa.Column("path_scope", LtreeCompat(), nullable=True))
    op.add_column(
        "api_keys",
        sa.Column("libraries", sa.ARRAY(sa.UUID(as_uuid=True)), nullable=True),
    )
    op.add_column("api_keys", sa.Column("content_access", sa.Boolean(), nullable=True))
    op.add_column("api_keys", sa.Column("reveal_paths", sa.Boolean(), nullable=True))
    op.add_column("api_keys", sa.Column("rate_limit", sa.Integer(), nullable=True))


def downgrade() -> None:
    for col in (
        "rate_limit",
        "reveal_paths",
        "content_access",
        "libraries",
        "path_scope",
        "llm_role",
    ):
        op.drop_column("api_keys", col)
