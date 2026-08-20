"""P11-T6 — canned-report registry (reporting v1) + shared export serializers.

Each canned report is *code, not a DB row* (research §4): a small, frozen
:class:`CannedReport` pairing human metadata (id/title/description/columns) with
ONE efficient SQLAlchemy ``Select`` builder and a per-row serializer. There are
no migrations and no querydsl here — canned reports take at most light params
(``library_id``, a top-N ``limit``) so they cannot be broken by a malformed
filter. Custom/saved-query reports, the ``meta.``/``cf.`` grammar, xlsx, and
scheduled delivery are later Phase-11 tasks (see
``docs/tasks/phase-11-reporting-tasks.md``).

Every builder returns a single streamable statement so the API can serve it two
ways off ONE query (research §6):

* **JSON** — a bounded page (``limit``/``offset``), materialised small.
* **Streaming export** — ``AsyncSession.stream()`` + ``yield_per`` (a server-side
  cursor), so even a 750k-row export peaks at ~one row of memory, never the whole
  result. CSV, NDJSON, and XML all ride this same cursor via
  :func:`render_rows`; only JSON is the paginated UI envelope.

Two reports compute a derived column in Python from already-fetched JSONB (no new
extraction, no persisted derived value — invariant 2): ``low_quality_video``
(the §3 scorer) and ``corrupt_media`` (error classification). ``low_quality_video``
additionally carries a ``post_filter`` (keep score >= review band); the API
paginates it through the streaming cursor so the Python filter never breaks
offset/limit alignment or bounds memory.

**P11 polish (link model + richer exports):** every per-item report row carries an
``item_id`` (so the UI can open the item detail modal, exactly like a search hit)
plus full path context — ``path`` (container-absolute), ``native_path``
(``native_prefix``-joined, invariant 3) and ``share_url`` (``share_prefix``-joined,
the UI open-location prefix). Aggregate reports (``unmapped_extensions``,
``duplicate_files``) carry no single item, so they declare a *smart link* instead
(``row_link`` = ``search_ext``/``search_hash``): the UI turns the row into a
pre-filtered search. ``row_link`` is a per-report field the UI switches on.
"""

from __future__ import annotations

import csv
import io
import json
import uuid
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from xml.sax.saxutils import escape as _xml_escape
from xml.sax.saxutils import quoteattr as _xml_quoteattr

from sqlalchemy import (
    Select,
    Text,
    and_,
    case,
    cast,
    func,
    literal,
    or_,
    select,
    text,
    true,
    type_coerce,
)
from sqlalchemy.dialects.postgresql import ARRAY as PG_ARRAY
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased
from sqlalchemy.sql.elements import Grouping

from filearr import share_map
from filearr.models import (
    Agent,
    Item,
    ItemStatus,
    Library,
    PermissionSnapshot,
    PrincipalAlias,
    ScanRun,
)
from filearr.quality_score import REVIEW_BAND, score_item

#: Server-side cursor batch size for streaming exports (research §6.2).
YIELD_PER = 1000

#: Hard ceiling on any single ``limit`` (top-N cap / JSON page size / export cap).
#: A cheap guard against an accidental "give me 100M rows" request (research §7).
MAX_LIMIT = 100_000

#: The single BINARY export format (assembled to a temp file with xlsxwriter
#: ``constant_memory=True`` then streamed — a zip cannot be produced row-by-row).
XLSX_FORMAT = "xlsx"

#: The machine-readable STREAMING export formats (full-result, server-side cursor,
#: honouring an optional row cap). ``json`` is deliberately NOT here — it is the
#: paginated UI envelope, a different shape entirely.
STREAMING_FORMATS: tuple[str, ...] = ("csv", "ndjson", "xml")

#: All formats a run endpoint accepts.
ALL_FORMATS: tuple[str, ...] = ("json", *STREAMING_FORMATS, XLSX_FORMAT)

#: MIME + filename-extension per streaming format (the integration Content-Types).
FORMAT_CONTENT_TYPE: dict[str, str] = {
    "csv": "text/csv",
    "ndjson": "application/x-ndjson",
    "xml": "application/xml",
    "xlsx": (
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    ),
}
FORMAT_EXTENSION: dict[str, str] = {
    "csv": "csv",
    "ndjson": "ndjson",
    "xml": "xml",
    "xlsx": "xlsx",
}

#: Every machine-readable export format the download paths accept (text streaming
#: + xlsx). ``json`` (the paginated UI envelope) is excluded — it is a different
#: shape entirely.
EXPORT_FORMATS: tuple[str, ...] = (*STREAMING_FORMATS, XLSX_FORMAT)

#: Columns carrying full path context, appended to every per-item report. Kept in
#: exports always; the UI hides them behind a "show all columns" toggle.
PATH_CONTEXT_COLUMNS: tuple[str, ...] = ("path", "native_path", "share_url", "share_unc")

#: Substrings (lowercased) that classify an ``_extract_error`` as an ffprobe /
#: media-decode rejection rather than a tag/parser-level error. The extractor
#: prefixes every ffprobe failure with ``"ffprobe "`` (see
#: ``filearr.tasks.ffprobe.FfprobeError``); the decode phrases catch the raw
#: ffmpeg message text embedded in ``"ffprobe failed: <msg>"``.
FFPROBE_ERROR_MARKERS = (
    "ffprobe",
    "invalid data found",
    "moov atom not found",
    "error while decoding",
    "could not find codec",
    "does not contain any stream",
)

_FORMULA_LEADERS = ("=", "+", "-", "@", "\t", "\r")


def csv_safe(value: object) -> str:
    """OWASP CSV-injection guard: neutralise a cell that a spreadsheet would
    interpret as a formula by prefixing a single quote. ``None`` -> empty."""
    s = "" if value is None else str(value)
    if s and s[0] in _FORMULA_LEADERS:
        return "'" + s
    return s


def join_prefix(prefix: str | None, rel_path: str) -> str | None:
    """Join a library path prefix (``native_prefix`` / ``share_prefix``) onto an
    item ``rel_path`` (invariant 3). Mirrors ``api.items._with_native_path``:
    the separator is inferred from the prefix (backslash for a Windows/UNC
    prefix, forward slash otherwise) and the rel_path's forward slashes are
    rewritten to match. ``None``/empty prefix -> ``None`` (no affordance)."""
    if not prefix:
        return None
    sep = "\\" if "\\" in prefix else "/"
    return prefix.rstrip(sep) + sep + rel_path.replace("/", sep)


@dataclass(frozen=True)
class ReportParams:
    """Light, non-querydsl parameterisation for a canned report."""

    library_id: uuid.UUID | None = None
    limit: int = 1000
    #: IN-T2 (2026-08-13) — ONE generic numeric slot for reports that declare
    #: ``supports_threshold``. Deliberately generic rather than a per-report
    #: field: canned reports take *light* params by design (module docstring), and
    #: a single validated int (1..36500 at the API layer, see
    #: ``api.reports._check_common``) cannot be shaped into a malformed filter.
    #: ``None`` => the report's ``default_threshold_days``.
    threshold_days: int | None = None


@dataclass(frozen=True)
class CannedReport:
    """One canned report: metadata + a query builder + a row serializer."""

    id: str
    title: str
    description: str
    columns: tuple[str, ...]
    build: Callable[[ReportParams], Select]
    row: Callable[[Any], dict]
    supports_library: bool = False
    #: When true, ``limit`` is the report's definitional top-N cap and bounds the
    #: export too (e.g. ``largest_files`` must never dump the whole library).
    is_capped: bool = False
    #: Default ``limit`` (top-N for capped reports; JSON page size otherwise).
    default_limit: int = 1000
    #: Optional Python-side keep predicate over the serialized row (scored report).
    post_filter: Callable[[dict], bool] | None = None
    #: How the UI makes a row interactive (P11 polish):
    #: ``item`` — per-item, open the ItemDetail modal by ``item_id``;
    #: ``search_ext`` — aggregate extension row -> ``#/search?extension=<ext>``;
    #: ``search_hash`` — aggregate hash group -> ``#/search?hash=<hash>``;
    #: ``none`` — no interaction.
    row_link: str = "none"
    #: IN-T2 (2026-08-13) — this report reads ``ReportParams.threshold_days``. The
    #: UI renders a numeric input ONLY for reports that declare it (the flag is
    #: surfaced in :meth:`meta`), so no other report grows a stray control.
    supports_threshold: bool = False
    #: Human label for that input (e.g. "Not modified in the last (days)").
    threshold_label: str = ""
    #: Value used when the caller passes no ``threshold_days``.
    default_threshold_days: int = 0
    #: IN-T1 (2026-08-13) — OPTIONAL builder that folds the RBAC scope predicate
    #: INTO the statement instead of having the caller ``.where()`` it on afterwards.
    #:
    #: Rationale (this is a correctness *and* a security constraint, not a style
    #: choice): a report whose top-level statement selects from a SUBQUERY — which
    #: ``duplicate_files_detail`` must, because a window function is illegal in
    #: ``WHERE`` — cannot accept an outer ``.where(Item.path_scope ...)``. Item is
    #: not in that statement's FROM list, so SQLAlchemy would auto-add ``items``
    #: and produce a CARTESIAN PRODUCT: wrong rows AND a silently ineffective
    #: scope filter. Such a report supplies ``scoped_build`` and pushes the clause
    #: down to the inner per-item select, which also preserves the documented
    #: guarantee that a denied item never *contributes to a group* (its copy count
    #: must not leak through an aggregate either — see
    #: :func:`stream_report_rows`).
    scoped_build: Callable[[ReportParams, Any], Select] | None = None

    def statement(self, params: ReportParams, scope_clause=None) -> Select:
        """The runnable statement for ``params``, scope predicate already applied.

        The ONE place that knows whether a report scopes via ``scoped_build``
        (pushed down) or via a plain outer ``.where()``. Every execution path
        (JSON page, streaming export, background export, LLM tool) goes through
        here so none of them can accidentally skip the push-down."""
        if self.scoped_build is not None:
            return self.scoped_build(params, scope_clause)
        stmt = self.build(params)
        if scope_clause is not None:
            stmt = stmt.where(scope_clause)
        return stmt

    def meta(self) -> dict:
        """Registry-listing shape (no query executed)."""
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "columns": list(self.columns),
            "supports_library": self.supports_library,
            "is_capped": self.is_capped,
            "default_limit": self.default_limit,
            "row_link": self.row_link,
            # IN-T2: the UI renders the threshold input off these three (and only
            # for reports where supports_threshold is true).
            "supports_threshold": self.supports_threshold,
            "threshold_label": self.threshold_label,
            "default_threshold_days": self.default_threshold_days,
        }


