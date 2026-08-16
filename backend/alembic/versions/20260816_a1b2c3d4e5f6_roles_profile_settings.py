"""roles as data, self-service profile, session-timeout overrides, app settings

Four things, one migration, because they ship as one feature (2026-08-16):

* ``roles`` — the global role vocabulary moves out of a CHECK constraint into a
  table: the three builtins are seeded with the permissions they always had
  (undeletable via the API; permissions editable) and operators may add custom
  roles. ``principals.global_role`` becomes an FK to it (ON UPDATE CASCADE, ON
  DELETE RESTRICT — a role in use cannot be dropped at the DB level either).
* ``users.display_name`` / ``users.phone`` — the account page's profile.
* ``principals.session_inactivity_hours`` / ``session_ttl_hours`` — per-user
  session-timeout overrides (NULL = global); ``principals.preferences`` — the
  self-service preferences bag (theme defaults etc.).
* ``app_settings`` — runtime-editable overrides for env defaults; first keys
  are the global session timeouts.

Revision ID: a1b2c3d4e5f6
Revises: c7d2e4a91b38
Create Date: 2026-08-16
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "a1b2c3d4e5f6"
down_revision = "c7d2e4a91b38"
branch_labels = None
depends_on = None

_ALL_ACTIONS = [
    "search_metadata",
    "search_content",
    "download",
    "upload",
    "modify",
    "delete",
    "edit_metadata",
    "manage_alerts",
]
_USER_ACTIONS = [a for a in _ALL_ACTIONS if a != "delete"]
_VIEWER_ACTIONS = ["search_metadata", "search_content"]


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("name", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), server_default=sa.text("''"), nullable=False),
        sa.Column("builtin", sa.Boolean(), server_default=sa.text("false"), nullable=False),
        sa.Column(
            "scopes", postgresql.ARRAY(sa.Text()), server_default=sa.text("'{}'"), nullable=False
        ),
        sa.Column(
            "ceiling_actions",
            postgresql.ARRAY(sa.Text()),
            server_default=sa.text("'{}'"),
            nullable=False,
        ),
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
    roles = sa.table(
        "roles",
        sa.column("name", sa.Text()),
        sa.column("display_name", sa.Text()),
        sa.column("description", sa.Text()),
        sa.column("builtin", sa.Boolean()),
        sa.column("scopes", postgresql.ARRAY(sa.Text())),
        sa.column("ceiling_actions", postgresql.ARRAY(sa.Text())),
    )
    op.bulk_insert(
        roles,
        [
            {
                "name": "admin",
                "display_name": "Administrator",
                "builtin": True,
                "description": (
                    "Full control: every API scope, every action on every path, all admin panels."
                ),
                "scopes": ["admin", "write", "read"],
                "ceiling_actions": _ALL_ACTIONS,
            },
            {
                "name": "user",
                "display_name": "User",
                "builtin": True,
                "description": "Read and write within granted paths; cannot delete or administer.",
                "scopes": ["write", "read"],
                "ceiling_actions": _USER_ACTIONS,
            },
            {
                "name": "viewer",
                "display_name": "Viewer",
                "builtin": True,
                "description": "Read-only search within granted paths; no downloads or edits.",
                "scopes": ["read"],
                "ceiling_actions": _VIEWER_ACTIONS,
            },
        ],
    )

    # principals.global_role: CHECK -> FK to roles.name
    op.drop_constraint("principals_global_role_valid", "principals", type_="check")
    op.create_foreign_key(
        "fk_principals_global_role_roles",
        "principals",
        "roles",
        ["global_role"],
        ["name"],
        onupdate="CASCADE",
        ondelete="RESTRICT",
    )
    op.add_column("principals", sa.Column("session_inactivity_hours", sa.Integer(), nullable=True))
    op.add_column("principals", sa.Column("session_ttl_hours", sa.Integer(), nullable=True))
    op.add_column(
        "principals",
        sa.Column(
            "preferences", postgresql.JSONB(), server_default=sa.text("'{}'::jsonb"), nullable=False
        ),
    )

    op.add_column("users", sa.Column("display_name", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("phone", sa.Text(), nullable=True))

    op.create_table(
        "app_settings",
        sa.Column("key", sa.Text(), primary_key=True),
        sa.Column("value", postgresql.JSONB(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("principals.id", ondelete="SET NULL"),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_table("app_settings")
    op.drop_column("users", "phone")
    op.drop_column("users", "display_name")
    op.drop_column("principals", "preferences")
    op.drop_column("principals", "session_ttl_hours")
    op.drop_column("principals", "session_inactivity_hours")
    op.drop_constraint("fk_principals_global_role_roles", "principals", type_="foreignkey")
    # Any principal on a custom role cannot satisfy the old CHECK: fold them to
    # viewer (fail closed) before re-adding it.
    op.execute(
        "UPDATE principals SET global_role='viewer' "
        "WHERE global_role NOT IN ('admin','user','viewer')"
    )
    op.create_check_constraint(
        "principals_global_role_valid",
        "principals",
        "global_role IN ('admin','user','viewer')",
    )
    op.drop_table("roles")
