"""Search endpoint — flat typed params translated to Meilisearch filter syntax,
opaque cursor pagination wrapping the engine's offset model."""

import base64
import json
import logging
import math
import re
import uuid

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from meilisearch_python_sdk.errors import MeilisearchApiError
from meilisearch_python_sdk.models.search import Federation, Hybrid, SearchParams

from filearr.chunking import chunks_index_uid
from filearr.config import get_settings
from filearr.embed import embed_query
from filearr.file_groups import FILE_CATEGORIES, FILE_GROUPS
from filearr.meili_ops import DEFAULT_EMBEDDER_NAME
from filearr.schemas import FederatedHit, FederatedSearchResponse, SearchResponse
from filearr.search import client
from filearr.security import require_search_scope

router = APIRouter()

log = logging.getLogger("filearr.search")

PAGE_SIZE = 50


def _is_facet_unavailable(err: MeilisearchApiError) -> bool:
    """True when a Meili search failed only because a REQUESTED facet is not (yet)
    filterable on the index.

    This is the transient window after a new facet-able attribute is added to
    ``FILTERABLE_ATTRIBUTES``: ``ensure_index()`` applies the setting at boot, but
    Meilisearch re-indexes the whole corpus before the attribute becomes
    facetable, and on a large index that takes minutes. A facet request during
    that window must degrade (drop facet COUNTS) rather than 500 the entire search
    — losing counts briefly is fine; losing all results is a deploy blocker
    (regression found live 2026-07-18 when ``file_group`` shipped)."""
    code = getattr(err, "code", "") or ""
    return code == "invalid_search_facets" or "not filterable" in str(err).lower()

# Facets requested on every search. ``size``/``mtime`` are here only so Meili
# returns their ``facetStats`` (min/max) — the P3-T4 range sliders derive their
# bounds from those stats, never from hardcoded constants. Both are numeric and
# facet-search-disabled (meili_ops.FACET_SEARCH_DISABLED), so requesting them as
# facets is cheap: we consume the stats, not the (near-unique) value distribution.
FACETS = [
    "file_category",
    "file_group",
    "extension",
    "year",
    "tags",
    "status",
    "is_sidecar",
    "size",
    "mtime",
]

# P3-T5 highlighting/cropping. ``body_text`` (document body) is cropped to a short
# window around the match for the result snippet; ``title``/``filename`` get inline
# highlight markers. Meili wraps matches in the pre/post tags below; we pass the
# whole ``_formatted`` block through to the frontend as ``highlight`` (title/
# filename) + ``snippet`` (cropped body). The frontend renders these SAFELY (text
# nodes + <mark>, never {@html}) — the tags are the ONLY markup that ever appears.
HIGHLIGHT_ATTRS = ["body_text", "title", "filename"]
CROP_ATTRS = ["body_text"]
CROP_LENGTH = 30  # words of context around a body match (a snippet, not a page)
HIGHLIGHT_PRE = "<em>"
HIGHLIGHT_POST = "</em>"


def _encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"o": offset}).encode()).decode()


def _decode_cursor(cursor: str | None) -> int:
    if not cursor:
        return 0
    try:
        return int(json.loads(base64.urlsafe_b64decode(cursor)).get("o", 0))
    except Exception:
        return 0


# P3-T1: a content hash is a lowercase-hex string of 8..64 chars (a truncated
# xxh3 quick_hash up to a full sha256/xxh128). Validated BEFORE the value is ever
# interpolated into a Meili filter string (defense in depth alongside the
# endpoint's ``Query(pattern=...)`` — no user input reaches a filter unchecked).
HASH_RE = re.compile(r"^[0-9a-f]{8,64}$")


def meili_quote(value: str) -> str:
    """Escape a string for interpolation inside a SINGLE-QUOTED Meilisearch
    filter literal.

    SECURITY (2026-08-20): the free-string filter params (``library``,
    ``status``, ``extension``, ``tags``, ``sidecar_of``) used to be interpolated
    raw. Because clauses are joined with ``" AND "`` and Meili binds ``AND``
    tighter than ``OR``, a value like ``active' OR status = 'active`` dangled an
    ``OR`` outside the AND-chain and matched every document — bypassing the
    RBAC ``scope_filter`` that is appended last. Escaping the backslash first
    (so it can't neutralise the quote escape) then the single quote makes the
    value an inert string literal: an embedded quote can never terminate the
    literal and start new filter syntax. Meili's grammar unescapes ``\\'`` back
    to a literal quote, so a legitimate tag/filename containing an apostrophe
    still matches exactly."""
    return value.replace("\\", "\\\\").replace("'", "\\'")