# --------------------------------------------------------------------------- #
# Shared building blocks                                                       #
# --------------------------------------------------------------------------- #
_ACTIVE = Item.status == ItemStatus.active


def _apply_library(stmt: Select, params: ReportParams) -> Select:
    if params.library_id is not None:
        stmt = stmt.where(Item.library_id == params.library_id)
    return stmt


def _classify_extract_error(err: str | None) -> str:
    """ffprobe/media-decode rejection vs. a tag/parser-level error."""
    low = (err or "").lower()
    return "ffprobe" if any(m in low for m in FFPROBE_ERROR_MARKERS) else "tag"


def _path_context(r: Any) -> dict:
    """Full path context for a per-item row: container-absolute ``path`` plus the
    ``native_prefix``/``share_prefix``-joined variants (invariant 3). The builder
    must select ``Item.path``, ``Library.native_prefix`` and
    ``Library.share_prefix`` (aliased as ``native_prefix``/``share_prefix``)."""
    rel = r.rel_path
    return {
        "path": r.path,
        "native_path": join_prefix(r.native_prefix, rel),
        # OPS-T7: manual share_prefix wins; else the deploy mount map resolves the
        # item's absolute container path to a network URL (auto share_prefix).
        "share_url": share_map.item_share_url(r.share_prefix, r.path, rel),
        # UI-T15: Windows-UNC counterpart of ``share_url`` (None for non-SMB
        # schemes / POSIX mounts). API consumers pick share_url vs share_unc per
        # the calling system's OS.
        "share_unc": share_map.item_share_location(
            r.share_prefix, r.path, rel
        ).unc,
    }


# --------------------------------------------------------------------------- #
# 1. unmapped_extensions — feeds OPS-T4 (extension-map expansion)             #
# --------------------------------------------------------------------------- #
def _build_unmapped(params: ReportParams) -> Select:
    ext = func.coalesce(Item.extension, literal("")).label("extension")
    stmt = (
        select(
            ext,
            func.count().label("file_count"),
            func.coalesce(func.sum(Item.size), 0).label("total_bytes"),
        )
        # sidecar_of IS NULL EXCLUDES linked sidecars (.nfo, *_JRSidecar.xml,
        # .xmp, .thm, ...): they are bookkeeping rows and would otherwise drown the
        # real unmapped-extension signal (nfo+xml alone were ~122k of the live
        # 750k-corpus rows). _ACTIVE already scopes to status='active'. W8-B:
        # file_category='other' is the taxonomy catch-all (a genuinely unrecognised
        # extension) — a tighter, more correct "unmapped" signal than the old
        # media_type='other' bucket (which also swept archives/code/system files
        # that now have real categories).
        .where(_ACTIVE, Item.file_category == "other", Item.sidecar_of.is_(None))
        .group_by(ext)
        .order_by(func.count().desc(), ext.asc())
    )
    return _apply_library(stmt, params)


def _row_unmapped(r: Any) -> dict:
    return {
        "extension": r.extension or "",
        "file_count": int(r.file_count),
        "total_bytes": int(r.total_bytes or 0),
    }


# --------------------------------------------------------------------------- #
# 2. bad_mtime — future-dated files (mtime > now + 48h)                        #
# --------------------------------------------------------------------------- #
def _build_bad_mtime(params: ReportParams) -> Select:
    stmt = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.mtime,
            Item.size,
        )
        .join(Library, Item.library_id == Library.id)
        .where(_ACTIVE, Item.mtime > text("now() + interval '48 hours'"))
        .order_by(Item.mtime.desc(), Item.rel_path.asc())
    )
    return _apply_library(stmt, params)


def _row_bad_mtime(r: Any) -> dict:
    return {
        "item_id": str(r.item_id),
        "rel_path": r.rel_path,
        "library": r.library,
        "mtime": r.mtime.isoformat() if r.mtime is not None else None,
        "size": int(r.size),
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 3. corrupt_media — items carrying an _extract_error, classified             #
# --------------------------------------------------------------------------- #
def _build_corrupt(params: ReportParams) -> Select:
    stmt = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.metadata_["_extract_error"].astext.label("error_text"),
        )
        .join(Library, Item.library_id == Library.id)
        .where(_ACTIVE, Item.metadata_.has_key("_extract_error"))
        .order_by(Item.rel_path.asc())
    )
    return _apply_library(stmt, params)


def _row_corrupt(r: Any) -> dict:
    return {
        "item_id": str(r.item_id),
        "rel_path": r.rel_path,
        "library": r.library,
        "error_class": _classify_extract_error(r.error_text),
        "error_text": r.error_text or "",
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 4. largest_files — top-N by size (capped)                                   #
# --------------------------------------------------------------------------- #
def _build_largest(params: ReportParams) -> Select:
    stmt = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.file_category,
            Item.size,
        )
        .join(Library, Item.library_id == Library.id)
        .where(_ACTIVE)
        .order_by(Item.size.desc(), Item.rel_path.asc())
    )
    return _apply_library(stmt, params)


def _row_largest(r: Any) -> dict:
    return {
        "item_id": str(r.item_id),
        "rel_path": r.rel_path,
        "library": r.library,
        "file_category": r.file_category,
        "size": int(r.size),
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 5. low_quality_video — §3 scored heuristic (Python), review+ only           #
# --------------------------------------------------------------------------- #
def _build_low_quality(params: ReportParams) -> Select:
    stmt = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.size,
            Item.metadata_.label("md"),
            Item.user_metadata.label("umd"),
        )
        .join(Library, Item.library_id == Library.id)
        .where(
            _ACTIVE,
            Item.file_category == "video",
            Item.metadata_.has_key("height"),
        )
        .order_by(Item.size.desc(), Item.rel_path.asc())
    )
    return _apply_library(stmt, params)


