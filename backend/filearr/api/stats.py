"""Aggregate stats endpoints (P3-T14 timeline).

A thin, cheap grouped-count layer over Postgres truth — no new infra, no index
beyond the existing columns. The timeline is a date histogram over ``items.mtime``
(``date_trunc`` by month/year) that the frontend renders as clickable bars; a
click maps a bucket to an ``mtime`` range filter on ``/search``.

There is no dedicated ``mtime`` index today, so this is a bounded full-column
aggregate (grouped count over two columns). At homelab scale that is trivially
cheap; if a very large corpus ever makes it slow, add a b-tree on
``items(mtime)`` (noted as a future optimisation, not needed now)."""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.db import get_session
from filearr.models import Item, ItemStatus, Library
from filearr.schemas import (
    LibraryStatsResponse,
    LibraryStatsRow,
    TimelineBucket,
    TimelineResponse,
)
from filearr.security import PermissionContext, require_permission

router = APIRouter()

# FIX-3: an mtime more than 48h in the future is a SUSPECT timestamp (bad copy
# tool / mis-set clock), NOT a real point on the timeline — it is counted into a
# separate "invalid dates" bar rather than distorting the histogram's range. This
# matches ``search.recency_bucket``'s 48h future-skew window.
_FUTURE_SKEW = timedelta(hours=48)


def _next_boundary(start: datetime, bucket: str) -> datetime:
    """The exclusive upper edge of a month/year bucket whose lower edge is
    ``start`` (a ``date_trunc`` result). Pure calendar arithmetic — no dateutil."""
    if bucket == "year":
        return start.replace(year=start.year + 1)
    # month
    if start.month == 12:
        return start.replace(year=start.year + 1, month=1)
    return start.replace(month=start.month + 1)


@router.get(
    "/timeline",
    response_model=TimelineResponse,
)
async def timeline(
    library: uuid.UUID | None = Query(default=None, description="scope to one library"),
    bucket: str = Query(
        default="month",
        pattern="^(month|year)$",
        description="histogram granularity: month or year",
    ),
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> TimelineResponse:
    """Date histogram of active items by ``mtime`` (P3-T14).

    Buckets are ``date_trunc(bucket, mtime)`` counts over ``status='active'``
    non-sidecar items (optionally scoped to ``library``), ascending. Sidecars
    (``sidecar_of`` set) are excluded to match search's default visibility.
    Items with an mtime beyond the
    48h future-skew window are excluded from the bars and reported as
    ``invalid_count`` with an ``invalid_mtime_gte`` the UI can turn into a
    ``mtime_gte`` filter to inspect them."""
    now = datetime.now(UTC)
    future_edge = now + _FUTURE_SKEW
    # +1s so the threshold expresses "strictly beyond the 48h window" as a
    # ``mtime_gte`` (build_filters uses ``mtime >= gte``).
    invalid_mtime_gte = int(future_edge.timestamp()) + 1

    # Truncate in UTC explicitly (``mtime AT TIME ZONE 'UTC'``) so buckets are
    # deterministic regardless of the DB session time zone; the result is a naive
    # UTC wall-clock timestamp we re-stamp as UTC below.
    bucket_col = func.date_trunc(
        bucket, Item.mtime.op("AT TIME ZONE")("UTC")
    ).label("bucket")
    # T3 consistency: sidecar files (.xmp/.nfo/artwork) are hidden from default
    # search, so they must not distort the histogram either — a bulk metadata
    # export that stamps 400k .xmp sidecars in one week is a tooling event, not
    # a content timeline (live 2026-08: July bar was 87% .xmp sidecars).
    base = (Item.status == "active") & (Item.sidecar_of.is_(None))
    if library is not None:
        base = base & (Item.library_id == library)
    # P6-T4: histogram counts only the caller's readable items.
    scope_clause = ctx.sql_clause()
    if scope_clause is not None:
        base = base & scope_clause

    rows = (
        await session.execute(
            select(bucket_col, func.count())
            .where(base & (Item.mtime <= future_edge))
            .group_by(bucket_col)
            .order_by(bucket_col)
        )
    ).all()

    buckets: list[TimelineBucket] = []
    for start, count in rows:
        if start is None:
            continue
        # date_trunc(... AT TIME ZONE 'UTC') returns a naive UTC timestamp.
        if start.tzinfo is None:
            start = start.replace(tzinfo=UTC)
        end = _next_boundary(start, bucket)
        buckets.append(
            TimelineBucket(
                start=start,
                start_epoch=int(start.timestamp()),
                end_epoch=int(end.timestamp()),
                count=int(count),
            )
        )

    invalid_count = (
        await session.execute(
            select(func.count())
            .select_from(Item)
            .where(base & (Item.mtime > future_edge))
        )
    ).scalar_one()

    return TimelineResponse(
        bucket=bucket,
        library=library,
        buckets=buckets,
        invalid_count=int(invalid_count),
        invalid_mtime_gte=invalid_mtime_gte,
    )


@router.get("/libraries", response_model=LibraryStatsResponse)
async def library_stats(
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> LibraryStatsResponse:
    """Catalog footprint per library: active file count + total bytes (with the
    sidecar subset called out), plus the tombstoned missing/trashed tails.

    One grouped aggregate over ``items`` (count/sum by library and status) — a
    bounded full-column pass, same cost class as the timeline histogram, run
    only when the overview is opened (deliberately NOT folded into
    ``GET /libraries``, which many dropdowns hit). Scoped principals only count
    items they can read (P6-T4)."""
    scope_clause = ctx.sql_clause()

    lib_rows = (
        await session.execute(
            select(Library.id, Library.name, Library.source_agent_id)
        )
    ).all()

    base = select(
        Item.library_id,
        Item.status,
        func.count().label("n"),
        func.coalesce(func.sum(Item.size), 0).label("bytes"),
        func.count().filter(Item.sidecar_of.isnot(None)).label("sidecars"),
    ).group_by(Item.library_id, Item.status)
    if scope_clause is not None:
        base = base.where(scope_clause)
    agg = {(r.library_id, r.status): r for r in (await session.execute(base)).all()}

    rows: list[LibraryStatsRow] = []
    total_files = total_bytes = 0
    for lib_id, name, source_agent_id in lib_rows:
        active = agg.get((lib_id, ItemStatus.active))
        missing = agg.get((lib_id, ItemStatus.missing))
        trashed = agg.get((lib_id, ItemStatus.trashed))
        file_count = int(active.n) if active else 0
        lib_bytes = int(active.bytes) if active else 0
        rows.append(
            LibraryStatsRow(
                library_id=lib_id,
                name=name,
                is_agent=source_agent_id is not None,
                file_count=file_count,
                total_bytes=lib_bytes,
                sidecar_count=int(active.sidecars) if active else 0,
                missing_count=int(missing.n) if missing else 0,
                trashed_count=int(trashed.n) if trashed else 0,
            )
        )
        total_files += file_count
        total_bytes += lib_bytes

    rows.sort(key=lambda r: (-r.total_bytes, r.name.lower()))
    return LibraryStatsResponse(
        libraries=rows, total_files=total_files, total_bytes=total_bytes
    )