def build_filters(
    *,
    file_category: list[str] | None = None,
    file_group: list[str] | None = None,
    library: str | None = None,
    status: str | None = "active",
    extension: str | None = None,
    year_gte: int | None = None,
    year_lte: int | None = None,
    size_gte: int | None = None,
    size_lte: int | None = None,
    mtime_gte: int | None = None,
    mtime_lte: int | None = None,
    tags: str | None = None,
    sidecar_of: str | None = None,
    include_sidecars: bool = False,
    hash: str | None = None,
) -> list[str]:
    """Build Meilisearch filter clauses. Sidecars are excluded by default (T3);
    an explicit ``sidecar_of`` or ``include_sidecars`` opts them back in. A
    ``hash`` adds an exact ``quick_hash``/``content_hash`` OR match (P3-T1). The
    ``size_*``/``mtime_*`` bounds (P3-T4) become numeric range filters — ``size``
    in bytes, ``mtime`` in epoch seconds (matching build_doc's projection)."""
    filters: list[str] = []
    if hash is not None:
        if not HASH_RE.match(hash):
            # Should be unreachable via the endpoint (Query validates first); a
            # hard guard so a direct caller cannot inject filter syntax.
            raise ValueError("hash must be 8-64 lowercase hex chars")
        # Exact match against either digest column; typo tolerance is disabled on
        # both (HASH_ATTRIBUTES), so a one-hex-digit difference matches nothing.
        filters.append(f"(quick_hash = '{hash}' OR content_hash = '{hash}')")
    if file_category:
        # W8-A: repeatable => OR (a file has exactly one category). Every value is
        # validated against the controlled ``FILE_CATEGORIES`` vocabulary BEFORE it
        # is interpolated (same injection-safe posture as ``file_group``/``hash``).
        # Unknown values are dropped.
        valid_cats = [c for c in file_category if c in FILE_CATEGORIES]
        if valid_cats:
            filters.append(
                "(" + " OR ".join(f"file_category = '{c}'" for c in valid_cats) + ")"
            )
    if file_group:
        # Repeatable => OR semantics (a file has exactly one group). Every value is
        # validated against the controlled ``FILE_GROUPS`` vocabulary BEFORE it is
        # interpolated, so no user input reaches the Meili filter unchecked (same
        # defense-in-depth posture as the hash filter). Unknown values are dropped.
        valid = [g for g in file_group if g in FILE_GROUPS]
        if valid:
            filters.append(
                "(" + " OR ".join(f"file_group = '{g}'" for g in valid) + ")"
            )
    if library:
        filters.append(f"library_id = '{meili_quote(library)}'")
    if status:
        filters.append(f"status = '{meili_quote(status)}'")
    if extension:
        filters.append(f"extension = '{meili_quote(extension)}'")
    if year_gte is not None:
        filters.append(f"year >= {year_gte}")
    if year_lte is not None:
        filters.append(f"year <= {year_lte}")
    # P3-T4 numeric range filters. Values are ints (FastAPI-coerced), so the
    # interpolation is injection-safe; ``size`` is bytes, ``mtime`` epoch seconds.
    if size_gte is not None:
        filters.append(f"size >= {int(size_gte)}")
    if size_lte is not None:
        filters.append(f"size <= {int(size_lte)}")
    if mtime_gte is not None:
        filters.append(f"mtime >= {int(mtime_gte)}")
    if mtime_lte is not None:
        filters.append(f"mtime <= {int(mtime_lte)}")
    if tags:
        filters.extend(f"tags = '{meili_quote(t.strip())}'" for t in tags.split(","))
    if sidecar_of:
        # Explicitly requesting a parent's sidecars implies including sidecars.
        filters.append(f"sidecar_of = '{meili_quote(sidecar_of)}'")
    elif not include_sidecars:
        # T3 default: hide sidecar files (episode .nfo/thumb, poster.jpg, ...).
        filters.append("is_sidecar = false")
    return filters


# --------------------------------------------------------------------------- #
# R8 (roadmap §8 "geo filters (photo GPS)") — geo filter/sort compilation.      #
#                                                                               #
# These build Meilisearch's geo FILTER FUNCTIONS over the reserved ``_geo``      #
# attribute. They are pure and injection-safe by construction: every value is    #
# range-checked and re-formatted as a fixed-precision decimal, so nothing a      #
# caller typed is ever interpolated verbatim into a filter expression (the same  #
# posture as the hash / file_group filters above).                              #
#                                                                               #
# The GPS EXPOSURE GATE IS NOT HERE and must never be moved here. Whether a      #
# document has a ``_geo`` point at all is decided at PROJECTION time by          #
# ``search.build_doc`` from the per-library ``Library.expose_gps`` flag          #
# (CWE-1230, P3-T11; the pure key filter is ``exif.strip_gps``). Consequently a  #
# geo query on a deployment where no library exposes GPS is not an error — it    #
# filters over an attribute no document carries and returns zero hits. That      #
# silence is the honest answer: erroring would itself disclose the configuration.#
# --------------------------------------------------------------------------- #


class GeoParamError(ValueError):
    """A geo parameter combination that cannot be honoured as written.

    Raised for out-of-range coordinates, partially-specified centres/boxes and
    inverted bounding boxes. The endpoint maps it to HTTP 422: a nonsensical geo
    query is REFUSED, never silently normalised, because quietly "fixing" a
    swapped bounding box would return results for a region the caller never asked
    about and they would have no way to tell."""


#: Fixed-precision coordinate rendering. 7 decimals is ~1 cm at the equator —
#: far beyond any consumer GPS — and avoids Python's scientific notation for very
#: small magnitudes (``1e-07``), which Meili's filter parser would not accept.
_COORD_FMT = "{:.7f}"
#: Metres, rendered the same way (3 decimals = mm) for the same parser reasons.
_METRES_FMT = "{:.3f}"


def _check_lat(value: float, name: str) -> float:
    if not -90.0 <= value <= 90.0:
        raise GeoParamError(f"{name} must be a latitude in [-90, 90] (got {value})")
    return value


def _check_lng(value: float, name: str) -> float:
    if not -180.0 <= value <= 180.0:
        raise GeoParamError(f"{name} must be a longitude in [-180, 180] (got {value})")
    return value