def _row_low_quality(r: Any) -> dict:
    effective = {**(r.md or {}), **(r.umd or {})}
    res = score_item(effective)
    return {
        "item_id": str(r.item_id),
        "rel_path": r.rel_path,
        "library": r.library,
        "size": int(r.size),
        "resolution": effective.get("resolution") or "",
        "video_codec": effective.get("video_codec") or "",
        "score": res.score,
        "band": res.band,
        "reasons": "; ".join(res.reasons),
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 6. duplicate_files — content_hash (fallback quick_hash+size) groups, N>1     #
# --------------------------------------------------------------------------- #
def _build_duplicates(params: ReportParams) -> Select:
    # content_hash groups collapse exact-content copies; where content_hash was
    # never computed (quick_only policy / oversize), fall back to the cheap
    # quick_hash keyed with size so unrelated files sharing a quick_hash but of
    # different length don't get merged.
    dup_key = func.coalesce(
        Item.content_hash,
        Item.quick_hash.concat(literal(":")).concat(cast(Item.size, Text)),
    ).label("dup_key")
    wasted = (func.sum(Item.size) - func.max(Item.size)).label("wasted_bytes")
    # QH-T5: which hash tier grouped this cluster. ``content_hash`` is uniform
    # within a content group (present) and NULL for a quick-hash fallback group,
    # so max(content_hash) IS NOT NULL uniquely distinguishes the tiers. A
    # ``quick_hash`` tier is a SAMPLED signal (head+tail window for >128KiB, or,
    # pre-QH-T1, a partial read in the 64-128KiB band) — not byte-verified.
    hash_tier = case(
        (func.max(Item.content_hash).isnot(None), literal("content_hash")),
        else_=literal("quick_hash"),
    ).label("hash_tier")
    stmt = (
        select(
            dup_key,
            func.count().label("copies"),
            wasted,
            hash_tier,
            # Group representatives for the "exact-copy" search link: content_hash
            # is uniform within a content group (NULL for a quick-hash fallback
            # group); quick_hash is the fallback link target.
            func.max(Item.content_hash).label("content_hash"),
            func.max(Item.quick_hash).label("quick_hash"),
            func.string_agg(
                Library.name.concat(literal(":")).concat(Item.rel_path),
                literal("; "),
            ).label("paths"),
        )
        .join(Library, Item.library_id == Library.id)
        # QH-T5 (§3b): exclude zero-byte files entirely. Every empty file
        # legitimately shares quick_hash("")+size=0, so they grouped into one giant
        # false-positive cluster (the live 3,711-copy row) — byte-identical does
        # not imply meaningfully duplicate when the shared content is empty. This
        # is a hard rule, independent of the hashing fix.
        .where(
            _ACTIVE,
            Item.size > 0,
            or_(Item.content_hash.isnot(None), Item.quick_hash.isnot(None)),
        )
        .group_by(dup_key)
        .having(func.count() > 1)
        .order_by((func.sum(Item.size) - func.max(Item.size)).desc())
    )
    return _apply_library(stmt, params)


def _row_duplicates(r: Any) -> dict:
    return {
        "dup_key": r.dup_key,
        "copies": int(r.copies),
        "wasted_bytes": int(r.wasted_bytes or 0),
        # QH-T5: the grouping tier. 'quick_hash' groups are a SAMPLED signal (not
        # byte-verified); 'content_hash' groups are full-hash-confirmed exact
        # duplicates. The UI surfaces this with a "sampled signal" caveat.
        "hash_tier": r.hash_tier,
        # UI links exact-copy listing on the content hash, falling back to the
        # quick hash for a quick-only group (both are exact search targets).
        "content_hash": r.content_hash,
        "quick_hash": r.quick_hash,
        "paths": r.paths or "",
    }


# --------------------------------------------------------------------------- #
# 6b. duplicate_files_detail — ONE ROW PER COPY (IN-T1, 2026-08-13)            #
# --------------------------------------------------------------------------- #
# WHY this exists alongside the aggregate ``duplicate_files``: that report's
# ``paths`` column is a single ``string_agg`` blob with no ``item_id`` and no
# path translation, so a script cannot act on it safely — it cannot tell which
# physical file on which machine each entry is, and it cannot re-verify anything
# before touching bytes. Phase-11 research §11 Q6 flagged the aggregate as an
# INTERIM shape "awaiting per-copy convergence"; this is that convergence. The
# summary report stays untouched (it is the cheap "how much is wasted" view).
#
# Filearr still never acts on media (governing principle: insight, not
# management). This report + its exports are the input to the operator's OWN
# script on the operator's OWN machine — see docs-site/reports.md.
#
# The two derived group columns are computed with WINDOW functions over the same
# base predicate as the aggregate, NOT with a self-join or a second pass:
#   * ``copies_in_group`` = COUNT(*) OVER (PARTITION BY dup_key)  -> the >1 filter
#   * ``group_rank``      = ROW_NUMBER() OVER (PARTITION BY dup_key
#                                             ORDER BY mtime DESC, item_id) - 1
# A window function is ILLEGAL in WHERE, so the whole thing is wrapped in a
# subquery and filtered outside — which is exactly why this report carries a
# ``scoped_build`` (see CannedReport.scoped_build for the cartesian-product trap).
def _dup_key_expr():
    """The grouping key, IDENTICAL to ``_build_duplicates`` (one definition of
    "same file" across the summary and the per-copy view — divergence here would
    mean the two reports disagree about what a duplicate is)."""
    return func.coalesce(
        Item.content_hash,
        Item.quick_hash.concat(literal(":")).concat(cast(Item.size, Text)),
    )


def _build_duplicates_detail(params: ReportParams, scope_clause=None) -> Select:
    dup_key = _dup_key_expr()
    part = dup_key  # PARTITION BY expression, reused by every window below
    # mtime DESC ranks the NEWEST copy 0. ``Item.id`` is the deterministic
    # tie-break: uuidv7 PKs are unique, so two copies sharing a byte-identical
    # mtime (common — a cp -p / rsync -a duplicate keeps the timestamp) still
    # rank stably ACROSS RE-RUNS. Without it Postgres is free to return a
    # different winner each run and a script's "keep" file would drift.
    rank = (
        func.row_number().over(partition_by=part, order_by=(Item.mtime.desc(), Item.id.asc()))
        - 1
    ).label("group_rank")
    copies = func.count().over(partition_by=part).label("copies_in_group")
    # Group-level wasted bytes (all copies but the largest), used ONLY for
    # ordering — the biggest win streams first, so a capped/limited export is
    # still the rows an operator most wants, and whole groups stay adjacent.
    wasted = (
        func.sum(Item.size).over(partition_by=part)
        - func.max(Item.size).over(partition_by=part)
    ).label("group_wasted_bytes")
    # QH-T5 tier, computed as a WINDOW max rather than per row so every row in a
    # group reports the SAME tier the aggregate report would report for it (a
    # mixed group — content_hash on one row, a colliding quick:size key on
    # another — must not hand a script two different confidence answers).
    tier = case(
        (func.max(Item.content_hash).over(partition_by=part).isnot(None), literal("content_hash")),
        else_=literal("quick_hash"),
    ).label("hash_tier")
    inner = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.size,
            Item.mtime,
            Item.content_hash,
            Item.quick_hash,
            dup_key.label("group_key"),
            rank,
            copies,
            wasted,
            tier,
        )
        .join(Library, Item.library_id == Library.id)
        # Same predicate as the aggregate, including the QH-T5 size>0 exclusion
        # (every empty file trivially shares a hash — the live 3,711-copy cluster).
        .where(
            _ACTIVE,
            Item.size > 0,
            or_(Item.content_hash.isnot(None), Item.quick_hash.isnot(None)),
        )
    )
    inner = _apply_library(inner, params)
    # RBAC pushed DOWN into the per-item select: a denied item must neither appear
    # nor inflate copies_in_group / group_rank for the rows that do appear.
    if scope_clause is not None:
        inner = inner.where(scope_clause)
    sub = inner.subquery("dups")
    return (
        select(sub)
        .where(sub.c.copies_in_group > 1)
        .order_by(
            sub.c.group_wasted_bytes.desc(),
            sub.c.group_key.asc(),
            sub.c.group_rank.asc(),
        )
    )


def _build_duplicates_detail_plain(params: ReportParams) -> Select:
    """``build`` shim (the registry's mandatory unscoped entry point). Every
    execution path goes through :meth:`CannedReport.statement`, which prefers
    ``scoped_build``; this exists for callers that legitimately have no scope
    predicate (auth-off / admin / API key) and for ``EXPLAIN``-style use."""
    return _build_duplicates_detail(params, None)


