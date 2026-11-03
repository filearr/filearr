"""items: partial index for the /stats semantic coverage counts

``ix_items_embedding_fp`` — a btree over ``metadata ->> '_embedding_fp'``
restricted to active rows that carry a fingerprint. It turns the two counts in
``filearr.embed_stats.semantic_counts`` from heap walks that de-TOAST metadata
(the with-fingerprint count hit the 5 s /stats statement timeout 50x in four
days on the ~1.1M-item instance) into an index-only scan and an Index Cond
lookup. The predicate must stay textually equal to the queries in embed_stats.

Built CONCURRENTLY (in an autocommit block — CIC cannot run inside a
transaction): a plain CREATE INDEX would hold a SHARE lock on ``items`` — and so
block every scan/extract write — for as long as it takes to evaluate the
predicate over a million metadata blobs. ``IF NOT EXISTS`` makes a re-run after
an interrupted build harmless; an interrupted CIC can leave an INVALID index
behind, which the upgrade drops and rebuilds.

Excluded from autogenerate (``alembic/env.py`` _RUNTIME_CONDITIONAL_INDEXES):
it is a partial EXPRESSION index, the same shape as
uq_alert_events_dedup_pending, whose textual comparison is brittle — this
migration is its single source of truth.

Revision ID: 9c3f1e7a2b58
Revises: 7e2c4b9a1d36
Create Date: 2026-11-02
"""

from __future__ import annotations

from alembic import op

revision = "9c3f1e7a2b58"
down_revision = "7e2c4b9a1d36"
branch_labels = None
depends_on = None

_NAME = "ix_items_embedding_fp"


def upgrade() -> None:
    with op.get_context().autocommit_block():
        # A previous interrupted CONCURRENTLY build leaves an INVALID index that
        # IF NOT EXISTS would happily keep forever; drop that one first.
        op.execute(
            f"""
            DO $$
            BEGIN
                IF EXISTS (
                    SELECT 1 FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid
                    WHERE c.relname = '{_NAME}' AND NOT i.indisvalid
                ) THEN
                    EXECUTE 'DROP INDEX {_NAME}';
                END IF;
            END $$;
            """
        )
        op.execute(
            f"CREATE INDEX CONCURRENTLY IF NOT EXISTS {_NAME} "
            "ON items ((metadata ->> '_embedding_fp')) "
            "WHERE status = 'active' AND metadata ? '_embedding_fp'"
        )


def downgrade() -> None:
    with op.get_context().autocommit_block():
        op.execute(f"DROP INDEX CONCURRENTLY IF EXISTS {_NAME}")