def build_geo_filters(
    *,
    geo_lat: float | None = None,
    geo_lng: float | None = None,
    geo_radius_m: float | None = None,
    geo_top_lat: float | None = None,
    geo_right_lng: float | None = None,
    geo_bottom_lat: float | None = None,
    geo_left_lng: float | None = None,
) -> list[str]:
    """Compile the geo filter clauses (``[]`` when the caller passed no geo params).

    Two independent, composable shapes:

    * **radius** — ``geo_lat`` + ``geo_lng`` + ``geo_radius_m`` compile to
      ``_geoRadius(lat, lng, metres)``. A radius without a centre is refused, and
      guessing a default radius for a lone centre would answer a question nobody
      asked — a centre alone yields NO clause here (it may instead be serving
      ``geo_sort``; :func:`compile_geo` is where that is resolved).
    * **bounding box** — the four edges compile to
      ``_geoBoundingBox([top, right], [bottom, left])`` (Meili's own
      top-right/bottom-left argument order). All four are required together.

    Supplying both is legal and ANDs (the caller's map viewport plus a distance
    cap). The returned clauses join with the ordinary filters and the RBAC scope
    filter through the same ``" AND ".join`` the endpoint already uses, so geo
    NARROWS an authorised result set and can never widen one.

    Raises :class:`GeoParamError` on an out-of-range coordinate, a partially
    specified centre/box, a non-positive radius, or a box whose top is below its
    bottom / whose left edge is east of its right edge. Meilisearch bounding boxes
    do not cross the 180th meridian; a caller who needs that splits the query at
    the antimeridian (documented rather than silently rewritten)."""
    filters: list[str] = []

    if (geo_lat is None) != (geo_lng is None):
        raise GeoParamError("geo_lat and geo_lng must be given together")
    if geo_radius_m is not None and geo_lat is None:
        raise GeoParamError("geo_radius_m needs a centre — pass geo_lat and geo_lng")
    # A centre with NO radius is not an error here: it is also the origin of the
    # ``geo_sort`` distance ordering. That it must serve one purpose or the other
    # is checked once, in :func:`compile_geo`.
    if geo_radius_m is not None:
        lat = _check_lat(float(geo_lat), "geo_lat")  # type: ignore[arg-type]
        lng = _check_lng(float(geo_lng), "geo_lng")  # type: ignore[arg-type]
        radius = float(geo_radius_m)  # type: ignore[arg-type]
        # ``isfinite`` rejects NaN and ±inf, which pydantic otherwise parses
        # happily from a query string and which would render literally into the
        # filter expression as ``inf``.
        if not math.isfinite(radius) or radius <= 0:
            raise GeoParamError("geo_radius_m must be a positive, finite distance in metres")
        filters.append(
            "_geoRadius("
            f"{_COORD_FMT.format(lat)}, {_COORD_FMT.format(lng)}, "
            f"{_METRES_FMT.format(radius)})"
        )

    box_parts = (geo_top_lat, geo_right_lng, geo_bottom_lat, geo_left_lng)
    if any(p is not None for p in box_parts):
        if any(p is None for p in box_parts):
            raise GeoParamError(
                "a bounding-box query needs all of geo_top_lat, geo_right_lng, "
                "geo_bottom_lat and geo_left_lng"
            )
        top = _check_lat(float(geo_top_lat), "geo_top_lat")  # type: ignore[arg-type]
        bottom = _check_lat(float(geo_bottom_lat), "geo_bottom_lat")  # type: ignore[arg-type]
        right = _check_lng(float(geo_right_lng), "geo_right_lng")  # type: ignore[arg-type]
        left = _check_lng(float(geo_left_lng), "geo_left_lng")  # type: ignore[arg-type]
        if top < bottom:
            raise GeoParamError(
                "geo_top_lat must be north of (>=) geo_bottom_lat — the box looks "
                "inverted; swapped edges are refused, not reordered"
            )
        if left > right:
            raise GeoParamError(
                "geo_left_lng must be west of (<=) geo_right_lng — Meilisearch "
                "bounding boxes cannot cross the 180th meridian; issue two queries"
            )
        filters.append(
            "_geoBoundingBox("
            f"[{_COORD_FMT.format(top)}, {_COORD_FMT.format(right)}], "
            f"[{_COORD_FMT.format(bottom)}, {_COORD_FMT.format(left)}])"
        )

    return filters


def build_geo_sort(
    *,
    geo_lat: float | None = None,
    geo_lng: float | None = None,
    geo_sort: str | None = None,
) -> str | None:
    """Compile the ``_geoPoint(lat, lng):asc|desc`` distance-sort expression.

    ``None`` when the caller did not ask for a distance ordering. Requires the same
    centre the radius filter uses — sorting "by distance" without a point to
    measure from is meaningless, so it is a 422 rather than an ignored parameter
    (a silently dropped sort looks like a ranking bug to the caller)."""
    if geo_sort is None:
        return None
    if geo_sort not in ("asc", "desc"):
        raise GeoParamError("geo_sort must be 'asc' (nearest first) or 'desc'")
    if geo_lat is None or geo_lng is None:
        raise GeoParamError(
            "geo_sort needs a centre point — pass geo_lat and geo_lng "
            "(with or without geo_radius_m)"
        )
    lat = _check_lat(float(geo_lat), "geo_lat")
    lng = _check_lng(float(geo_lng), "geo_lng")
    return f"_geoPoint({_COORD_FMT.format(lat)}, {_COORD_FMT.format(lng)}):{geo_sort}"