def _row_duplicates_detail(r: Any) -> dict:
    return {
        "item_id": str(r.item_id),
        "group_key": r.group_key,
        "group_rank": int(r.group_rank),
        "copies_in_group": int(r.copies_in_group),
        "hash_tier": r.hash_tier,
        # DATA, NOT A DECISION. "newest mtime wins" is one reasonable default and
        # nothing more; the docs say so plainly and the example scripts filter on
        # ``keep_hint == "candidate"``, never delete a "keep" row, and re-verify
        # size/mtime against the live file before touching anything.
        "keep_hint": "keep" if int(r.group_rank) == 0 else "candidate",
        "rel_path": r.rel_path,
        "library": r.library,
        "size": int(r.size),
        "mtime": r.mtime.isoformat() if r.mtime is not None else None,
        "content_hash": r.content_hash,
        "quick_hash": r.quick_hash,
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 6c. stale_files — mtime older than a PARAMETERIZED threshold (IN-T2)        #
# --------------------------------------------------------------------------- #
#: ``stale_files`` default window: two years. Chosen as "old enough that nobody
#: argues" rather than tuned — the whole point is that the operator sets it.
STALE_DEFAULT_DAYS = 730


def _build_stale(params: ReportParams) -> Select:
    days = STALE_DEFAULT_DAYS if params.threshold_days is None else int(params.threshold_days)
    # make_interval(years, months, weeks, days) keeps ``days`` a BIND PARAMETER —
    # no interval string is ever concatenated from caller input. (The API also
    # validates 1..36500 before we get here; this is the second lock.)
    cutoff = func.now() - func.make_interval(0, 0, 0, days)
    # Floor-days since mtime, computed in SQL so it stays correct for a streaming
    # export (no per-row Python clock drift across a 20-minute 750k-row dump).
    age_days = func.floor(
        func.extract("epoch", func.now() - Item.mtime) / 86400.0
    ).label("age_days")
    stmt = (
        select(
            Item.id.label("item_id"),
            Item.rel_path,
            Item.path,
            Library.name.label("library"),
            Library.native_prefix.label("native_prefix"),
            Library.share_prefix.label("share_prefix"),
            Item.mtime,
            age_days,
            Item.size,
        )
        .join(Library, Item.library_id == Library.id)
        .where(_ACTIVE, Item.mtime < cutoff)
        # Oldest first: the operator wants the far tail, not the boundary cases.
        .order_by(Item.mtime.asc(), Item.rel_path.asc())
    )
    return _apply_library(stmt, params)


def _row_stale(r: Any) -> dict:
    return {
        "item_id": str(r.item_id),
        "rel_path": r.rel_path,
        "library": r.library,
        "mtime": r.mtime.isoformat() if r.mtime is not None else None,
        "age_days": int(r.age_days or 0),
        "size": int(r.size),
        **_path_context(r),
    }


# --------------------------------------------------------------------------- #
# 7. largest_folders — du-style recursive folder totals (capped)              #
# --------------------------------------------------------------------------- #
def _build_largest_folders(params: ReportParams) -> Select:
    # Explode each item's rel_path into every ancestor-folder prefix via a
    # LATERAL generate_series over the path depth (du semantics: a folder's
    # total includes everything under it, so a parent always ranks at or above
    # its children). Items sitting directly in the library root (rel_path with
    # no '/') contribute to no folder — the library overview covers root totals.
    parts_raw = func.string_to_array(Item.rel_path, "/")
    # Grouping forces the parens Postgres requires to subscript a function
    # result — SQLAlchemy renders the bare (invalid) `string_to_array(...)[..]`
    # otherwise; type_coerce to ARRAY makes the slice operator available.
    parts = type_coerce(Grouping(parts_raw), PG_ARRAY(Text))
    depth = (
        func.generate_series(1, func.cardinality(parts_raw) - 1)
        .table_valued("depth")
        .render_derived()
        .lateral("d")
    )
    folder = func.array_to_string(parts[1 : depth.c.depth], "/").label("folder")
    stmt = (
        select(
            Library.id.label("library_id"),
            Library.name.label("library"),
            folder,
            depth.c.depth.label("depth"),
            func.count().label("file_count"),
            func.coalesce(func.sum(Item.size), 0).label("total_bytes"),
        )
        .select_from(Item)
        .join(Library, Item.library_id == Library.id)
        .join(depth, true())
        .where(_ACTIVE)
        .group_by(Library.id, Library.name, folder, depth.c.depth)
        .order_by(func.coalesce(func.sum(Item.size), 0).desc(), folder.asc())
    )
    return _apply_library(stmt, params)


def _row_largest_folders(r: Any) -> dict:
    return {
        # library_id is not a declared column (never exported to a spreadsheet,
        # like item_id) — it powers the UI's Browse deep-link for the row.
        "library_id": str(r.library_id),
        "library": r.library,
        "folder": r.folder,
        "depth": int(r.depth),
        "file_count": int(r.file_count),
        "total_bytes": int(r.total_bytes or 0),
    }


# --------------------------------------------------------------------------- #
# Registry                                                                     #
# --------------------------------------------------------------------------- #
_REPORTS: tuple[CannedReport, ...] = (
    CannedReport(
        id="unmapped_extensions",
        title="Unmapped extensions",
        description=(
            "Non-sidecar extensions landing in file_category='other' — count and "
            "total bytes per extension, most common first. Linked sidecars "
            "(.nfo/.xml/.xmp/.thm/artwork) are excluded so the tail is genuinely "
            "unmappable. An empty extension row ('') = extensionless files (no "
            "extension signal; left as 'other'). Feeds extension-map expansion "
            "(OPS-T4)."
        ),
        columns=("extension", "file_count", "total_bytes"),
        build=_build_unmapped,
        row=_row_unmapped,
        supports_library=True,
        row_link="search_ext",
    ),
    CannedReport(
        id="bad_mtime",
        title="Future-dated files",
        description=(
            "Items whose modified-time is more than 48 hours in the future — a "
            "common sign of a bad clock, timezone bug, or corrupt timestamp."
        ),
        columns=("rel_path", "library", "mtime", "size", *PATH_CONTEXT_COLUMNS),
        build=_build_bad_mtime,
        row=_row_bad_mtime,
        supports_library=True,
        row_link="item",
    ),
    CannedReport(
        id="corrupt_media",
        title="Extraction errors",
        description=(
            "Items that recorded an extraction error, classified as an ffprobe / "
            "media-decode rejection (likely corrupt/truncated media) vs. a "
            "tag/parser-level error."
        ),
        columns=("rel_path", "library", "error_class", "error_text", *PATH_CONTEXT_COLUMNS),
        build=_build_corrupt,
        row=_row_corrupt,
        supports_library=True,
        row_link="item",
    ),
    CannedReport(
        id="largest_files",
        title="Largest files",
        description="The largest files by size (top N, default 500).",
        columns=("rel_path", "library", "file_category", "size", *PATH_CONTEXT_COLUMNS),
        build=_build_largest,
        row=_row_largest,
        supports_library=True,
        is_capped=True,
        default_limit=500,
        row_link="item",
    ),
    CannedReport(
        id="low_quality_video",
        title="Low-quality video candidates",
        description=(
            "Probed video scored for low quality over existing ffprobe fields "
            "(resolution floor, legacy codecs, bitrate-per-pixel floor, HDR/audio "
            "oddities). Shows the score, band, and the reasons that fired; only "
            "review-band and above are listed."
        ),
        columns=(
            "rel_path",
            "library",
            "size",
            "resolution",
            "video_codec",
            "score",
            "band",
            "reasons",
            *PATH_CONTEXT_COLUMNS,
        ),
        build=_build_low_quality,
        row=_row_low_quality,
        supports_library=True,
        post_filter=lambda d: d["score"] >= REVIEW_BAND,
        row_link="item",
    ),
    CannedReport(
        id="duplicate_files",
        title="Duplicate files",
        description=(
            "Groups of identical files by content hash (falling back to quick "
            "hash + size), with copy count, hash tier, aggregated paths, and "
            "wasted bytes (all copies but one). A 'quick_hash' tier is a SAMPLED "
            "signal, not byte-verified; 'content_hash' is a full-hash-confirmed "
            "exact duplicate. Zero-byte files are excluded (every empty file "
            "trivially shares a hash)."
        ),
        columns=("dup_key", "copies", "hash_tier", "wasted_bytes", "paths"),
        build=_build_duplicates,
        row=_row_duplicates,
        supports_library=True,
        row_link="search_hash",
    ),
    CannedReport(
        id="duplicate_files_detail",
        title="Duplicate copies",
        description=(
            "ONE ROW PER COPY of every duplicate group (the actionable companion "
            "to 'Duplicate files', which is one aggregated row per group). Each "
            "row carries its own item, full path context, size, mtime and hashes, "
            "plus group_rank (0 = newest copy by mtime, ties broken by item id so "
            "re-runs are stable) and keep_hint ('keep' for rank 0, else "
            "'candidate'). keep_hint is DATA, not a decision — 'newest mtime' is "
            "just one reasonable default; scripts filter on it. Groups stream "
            "biggest-waste-first and stay contiguous. A 'quick_hash' tier group is "
            "a SAMPLED signal, NOT byte-verified — verify before deleting. "
            "Filearr never touches the files: export this and act with your own "
            "script (docs: Reports & exports)."
        ),
        columns=(
            "group_key",
            "group_rank",
            "copies_in_group",
            "hash_tier",
            "keep_hint",
            "rel_path",
            "library",
            "size",
            "mtime",
            "content_hash",
            "quick_hash",
            *PATH_CONTEXT_COLUMNS,
        ),
        build=_build_duplicates_detail_plain,
        scoped_build=_build_duplicates_detail,
        row=_row_duplicates_detail,
        supports_library=True,
        # NOT capped: a limited export must still be usable, and it is — the
        # ordering keeps whole groups adjacent, biggest waste first.
        row_link="item",
    ),
    CannedReport(
        id="stale_files",
        title="Not modified in years",
        description=(
            "Files whose LAST-MODIFIED time is older than the chosen threshold "
            "(default 730 days), oldest first, with the age in whole days. "
            "IMPORTANT: this is modification age, not access age — Filearr does "
            "not capture filesystem access times at all (and atime is unreliable "
            "or disabled outright on most mounts, including every noatime and "
            "network mount), so 'untouched' here means 'unmodified', never "
            "'unread'. A file you watch weekly and never edit is stale by this "
            "definition."
        ),
        columns=(
            "rel_path",
            "library",
            "mtime",
            "age_days",
            "size",
            *PATH_CONTEXT_COLUMNS,
        ),
        build=_build_stale,
        row=_row_stale,
        supports_library=True,
        row_link="item",
        supports_threshold=True,
        threshold_label="Not modified in the last (days)",
        default_threshold_days=STALE_DEFAULT_DAYS,
    ),
    CannedReport(
        id="largest_folders",
        title="Largest folders",
        description=(
            "Folders ranked by recursive size (du-style): every folder at every "
            "depth with its total INCLUDING subfolders — a parent always ranks "
            "at or above its children. file_count is all active files under the "
            "folder; files sitting directly in the library root belong to no "
            "folder. Top N by size (default 500)."
        ),
        columns=("folder", "library", "depth", "file_count", "total_bytes"),
        build=_build_largest_folders,
        row=_row_largest_folders,
        supports_library=True,
        is_capped=True,
        default_limit=500,
        row_link="browse",
    ),
)

# --------------------------------------------------------------------------- #
# W7-T7 (2026-08-19): permission reports over permission_snapshots.            #
# Latest snapshot per (agent, path); ACEs unnested via jsonb_array_elements.   #
# RBAC: an admin (no scope clause) sees everything; a scoped principal only     #
# sees snapshots linked to items they may see (scoped_build joins items).      #
# Exclusion defaults per research §4: well-known principals and inherited ACEs #
# are hidden so the report highlights explicit, meaningful grants.            #
# --------------------------------------------------------------------------- #
from sqlalchemy import Boolean as _SABoolean  # noqa: E402
from sqlalchemy import Integer as _SAInteger  # noqa: E402
from sqlalchemy.dialects.postgresql import JSONB as _JSONB  # noqa: E402


def _latest_snapshots_subq(name: str = "ps_ranked"):
    """One row per (agent, path): the newest snapshot. ``name`` lets a query
    embed two independent instances (the outliers report joins child → parent)."""
    ranked = (
        select(
            PermissionSnapshot,
            func.row_number()
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at.desc(), PermissionSnapshot.id.desc()),
            )
            .label("rn"),
        )
    ).subquery(name)
    return ranked


