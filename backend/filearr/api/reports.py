"""P11-T6 — reporting v1 API (canned reports, read scope).

* ``GET /api/v1/reports`` — the canned-report registry (metadata only).
* ``GET /api/v1/reports/{id}`` — run one report, either as a paginated JSON page
  (``format=json``, default) or a **streaming** machine-readable export
  (``format=csv|ndjson|xml``).

This endpoint IS the integration surface: the three streaming formats are the
supported way to pull a full report result into another tool. Each streams off a
server-side Postgres cursor (:func:`filearr.reports.stream_report_rows` +
:func:`filearr.reports.render_rows`) so a multi-hundred-thousand-row export peaks
at ~one row of memory (research §6.2). JSON stays the paginated UI envelope
(``limit``/``offset``/``has_more``); the streaming formats are full-result and
honour an OPTIONAL ``limit`` as a row cap (absent = the whole result). Every CSV
cell is formula-injection-guarded (OWASP; catalog data is untrusted) and every
XML name/value is escaped.

Reporting/export is data-exfiltration-shaped, but pre-RBAC (Phase 6) the
project's scope model applies: all report endpoints require ``read``. When RBAC
lands, this tightens to the ``download`` action + path-scoped ACL (P11-T10).
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from sqlalchemy import case, func, literal, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import db as _db
from filearr.db import get_session
from filearr.models import Item, ItemStatus, Library
from filearr.reports import (
    ALL_FORMATS,
    EXPORT_FORMATS,
    FORMAT_CONTENT_TYPE,
    FORMAT_EXTENSION,
    MAX_LIMIT,
    XLSX_FORMAT,
    CannedReport,
    ReportParams,
    get_report,
    list_reports,
    render_rows,
    render_xlsx_to_path,
    stream_report_rows,
)
from filearr.security import PermissionContext, require_permission, require_scope

router = APIRouter()

_FORMAT_DOC = (
    "Output format. `json` = the paginated UI envelope (limit/offset/has_more). "
    "`csv`, `ndjson`, `xml` = streaming full-result machine-readable exports "
    "(the integration surface) with the correct Content-Type + download filename; "
    "these honour `limit` as an optional row cap (omit `limit` = whole result)."
)


def _resolve(report_id: str) -> CannedReport:
    report = get_report(report_id)
    if report is None:
        raise HTTPException(404, f"unknown report {report_id!r}")
    return report


#: IN-T2 bounds for ``threshold_days`` (1 day .. ~100 years). Rejecting 0 and
#: negatives is the point: a 0-day "stale" threshold means "every file", which is
#: never what an operator meant to ask for, and a negative one would invert the
#: predicate into a future-dated query that ``bad_mtime`` already owns. The upper
#: bound is a sanity ceiling, not a capability limit.
THRESHOLD_MIN_DAYS = 1
THRESHOLD_MAX_DAYS = 36500

_THRESHOLD_DOC = (
    "Threshold in DAYS, for reports whose registry metadata sets "
    "`supports_threshold` (today: `stale_files`). Omit to use the report's "
    "`default_threshold_days`. Rejected with 422 for a report that declares no "
    "threshold, so a typo'd param never silently does nothing."
)


def _check_threshold(report: CannedReport, threshold_days: int | None) -> None:
    """Validate ``threshold_days`` against the report + the 1..36500 bounds.

    Shared by the run endpoint and the background-export enqueue so both reject
    identically — an export must never accept a value the sync path refuses (they
    execute the same builder)."""
    if threshold_days is None:
        return
    if not report.supports_threshold:
        raise HTTPException(
            422, f"report {report.id!r} does not support threshold_days"
        )
    if threshold_days < THRESHOLD_MIN_DAYS or threshold_days > THRESHOLD_MAX_DAYS:
        raise HTTPException(
            422,
            f"threshold_days must be between {THRESHOLD_MIN_DAYS} and "
            f"{THRESHOLD_MAX_DAYS}",
        )


def _check_common(
    report: CannedReport,
    fmt: str,
    limit: int | None,
    offset: int,
    library_id: uuid.UUID | None,
    threshold_days: int | None = None,
) -> None:
    """Validation shared by JSON + streaming exports."""
    if fmt not in ALL_FORMATS:
        raise HTTPException(422, f"format must be one of {', '.join(ALL_FORMATS)}")
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    if offset < 0:
        raise HTTPException(422, "offset must be >= 0")
    if library_id is not None and not report.supports_library:
        raise HTTPException(422, f"report {report.id!r} does not support library_id")
    _check_threshold(report, threshold_days)


def _stream_params(
    report: CannedReport,
    limit: int | None,
    library_id: uuid.UUID | None,
    threshold_days: int | None = None,
) -> tuple[ReportParams, int | None]:
    """Streaming-export (params, cap). For a CAPPED report ``limit`` is the
    definitional top-N (applied inside the cursor) so the outer cap is ``None``;
    otherwise a full stream, capped only when the caller passed ``limit``."""
    if report.is_capped:
        eff = report.default_limit if limit is None else limit
        return (
            ReportParams(library_id=library_id, limit=eff, threshold_days=threshold_days),
            None,
        )
    return (
        ReportParams(
            library_id=library_id,
            limit=report.default_limit,
            threshold_days=threshold_days,
        ),
        limit,
    )


@router.get("", dependencies=[Depends(require_scope("read"))])
async def get_reports() -> dict:
    """The canned-report registry (no query executed)."""
    return {"reports": list_reports()}


# --------------------------------------------------------------------------- #
# IN-T3 — folder drill-down for the treemap view (2026-08-13)                  #
# --------------------------------------------------------------------------- #
# Deliberately NOT a canned report: `largest_folders` is a flat global top-N
# across ALL depths, which cannot drive a treemap (a treemap needs one level at a
# time, and a du-style recursive list double-counts every ancestor). This returns
# the DIRECT CHILDREN of one parent only — small per call, hierarchical by
# construction, and the UI drills by issuing another call. `largest_folders` and
# its exports are untouched.
#
# ROUTE ORDER MATTERS: this must be declared BEFORE `@router.get("/{report_id}")`
# or FastAPI matches "folder-tree" as a report id and 404s. Do not move it below.

#: Default / hard ceiling on children returned per drill level. A treemap with
#: 500 rectangles is already unreadable; the cap exists so a pathological folder
#: (one live library has ~40k siblings in a flat dump directory) cannot turn a
#: click into a multi-megabyte JSON payload.
FOLDER_TREE_DEFAULT_LIMIT = 100
FOLDER_TREE_MAX_LIMIT = 500

#: Reserved child name for "files sitting directly in this folder" (as opposed to
#: in a subfolder). "." is borrowed from POSIX for exactly this meaning and cannot
#: collide with a real path segment — a literal "." segment is not something a
#: filesystem hands out in a rel_path (the scanner walks resolved paths).
FILES_HERE = "."


@router.get("/folder-tree")
async def folder_tree(
    library_id: uuid.UUID | None = None,
    parent: str = "",
    limit: int = FOLDER_TREE_DEFAULT_LIMIT,
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
) -> dict:
    """Direct children of ``parent``, one level, ordered by total bytes DESC.

    Two modes:

    * **all-libraries root** (no ``library_id``, ``parent`` empty) — one child per
      LIBRARY, so the top of the treemap is library-sized rectangles. Drilling into
      one pins ``library_id`` from the child row.
    * **within a library** (``library_id`` set) — the first path segment below
      ``parent`` for every active file under it. Files living directly IN
      ``parent`` are aggregated under the reserved ``"."`` child.

    This is a read/JSON surface (screen viewing, not a machine export), so RBAC is
    the plain visibility ``ctx.sql_clause()`` — the same predicate a report's JSON
    page uses. A denied file contributes to no child's count or bytes."""
    if limit < 1 or limit > FOLDER_TREE_MAX_LIMIT:
        raise HTTPException(422, f"limit must be between 1 and {FOLDER_TREE_MAX_LIMIT}")
    parent = parent.strip("/")
    if library_id is None and parent:
        # There is no meaningful cross-library "media/movies" — two libraries can
        # both have one and they are different folders. 422 rather than guessing.
        raise HTTPException(422, "parent requires library_id")

    scope_clause = ctx.sql_clause()
    if library_id is None:
        return await _folder_tree_libraries(session, limit, scope_clause)
    return await _folder_tree_children(session, library_id, parent, limit, scope_clause)