def compile_geo(
    *,
    geo_lat: float | None = None,
    geo_lng: float | None = None,
    geo_radius_m: float | None = None,
    geo_top_lat: float | None = None,
    geo_right_lng: float | None = None,
    geo_bottom_lat: float | None = None,
    geo_left_lng: float | None = None,
    geo_sort: str | None = None,
) -> tuple[list[str], str | None]:
    """The single endpoint-facing geo compiler: ``(filter_clauses, sort_expression)``.

    Combines :func:`build_geo_filters` and :func:`build_geo_sort` and adds the one
    rule that spans them: a centre point must actually be USED — either to bound a
    radius or as the origin of a distance sort. A bare ``geo_lat``/``geo_lng`` pair
    with neither is an incomplete query, and answering it as though the caller had
    passed no geo parameters at all would silently return the whole catalog to
    someone who believes they asked a spatial question.

    ``([], None)`` when no geo parameter was supplied, which is what makes the
    feature inert for every existing caller. Raises :class:`GeoParamError`."""
    filters = build_geo_filters(
        geo_lat=geo_lat,
        geo_lng=geo_lng,
        geo_radius_m=geo_radius_m,
        geo_top_lat=geo_top_lat,
        geo_right_lng=geo_right_lng,
        geo_bottom_lat=geo_bottom_lat,
        geo_left_lng=geo_left_lng,
    )
    sort_expr = build_geo_sort(geo_lat=geo_lat, geo_lng=geo_lng, geo_sort=geo_sort)
    if geo_lat is not None and geo_radius_m is None and sort_expr is None:
        raise GeoParamError(
            "geo_lat/geo_lng on their own do nothing — add geo_radius_m for a "
            "radius filter, or geo_sort to order by distance from that point"
        )
    return filters, sort_expr


def _shape_hit(hit: dict) -> dict:
    """Attach the safe ``snippet`` (cropped body) + ``highlight`` (title/filename)
    to a search hit and drop the raw ``body_text`` / ``_formatted`` payload (P3-T5).

    Meili returns ``_formatted`` with matches wrapped in ``<em>``/``</em>``; we
    surface ONLY those cropped/highlighted strings (marker tags are the sole
    markup). The full ``body_text`` (up to the index cap) is removed from the hit
    so a search response never ships kilobytes of body per row — the frontend uses
    the short snippet, and renders every value as text + <mark>, never {@html}."""
    out = {k: v for k, v in hit.items() if k not in ("body_text", "_formatted")}
    fmt = hit.get("_formatted")
    if isinstance(fmt, dict):
        snippet = fmt.get("body_text")
        if isinstance(snippet, str) and snippet.strip():
            out["snippet"] = snippet
        highlight = {
            key: fmt[key]
            for key in ("title", "filename")
            if isinstance(fmt.get(key), str)
        }
        if highlight:
            out["highlight"] = highlight
    return out


