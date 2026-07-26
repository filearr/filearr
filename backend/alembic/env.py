"""Alembic environment — sync engine, URL from filearr settings (FILEARR_DATABASE_URL)."""

from alembic import context
from sqlalchemy import create_engine, pool

from filearr.config import get_settings
from filearr.models import Base

target_metadata = Base.metadata

# Tables owned by other tools (never create/drop/alter via our migrations)
_FOREIGN_PREFIXES = ("procrastinate_",)

# Indexes deliberately invisible to autogenerate / `alembic check`, because the
# ORM models cannot truthfully declare them:
#   * ix_*_scope_gist / ix_*_scope — the RBAC ltree migration (d7e4c1b9f3a2)
#     creates a GiST index when the ltree extension exists and a btree prefix
#     index otherwise. Which one a given DB carries is an ENVIRONMENT fact
#     (prod has contrib; the pgserver sandbox does not), so any single model
#     declaration would register drift somewhere.
#   * uq_alert_events_dedup_pending — a partial EXPRESSION index
#     (COALESCE(item_id, nil-uuid), WHERE NOT delivered; f3b8d2a41c5e).
#     Autogenerate's expression comparison is textual and brittle across
#     alembic versions; the migration remains its single source of truth.
_RUNTIME_CONDITIONAL_INDEXES = frozenset(
    {
        "ix_items_path_scope_gist",
        "ix_items_path_scope",
        "ix_path_grants_scope_gist",
        "ix_path_grants_scope",
        "uq_alert_events_dedup_pending",
    }
)


def include_name(name, type_, parent_names):
    if type_ == "table":
        if name is not None and name.startswith(_FOREIGN_PREFIXES):
            return False
        # never emit drops for reflected tables we don't model
        return name in target_metadata.tables
    if type_ == "index" and name in _RUNTIME_CONDITIONAL_INDEXES:
        return False
    return True


def _url() -> str:
    return get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        include_name=include_name,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    engine = create_engine(_url(), poolclass=pool.NullPool)
    with engine.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_name=include_name,
        )
        with context.begin_transaction():
            context.run_migrations()
    engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