def _perm_base(
    params: ReportParams,
    scope_clause=None,
    *,
    broad_only: bool = False,
    outliers: bool = False,
) -> Select:
    ranked = _latest_snapshots_subq()
    from sqlalchemy import column as _sacolumn

    ace = (
        func.jsonb_array_elements(ranked.c.aces)
        .table_valued(_sacolumn("value", _JSONB))
        .render_derived()
    )
    a = ace.c.value
    principal = a["principal"]
    p_id = principal["id"].astext
    p_name = principal["name"].astext
    p_wk = principal["well_known"].astext
    p_kind = principal["kind"].astext
    inherited = a["inherited"].astext.cast(_SABoolean)
    # W7-T8 (2026-08-20): fold host-local identities into their canonical
    # cross-host identity via principal_aliases (presentation-side; snapshots
    # stay verbatim). The raw id remains in principal_id for forensics.
    alias = aliased(PrincipalAlias)
    stmt = (
        select(
            ranked.c.agent_id.label("agent_id"),
            Agent.name.label("agent"),
            ranked.c.path.label("path"),
            ranked.c.is_dir.label("is_dir"),
            ranked.c.item_id.label("item_id"),
            ranked.c.fidelity.label("fidelity"),
            ranked.c.collected_at.label("collected_at"),
            ranked.c.owner["name"].astext.label("owner_name"),
            ranked.c.owner["id"].astext.label("owner_id"),
            p_id.label("principal_id"),
            p_name.label("principal_name"),
            p_kind.label("principal_kind"),
            p_wk.label("well_known"),
            alias.canonical.label("principal_canonical"),
            alias.display.label("principal_display"),
            a["type"].astext.label("ace_type"),
            a["verbs"].label("verbs"),
            a["raw_mask"].astext.label("raw_mask"),
            inherited.label("inherited"),
            a["scope"].astext.label("scope"),
        )
        .select_from(ranked)
        .join(ace, literal(True))
        .join(Agent, Agent.id == ranked.c.agent_id)
        .outerjoin(alias, alias.alias == p_id)
        .where(ranked.c.rn == 1)
        # exclusion defaults (§4): explicit ACEs from non-well-known principals
        .where(or_(inherited.is_(None), inherited.is_(False)))
    )
    if broad_only:
        # a broad principal (Everyone / Authenticated Users / POSIX "other")
        # granted something beyond read
        broad = or_(
            p_wk.in_(("EVERYONE", "AUTHENTICATED_USERS", "USERS", "INTERACTIVE")),
            p_id.in_(("S-1-1-0", "S-1-5-11", "other")),
        )
        # JSONB @> '["write"]' etc. -- pass Python lists so psycopg serialises
        # a JSON ARRAY (a str would arrive as a JSON string and never match).
        # Verb tokens are the agent's normalized set (permissions.Verb): "full"
        # and "change_perms" (the earlier "full_control"/"change_permissions"
        # spellings never matched anything the collector emits; kept as
        # harmless extras in case a third-party producer uses them).
        writes = or_(
            a["verbs"].contains(["write"]),
            a["verbs"].contains(["delete"]),
            a["verbs"].contains(["full"]),
            a["verbs"].contains(["change_perms"]),
            a["verbs"].contains(["take_ownership"]),
            a["verbs"].contains(["full_control"]),
            a["verbs"].contains(["change_permissions"]),
        )
        stmt = stmt.where(broad, a["type"].astext == "allow", writes)
    else:
        stmt = stmt.where(or_(p_wk.is_(None), p_wk == ""))
    if outliers:
        # W7-T7 §4 outliers view (closes the last W7 scaffold, 2026-08-20): keep
        # only explicit ACEs that DEVIATE from the parent path's baseline — an
        # explicit entry that merely RESTATES an ACE the parent already carries
        # for the same (principal, type, verbs) is inheritance re-applied by a
        # tool (robocopy /SEC, icacls reset, cp -p ...), not a hand-made grant,
        # and it drowns the review list. Parent = the newest snapshot of the
        # path with the last /- or \-separated segment removed (agent-native
        # separators — both are handled; a drive root like "C:\" has no parent
        # match). No parent snapshot inventoried => baseline "unknown": the row
        # is KEPT (an explicit grant with no known baseline is still worth
        # review) and the baseline column says so.
        from sqlalchemy import column as _sacolumn2

        parent = _latest_snapshots_subq("ps_parent")
        parent_path = func.regexp_replace(ranked.c.path, r"[/\\]+[^/\\]+$", "")
        stmt = stmt.outerjoin(
            parent,
            and_(
                parent.c.agent_id == ranked.c.agent_id,
                parent.c.path == parent_path,
                parent.c.rn == 1,
            ),
        )
        pa = (
            func.jsonb_array_elements(parent.c.aces)
            .table_valued(_sacolumn2("value", _JSONB))
            .render_derived()
        )
        restates = (
            select(literal(1))
            .select_from(pa)
            .where(
                pa.c.value["principal"]["id"].astext == p_id,
                pa.c.value["type"].astext == a["type"].astext,
                pa.c.value["verbs"] == a["verbs"],
            )
            .exists()
        )
        stmt = stmt.where(or_(parent.c.id.is_(None), ~restates))
        stmt = stmt.add_columns(
            case((parent.c.id.is_(None), literal("unknown")), else_=literal("deviates")).label(
                "baseline"
            )
        )
    if params.library_id is not None or scope_clause is not None:
        stmt = stmt.join(Item, Item.id == ranked.c.item_id)
        if params.library_id is not None:
            stmt = stmt.where(Item.library_id == params.library_id)
        if scope_clause is not None:
            stmt = stmt.where(scope_clause)
    return stmt.order_by(Agent.name, ranked.c.path, a["order_index"].astext.cast(_SAInteger))


def _build_perm_by_principal(params: ReportParams) -> Select:
    return _perm_base(params)


def _scoped_perm_by_principal(params: ReportParams, scope_clause) -> Select:
    return _perm_base(params, scope_clause)


def _build_perm_broad(params: ReportParams) -> Select:
    return _perm_base(params, broad_only=True)


def _scoped_perm_broad(params: ReportParams, scope_clause) -> Select:
    return _perm_base(params, scope_clause, broad_only=True)


def _build_perm_outliers(params: ReportParams) -> Select:
    return _perm_base(params, outliers=True)


def _scoped_perm_outliers(params: ReportParams, scope_clause) -> Select:
    return _perm_base(params, scope_clause, outliers=True)


def _row_perm(r: Any) -> dict:
    verbs = r.verbs if isinstance(r.verbs, list) else (json.loads(r.verbs) if r.verbs else [])
    return {
        "agent": r.agent,
        "path": r.path,
        "is_dir": bool(r.is_dir),
        "owner": r.owner_name or r.owner_id or "",
        # canonical identity first (W7-T8 alias), else the raw resolution
        "principal": r.principal_display
        or r.principal_canonical
        or r.principal_name
        or r.principal_id
        or "",
        "canonical_id": r.principal_canonical,
        "principal_id": r.principal_id or "",
        "kind": r.principal_kind or "",
        "ace_type": r.ace_type or "",
        "verbs": ",".join(str(v) for v in verbs),
        "raw_mask": r.raw_mask or "",
        "scope": r.scope or "",
        "fidelity": r.fidelity or "",
        "collected_at": r.collected_at.isoformat() if r.collected_at else "",
        "item_id": str(r.item_id) if r.item_id else None,
    }