async def _folder_tree_libraries(session, limit: int, scope_clause) -> dict:
    """Root level, all libraries: one child per library (bytes + file count)."""
    stmt = (
        select(
            Library.id.label("library_id"),
            Library.name.label("library"),
            func.count().label("file_count"),
            func.coalesce(func.sum(Item.size), 0).label("total_bytes"),
        )
        .select_from(Item)
        .join(Library, Item.library_id == Library.id)
        .where(Item.status == ItemStatus.active)
        .group_by(Library.id, Library.name)
        .order_by(func.coalesce(func.sum(Item.size), 0).desc(), Library.name.asc())
        .limit(limit + 1)  # +1 probes for truncation without a second COUNT query
    )
    if scope_clause is not None:
        stmt = stmt.where(scope_clause)
    rows = (await session.execute(stmt)).all()
    truncated = len(rows) > limit
    children = [
        {
            "name": r.library,
            # A library child's folder is the library ROOT (empty rel_path prefix):
            # drilling calls back with library_id=<this> and parent="".
            "folder": "",
            "library_id": str(r.library_id),
            "library": r.library,
            "file_count": int(r.file_count),
            "total_bytes": int(r.total_bytes or 0),
            # A library with any visible file has something to drill into.
            "has_children": int(r.file_count) > 0,
        }
        for r in rows[:limit]
    ]
    return {"parent": "", "library_id": None, "children": children, "truncated": truncated}


