"""doc_chunks + libraries.chunking_enabled: LLM/RAG tier-2 passage store (M2).

Design: docs/research/llm-rag-integration.md §4 tier 2. Chunks are Postgres
truth (the Meili `<index>_chunks` projection is disposable, invariant 1);
``embedding`` persists the chunk vector so a projection rebuild never
re-embeds (mirrors the item-level ``_embedding`` refinement). Chunking is a
per-library opt-in (design §10.2), like the OCR pass.

Revision ID: b8d4e6f2a157
Revises: a3f1c7e2d940
Create Date: 2026-08-06
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision = "b8d4e6f2a157"
down_revision = "a3f1c7e2d940"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "doc_chunks",
        sa.Column(
            "id",
            UUID(as_uuid=True),
            primary_key=True,
            server_default=sa.text("uuidv7()"),
        ),
        sa.Column(
            "item_id",
            UUID(as_uuid=True),
            sa.ForeignKey("items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_no", sa.Integer(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("embedding", JSONB(), nullable=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint("item_id", "chunk_no", name="uq_doc_chunks_item_chunk"),
    )
    op.create_index("ix_doc_chunks_item_id", "doc_chunks", ["item_id"])
    op.add_column(
        "libraries",
        sa.Column(
            "chunking_enabled",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )


def downgrade() -> None:
    op.drop_column("libraries", "chunking_enabled")
    op.drop_index("ix_doc_chunks_item_id", table_name="doc_chunks")
    op.drop_table("doc_chunks")