@router.get("/search", response_model=SearchResponse)
async def search(
    request: Request,
    q: str = "",
    file_category: list[str] | None = Query(
        default=None,
        description="file-category filter (repeatable = OR); the coarse, extension-"
        "derived parent of file_group (the authoritative type filter, successor to "
        "the removed media_type). See GET /taxonomy for the vocabulary. Unknown "
        "values are ignored.",
    ),
    file_group: list[str] | None = Query(
        default=None,
        description="file-group filter (repeatable = OR); the finer, extension-"
        "derived similarity bucket. See GET /system/file-groups for the vocabulary. "
        "Unknown values are ignored.",
    ),
    library: str | None = None,
    status: str | None = Query(default="active"),
    extension: str | None = None,
    year_gte: int | None = None,
    year_lte: int | None = None,
    size_gte: int | None = Query(
        default=None, ge=0, description="P3-T4 min file size in bytes (inclusive)"
    ),
    size_lte: int | None = Query(
        default=None, ge=0, description="P3-T4 max file size in bytes (inclusive)"
    ),
    mtime_gte: int | None = Query(
        default=None, description="P3-T4 min mtime as epoch seconds (inclusive)"
    ),
    mtime_lte: int | None = Query(
        default=None, description="P3-T4 max mtime as epoch seconds (inclusive)"
    ),
    tags: str | None = Query(default=None, description="comma-separated, AND semantics"),
    sidecar_of: str | None = Query(
        default=None, description="return sidecars linked to this parent item id"
    ),
    include_sidecars: bool = Query(
        default=False,
        description="include sidecar files (.nfo/artwork/JRiver) in results; "
        "excluded by default so they don't pollute top-level hits",
    ),
    hash: str | None = Query(
        default=None,
        pattern="^[0-9a-f]{8,64}$",
        description="P3-T1 exact hash lookup: matches quick_hash OR content_hash "
        "(lowercase hex, 8-64 chars). Typo tolerance is off for these fields, so "
        "a single differing digit returns nothing.",
    ),
    sort: str | None = Query(
        default=None,
        pattern="^(newest|(title|year|size|mtime):(asc|desc))$",
        description="explicit ordering; 'newest' is an alias for 'mtime:desc'. "
        "Distinct from the ambient recency tie-breaker (always on).",
    ),
    # R8 geo filters (roadmap §8). Inert for every caller who omits them: with all
    # of them None ``build_geo_filters`` returns [] and the query is byte-identical
    # to the pre-R8 one. Range checks are declared here (FastAPI answers 422 before
    # the handler runs) AND re-asserted in build_geo_filters, so a direct caller of
    # the pure function cannot bypass them either.
    geo_lat: float | None = Query(
        default=None,
        ge=-90.0,
        le=90.0,
        description="R8 radius-query centre latitude. Requires geo_lng and "
        "geo_radius_m (or, with geo_sort, acts as the distance-sort origin).",
    ),
    geo_lng: float | None = Query(
        default=None,
        ge=-180.0,
        le=180.0,
        description="R8 radius-query centre longitude. Requires geo_lat.",
    ),
    geo_radius_m: float | None = Query(
        default=None,
        gt=0.0,
        description="R8 radius in METRES around (geo_lat, geo_lng) — compiles to "
        "Meilisearch's _geoRadius. Requires both centre coordinates.",
    ),
    geo_top_lat: float | None = Query(
        default=None, ge=-90.0, le=90.0,
        description="R8 bounding-box north edge; all four box params are required "
        "together (compiles to _geoBoundingBox).",
    ),
    geo_right_lng: float | None = Query(
        default=None, ge=-180.0, le=180.0, description="R8 bounding-box east edge."
    ),
    geo_bottom_lat: float | None = Query(
        default=None, ge=-90.0, le=90.0, description="R8 bounding-box south edge."
    ),
    geo_left_lng: float | None = Query(
        default=None, ge=-180.0, le=180.0, description="R8 bounding-box west edge."
    ),
    geo_sort: str | None = Query(
        default=None,
        pattern="^(asc|desc)$",
        description="R8 order by distance from (geo_lat, geo_lng): 'asc' = nearest "
        "first. Takes precedence over ``sort``, which is kept as the tie-break.",
    ),
    semantic: float = Query(
        default=0.0,
        ge=0.0,
        le=1.0,
        description="P3-T8 hybrid semantic ratio (0..1). 0 = pure keyword "
        "(default, unchanged behaviour); 1 = pure vector; in between blends both. "
        "Ignored (treated as 0) unless semantic search is enabled server-side.",
    ),
    cursor: str | None = None,
    limit: int = Query(default=PAGE_SIZE, le=200),
    search_in: str = Query(
        default="all",
        pattern="^(all|content|names)$",
        description="2026-08-20: which attributes the query text matches. "
        "'all' (default) = every searchable attribute; 'content' = the indexed "
        "FILE CONTENT only (body/OCR text and archive member names — a path or "
        "filename match alone no longer returns the item); 'names' = "
        "title/filename/path/tags and the name-ish metadata only (no body text).",
    ),
    # P6-T3 server-side RBAC scoping: None (admin / API key / auth-off) => no
    # filter (byte-identical to the pre-P6 path); else the deny-aware scope
    # expression ANDed into the Meili query so Meili does the row-level filtering.
    scope_filter: str | None = Depends(require_search_scope("read")),
) -> SearchResponse:
    """Typo-tolerant catalog search with facets, filters and optional geo bounds.

    **Geo queries and the GPS gate (R8).** ``geo_*`` narrows results to files whose
    photo coordinates fall in a radius or a bounding box. Only files in a library
    with ``expose_gps`` enabled carry coordinates in the search index at all: GPS is
    extracted and stored in Postgres as normal, but the projection omits it unless
    the library opted in (CWE-1230 default-hidden gate; ``PATCH /libraries/{id}``
    with ``expose_gps`` flips it and triggers a re-projection). So on a deployment
    where no library exposes GPS a geo query returns **zero hits and HTTP 200** —
    it is not an error, and deliberately so: failing loudly would itself tell the
    caller something about how the server is configured. Malformed geo parameters
    (half a centre, an inverted box, an out-of-range coordinate) ARE a 422."""
    filters = build_filters(
        file_category=file_category,
        file_group=file_group,
        library=library,
        status=status,
        extension=extension,
        year_gte=year_gte,
        year_lte=year_lte,
        size_gte=size_gte,
        size_lte=size_lte,
        mtime_gte=mtime_gte,
        mtime_lte=mtime_lte,
        tags=tags,
        sidecar_of=sidecar_of,
        include_sidecars=include_sidecars,
        hash=hash,
    )
    # R8: geo clauses join the ordinary ones (AND). Cross-field nonsense (a half
    # -specified centre/box, an inverted box) is a 422 — refused, never normalised.
    try:
        geo_clauses, geo_sort_expr = compile_geo(
            geo_lat=geo_lat,
            geo_lng=geo_lng,
            geo_radius_m=geo_radius_m,
            geo_top_lat=geo_top_lat,
            geo_right_lng=geo_right_lng,
            geo_bottom_lat=geo_bottom_lat,
            geo_left_lng=geo_left_lng,
            geo_sort=geo_sort,
        )
    except GeoParamError as exc:
        raise HTTPException(422, str(exc)) from exc
    filters.extend(geo_clauses)

    # P6-T3: narrow to the caller's granted scopes (Meili-side enforcement).
    # AFTER the geo clauses purely for readability — every clause is ANDed, so a
    # geo filter can only ever SHRINK the scope-authorised set, never widen it.
    if scope_filter:
        filters.append(scope_filter)

    # 'newest' is a friendly alias. FIX-3: it maps to the CLAMPED ``mtime_sort``
    # (min(mtime, index-time)) so a file with a bogus FUTURE mtime cannot float to
    # the top of a "newest" sort. Raw ``mtime`` stays available for an explicit
    # ``sort=mtime:asc|desc`` (display/debugging) and for range filters.
    sort_expr = "mtime_sort:desc" if sort == "newest" else sort
    # R8: an explicit distance ordering is the PRIMARY sort when asked for; any
    # ordinary ``sort`` follows it as a tie-break (Meili applies sort criteria in
    # order). Callers who pass neither are unaffected.
    sort_exprs = [e for e in (geo_sort_expr, sort_expr) if e]

    offset = _decode_cursor(cursor)
    s = get_settings()

    # P3-T8 hybrid search: when the caller asks for a semantic blend AND the
    # feature is enabled, embed the query locally (39 ms, approved) and hand Meili
    # a hybrid {semanticRatio, embedder} plus the query vector. When semantic is 0
    # or disabled we pass neither, so the keyword path is byte-identical to before.
    hybrid = None
    vector = None
    if semantic > 0.0 and s.semantic_enabled:
        vector = embed_query(q)
        hybrid = Hybrid(semantic_ratio=semantic, embedder=DEFAULT_EMBEDDER_NAME)

    search_kwargs = dict(
        filter=" AND ".join(filters) if filters else None,
        offset=offset,
        limit=limit,
        sort=sort_exprs or None,
        hybrid=hybrid,
        vector=vector,
        # P3-T5 snippets/highlighting. body_text is cropped to a short window;
        # title/filename get inline markers. Custom <em>/</em> tags (also the
        # SDK default) are the only markup Meili injects.
        attributes_to_highlight=HIGHLIGHT_ATTRS,
        attributes_to_crop=CROP_ATTRS,
        crop_length=CROP_LENGTH,
        highlight_pre_tag=HIGHLIGHT_PRE,
        highlight_post_tag=HIGHLIGHT_POST,
    )
    # search_in scoping (2026-08-20): restrict which attributes the QUERY TEXT
    # matches (filters/facets/sort untouched). Uses Meili attributesToSearchOn,
    # so ranking still runs over the restricted set.
    if search_in == "content":
        search_kwargs["attributes_to_search_on"] = ["body_text", "archive_members"]
    elif search_in == "names":
        search_kwargs["attributes_to_search_on"] = [
            "title", "filename", "path", "artist", "album", "author", "tags",
        ]
    async with client() as c:
        index = c.index(s.meili_index)
        try:
            result = await index.search(q, facets=FACETS, **search_kwargs)
        except MeilisearchApiError as err:
            # A newly-added facet still re-indexing must not take down search:
            # retry once WITHOUT facet distribution (results survive; counts drop
            # until the reindex completes). Any other API error is a real fault.
            if not _is_facet_unavailable(err):
                raise
            log.warning(
                "search facets unavailable (%s); serving without facet counts — a "
                "newly-added filterable attribute is likely still re-indexing",
                getattr(err, "code", "?"),
            )
            result = await index.search(q, facets=None, **search_kwargs)

    total = result.estimated_total_hits or 0
    next_cursor = _encode_cursor(offset + limit) if offset + limit < total else None
    # ``facet_stats`` (P3-T4): the SDK exposes per-numeric-facet min/max; pass it
    # through verbatim so the frontend range sliders derive bounds from real data.
    # A fake/older client that omits the attribute degrades to an empty dict.
    facet_stats = getattr(result, "facet_stats", None) or {}
    # P6-T9: opt-in read auditing (default OFF — read volume is high, value low
    # outside multi-tenant SaaS). Only fires when FILEARR_AUDIT_READS=true.
    if s.audit_reads:
        from filearr import audit

        await audit.emit(
            audit.SEARCH,
            request=request,
            principal_id=audit.actor_id(request),
            details={
                "q": q,
                "file_category": file_category,
                "library": library,
                "total": total,
            },
        )
    hits = [_shape_hit(h) for h in result.hits]

    # Frecency (roadmap §5 P3): bounded, page-local personal re-rank. Only on
    # the default relevance ordering and only on the FIRST page (an explicit
    # sort or a paged-through offset must stay stable), and strictly fail-open
    # — a frecency error never degrades search itself.
    if s.frecency_enabled and hits and sort is None and geo_sort is None and offset == 0:
        try:
            from filearr import frecency
            from filearr.db import SessionLocal

            owner = frecency.owner_from_actor(getattr(request.state, "actor", None))
            async with SessionLocal() as fsession:
                fscores = await frecency.scores_for(
                    fsession, owner, [str(h.get("id", "")) for h in hits]
                )
            hits = frecency.rerank(hits, fscores)
        except Exception:  # noqa: BLE001 - personalization must never break search
            log.debug("frecency re-rank skipped", exc_info=True)

    return SearchResponse(
        hits=hits,
        total=total,
        facets=result.facet_distribution or {},
        facet_stats=facet_stats,
        next_cursor=next_cursor,
    )