_PERM_COLUMNS = (
    "agent", "path", "is_dir", "owner", "principal", "canonical_id", "principal_id",
    "kind", "ace_type", "verbs", "raw_mask", "scope", "fidelity", "collected_at",
)


def _row_perm_outlier(r: Any) -> dict:
    return {**_row_perm(r), "baseline": r.baseline}


_PERM_OUTLIER_COLUMNS = _PERM_COLUMNS + ("baseline",)


# --- W7-T9 (2026-08-19): permission drift -----------------------------------
# Consecutive snapshot PAIRS per (agent, path) via LAG; the stored rows are
# already digest-gated (an unchanged re-collection writes nothing), so every
# pair IS a change. The ACE/owner/group diff itself is computed row-side with
# the pure ``permissions.diff_records`` engine (JSONB set-diff in SQL would be
# unreadable and could not apply the same key/material rules).
def _changes_base(params: ReportParams, scope_clause=None) -> Select:
    from datetime import timedelta

    lagged = (
        select(
            PermissionSnapshot.id.label("id"),
            PermissionSnapshot.agent_id.label("agent_id"),
            PermissionSnapshot.path.label("path"),
            PermissionSnapshot.is_dir.label("is_dir"),
            PermissionSnapshot.item_id.label("item_id"),
            PermissionSnapshot.collected_at.label("collected_at"),
            PermissionSnapshot.owner.label("owner"),
            PermissionSnapshot.group_.label("group_"),
            PermissionSnapshot.aces.label("aces"),
            PermissionSnapshot.fidelity.label("fidelity"),
            PermissionSnapshot.posture.label("posture"),
            func.lag(PermissionSnapshot.collected_at)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_collected_at"),
            func.lag(PermissionSnapshot.owner)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_owner"),
            func.lag(PermissionSnapshot.group_)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_group"),
            func.lag(PermissionSnapshot.aces)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_aces"),
            func.lag(PermissionSnapshot.fidelity)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_fidelity"),
            func.lag(PermissionSnapshot.posture)
            .over(
                partition_by=(PermissionSnapshot.agent_id, PermissionSnapshot.path),
                order_by=(PermissionSnapshot.collected_at, PermissionSnapshot.id),
            )
            .label("prev_posture"),
        )
    ).subquery("ps_lag")
    stmt = (
        select(
            lagged.c.id,
            lagged.c.agent_id,
            Agent.name.label("agent"),
            lagged.c.path,
            lagged.c.is_dir,
            lagged.c.item_id,
            lagged.c.collected_at,
            lagged.c.owner,
            lagged.c.group_,
            lagged.c.aces,
            lagged.c.fidelity,
            lagged.c.posture,
            lagged.c.prev_collected_at,
            lagged.c.prev_owner,
            lagged.c.prev_group,
            lagged.c.prev_aces,
            lagged.c.prev_fidelity,
            lagged.c.prev_posture,
        )
        .select_from(lagged)
        .join(Agent, Agent.id == lagged.c.agent_id)
        .where(lagged.c.prev_collected_at.is_not(None))
    )
    # threshold_days: "changes in the last N days"; None => the report default.
    days = params.threshold_days if params.threshold_days is not None else 30
    if days > 0:
        stmt = stmt.where(lagged.c.collected_at >= datetime.now(UTC) - timedelta(days=days))
    if params.library_id is not None or scope_clause is not None:
        stmt = stmt.join(Item, Item.id == lagged.c.item_id)
        if params.library_id is not None:
            stmt = stmt.where(Item.library_id == params.library_id)
        if scope_clause is not None:
            stmt = stmt.where(scope_clause)
    # id (uuidv7, time-ordered) breaks collected_at ties the same way LAG did
    return stmt.order_by(
        lagged.c.collected_at.desc(), lagged.c.id.desc(), Agent.name, lagged.c.path
    )


def _build_perm_changes(params: ReportParams) -> Select:
    return _changes_base(params)


def _scoped_perm_changes(params: ReportParams, scope_clause) -> Select:
    return _changes_base(params, scope_clause)


def _row_perm_change(r: Any) -> dict:
    from filearr.permissions import diff_records, record_from_wire, summarize_diff

    old = record_from_wire(
        owner=r.prev_owner, group=r.prev_group, aces=r.prev_aces,
        fidelity=r.prev_fidelity, posture=r.prev_posture,
    )
    new = record_from_wire(
        owner=r.owner, group=r.group_, aces=r.aces, fidelity=r.fidelity, posture=r.posture,
    )
    diff = diff_records(old, new)
    details = summarize_diff(diff)
    if not details:
        # digest differed but no ACE/owner/group delta: fidelity/posture only
        if (r.prev_fidelity or "") != (r.fidelity or ""):
            details = f"fidelity {r.prev_fidelity or '?'} → {r.fidelity or '?'}"
        else:
            details = "posture changed"
    fmt = lambda p: (p.display if p and p.display else (p.canonical_id if p else "")) or ""  # noqa: E731
    return {
        "agent": r.agent,
        "path": r.path,
        "is_dir": bool(r.is_dir),
        "changed_at": r.collected_at.isoformat() if r.collected_at else "",
        "previous_at": r.prev_collected_at.isoformat() if r.prev_collected_at else "",
        "owner_before": fmt(old.owner),
        "owner_after": fmt(new.owner),
        "added": len(diff.added),
        "removed": len(diff.removed),
        "modified": len(diff.modified),
        "details": details,
        "fidelity": r.fidelity or "",
        "item_id": str(r.item_id) if r.item_id else None,
    }


# --- 2026-08-20 library health digest ----------------------------------------
def _build_library_health(params: ReportParams) -> Select:
    """One row per library: the at-a-glance health view (scan trouble + the two
    hygiene signals + extract errors). Every count is a correlated scalar
    subquery over an indexed column set — one round trip, no N+1."""
    from datetime import timedelta

    now = datetime.now(UTC)
    items_active = (
        select(func.count())
        .select_from(Item)
        .where(Item.library_id == Library.id, Item.status == ItemStatus.active)
        .scalar_subquery()
    )
    unlinked_sidecars = (
        select(func.count())
        .select_from(Item)
        .where(
            Item.library_id == Library.id,
            Item.status == ItemStatus.active,
            Item.sidecar_of.is_(None),
            _sidecar_shape_predicate(),
        )
        .scalar_subquery()
    )
    empty_files = (
        select(func.count())
        .select_from(Item)
        .where(
            Item.library_id == Library.id,
            Item.status == ItemStatus.active,
            Item.size == 0,
        )
        .scalar_subquery()
    )
    missing_items = (
        select(func.count())
        .select_from(Item)
        .where(Item.library_id == Library.id, Item.status == ItemStatus.missing)
        .scalar_subquery()
    )
    extract_errors = (
        select(func.count())
        .select_from(Item)
        .where(
            Item.library_id == Library.id,
            Item.status == ItemStatus.active,
            Item.metadata_.has_key("_extract_error"),
        )
        .scalar_subquery()
    )
    last_scan_status = (
        select(ScanRun.status)
        .where(ScanRun.library_id == Library.id)
        .order_by(ScanRun.started_at.desc())
        .limit(1)
        .scalar_subquery()
    )
    last_scan_at = (
        select(func.max(ScanRun.started_at))
        .where(ScanRun.library_id == Library.id)
        .scalar_subquery()
    )
    last_success_at = (
        select(func.max(ScanRun.started_at))
        # A scan SUCCEEDS as "finished" (full walk) or "stopped" (graceful stop
        # kept its progress) -- both walked cleanly. "completed" is a ROLLOUT
        # status, never a ScanRun one, so it matched nothing and this column read
        # "" for every library (fixed 2026-08-20).
        .where(
            ScanRun.library_id == Library.id,
            ScanRun.status.in_(("finished", "stopped")),
        )
        .scalar_subquery()
    )
    failed_7d = (
        select(func.count())
        .select_from(ScanRun)
        .where(
            ScanRun.library_id == Library.id,
            ScanRun.status == "failed",
            ScanRun.started_at >= now - timedelta(days=7),
        )
        .scalar_subquery()
    )
    stmt = select(
        Library.id.label("library_id"),
        Library.name.label("library"),
        Library.source_agent_id.label("source_agent_id"),
        items_active.label("items"),
        missing_items.label("missing_items"),
        last_scan_status.label("last_scan_status"),
        last_scan_at.label("last_scan_at"),
        last_success_at.label("last_success_at"),
        failed_7d.label("failed_scans_7d"),
        unlinked_sidecars.label("unlinked_sidecars"),
        empty_files.label("empty_files"),
        extract_errors.label("extract_errors"),
    )
    if params.library_id is not None:
        stmt = stmt.where(Library.id == params.library_id)
    return stmt.order_by(Library.name)