async def _folder_tree_children(
    session, library_id: uuid.UUID, parent: str, limit: int, scope_clause
) -> dict:
    """One level below ``parent`` inside one library."""
    prefix = f"{parent}/" if parent else ""
    # rel_path with the parent prefix stripped: everything the child name and the
    # depth probe are derived from. substr is 1-indexed in Postgres.
    rest = func.substr(Item.rel_path, len(prefix) + 1)
    first_sep = func.strpos(rest, "/")
    # A file directly in `parent` has no separator left -> the reserved "." child.
    name = case(
        (first_sep > 0, func.split_part(rest, "/", 1)),
        else_=literal(FILES_HERE),
    )
    # "has_children" = does any file sit MORE than one segment below `parent`,
    # i.e. is there a SECOND separator after the first. Aggregated with bool_or in
    # the same single pass rather than issuing one EXISTS probe per child row:
    # identical semantics ("EXISTS a deeper separator"), one index scan instead of
    # N round-trips — and N is up to `limit` (500).
    deeper = case(
        (first_sep > 0, func.strpos(func.substr(rest, first_sep + 1), "/") > 0),
        else_=literal(False),
    )
    stmt = (
        select(
            name.label("name"),
            func.count().label("file_count"),
            func.coalesce(func.sum(Item.size), 0).label("total_bytes"),
            func.bool_or(deeper).label("has_children"),
        )
        .where(
            Item.status == ItemStatus.active,
            Item.library_id == library_id,
        )
        .group_by(name)
        .order_by(func.coalesce(func.sum(Item.size), 0).desc(), name.asc())
        .limit(limit + 1)
    )
    if prefix:
        # autoescape: a real folder name may contain % or _ and must not become a
        # LIKE wildcard. The trailing slash in `prefix` also makes the match
        # folder-boundary-exact, so "Movies/" never picks up "MoviesOld/...".
        stmt = stmt.where(Item.rel_path.startswith(prefix, autoescape=True))
    if scope_clause is not None:
        stmt = stmt.where(scope_clause)
    rows = (await session.execute(stmt)).all()
    truncated = len(rows) > limit
    lib = await session.get(Library, library_id)
    if lib is None:
        raise HTTPException(404, "library not found")
    children = [
        {
            "name": r.name,
            # The "." child is not a folder of its own — its folder IS the parent
            # (the UI renders it but must not drill into it).
            "folder": parent if r.name == FILES_HERE else f"{prefix}{r.name}",
            "library_id": str(library_id),
            "library": lib.name,
            "file_count": int(r.file_count),
            "total_bytes": int(r.total_bytes or 0),
            "has_children": bool(r.has_children) and r.name != FILES_HERE,
        }
        for r in rows[:limit]
    ]
    return {
        "parent": parent,
        "library_id": str(library_id),
        "children": children,
        "truncated": truncated,
    }


