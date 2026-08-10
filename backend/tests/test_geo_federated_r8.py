"""R8 (roadmap §8) — Meilisearch geo filters + federated multi-search.

Two features, one security story, so one module:

* **Geo.** ``search.build_doc`` projects Meili's reserved ``_geo`` point, but ONLY
  for a library whose ``expose_gps`` is on. The gate itself lives in
  ``filearr.exif.strip_gps`` + the ``Library.expose_gps`` column (CWE-1230,
  P3-T11) and these tests pin that it is not quietly relocated or widened: a
  library with the flag off must produce a document with NO ``_geo`` key at all,
  and flipping the flag off must REWRITE the already-indexed documents (Meili's
  add-or-update endpoint merges, so a plain re-sync would leave the old point
  behind — that is the leak these tests exist to prevent).
* **Federation.** The merged item+passage search must carry the SAME RBAC scope
  filter into EVERY sub-query and label each hit's source index.

Meilisearch is faked throughout (no server); the DB-backed cases use the shared
``pg_uri`` fixture + alembic head, following ``test_gps_gate_p3.py``.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import chunking as chunking_mod
from filearr import db as db_mod
from filearr import meili_ops
from filearr import search as search_mod
from filearr.api import search as search_api
from filearr.api.search import (
    GeoParamError,
    build_geo_filters,
    build_geo_sort,
    compile_geo,
)
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import DocChunk, Item, ItemStatus, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent

_LAT, _LNG = 37.7749, -122.4194


def _make_item(meta: dict | None = None, **kw) -> Item:
    base = dict(
        id=uuid.uuid4(),
        library_id=uuid.uuid4(),
        file_category="image",
        file_group="raster-photo",
        path="/data/p.jpg",
        rel_path="p.jpg",
        filename="p.jpg",
        extension="jpg",
        size=1,
        mtime=datetime.now(UTC),
        metadata_=meta or {},
        user_metadata={},
        external_ids={},
        tags=[],
        status=ItemStatus.active,
        sidecar_of=None,
    )
    base.update(kw)
    return Item(**base)


_GEO_META = {
    "exif.camera_make": "Canon",
    "exif.gps_latitude": _LAT,
    "exif.gps_longitude": _LNG,
}


# --------------------------------------------------------------------------- #
# 1. The _geo projection and its gate                                          #
# --------------------------------------------------------------------------- #
def test_geo_emitted_for_exposed_library():
    doc = search_mod.build_doc(_make_item(dict(_GEO_META)), expose_gps=True)
    assert doc["_geo"] == {"lat": _LAT, "lng": _LNG}


def test_geo_absent_when_library_does_not_expose_gps():
    """The CWE-1230 gate: no ``_geo`` KEY at all (not a null, not a placeholder),
    so a geo query cannot even indirectly confirm that a located file exists."""
    doc = search_mod.build_doc(_make_item(dict(_GEO_META)), expose_gps=False)
    assert "_geo" not in doc
    # ...and the default is off, i.e. a caller that forgets the flag cannot leak.
    assert "_geo" not in search_mod.build_doc(_make_item(dict(_GEO_META)))


def test_geo_absent_without_coordinates():
    doc = search_mod.build_doc(_make_item({"exif.camera_make": "Canon"}), expose_gps=True)
    assert "_geo" not in doc


@pytest.mark.parametrize(
    "meta",
    [
        {"exif.gps_latitude": _LAT},  # partial: latitude only
        {"exif.gps_longitude": _LNG},  # partial: longitude only
        {"exif.gps_latitude": _LAT, "exif.gps_longitude": None},
    ],
)
def test_geo_absent_for_partial_pair(meta):
    assert "_geo" not in search_mod.build_doc(_make_item(dict(meta)), expose_gps=True)


@pytest.mark.parametrize(
    "lat,lng",
    [
        (float("nan"), _LNG),
        (float("inf"), _LNG),
        (_LAT, float("-inf")),
        (91.0, _LNG),  # latitude out of range
        (-90.5, _LNG),
        (_LAT, 180.5),  # longitude out of range
        ("not-a-number", _LNG),
        (True, _LNG),  # bool is an int subclass but never a latitude
        ({"lat": 1}, _LNG),
    ],
)
def test_geo_dropped_not_clamped_for_malformed_coordinates(lat, lng):
    """A malformed pair is DROPPED. Clamping would invent a real-world location the
    camera never recorded — and an out-of-range ``_geo`` also fails the whole Meili
    indexing batch, taking unrelated documents down with it."""
    doc = search_mod.build_doc(
        _make_item({"exif.gps_latitude": lat, "exif.gps_longitude": lng}),
        expose_gps=True,
    )
    assert "_geo" not in doc


def test_geo_accepts_numeric_strings():
    """exiftool output is untrusted parser output and has historically arrived as
    numeric strings; those are still a valid location."""
    doc = search_mod.build_doc(
        _make_item({"exif.gps_latitude": "37.7749", "exif.gps_longitude": "-122.4194"}),
        expose_gps=True,
    )
    assert doc["_geo"] == {"lat": _LAT, "lng": _LNG}


def test_geo_boundary_values_are_valid():
    doc = search_mod.build_doc(
        _make_item({"exif.gps_latitude": -90, "exif.gps_longitude": 180}),
        expose_gps=True,
    )
    assert doc["_geo"] == {"lat": -90.0, "lng": 180.0}


def test_geo_settings_are_filterable_and_sortable():
    assert meili_ops.GEO_ATTR == "_geo"
    assert "_geo" in meili_ops.FILTERABLE_ATTRIBUTES
    assert "_geo" in meili_ops.SORTABLE_ATTRIBUTES
    # Sent as a PLAIN STRING (the documented spelling for Meili's reserved geo
    # field) inside the same update_filterable_attributes payload as the
    # object-form attributes — see meili_ops.STRING_FORM_FILTERABLE for why an
    # unverified object-form entry is not worth risking on a boot-critical call.
    assert meili_ops.STRING_FORM_FILTERABLE == ("_geo",)
    settings = search_mod._filterable_settings()
    assert "_geo" in [f for f in settings if isinstance(f, str)]
    # ...and the drift token matches what _project_current derives from a string
    # element, so a matching index never re-applies settings on every boot.
    desired = search_mod._desired_settings()
    assert "_geo:True" in desired["filterableAttributes"]
    assert "_geo" in desired["sortableAttributes"]


# --------------------------------------------------------------------------- #
# 2. Geo filter / sort compilation                                             #
# --------------------------------------------------------------------------- #
def test_no_geo_params_compiles_to_nothing():
    """Inert for every caller who does not use it."""
    assert build_geo_filters() == []
    assert build_geo_sort() is None


def test_radius_filter_compiles():
    (clause,) = build_geo_filters(geo_lat=_LAT, geo_lng=_LNG, geo_radius_m=1500)
    assert clause == "_geoRadius(37.7749000, -122.4194000, 1500.000)"


def test_bounding_box_filter_compiles():
    (clause,) = build_geo_filters(
        geo_top_lat=38.0, geo_right_lng=-122.0, geo_bottom_lat=37.0, geo_left_lng=-123.0
    )
    # Meili's own argument order: [top, right], [bottom, left].
    assert clause == "_geoBoundingBox([38.0000000, -122.0000000], [37.0000000, -123.0000000])"


def test_radius_and_box_compose():
    clauses = build_geo_filters(
        geo_lat=_LAT,
        geo_lng=_LNG,
        geo_radius_m=1000,
        geo_top_lat=38.0,
        geo_right_lng=-122.0,
        geo_bottom_lat=37.0,
        geo_left_lng=-123.0,
    )
    assert len(clauses) == 2


def test_tiny_coordinates_avoid_scientific_notation():
    """``repr(1e-07)`` is ``'1e-07'``, which Meili's filter parser rejects; the
    fixed-precision rendering is what keeps a near-null-island query valid."""
    (clause,) = build_geo_filters(geo_lat=1e-7, geo_lng=1e-7, geo_radius_m=10)
    assert "e-" not in clause


def test_geo_sort_compiles():
    assert (
        build_geo_sort(geo_lat=_LAT, geo_lng=_LNG, geo_sort="asc")
        == "_geoPoint(37.7749000, -122.4194000):asc"
    )


@pytest.mark.parametrize(
    "kwargs",
    [
        {"geo_lat": _LAT},  # half a centre
        {"geo_lng": _LNG},
        {"geo_radius_m": 100},  # radius without centre
        {"geo_lat": _LAT, "geo_lng": _LNG, "geo_radius_m": 0},  # non-positive
        {"geo_lat": _LAT, "geo_lng": _LNG, "geo_radius_m": -5},
        # non-finite: pydantic parses "inf"/"nan" from a query string happily, and
        # an unchecked one would render literally into the filter expression
        {"geo_lat": _LAT, "geo_lng": _LNG, "geo_radius_m": float("inf")},
        {"geo_lat": _LAT, "geo_lng": _LNG, "geo_radius_m": float("nan")},
        {"geo_lat": float("nan"), "geo_lng": _LNG, "geo_radius_m": 10},
        {"geo_top_lat": 38.0},  # partial box
        {"geo_top_lat": 38.0, "geo_right_lng": -122.0, "geo_bottom_lat": 37.0},
        # inverted box: refused, never silently reordered
        {
            "geo_top_lat": 37.0,
            "geo_right_lng": -122.0,
            "geo_bottom_lat": 38.0,
            "geo_left_lng": -123.0,
        },
        # east/west edges swapped (a box crossing the antimeridian is unsupported)
        {
            "geo_top_lat": 38.0,
            "geo_right_lng": -123.0,
            "geo_bottom_lat": 37.0,
            "geo_left_lng": -122.0,
        },
        # out of range even though FastAPI would also have caught it
        {"geo_lat": 91.0, "geo_lng": _LNG, "geo_radius_m": 10},
        {"geo_lat": _LAT, "geo_lng": 181.0, "geo_radius_m": 10},
    ],
)
def test_nonsense_geo_params_are_refused(kwargs):
    with pytest.raises(GeoParamError):
        build_geo_filters(**kwargs)


@pytest.mark.parametrize(
    "kwargs",
    [
        {"geo_sort": "asc"},  # no centre to measure from
        {"geo_lat": _LAT, "geo_sort": "asc"},  # half a centre
        {"geo_lat": _LAT, "geo_lng": _LNG, "geo_sort": "sideways"},
    ],
)
def test_nonsense_geo_sort_is_refused(kwargs):
    with pytest.raises(GeoParamError):
        build_geo_sort(**kwargs)


def test_centre_must_be_used_for_something():
    """A bare centre is an incomplete spatial question; answering it as "no geo
    params" would hand back the whole catalog to a caller who thinks they asked
    for a place."""
    with pytest.raises(GeoParamError):
        compile_geo(geo_lat=_LAT, geo_lng=_LNG)
    # ...but a centre that feeds a distance SORT (no radius) is legitimate.
    filters, sort_expr = compile_geo(geo_lat=_LAT, geo_lng=_LNG, geo_sort="asc")
    assert filters == []
    assert sort_expr == "_geoPoint(37.7749000, -122.4194000):asc"


def test_compile_geo_is_inert_without_params():
    assert compile_geo() == ([], None)


# --------------------------------------------------------------------------- #
# 3. Endpoint wiring: composition with ordinary + RBAC filters                 #
# --------------------------------------------------------------------------- #
class _FakeIndex:
    """Minimal Meili index. Models the ONE engine property the geo tests depend
    on: a document with no ``_geo`` can never match a geo filter."""

    def __init__(self, sink: dict, docs: list[dict]):
        self._sink = sink
        self._docs = docs

    async def search(self, q, **kwargs):
        self._sink["q"] = q
        self._sink.update(kwargs)
        hits = self._docs
        f = kwargs.get("filter") or ""
        if "_geoRadius" in f or "_geoBoundingBox" in f:
            hits = [d for d in hits if "_geo" in d]
        return SimpleNamespace(
            hits=hits,
            estimated_total_hits=len(hits),
            facet_distribution={},
            facet_stats={},
        )


class _FakeClient:
    def __init__(self, sink: dict, docs: list[dict], federated=None):
        self._sink = sink
        self._docs = docs
        self._federated = federated

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def index(self, name):
        self._sink["index"] = name
        return _FakeIndex(self._sink, self._docs)

    async def multi_search(self, queries, *, federation=None, **kw):
        self._sink["queries"] = queries
        self._sink["federation"] = federation
        return self._federated(queries, federation)


def _app(monkeypatch, *, docs=None, federated=None, scope_filter=None):
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(get_settings(), "frecency_enabled", False)
    sink: dict = {}
    monkeypatch.setattr(
        search_api, "client", lambda: _FakeClient(sink, docs or [], federated)
    )
    app = create_app()
    if scope_filter is not None:
        _override_search_scope(app, scope_filter)
    return httpx.ASGITransport(app=app), sink, app


def _override_search_scope(app, scope_filter: str) -> None:
    """Stand in for the P6-T3 ``require_search_scope`` dependency (which compiles a
    principal's live grants) so the tests can assert the compiled scope EXPRESSION
    reaches the engine. ``require_search_scope`` builds a fresh closure per call, so
    the override has to target the exact callables the routes are bound to."""

    import inspect

    for endpoint in (search_api.search, search_api.search_federated):
        dep = inspect.signature(endpoint).parameters["scope_filter"].default.dependency
        app.dependency_overrides[dep] = lambda: scope_filter


@pytest.mark.asyncio
async def test_geo_params_reach_the_engine_filter(monkeypatch):
    transport, sink, _ = _app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/api/v1/search?q=x&geo_lat=37.7749&geo_lng=-122.4194&geo_radius_m=500"
            "&extension=jpg&geo_sort=asc"
        )
    assert r.status_code == 200, r.text
    assert "_geoRadius(37.7749000, -122.4194000, 500.000)" in sink["filter"]
    # composes with the ordinary filters (AND), never replaces them
    assert "extension = 'jpg'" in sink["filter"]
    assert "is_sidecar = false" in sink["filter"]
    # distance is the primary sort criterion when requested
    assert sink["sort"][0] == "_geoPoint(37.7749000, -122.4194000):asc"


@pytest.mark.asyncio
async def test_geo_sort_precedes_ordinary_sort(monkeypatch):
    transport, sink, _ = _app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/api/v1/search?geo_lat=1&geo_lng=2&geo_sort=asc&sort=newest"
        )
    assert r.status_code == 200, r.text
    assert sink["sort"] == ["_geoPoint(1.0000000, 2.0000000):asc", "mtime_sort:desc"]


@pytest.mark.asyncio
async def test_geo_composes_with_rbac_scope_filter(monkeypatch):
    """The scope filter is never replaced or reordered away by a geo query — geo
    can only ever SHRINK the set a principal is authorised to see."""
    scope = 'path_scope IN ["lib_a.photos"]'
    transport, sink, app = _app(monkeypatch, scope_filter=scope)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(
            "/api/v1/search?geo_top_lat=38&geo_right_lng=-122&geo_bottom_lat=37"
            "&geo_left_lng=-123"
        )
    assert r.status_code == 200, r.text
    assert scope in sink["filter"]
    assert "_geoBoundingBox(" in sink["filter"]
    assert " AND " in sink["filter"]
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_search_without_geo_params_is_unchanged(monkeypatch):
    transport, sink, _ = _app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search?q=x")
    assert r.status_code == 200, r.text
    assert "_geo" not in (sink["filter"] or "")
    assert sink["sort"] is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "qs",
    [
        "geo_lat=37.7",  # incomplete centre
        "geo_radius_m=500",  # radius with no centre
        "geo_lat=37.7&geo_lng=-122.4",  # centre with no radius
        "geo_lat=95&geo_lng=-122.4&geo_radius_m=10",  # out of range
        "geo_lat=37.7&geo_lng=-122.4&geo_radius_m=-1",  # negative radius
        "geo_lat=37.7&geo_lng=-122.4&geo_radius_m=inf",  # non-finite radius
        "geo_lat=nan&geo_lng=-122.4&geo_radius_m=10",  # non-finite centre
        "geo_lat=abc&geo_lng=-122.4&geo_radius_m=10",  # garbage
        "geo_top_lat=38&geo_right_lng=-122",  # partial box
        "geo_top_lat=37&geo_right_lng=-122&geo_bottom_lat=38&geo_left_lng=-123",
        "geo_sort=asc",  # sort with no centre
        "geo_lat=37.7&geo_lng=-122.4&geo_radius_m=10&geo_sort=up",  # bad direction
    ],
)
async def test_bad_geo_params_are_422(monkeypatch, qs):
    transport, _, _ = _app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/v1/search?{qs}")
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_geo_query_is_empty_not_an_error_without_exposed_libraries(monkeypatch):
    """A deployment where NO library exposes GPS: every projected document lacks
    ``_geo``, so the geo filter matches nothing. The honest answer is 200 + zero
    hits — erroring would itself disclose how the server is configured."""
    corpus = [
        search_mod.build_doc(_make_item(dict(_GEO_META)), expose_gps=False),
        search_mod.build_doc(_make_item(dict(_GEO_META)), expose_gps=False),
    ]
    assert all("_geo" not in d for d in corpus)
    transport, _, _ = _app(monkeypatch, docs=corpus)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        plain = await c.get("/api/v1/search?q=")
        geo = await c.get("/api/v1/search?geo_lat=37.77&geo_lng=-122.41&geo_radius_m=5000")
    assert plain.status_code == 200 and plain.json()["total"] == 2
    assert geo.status_code == 200, geo.text
    assert geo.json()["hits"] == []
    assert geo.json()["total"] == 0


# --------------------------------------------------------------------------- #
# 4. expose_gps flip -> re-projection with REPLACE semantics                    #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def maker(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM doc_chunks"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    m = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", m)
    yield m
    await engine.dispose()


async def _seed_library(m, *, expose_gps: bool) -> tuple[uuid.UUID, uuid.UUID]:
    async with m() as s:
        lib = Library(name=f"photos-{uuid.uuid4().hex[:6]}", root_path="/data/photos",
                      expose_gps=expose_gps)
        s.add(lib)
        await s.flush()
        item = Item(
            library_id=lib.id,
            file_category="image",
            file_group="raster-photo",
            path="/data/photos/p.jpg",
            rel_path="p.jpg",
            filename="p.jpg",
            extension="jpg",
            size=1,
            mtime=datetime.now(UTC),
            metadata_=dict(_GEO_META),
            user_metadata={},
        )
        s.add(item)
        await s.commit()
        return lib.id, item.id


@pytest.mark.asyncio
async def test_replace_docs_uses_add_or_replace_not_merge(monkeypatch):
    """The whole point of ``replace_docs``: Meili's add-or-UPDATE (``PUT``, what
    ``upsert_docs`` uses) MERGES, so an omitted ``_geo`` would survive; add-or-
    REPLACE (``POST`` = ``add_documents``) rewrites the document wholesale."""
    calls: dict[str, list] = {"add": [], "update": []}

    class _Idx:
        async def add_documents(self, docs, primary_key=None):
            calls["add"].append(docs)

        async def update_documents(self, docs, primary_key=None):
            calls["update"].append(docs)

    class _C:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        def index(self, name):
            return _Idx()

    monkeypatch.setattr(search_mod, "client", lambda: _C())
    await search_mod.replace_docs([{"id": "1"}])
    await search_mod.upsert_docs([{"id": "1"}])
    assert calls["add"] == [[{"id": "1"}]]
    assert calls["update"] == [[{"id": "1"}]]


@pytest.mark.asyncio
async def test_reproject_library_rewrites_documents_without_geo(monkeypatch, maker):
    """Flip OFF: the re-projection writes documents with NO ``_geo`` through the
    REPLACE path, which is what actually removes the stale point from the index."""
    from filearr.tasks import index_sync

    lib_id, item_id = await _seed_library(maker, expose_gps=True)
    written: list[list[dict]] = []

    async def _replace(docs):
        written.append(docs)

    monkeypatch.setattr(index_sync, "SessionLocal", maker)
    monkeypatch.setattr(index_sync, "replace_docs", _replace)

    # exposed -> the point is projected
    assert await index_sync.reproject_library(str(lib_id)) == 1
    assert written[-1][0]["_geo"] == {"lat": _LAT, "lng": _LNG}

    async with maker() as s:
        lib = await s.get(Library, lib_id)
        lib.expose_gps = False
        await s.commit()

    assert await index_sync.reproject_library(str(lib_id)) == 1
    assert "_geo" not in written[-1][0]
    assert written[-1][0]["id"] == str(item_id)
    # the extracted truth in Postgres is untouched (invariant 2)
    async with maker() as s:
        assert (await s.get(Item, item_id)).metadata_["exif.gps_latitude"] == _LAT


@pytest.mark.asyncio
async def test_reproject_library_tolerates_a_deleted_library(monkeypatch, maker):
    from filearr.tasks import index_sync

    monkeypatch.setattr(index_sync, "SessionLocal", maker)
    monkeypatch.setattr(index_sync, "replace_docs", lambda docs: None)
    assert await index_sync.reproject_library(str(uuid.uuid4())) == 0


@pytest.fixture
async def api_client(maker, monkeypatch):
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c
    app.dependency_overrides.clear()


@pytest.fixture
def deferred(monkeypatch):
    """Capture ``reproject_library.defer_async`` and neutralise the job-queue pool
    (the API process would otherwise open a procrastinate connection)."""
    import contextlib

    from filearr.api import libraries as libraries_api
    from filearr.tasks import index_sync

    calls: list[dict] = []

    @contextlib.asynccontextmanager
    async def _noop_pool():
        yield

    async def _defer(**kw):
        calls.append(kw)

    monkeypatch.setattr(libraries_api, "open_pool_if_needed", _noop_pool)
    monkeypatch.setattr(index_sync.reproject_library, "defer_async", _defer)
    return calls


@pytest.mark.asyncio
async def test_patch_expose_gps_triggers_reprojection(api_client, maker, deferred):
    lib_id, _ = await _seed_library(maker, expose_gps=False)

    r = await api_client.patch(f"/api/v1/libraries/{lib_id}", json={"expose_gps": True})
    assert r.status_code == 200, r.text
    assert deferred == [{"library_id": str(lib_id)}]

    # ...and the OFF direction, the security-critical one.
    r = await api_client.patch(f"/api/v1/libraries/{lib_id}", json={"expose_gps": False})
    assert r.status_code == 200, r.text
    assert deferred[-1] == {"library_id": str(lib_id)}
    assert len(deferred) == 2


@pytest.mark.asyncio
async def test_patch_without_gps_change_does_not_reproject(api_client, maker, deferred):
    """An unrelated edit (or a no-op write of the same value) must not queue a
    whole-library re-projection."""
    lib_id, _ = await _seed_library(maker, expose_gps=False)
    r = await api_client.patch(f"/api/v1/libraries/{lib_id}", json={"name": "renamed"})
    assert r.status_code == 200, r.text
    r = await api_client.patch(f"/api/v1/libraries/{lib_id}", json={"expose_gps": False})
    assert r.status_code == 200, r.text
    assert deferred == []


# --------------------------------------------------------------------------- #
# 5. Federated multi-search                                                    #
# --------------------------------------------------------------------------- #
def _federated_result(item_uid: str, chunk_uid: str):
    """A Meili-shaped federated response: ONE merged hit list, each hit carrying
    the ``_federation`` block the engine attaches."""

    def _make(queries, federation):
        uids = {q.index_uid for q in queries}
        hits = []
        if item_uid in uids:
            hits.append(
                {
                    "id": "11111111-1111-1111-1111-111111111111",
                    "filename": "beach.jpg",
                    "title": "beach",
                    "body_text": "should not be echoed",
                    "_federation": {"indexUid": item_uid, "weightedRankingScore": 0.9},
                }
            )
        if chunk_uid in uids:
            hits.append(
                {
                    "id": "22222222-2222-2222-2222-222222222222_3",
                    "item_id": "22222222-2222-2222-2222-222222222222",
                    "chunk_no": 3,
                    "filename": "notes.pdf",
                    "rel_path": "docs/notes.pdf",
                    "text": "x" * 5000,
                    "_federation": {"indexUid": chunk_uid, "weightedRankingScore": 0.4},
                }
            )
        return SimpleNamespace(
            hits=hits,
            estimated_total_hits=len(hits),
            facets_by_index={item_uid: {"extension": {"jpg": 1}}},
        )

    return _make


def _federated_app(monkeypatch, scope_filter=None):
    s = get_settings()
    item_uid = s.meili_index
    chunk_uid = chunking_mod.chunks_index_uid(s)
    transport, sink, app = _app(
        monkeypatch,
        federated=_federated_result(item_uid, chunk_uid),
        scope_filter=scope_filter,
    )
    return transport, sink, app, item_uid, chunk_uid


@pytest.mark.asyncio
async def test_federated_merges_both_indexes_and_labels_sources(monkeypatch):
    transport, sink, _, item_uid, chunk_uid = _federated_app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/federated?q=beach&limit=5")
    assert r.status_code == 200, r.text
    body = r.json()
    # both indexes were queried, in ONE federated call
    assert {q.index_uid for q in sink["queries"]} == {item_uid, chunk_uid}
    assert sink["federation"].limit == 5
    assert body["indexes"] == ["items", "passages"]
    # one merged list, each hit labelled with the index it came from
    assert [h["source"] for h in body["hits"]] == ["items", "passages"]
    assert body["hits"][0]["score"] == 0.9


@pytest.mark.asyncio
async def test_federated_hits_resolve_to_items_without_lookups(monkeypatch):
    """Chunk documents carry ``item_id`` (plus denormalised filename/rel_path), so
    a merged list renders with zero per-hit lookups — no N+1."""
    transport, _, _, _, _ = _federated_app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get("/api/v1/search/federated?q=beach")).json()
    item_hit, passage_hit = body["hits"]
    assert item_hit["item_id"] == "11111111-1111-1111-1111-111111111111"
    assert passage_hit["item_id"] == "22222222-2222-2222-2222-222222222222"
    assert passage_hit["fields"]["rel_path"] == "docs/notes.pdf"
    # passage text is a snippet, not a document dump
    assert len(passage_hit["fields"]["text"]) == 2000
    # the item hit does not echo the whole indexed body
    assert "body_text" not in item_hit["fields"]


@pytest.mark.asyncio
async def test_federated_carries_rbac_filter_into_every_subquery(monkeypatch):
    """A federated query is a new way to reach documents, so it is a new place to
    get RBAC wrong: EVERY sub-query must carry the compiled scope filter."""
    scope = 'path_scope IN ["lib_a.docs"]'
    transport, sink, app, _, _ = _federated_app(monkeypatch, scope_filter=scope)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/federated?q=secret")
    assert r.status_code == 200, r.text
    assert len(sink["queries"]) == 2
    for q in sink["queries"]:
        assert scope in (q.filter or ""), q.index_uid
    app.dependency_overrides.clear()


@pytest.mark.asyncio
async def test_federated_subqueries_only_use_filters_their_index_supports(monkeypatch):
    """The chunks index has no ``status``/``is_sidecar`` attribute; sending them
    would make Meilisearch reject the whole multi-search."""
    lib = "0192aaaa-bbbb-7ccc-8ddd-eeeeffff0000"
    transport, sink, _, item_uid, chunk_uid = _federated_app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/v1/search/federated?q=x&library={lib}")
    assert r.status_code == 200, r.text
    by_uid = {q.index_uid: (q.filter or "") for q in sink["queries"]}
    assert "status = 'active'" in by_uid[item_uid]
    assert "is_sidecar = false" in by_uid[item_uid]
    assert "status" not in by_uid[chunk_uid]
    assert "is_sidecar" not in by_uid[chunk_uid]
    # the shared, index-agnostic filter still reaches both
    assert f"library_id = '{lib}'" in by_uid[item_uid]
    assert f"library_id = '{lib}'" in by_uid[chunk_uid]


@pytest.mark.asyncio
async def test_federated_library_must_be_a_uuid(monkeypatch):
    """The library id is interpolated into the filter of EVERY sub-query, next to
    the RBAC scope clause; typing it as a UUID means nothing a caller types can
    reach that expression."""
    transport, _, _, _, _ = _federated_app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/federated?q=x&library=' OR path_scope = 'x")
    assert r.status_code == 422, r.text


@pytest.mark.asyncio
async def test_federated_facets_by_index_uses_logical_names(monkeypatch):
    transport, sink, _, _, _ = _federated_app(monkeypatch)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        body = (await c.get("/api/v1/search/federated?q=x&facets=true")).json()
    assert sink["federation"].facets_by_index is not None
    assert body["facets_by_index"]["items"] == {"extension": {"jpg": 1}}


@pytest.mark.asyncio
async def test_federated_degrades_when_the_chunks_index_is_absent(monkeypatch):
    """Chunking is a per-library opt-in; with no chunks index the federation still
    answers over the item index instead of 500ing."""
    from meilisearch_python_sdk.errors import MeilisearchApiError

    s = get_settings()
    item_uid, chunk_uid = s.meili_index, chunking_mod.chunks_index_uid(s)
    inner = _federated_result(item_uid, chunk_uid)
    state = {"n": 0}

    def _make(queries, federation):
        state["n"] += 1
        if state["n"] == 1:
            err = MeilisearchApiError.__new__(MeilisearchApiError)
            err.code = "index_not_found"
            raise err
        return inner(queries, federation)

    transport, sink, _ = _app(monkeypatch, federated=_make)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get("/api/v1/search/federated?q=x")
    assert r.status_code == 200, r.text
    assert r.json()["indexes"] == ["items"]
    assert [h["source"] for h in r.json()["hits"]] == ["items"]


# --------------------------------------------------------------------------- #
# 6. Chunk documents carry the same RBAC scope shape as item documents         #
# --------------------------------------------------------------------------- #
def test_chunk_doc_projects_the_path_scope_ancestor_array():
    """One compiled scope expression must be correct against BOTH indexes: the
    filter tests array membership on every ancestor prefix, so a chunk doc that
    carried only the leaf scope would drop passages the caller may see."""
    item = _make_item(path_scope="lib_a.photos.trip")
    chunk = DocChunk(item_id=item.id, chunk_no=0, text_="hello", embedding=None)
    doc = chunking_mod.build_chunk_doc(chunk, item)
    assert doc["path_scope"] == ["lib_a", "lib_a.photos", "lib_a.photos.trip"]
    assert doc["item_id"] == str(item.id)