# ---------------------------------------------------------------------------
# R8 (roadmap §8 "federated multi-search + facetsByIndex") — one merged, ranked
# result list across the ITEM index and the PASSAGE (chunks) index.
#
# SDK reality check (meilisearch-python-sdk 7.2.3, verified in
# ``.venv312/Lib/site-packages/meilisearch_python_sdk``): federation IS supported.
# ``AsyncClient.multi_search(queries: list[SearchParams], *, federation=...)``
# posts to ``/multi-search``; passing a ``Federation`` (or ``FederationMerged``)
# makes Meili return ONE ``SearchResultsFederated`` with a single merged hit list
# instead of a list of per-index ``SearchResultsWithUID``. ``Federation`` carries
# ``limit`` / ``offset`` / ``facetsByIndex`` / ``distinct``; per-query
# ``FederationOptions(weight=...)`` tunes each sub-query's contribution to the
# merged ranking. Everything this endpoint needs exists at the pinned version —
# nothing here is emulated client-side, and in particular the interleaving and
# ranking are Meilisearch's, not ours.
# ---------------------------------------------------------------------------

#: Logical index names in the response. The Meili uids are deployment-configurable
#: (``FILEARR_MEILI_INDEX`` and ``<index>_chunks``), so the public contract uses
#: these stable names and the endpoint maps uid -> name.
SOURCE_ITEMS = "items"
SOURCE_PASSAGES = "passages"