@router.get("/{report_id}")
async def run_report(
    report_id: str,
    format: str = Query("json", description=_FORMAT_DOC),
    limit: int | None = None,
    offset: int = 0,
    library_id: uuid.UUID | None = None,
    threshold_days: int | None = Query(None, description=_THRESHOLD_DOC),
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("search_metadata")),
):
    report = _resolve(report_id)
    _check_common(report, format, limit, offset, library_id, threshold_days)

    # P11-T10 action split: a machine-readable EXPORT (csv/ndjson/xml/xlsx) is
    # data-exfiltration-shaped and requires the `download` action; the paginated
    # JSON page (screen-viewing) stays on `search_metadata`. For a scoped
    # principal an export additionally scopes rows to what they may DOWNLOAD (not
    # merely view). Unrestricted (admin/API key/auth-off) => no-op, legacy.
    if format in EXPORT_FORMATS:
        ctx.require_capability("download")
        scope_clause = ctx.sql_clause(action="download")
        params, cap = _stream_params(report, limit, library_id, threshold_days)
        if format == XLSX_FORMAT:
            return xlsx_response(report, params, cap, scope_clause=scope_clause)
        return export_response(report, params, format, cap, scope_clause=scope_clause)

    # JSON page: visibility scope (a denied row never appears).
    scope_clause = ctx.sql_clause()
    eff_limit = report.default_limit if limit is None else limit
    params = ReportParams(
        library_id=library_id, limit=eff_limit, threshold_days=threshold_days
    )
    rows, has_more = await _json_page(
        session, report, params, eff_limit, offset, scope_clause=scope_clause
    )
    return {
        "report": report.meta(),
        "columns": list(report.columns),
        "rows": rows,
        "limit": eff_limit,
        "offset": offset,
        "count": len(rows),
        "has_more": has_more,
    }


async def _json_page(
    session: AsyncSession,
    report: CannedReport,
    params: ReportParams,
    limit: int,
    offset: int,
    scope_clause=None,
) -> tuple[list[dict], bool]:
    """One page of rows + a has-more flag.

    For a report with a Python ``post_filter`` (the scored heuristic) the SQL
    offset/limit cannot be trusted (they count pre-filter rows), so we page
    through the streaming cursor, skipping/taking in Python — memory stays
    bounded by the page window, not the full candidate set. Simple reports page
    directly in SQL (index-served ``LIMIT/OFFSET``)."""
    if report.post_filter is not None:
        rows: list[dict] = []
        idx = 0
        has_more = False
        async for d in stream_report_rows(session, report, params, scope_clause):
            if idx < offset:
                idx += 1
                continue
            if len(rows) < limit:
                rows.append(d)
                idx += 1
            else:
                has_more = True
                break
        return rows, has_more

    # IN-T1: statement() (not build()) — a report whose top level selects from a
    # subquery scopes by pushing the predicate down; an outer .where() there would
    # cartesian-join ``items`` back in and silently defeat the filter.
    stmt = report.statement(params, scope_clause)
    stmt = stmt.offset(offset).limit(limit + 1)
    result = await session.execute(stmt)
    fetched = [report.row(r) for r in result.all()]
    has_more = len(fetched) > limit
    return fetched[:limit], has_more