def _row_library_health(r: Any) -> dict:
    # A one-word verdict so the digest e-mail's first column says it all.
    agent_owned = r.source_agent_id is not None
    problems = []
    if not agent_owned:
        if r.last_scan_status is None:
            problems.append("never scanned")
        elif r.last_scan_status == "failed":
            problems.append("last scan FAILED")
        if (r.failed_scans_7d or 0) >= 3:
            problems.append(f"{r.failed_scans_7d} failed scans in 7d")
    if (r.unlinked_sidecars or 0) > 0:
        problems.append(f"{r.unlinked_sidecars} unlinked sidecars")
    if (r.empty_files or 0) > 0:
        problems.append(f"{r.empty_files} empty files")
    if (r.extract_errors or 0) > 0:
        problems.append(f"{r.extract_errors} extract errors")
    return {
        "library": r.library,
        "status": "OK" if not problems else "; ".join(problems),
        "kind": "agent" if agent_owned else "central",
        "items": int(r.items or 0),
        "missing_items": int(r.missing_items or 0),
        "last_scan_status": r.last_scan_status or ("n/a" if agent_owned else "never"),
        "last_scan_at": r.last_scan_at.isoformat() if r.last_scan_at else "",
        "last_success_at": r.last_success_at.isoformat() if r.last_success_at else "",
        "failed_scans_7d": int(r.failed_scans_7d or 0),
        "unlinked_sidecars": int(r.unlinked_sidecars or 0),
        "empty_files": int(r.empty_files or 0),
        "extract_errors": int(r.extract_errors or 0),
    }


# --- 2026-08-20 hygiene reports (user request: empty files + low-info sidecars)
# Whole-file sidecar extensions (filearr.sidecar._ALWAYS_SIDECAR_EXTS /
# _STEM_SIDECAR_EXTS). Kept as a fallback name; the full shape match below also
# covers directory/stem artwork + JRiver, which a bare extension list misses.
_SIDECAR_EXTS = ("nfo", "xmp", "thm")

# A SQL predicate mirroring filearr.sidecar.classify's PATH SHAPE, so the hygiene
# reports flag the SAME files the scan links (or fails to link). A bare extension
# list missed the dominant pollution class — directory artwork (poster.jpg,
# folder.jpg, ...) and stem-suffixed artwork (Movie-thumb.jpg) — which are jpg/png
# and can't be matched by extension alone (that would flag every real photo). The
# constants are imported from the sidecar module so the two never drift.


def _sidecar_shape_predicate():
    """OR of: whole-file sidecar extensions; directory-artwork filenames; JRiver
    ``*_JRSidecar.xml``; stem-suffixed artwork (``-thumb``/``-poster``/... over an
    image extension). Case-insensitive on the filename."""
    from filearr.sidecar import _ART_EXTS, _DIR_ARTWORK_NAMES, _STEM_SUFFIXES

    fname = func.lower(Item.filename)
    suffixes = "|".join(s.lstrip("-") for s in _STEM_SUFFIXES)
    exts = "|".join(e.lstrip(".") for e in _ART_EXTS)
    stem_art_re = rf"-({suffixes})\.({exts})$"
    return or_(
        Item.extension.in_(_SIDECAR_EXTS),
        fname.in_(sorted(_DIR_ARTWORK_NAMES)),
        fname.like("%\\_jrsidecar.xml", escape="\\"),
        fname.op("~")(stem_art_re),
    )


def _build_empty_files(params: ReportParams) -> Select:
    stmt = (
        select(
            Library.name.label("library"),
            Item.id.label("item_id"),
            Item.rel_path.label("rel_path"),
            Item.extension.label("extension"),
            Item.file_group.label("file_group"),
            Item.mtime.label("mtime"),
            (Item.sidecar_of.is_not(None)).label("is_sidecar"),
        )
        .join(Library, Library.id == Item.library_id)
        .where(Item.status == ItemStatus.active, Item.size == 0)
    )
    if params.library_id is not None:
        stmt = stmt.where(Item.library_id == params.library_id)
    return stmt.order_by(Library.name, Item.rel_path)


def _row_empty_file(r: Any) -> dict:
    return {
        "library": r.library,
        "rel_path": r.rel_path,
        "extension": r.extension or "",
        "file_group": r.file_group or "",
        "is_sidecar": bool(r.is_sidecar),
        "mtime": r.mtime.isoformat() if r.mtime else "",
        "item_id": str(r.item_id),
    }


def _build_sidecar_hygiene(params: ReportParams) -> Select:
    """Sidecar-shaped rows worth attention, one ``issue`` per row:

    * ``unlinked`` — a sidecar-extension file with NO parent link. It behaves
      like a first-class item (visible in search/timeline) either because the
      library has not had a successful scan/association pass since it landed,
      or because no plausible parent exists next to it.
    * ``empty`` / ``tiny`` — a LINKED sidecar of 0 / <= 64 bytes: it contributes
      no metadata or artwork and is safe to clean up at the source."""
    issue = case(
        (Item.sidecar_of.is_(None), literal("unlinked")),
        (Item.size == 0, literal("empty")),
        else_=literal("tiny"),
    )
    stmt = (
        select(
            Library.name.label("library"),
            Item.id.label("item_id"),
            Item.rel_path.label("rel_path"),
            Item.extension.label("extension"),
            Item.size.label("size"),
            issue.label("issue"),
            Item.mtime.label("mtime"),
        )
        .join(Library, Library.id == Item.library_id)
        .where(
            Item.status == ItemStatus.active,
            or_(
                # unlinked sidecar-shaped rows (full classify shape, not just ext)
                and_(Item.sidecar_of.is_(None), _sidecar_shape_predicate()),
                # linked but content-free
                and_(Item.sidecar_of.is_not(None), Item.size <= 64),
            ),
        )
    )
    if params.library_id is not None:
        stmt = stmt.where(Item.library_id == params.library_id)
    return stmt.order_by(issue, Library.name, Item.rel_path)


def _row_sidecar_hygiene(r: Any) -> dict:
    return {
        "library": r.library,
        "rel_path": r.rel_path,
        "extension": r.extension or "",
        "size": int(r.size or 0),
        "issue": r.issue,
        "mtime": r.mtime.isoformat() if r.mtime else "",
        "item_id": str(r.item_id),
    }


_PERM_CHANGE_COLUMNS = (
    "agent", "path", "is_dir", "changed_at", "previous_at", "owner_before", "owner_after",
    "added", "removed", "modified", "details", "fidelity",
)