#: Attributes of a passage hit worth returning. ``item_id`` is the join key back to
#: the catalog; ``filename``/``rel_path``/``library_id`` are DENORMALISED onto every
#: chunk document by ``chunking.build_chunk_doc``, which is what lets a merged
#: result list be rendered with ZERO per-hit lookups (no N+1 against Postgres or
#: against the item index).
_PASSAGE_FIELDS = ("item_id", "chunk_no", "filename", "rel_path", "library_id", "text")
_ITEM_FIELDS = (
    "id", "library_id", "title", "filename", "rel_path", "extension",
    "size", "mtime", "file_category", "file_group", "tags",
)

#: Passage text is a retrieval snippet, not a document viewer.
_PASSAGE_TEXT_CAP = 2000


@router.get("/search/federated", response_model=FederatedSearchResponse)
async def search_federated(
    q: str = Query(default="", description="the query text, applied to BOTH indexes"),
    # Typed as a UUID (not a bare string) so FastAPI rejects anything else with a
    # 422 before it can reach a filter expression. That matters more here than on
    # ``/search``: this value is interpolated into the filter of EVERY sub-query,
    # alongside the RBAC scope clause, so a syntax-injecting value would be a way
    # to tamper with an authorisation filter.
    library: uuid.UUID | None = Query(
        default=None,
        description="restrict both sub-queries to one library id (library_id is "
        "filterable on the item AND the chunk index)",
    ),
    status: str | None = Query(
        default="active",
        description="item status filter; applies to the item sub-query only — the "
        "chunks index carries no status attribute (see the docstring).",
    ),
    include_sidecars: bool = Query(
        default=False, description="include sidecar files in the item sub-query"
    ),
    facets: bool = Query(
        default=False,
        description="also return Meili's facetsByIndex distribution for the item "
        "index (the chunks index has no facetable attributes).",
    ),
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
    scope_filter: str | None = Depends(require_search_scope("read")),
) -> FederatedSearchResponse:
    """Federated search across catalog items AND indexed passages (R8).

    Returns ONE merged list ranked by Meilisearch's federated scoring, with each
    hit labelled by the logical index it came from (``items`` | ``passages``), so a
    caller never has to interleave two result sets and guess at relative relevance.

    **This is real federation**, not a client-side merge: the SDK's
    ``multi_search(..., federation=Federation(...))`` is available at our pinned
    ``meilisearch-python-sdk==7.2.3`` and the ranking/pagination happen server-side
    (``limit``/``offset`` are federation-level, which is why the per-query limits
    are dropped from the payload by the SDK).

    **RBAC.** Every sub-query carries the SAME compiled scope filter the
    single-index ``/search`` path applies (P6-T3 server-side proxy: Meili does the
    row-level filtering, the API never post-filters). A federated query is a new
    way to reach documents and therefore a new place to get RBAC wrong; the filter
    is built once and appended to each sub-query's filter list, and both chunk and
    item documents project the same ``path_scope`` ancestor array so one expression
    is correct against both indexes.

    **Why the sub-queries carry different ordinary filters.** The chunks index is
    deliberately tiny-spec (``chunking.CHUNK_FILTERABLE`` = ``item_id`` /
    ``library_id`` / ``path_scope``): it has no ``status`` or ``is_sidecar``
    attribute, so sending the item filters at it would make Meilisearch reject the
    whole multi-search. Each sub-query therefore gets exactly the filters its index
    supports — with the RBAC clause present in both, which is the part that matters.

    **No N+1.** Chunk documents already carry ``item_id`` plus the denormalised
    ``filename``/``rel_path``/``library_id``, so every hit is resolvable to a
    catalog item with no per-hit database or index lookup.

    **Chunking is optional.** When no library has opted into chunking the passage
    index does not exist; Meili answers the multi-search with ``index_not_found``,
    and this endpoint retries against the item index alone and reports the reduced
    ``indexes`` list rather than failing (the same degrade-don't-500 posture as the
    facet fallback on ``/search``)."""
    s = get_settings()
    chunks_uid = chunks_index_uid(s)

    item_filters = build_filters(
        library=str(library) if library else None,
        status=status,
        include_sidecars=include_sidecars,
    )
    # The chunks index only knows library_id / item_id / path_scope.
    passage_filters = [f"library_id = '{library}'"] if library else []
    if scope_filter:
        # P6-T3 — the SAME expression on BOTH sub-queries. Never one or the other.
        item_filters.append(scope_filter)
        passage_filters.append(scope_filter)

    def _params(uid: str, clauses: list[str]) -> SearchParams:
        return SearchParams(
            index_uid=uid,
            query=q,
            filter=" AND ".join(clauses) if clauses else None,
        )

    uid_to_source = {s.meili_index: SOURCE_ITEMS, chunks_uid: SOURCE_PASSAGES}
    queries = [_params(s.meili_index, item_filters), _params(chunks_uid, passage_filters)]
    federation = Federation(
        limit=limit,
        offset=offset,
        facets_by_index={s.meili_index: FACETS} if facets else None,
    )

    async with client() as c:
        try:
            result = await c.multi_search(queries, federation=federation)
        except MeilisearchApiError as err:
            code = getattr(err, "code", "") or ""
            if code == "index_not_found":
                # Chunking is a per-library opt-in; with no chunks index the
                # federation still works, just over one index.
                queries = queries[:1]
                result = await c.multi_search(queries, federation=federation)
            elif _is_facet_unavailable(err):
                log.warning(
                    "federated search facets unavailable (%s); serving without "
                    "facet counts — a filterable attribute is likely still "
                    "re-indexing", code,
                )
                federation = Federation(limit=limit, offset=offset)
                result = await c.multi_search(queries, federation=federation)
            else:
                raise

    queried = [uid_to_source[qp.index_uid] for qp in queries]
    hits = [_shape_federated_hit(h, uid_to_source) for h in result.hits]
    facets_by_index = {
        uid_to_source.get(uid, uid): block
        for uid, block in (getattr(result, "facets_by_index", None) or {}).items()
    }
    return FederatedSearchResponse(
        hits=hits,
        total=result.estimated_total_hits or 0,
        limit=limit,
        offset=offset,
        indexes=queried,
        facets_by_index=facets_by_index,
    )


