"""Async SQLAlchemy engine/session (psycopg3 driver)."""

from collections.abc import AsyncGenerator

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from filearr.config import get_settings

_settings = get_settings()

engine = create_async_engine(
    _settings.database_url,
    pool_size=10,
    max_overflow=20,
    pool_pre_ping=True,
)

SessionLocal = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


#: Postgres caps ONE statement at 65,535 bind parameters (a wire-protocol
#: int16). Every ``col.in_(python_list)`` expands to one param per element, so
#: any list that can scale with the catalog must be chunked. Live 2026-08-16:
#: the first scan of a 303k-file library crashed at ``Item.id.in_(new_item_ids)``
#: with "number of parameters must be between 0 and 65535".
IN_CHUNK = 10_000


def in_chunks(values, size: int = IN_CHUNK):
    """Yield ``values`` (any iterable) as lists of at most ``size``."""
    seq = list(values)
    for i in range(0, len(seq), size):
        yield seq[i : i + size]


async def scalars_where_in(session, stmt, column, values, *, size: int = IN_CHUNK) -> list:
    """``stmt.where(column.in_(values))`` executed in bind-safe chunks; returns
    the concatenated ``.scalars()`` rows (identity-mapped, so an ORM row is one
    instance no matter which chunk loaded it). Empty ``values`` -> ``[]`` with
    no round trip."""
    out: list = []
    for chunk in in_chunks(values, size):
        out.extend((await session.execute(stmt.where(column.in_(chunk)))).scalars().all())
    return out


async def get_session() -> AsyncGenerator[AsyncSession]:
    async with SessionLocal() as session:
        yield session