_REPORTS = _REPORTS + (
    CannedReport(
        id="permissions_by_principal",
        title="Permissions: explicit grants by principal",
        description=(
            "Every EXPLICIT (non-inherited) allow/deny entry from a non-system "
            "principal on paths an agent has inventoried with the 'permissions' "
            "collector -- one row per ACE, newest snapshot per path. Well-known "
            "principals (SYSTEM, Administrators, root, ...) and inherited entries "
            "are hidden so what remains is the meaningful, hand-granted access. "
            "'fidelity' says how much to trust the row: synthesized_from_mode means "
            "a cifs mount without cifsacl -- mount options, not the server ACL. "
            "Owner/group are always shown. Read agent-side; central stores the "
            "normalized record verbatim (raw_mask is the native mask)."
        ),
        columns=_PERM_COLUMNS,
        build=_build_perm_by_principal,
        row=_row_perm,
        supports_library=True,
        scoped_build=_scoped_perm_by_principal,
        default_limit=1000,
    ),
    CannedReport(
        id="permissions_broad_access",
        title="Permissions: broad write access",
        description=(
            "Paths where a BROAD principal -- Everyone, Authenticated Users, "
            "Users, or POSIX 'other' -- holds an explicit (non-inherited) allow "
            "with write, delete, change-permissions or full control. The "
            "'world-writable' review list. Newest snapshot per path; same "
            "fidelity caveat as the by-principal report."
        ),
        columns=_PERM_COLUMNS,
        build=_build_perm_broad,
        row=_row_perm,
        supports_library=True,
        scoped_build=_scoped_perm_broad,
        default_limit=1000,
    ),
    CannedReport(
        id="permissions_explicit_outliers",
        title="Permissions: explicit outliers vs parent",
        description=(
            "Explicit (non-inherited) ACEs that DEVIATE from the parent "
            "directory's baseline -- the by-principal list minus entries that "
            "merely restate an ACE the parent already carries for the same "
            "principal, type and verbs (inheritance re-applied by robocopy "
            "/SEC, icacls reset, cp -p and friends). What remains is the "
            "hand-granted deviation an audit actually cares about. 'baseline' "
            "is 'deviates' when the parent path has a snapshot to compare "
            "against, 'unknown' when it does not (inventory the parent "
            "directory too for a definitive verdict). Same well-known/"
            "inherited exclusions and fidelity caveat as the by-principal "
            "report."
        ),
        columns=_PERM_OUTLIER_COLUMNS,
        build=_build_perm_outliers,
        row=_row_perm_outlier,
        supports_library=True,
        scoped_build=_scoped_perm_outliers,
        default_limit=1000,
    ),
    CannedReport(
        id="permission_changes",
        title="Permissions: changes (drift)",
        description=(
            "What changed between consecutive permission snapshots of the same "
            "path -- one row per change event, newest first: ACEs added (+), "
            "removed (-) and modified (~ before -> after), plus owner/group "
            "changes. Only paths an agent re-inventoried with the 'permissions' "
            "collector appear; an unchanged re-collection writes no snapshot, so "
            "every row here is real drift. Pair it with the 'System: permission "
            "change' alert rule for push notification. The threshold input "
            "limits rows to changes in the last N days (default 30; retention "
            "is FILEARR_PERMISSION_SNAPSHOTS_RETAIN snapshots per path)."
        ),
        columns=_PERM_CHANGE_COLUMNS,
        build=_build_perm_changes,
        row=_row_perm_change,
        supports_library=True,
        scoped_build=_scoped_perm_changes,
        default_limit=1000,
        supports_threshold=True,
        threshold_label="Changes in the last (days)",
        default_threshold_days=30,
    ),
    CannedReport(
        id="library_health",
        title="Library health digest",
        description=(
            "One row per library with a one-word verdict and the signals "
            "behind it: last scan status and age, failed scans in the last 7 "
            "days, active/missing item counts, unlinked sidecars, empty files "
            "and extract errors. The at-a-glance view (and the natural weekly "
            "scheduled e-mail); each count has a detail report -- "
            "sidecar_hygiene, empty_files, and the Jobs/Libraries pages."
        ),
        columns=("library", "status", "kind", "items", "missing_items",
                 "last_scan_status", "last_scan_at", "last_success_at",
                 "failed_scans_7d", "unlinked_sidecars", "empty_files",
                 "extract_errors"),
        build=_build_library_health,
        row=_row_library_health,
        supports_library=True,
        default_limit=1000,
    ),
    CannedReport(
        id="empty_files",
        title="Empty files",
        description=(
            "Every active zero-byte file (sidecars flagged). Zero-byte files "
            "carry no content, can never be content-hashed, and are excluded "
            "from the duplicate reports -- this is the cleanup list. Common "
            "sources: interrupted copies, placeholder exports, touch artifacts."
        ),
        columns=("library", "rel_path", "extension", "file_group", "is_sidecar",
                 "mtime", "item_id"),
        build=_build_empty_files,
        row=_row_empty_file,
        supports_library=True,
        row_link="item",
        default_limit=1000,
    ),
    CannedReport(
        id="sidecar_hygiene",
        title="Sidecars: unlinked / empty",
        description=(
            "Sidecar-shaped files that need attention. 'unlinked' = a "
            ".nfo/.xmp/.thm with no parent link -- it behaves like a "
            "first-class item (visible in search and the timeline) because the "
            "library has not completed a scan/association pass since it "
            "landed, or no plausible parent sits next to it. 'empty'/'tiny' = "
            "a linked sidecar of 0 / <=64 bytes contributing no metadata or "
            "artwork -- safe to delete at the source. A large 'unlinked' count "
            "usually means: fix the library's failing scan, then rescan."
        ),
        columns=("library", "rel_path", "extension", "size", "issue", "mtime", "item_id"),
        build=_build_sidecar_hygiene,
        row=_row_sidecar_hygiene,
        supports_library=True,
        row_link="item",
        default_limit=1000,
    ),
)

CANNED_REPORTS: dict[str, CannedReport] = {r.id: r for r in _REPORTS}


def list_reports() -> list[dict]:
    """Registry listing (metadata only, no query executed)."""
    return [r.meta() for r in _REPORTS]


def get_report(report_id: str) -> CannedReport | None:
    return CANNED_REPORTS.get(report_id)


async def stream_report_rows(
    session: AsyncSession,
    report: CannedReport,
    params: ReportParams,
    scope_clause=None,
) -> AsyncIterator[dict]:
    """Yield serialized rows off a server-side cursor (memory ~ one row).

    Applies the report's ``post_filter`` and, for capped reports, the top-N
    ``limit`` — so an export of ``largest_files`` streams only the top N and a
    ``low_quality_video`` export streams only review-band-and-above rows, all
    without materialising the full result set.

    ``scope_clause`` (P6-T4) is an optional RBAC ``WHERE`` predicate over
    ``items.path_scope``: a scoped principal's report/export never surfaces a row
    they cannot read. It is applied BEFORE any grouping/limit, so a denied item
    neither appears nor contributes to an aggregate (e.g. a duplicate group) —
    :meth:`CannedReport.statement` handles both the plain outer-``where`` case and
    the pushed-down ``scoped_build`` case (IN-T1)."""
    stmt = report.statement(params, scope_clause)
    if report.is_capped:
        stmt = stmt.limit(params.limit)
    result = await session.stream(stmt.execution_options(yield_per=YIELD_PER))
    async for row in result:
        d = report.row(row)
        if report.post_filter is not None and not report.post_filter(d):
            continue
        yield d


# --------------------------------------------------------------------------- #
# Shared streaming serializers (P11-T4): one row iterator -> csv / ndjson / xml #
# --------------------------------------------------------------------------- #
async def render_rows(
    fmt: str,
    columns: list[str],
    rows: AsyncIterator[dict],
    *,
    report_id: str,
    generated: str | None = None,
) -> AsyncIterator[str]:
    """Serialize an async row iterator into a chosen STREAMING export format.

    * ``csv`` — the report's ``columns`` only (so ``item_id`` never leaks into a
      spreadsheet), every cell formula-injection-guarded (:func:`csv_safe`).
    * ``ndjson`` — one compact JSON object per line (the full row dict, including
      ``item_id`` and any extra keys); ingestion-friendly, streams unbounded.
    * ``xml`` — a flat ``<report><row><col name=…>…`` document with a declared
      UTF-8 encoding; EVERY name and value is escaped, so a hostile filename
      (``<>&"'``) can never break well-formedness.

    All three are single-pass generators: peak memory is ~one row regardless of
    result size."""
    if fmt == "csv":
        buf = io.StringIO()
        writer = csv.writer(buf)
        writer.writerow(columns)
        yield buf.getvalue()
        buf.seek(0)
        buf.truncate(0)
        async for d in rows:
            writer.writerow([csv_safe(d.get(c)) for c in columns])
            yield buf.getvalue()
            buf.seek(0)
            buf.truncate(0)
        return

    if fmt == "ndjson":
        async for d in rows:
            yield json.dumps(d, separators=(",", ":"), default=str) + "\n"
        return

    if fmt == "xml":
        stamp = generated or datetime.now(UTC).isoformat()
        yield '<?xml version="1.0" encoding="UTF-8"?>\n'
        yield f"<report id={_xml_quoteattr(report_id)} generated={_xml_quoteattr(stamp)}>\n"
        async for d in rows:
            parts = ["  <row>"]
            for key, value in d.items():
                name = _xml_quoteattr(key)
                if value is None:
                    parts.append(f"<col name={name}/>")
                else:
                    parts.append(f"<col name={name}>{_xml_escape(str(value))}</col>")
            parts.append("</row>\n")
            yield "".join(parts)
        yield "</report>\n"
        return

    raise ValueError(f"unknown export format {fmt!r}")


# --------------------------------------------------------------------------- #
# XLSX export (P11-T4 remainder): xlsxwriter constant_memory, formula-guarded  #
# --------------------------------------------------------------------------- #
async def render_xlsx_to_path(
    columns: list[str],
    rows: AsyncIterator[dict],
    path: str,
    *,
    sheet_name: str = "report",
    cap: int | None = None,
) -> int:
    """Stream ``rows`` into an ``.xlsx`` workbook at ``path``; return the row count.

    Bounded memory: ``xlsxwriter.Workbook(path, {'constant_memory': True})`` flushes
    each row to a temp file as the next is written, so peak memory is ~one row
    regardless of total rows (research §6.1) — the same guarantee the text
    streaming formats have. Rows MUST be written top-to-bottom in order (they are).

    **Formula-injection guard (the xlsx equivalent of the CSV leading-quote):**
    the workbook is opened with ``strings_to_formulas=False`` and
    ``strings_to_numbers=False`` and EVERY cell is written with ``write_string``,
    so a catalog value like ``=SUM(A1)`` or ``+cmd`` is stored as a LITERAL string
    and never evaluated as a formula or coerced to a number. Only the report's
    declared ``columns`` are written (``item_id`` never leaks into a spreadsheet),
    mirroring the CSV serializer.
    """
    import xlsxwriter  # local import: only the xlsx path pays the import cost

    wb = xlsxwriter.Workbook(
        path,
        {
            "constant_memory": True,
            "strings_to_formulas": False,
            "strings_to_numbers": False,
            "strings_to_urls": False,
            "in_memory": False,
        },
    )
    ws = wb.add_worksheet(sheet_name[:31] or "report")
    try:
        for col_idx, name in enumerate(columns):
            ws.write_string(0, col_idx, str(name))
        n = 0
        async for d in rows:
            if cap is not None and n >= cap:
                break
            r = n + 1
            for col_idx, name in enumerate(columns):
                v = d.get(name)
                ws.write_string(r, col_idx, "" if v is None else str(v))
            n += 1
    finally:
        wb.close()
    return n