def _shape_federated_hit(hit: dict, uid_to_source: dict[str, str]) -> FederatedHit:
    """Label one federated hit with its logical source index and its item id.

    Meilisearch attaches a ``_federation`` block (``indexUid`` /
    ``weightedRankingScore``) to every hit of a federated search — that is the
    authoritative "which index did this come from", so it is used rather than any
    guess based on the hit's shape. A passage hit's ``item_id`` is already on the
    document (no lookup); an item hit's is its document id."""
    fed = hit.get("_federation") or {}
    source = uid_to_source.get(fed.get("indexUid", ""), SOURCE_ITEMS)
    if source == SOURCE_PASSAGES:
        fields = {k: hit[k] for k in _PASSAGE_FIELDS if k in hit}
        if isinstance(fields.get("text"), str):
            fields["text"] = fields["text"][:_PASSAGE_TEXT_CAP]
        item_id = hit.get("item_id")
    else:
        fields = {k: hit[k] for k in _ITEM_FIELDS if k in hit}
        item_id = hit.get("id")
    return FederatedHit(
        source=source,
        item_id=str(item_id) if item_id is not None else None,
        score=fed.get("weightedRankingScore"),
        fields=fields,
    )


# ---------------------------------------------------------------------------
# P3-T12 — tag facet type-ahead. A thin proxy over Meili's FACET-SEARCH endpoint
# (``AsyncIndex.facet_search``) against the existing ``tags`` array. Configuration
# -free: it inherits the index's typo tolerance (``tags`` is intentionally NOT in
# ``DISABLE_TYPO_ATTRIBUTES``, so a typo'd prefix still matches) and, per P9 R2,
# the facet VALUES come back COUNT-ordered because ``tags`` is a
# ``FACET_SEARCH_CANDIDATE`` with ``sortFacetValuesBy=count``. An optional
# ``type``/``library``/``status`` context narrows the suggestion counts to the
# current search scope (same ``build_filters`` shape, injection-safe).
# ---------------------------------------------------------------------------
@router.get("/search/tags")
async def search_tags(
    q: str = Query(default="", description="tag prefix/substring to complete"),
    file_category: list[str] | None = Query(
        default=None, description="scope suggestions to one or more file categories"
    ),
    library: str | None = None,
    status: str | None = Query(default="active"),
    limit: int = Query(default=20, ge=1, le=50),
    scope_filter: str | None = Depends(require_search_scope("read")),
) -> dict:
    """Typo-tolerant, count-ordered tag suggestions (P3-T12).

    Returns ``{"tags": [{"value": tag, "count": n}, ...]}`` — the most-common
    matching tags first (R2). ``q`` is the partial tag the user is typing; an
    empty ``q`` returns the top tags in the (optionally scoped) corpus."""
    # Optional scope context — build_filters excludes sidecars by default and adds
    # the equality clauses; the tag facet counts then reflect that scope.
    filters = build_filters(file_category=file_category, library=library, status=status)
    # P6-T3: apply the caller's RBAC scope filter to the facet counts too, so tag
    # suggestions never leak values that live only under un-granted paths.
    if scope_filter:
        filters.append(scope_filter)
    filter_expr = " AND ".join(filters) if filters else None

    s = get_settings()
    async with client() as c:
        res = await c.index(s.meili_index).facet_search(
            facet_name="tags",
            facet_query=q,
            filter=filter_expr,
        )

    # facet_hits carries {value, count}; the SDK already orders per the index
    # ``sortFacetValuesBy`` (count for tags). Cap to ``limit`` defensively.
    hits = getattr(res, "facet_hits", None) or []
    tags = [
        {"value": h.value, "count": h.count}
        for h in hits[:limit]
    ]
    return {"tags": tags}


# ---------------------------------------------------------------------------
# P3-T7 — the vocabulary of accepted ``/search`` query params, DERIVED from the
# endpoint signature (single source of truth) minus the opaque ``cursor`` (which
# is pagination state, never part of a saved query). ``saved_searches`` validates
# every stored ``params`` key against this frozenset, so:
#   * adding a new search param auto-extends the saved-search vocabulary (no
#     second edit), and
#   * renaming/removing a param makes the saved-search round-trip test fail loudly
#     rather than silently accepting a now-dead key.
# ``inspect.signature`` reads the ORIGINAL function (FastAPI's route decorator
# returns it unchanged), so this reflects exactly what a caller may pass.
# ---------------------------------------------------------------------------
import inspect  # noqa: E402  (kept next to its sole use)

SEARCH_PARAM_NAMES: frozenset[str] = frozenset(
    name
    for name in inspect.signature(search).parameters
    # ``cursor`` is pagination state; ``scope_filter`` is the P6-T3 injected RBAC
    # dependency (Depends), never a user-supplied search param.
    if name not in ("cursor", "scope_filter")
)
