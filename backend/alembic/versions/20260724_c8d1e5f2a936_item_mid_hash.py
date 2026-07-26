"""items.mid_hash — midpoint 64 KiB xxh3 sample (roadmap §13 move-rescue tier).

quick_hash samples only head+tail, so large files sharing an intro/outro and
size collide; under a quick_only policy (network libraries, no content_hash)
such a collision makes a genuine move ambiguous and it is refused. The
midpoint sample is a cheap third discriminator. NULL for files <=128 KiB
(fully covered by quick_hash) and for rows hashed before this migration —
extraction stamps it lazily on the next hash pass; move detection only uses
it when both sides carry one.

Revision ID: c8d1e5f2a936
Revises: b6e2d9f4a713
Create Date: 2026-07-24
"""

import sqlalchemy as sa

from alembic import op

revision = "c8d1e5f2a936"
down_revision = "b6e2d9f4a713"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("items", sa.Column("mid_hash", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("items", "mid_hash")