def export_response(
    report: CannedReport,
    params: ReportParams,
    fmt: str,
    cap: int | None,
    *,
    filename_id: str | None = None,
    scope_clause=None,
) -> StreamingResponse:
    """Stream a report as ``csv``/``ndjson``/``xml`` off a dedicated cursor.

    ``cap`` bounds the total rows streamed (``None`` = the whole result). A
    ``StreamingResponse`` body outlives the request-scoped ``Depends(get_session)``
    (which may already be closed when the generator runs), so we open our own
    session for the cursor."""
    columns = list(report.columns)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    fid = filename_id or report.id
    filename = f"filearr-{fid}-{stamp}.{FORMAT_EXTENSION[fmt]}"
    generated = datetime.now(UTC).isoformat()

    async def _rows():
        async with _db.SessionLocal() as session:
            n = 0
            async for d in stream_report_rows(session, report, params, scope_clause):
                if cap is not None and n >= cap:
                    break
                n += 1
                yield d

    body = render_rows(fmt, columns, _rows(), report_id=report.id, generated=generated)
    return StreamingResponse(
        body,
        media_type=FORMAT_CONTENT_TYPE[fmt],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def xlsx_response(
    report: CannedReport,
    params: ReportParams,
    cap: int | None,
    *,
    filename_id: str | None = None,
    scope_clause=None,
) -> StreamingResponse:
    """Stream a report as an ``.xlsx`` workbook (P11-T4 remainder).

    An xlsx is a zip whose central directory trails the data, so it cannot be
    produced row-by-row into the response — it is assembled to a diskguarded temp
    file with ``xlsxwriter`` ``constant_memory=True`` (peak memory ~one row, the
    same bound as the text formats) and then streamed back. Every cell is written
    as a literal string with ``strings_to_formulas`` off, so a catalog value like
    ``=SUM(A1)`` is never evaluated (the xlsx formula-injection guard). The temp
    file is removed after the body is fully sent."""
    import os
    import tempfile

    from filearr import diskguard
    from filearr.config import get_settings

    columns = list(report.columns)
    stamp = datetime.now(UTC).strftime("%Y%m%d")
    fid = filename_id or report.id
    filename = f"filearr-{fid}-{stamp}.{FORMAT_EXTENSION[XLSX_FORMAT]}"
    tmpdir = tempfile.gettempdir()
    settings = get_settings()
    diskguard.guard_write(tmpdir, settings)  # FIX-11 fail-closed pre-write
    fd, tmp_path = tempfile.mkstemp(prefix="filearr-xlsx-", suffix=".xlsx", dir=tmpdir)
    os.close(fd)

    async def _rows():
        async with _db.SessionLocal() as session:
            async for d in stream_report_rows(session, report, params, scope_clause):
                yield d

    async def _body():
        try:
            await render_xlsx_to_path(columns, _rows(), tmp_path, cap=cap)
            with open(tmp_path, "rb") as fh:
                while True:
                    chunk = fh.read(65536)
                    if not chunk:
                        break
                    yield chunk
        finally:
            try:
                os.remove(tmp_path)
            except OSError:
                pass

    return StreamingResponse(
        _body(),
        media_type=FORMAT_CONTENT_TYPE[XLSX_FORMAT],
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def _csv_response(report: CannedReport, params: ReportParams) -> StreamingResponse:
    """Back-compat shim: stream a report as CSV. Capped reports honour their
    top-N ``limit`` (applied in the cursor); others stream the full result."""
    cap = None
    return export_response(report, params, "csv", cap)


@router.post("/{report_id}/export", status_code=202)
async def enqueue_report_export(
    report_id: str,
    format: str = Query("csv", description="Export format (csv/ndjson/xml/xlsx)."),
    limit: int | None = None,
    library_id: uuid.UUID | None = None,
    threshold_days: int | None = Query(None, description=_THRESHOLD_DOC),
    session: AsyncSession = Depends(get_session),
    ctx: PermissionContext = Depends(require_permission("download")),
) -> dict:
    """Queue a BACKGROUND export of a canned report (P11-T5). Returns the created
    ``report_exports`` row; poll ``GET /exports/{id}`` then fetch
    ``GET /exports/{id}/download``. The sync ``GET /reports/{id}?format=...`` stays
    for smaller interactive exports."""
    from filearr.api.exports import _export_out, enqueue_export

    report = _resolve(report_id)
    if limit is not None and (limit < 1 or limit > MAX_LIMIT):
        raise HTTPException(422, f"limit must be between 1 and {MAX_LIMIT}")
    if library_id is not None and not report.supports_library:
        raise HTTPException(422, f"report {report.id!r} does not support library_id")
    # IN-T2: the SAME validation the sync path applies — a background export runs
    # the identical builder, so it must not be a way to smuggle an out-of-bounds
    # threshold past the API. The value round-trips through export.params and is
    # rebuilt in filearr.exports._resolve_report.
    _check_threshold(report, threshold_days)
    export = await enqueue_export(
        session, ctx, canned_report_key=report.id, fmt=format,
        library_id=library_id, limit=limit, threshold_days=threshold_days,
    )
    return _export_out(export)
