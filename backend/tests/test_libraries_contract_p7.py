"""P7 Task 1 — library/scan API contract fixes.

* B3: duplicate library name on create/rename -> 409 with a readable detail.
* B4: name/root_path stripped + non-blank; include/exclude globs validated with
  the scan's own GitIgnoreSpec construction -> 422 naming the bad line.
* B9: ``GET /scans`` filters (status list, library_id, limit) + ``rel_path``.
* B10: ``LibraryOut.last_scan`` = newest FULL run; ``last_scoped_scan`` apart.
* B11: trigger_scan's pre-defer reap records ``stats.error`` (full runs only).
* Library ``description``: optional free text, stripped, blank -> null, capped.
"""

from __future__ import annotations

import contextlib
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Library, ScanRun
from filearr.presets import build_exclusion_spec, glob_errors

BACKEND_DIR = Path(__file__).resolve().parent.parent
LIBS = "/api/v1/libraries"
SCANS = "/api/v1/scans"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def wired(pg_uri, monkeypatch):
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM scan_runs"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield {"client": c, "maker": maker}
    app.dependency_overrides.clear()
    await engine.dispose()


# --------------------------------------------------------------------------- #
# B3 — duplicate name -> 409                                                   #
# --------------------------------------------------------------------------- #
async def test_create_duplicate_name_is_409(wired):
    c = wired["client"]
    assert (await c.post(LIBS, json={"name": "Movies", "root_path": "/a"})).status_code == 201
    r = await c.post(LIBS, json={"name": "Movies", "root_path": "/b"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == 'a library named "Movies" already exists'
    # The session was rolled back: the API keeps working afterwards.
    assert (await c.post(LIBS, json={"name": "Shows", "root_path": "/b"})).status_code == 201


async def test_create_duplicate_after_strip_is_409(wired):
    c = wired["client"]
    assert (await c.post(LIBS, json={"name": "Music", "root_path": "/a"})).status_code == 201
    r = await c.post(LIBS, json={"name": "  Music ", "root_path": "/b"})
    assert r.status_code == 409
    assert r.json()["detail"] == 'a library named "Music" already exists'


async def test_rename_to_existing_name_is_409(wired):
    c = wired["client"]
    await c.post(LIBS, json={"name": "A", "root_path": "/a"})
    b = (await c.post(LIBS, json={"name": "B", "root_path": "/b"})).json()
    r = await c.patch(f"{LIBS}/{b['id']}", json={"name": "A"})
    assert r.status_code == 409, r.text
    assert r.json()["detail"] == 'a library named "A" already exists'
    # unchanged on disk
    async with wired["maker"]() as s:
        names = set((await s.execute(select(Library.name))).scalars())
    assert names == {"A", "B"}


# --------------------------------------------------------------------------- #
# B4 — strip / non-blank / glob validation                                     #
# --------------------------------------------------------------------------- #
async def test_create_strips_name_and_root(wired):
    r = await wired["client"].post(LIBS, json={"name": "  Pics  ", "root_path": " /data/p "})
    assert r.status_code == 201, r.text
    assert r.json()["name"] == "Pics"
    assert r.json()["root_path"] == "/data/p"


@pytest.mark.parametrize(
    "body",
    [
        {"name": "", "root_path": "/a"},
        {"name": "   ", "root_path": "/a"},
        {"name": "X", "root_path": ""},
        {"name": "X", "root_path": "  "},
    ],
)
async def test_create_rejects_blank_name_or_root(wired, body):
    r = await wired["client"].post(LIBS, json=body)
    assert r.status_code == 422, r.text


@pytest.mark.parametrize(
    "patch",
    [{"name": ""}, {"name": "  "}, {"root_path": ""}, {"name": None}, {"root_path": None}],
)
async def test_patch_rejects_blank_or_null_name_root(wired, patch):
    c = wired["client"]
    lib = (await c.post(LIBS, json={"name": "P", "root_path": "/p"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json=patch)
    assert r.status_code == 422, r.text


NOT_NULL_FIELDS = [
    "enabled_categories",
    "enabled_groups",
    "include_globs",
    "exclude_globs",
    "enabled_presets",
    "watch_mode",
    "hash_policy",
    "ocr_enabled",
    "chunking_enabled",
    "expose_gps",
    "count_pruned_files",
    "enabled",
]


@pytest.mark.parametrize("field", NOT_NULL_FIELDS)
async def test_patch_null_on_not_null_column_is_422(wired, field):
    """Explicit null on a NOT NULL column is a 422 naming the field (never an
    IntegrityError / 500, never a silent coercion to the default)."""
    c = wired["client"]
    lib = (await c.post(LIBS, json={"name": "N", "root_path": "/n"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={field: None})
    assert r.status_code == 422, r.text
    assert any(field in err["loc"] for err in r.json()["detail"]), r.text


@pytest.mark.parametrize(
    "field",
    [
        "description",
        "scan_cron",
        "native_prefix",
        "share_prefix",
        "hash_full_max_bytes",
        "extract_overrides",
    ],
)
async def test_patch_null_clears_nullable_column(wired, field):
    c = wired["client"]
    lib = (await c.post(LIBS, json={"name": "M", "root_path": "/m"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={field: None})
    assert r.status_code == 200, r.text
    assert r.json()[field] is None


async def test_patch_empty_list_clears_globs(wired):
    c = wired["client"]
    lib = (
        await c.post(LIBS, json={"name": "G", "root_path": "/g", "exclude_globs": ["*.tmp"]})
    ).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={"exclude_globs": []})
    assert r.status_code == 200, r.text
    assert r.json()["exclude_globs"] == []


class _Diag:
    def __init__(self, constraint_name):
        self.constraint_name = constraint_name


class _Orig(Exception):
    def __init__(self, sqlstate, constraint_name):
        super().__init__("boom")
        self.sqlstate = sqlstate
        self.diag = _Diag(constraint_name)


@pytest.mark.parametrize(
    ("sqlstate", "constraint", "expected"),
    [
        ("23505", "libraries_name_key", True),
        ("23505", "uq_libraries_source_agent_ref", False),
        ("23502", None, False),  # NOT NULL violation
        ("23503", "libraries_source_agent_id_fkey", False),
    ],
)
def test_is_duplicate_name_only_for_name_unique(sqlstate, constraint, expected):
    from sqlalchemy.exc import IntegrityError

    from filearr.api.libraries import _is_duplicate_name

    exc = IntegrityError("stmt", {}, _Orig(sqlstate, constraint))
    assert _is_duplicate_name(exc) is expected


async def test_other_integrity_error_is_not_409(wired):
    """A non-name IntegrityError propagates instead of masquerading as a
    duplicate name."""
    from fastapi import HTTPException
    from sqlalchemy.exc import IntegrityError

    from filearr.api.libraries import _commit_unique_name

    async with wired["maker"]() as s:
        s.add(Library(name="dup-agent-a", root_path="/a"))
        await s.commit()
        # NOT NULL root_path violation (bypasses the schema on purpose).
        s.add(Library(name="no-root", root_path=None))
        with pytest.raises(IntegrityError) as ei:
            await _commit_unique_name(s, "no-root")
        assert not isinstance(ei.value, HTTPException)
    async with wired["maker"]() as s:
        s.add(Library(name="dup-agent-a", root_path="/b"))
        with pytest.raises(HTTPException) as hi:
            await _commit_unique_name(s, "dup-agent-a")
        assert hi.value.status_code == 409


async def test_patch_strips_and_omitted_fields_untouched(wired):
    c = wired["client"]
    lib = (await c.post(LIBS, json={"name": "P", "root_path": "/p"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={"name": " Q "})
    assert r.status_code == 200, r.text
    assert r.json()["name"] == "Q"
    assert r.json()["root_path"] == "/p"


@pytest.mark.parametrize(
    ("field", "globs", "bad"),
    [
        ("exclude_globs", ["*.tmp", "foo\\"], "line 2 'foo\\\\'"),
        ("exclude_globs", ["\\"], "line 1"),
        ("exclude_globs", ["**/[z-a]"], "line 1"),
        ("include_globs", ["!"], "line 1 '!'"),
        ("include_globs", ["*.mkv", ""], "line 2 ''"),
        ("include_globs", ["ok/*", "!\\"], "line 2"),
    ],
)
async def test_bad_glob_is_422_naming_the_line(wired, field, globs, bad):
    c = wired["client"]
    r = await c.post(LIBS, json={"name": "G", "root_path": "/g", field: globs})
    assert r.status_code == 422, r.text
    err = r.json()["detail"][0]
    assert err["loc"][-1] == field
    assert bad in err["msg"]
    # PATCH runs the same check.
    lib = (await c.post(LIBS, json={"name": "G2", "root_path": "/g"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={field: globs})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == field


async def test_valid_globs_accepted(wired):
    r = await wired["client"].post(
        LIBS,
        json={
            "name": "OK",
            "root_path": "/ok",
            "exclude_globs": ["*.tmp", "cache/", "# comment", ""],
            "include_globs": ["keep.tmp", "!also.tmp"],
        },
    )
    assert r.status_code == 201, r.text


def test_glob_errors_agree_with_scan_spec():
    """Whatever glob_errors accepts, the scan's spec builder accepts too (and
    vice versa for the rejected samples) — one construction, not two."""
    good_ex, good_inc = ["*.tmp", "cache/", "[Tt]humbs.db"], ["keep.tmp", "!x"]
    assert glob_errors(good_ex, include=False) == []
    assert glob_errors(good_inc, include=True) == []
    build_exclusion_spec([], extra_excludes=good_ex, includes=good_inc)
    for ex, inc in ((["foo\\"], []), ([], ["!"]), ([], [""])):
        assert glob_errors(ex, include=False) or glob_errors(inc, include=True)
        with pytest.raises(Exception):  # noqa: B017 - pathspec raises several types
            build_exclusion_spec([], extra_excludes=ex, includes=inc)


async def test_listing_survives_legacy_bad_glob(wired):
    """LibraryOut must serialise any stored row (validators live on the write
    model only), or a pre-validation bad glob would 500 the whole listing."""
    async with wired["maker"]() as s:
        s.add(Library(name="legacy", root_path="/l", exclude_globs=["foo\\"]))
        await s.commit()
    r = await wired["client"].get(LIBS)
    assert r.status_code == 200, r.text
    assert r.json()[0]["exclude_globs"] == ["foo\\"]


# --------------------------------------------------------------------------- #
# B9 — GET /scans filters + rel_path                                           #
# --------------------------------------------------------------------------- #
async def _seed_runs(maker):
    now = datetime.now(UTC)
    async with maker() as s:
        a = Library(name="A", root_path="/a")
        b = Library(name="B", root_path="/b")
        s.add_all([a, b])
        await s.flush()
        # A long-running FULL scan of A, older than 60 scoped runs of B.
        s.add(ScanRun(library_id=a.id, status="running", started_at=now - timedelta(hours=2)))
        for i in range(60):
            s.add(
                ScanRun(
                    library_id=b.id,
                    status="finished",
                    rel_path=f"hot/{i}",
                    started_at=now - timedelta(minutes=60 - i),
                    finished_at=now - timedelta(minutes=59 - i),
                )
            )
        s.add(ScanRun(library_id=b.id, status="stopping", started_at=now - timedelta(hours=3)))
        s.add(
            ScanRun(
                library_id=a.id,
                status="failed",
                started_at=now - timedelta(days=1),
                stats={"error": "boom"},
            )
        )
        await s.commit()
        return a.id, b.id


async def test_list_scans_default_window_and_rel_path(wired):
    await _seed_runs(wired["maker"])
    r = await wired["client"].get(SCANS)
    assert r.status_code == 200, r.text
    rows = r.json()
    assert len(rows) == 50  # unchanged default
    assert all("rel_path" in row for row in rows)
    assert rows[0]["rel_path"] == "hot/59"  # newest first
    # the running full scan of A has dropped out of the global window (B9) …
    assert all(row["status"] != "running" for row in rows)


async def test_list_scans_status_filter_finds_live_runs(wired):
    a, b = await _seed_runs(wired["maker"])
    r = await wired["client"].get(SCANS, params={"status": "running,stopping"})
    assert r.status_code == 200, r.text
    got = {(row["library_id"], row["status"], row["rel_path"]) for row in r.json()}
    assert got == {(str(a), "running", None), (str(b), "stopping", None)}


async def test_list_scans_library_and_limit(wired):
    a, b = await _seed_runs(wired["maker"])
    c = wired["client"]
    rows = (await c.get(SCANS, params={"library_id": str(a)})).json()
    assert [row["status"] for row in rows] == ["running", "failed"]
    assert len((await c.get(SCANS, params={"library_id": str(b), "limit": 500})).json()) == 61
    assert len((await c.get(SCANS, params={"limit": 5})).json()) == 5
    rows = (await c.get(SCANS, params={"library_id": str(a), "status": "failed"})).json()
    assert len(rows) == 1 and rows[0]["stats"]["error"] == "boom"


@pytest.mark.parametrize(
    "params",
    [
        {"status": "completed"},
        {"status": "running,bogus"},
        {"status": ","},
        {"limit": 0},
        {"limit": 501},
        {"library_id": "nope"},
    ],
)
async def test_list_scans_rejects_bad_params(wired, params):
    r = await wired["client"].get(SCANS, params=params)
    assert r.status_code == 422, r.text


# --------------------------------------------------------------------------- #
# B10 — last_scan full-only + last_scoped_scan                                 #
# --------------------------------------------------------------------------- #
async def test_last_scan_is_full_and_scoped_rides_apart(wired):
    now = datetime.now(UTC)
    async with wired["maker"]() as s:
        lib = Library(name="L", root_path="/l")
        s.add(lib)
        await s.flush()
        full = ScanRun(
            library_id=lib.id,
            status="finished",
            started_at=now - timedelta(hours=5),
            finished_at=now - timedelta(hours=4),
            stats={"seen": 500_000, "files_per_s": 100.0},
        )
        # Three finished full runs back the throughput median.
        for h in (30, 40):
            s.add(
                ScanRun(
                    library_id=lib.id,
                    status="finished",
                    started_at=now - timedelta(hours=h),
                    stats={"files_per_s": 200.0},
                )
            )
        scoped = ScanRun(
            library_id=lib.id,
            status="failed",
            rel_path="hot",
            started_at=now - timedelta(minutes=5),
            stats={"seen": 12, "files_per_s": 5.0, "error": "disk gone"},
        )
        s.add_all([full, scoped])
        await s.commit()
        full_id, scoped_id = full.id, scoped.id

    body = (await wired["client"].get(LIBS)).json()[0]
    last, last_scoped = body["last_scan"], body["last_scoped_scan"]
    assert last["id"] == str(full_id)
    assert last["rel_path"] is None
    assert last["seen"] == 500_000
    assert last["median_files_per_s"] == 200.0
    assert last["throughput_runs"] == 3
    assert last_scoped["id"] == str(scoped_id)
    assert last_scoped["rel_path"] == "hot"
    assert last_scoped["status"] == "failed"
    assert last_scoped["error"] == "disk gone"
    # the full-scan median never rides on a scoped run (not comparable)
    assert last_scoped["median_files_per_s"] is None
    assert last_scoped["throughput_runs"] == 0


async def test_last_scan_null_when_only_scoped_runs(wired):
    async with wired["maker"]() as s:
        lib = Library(name="S", root_path="/s")
        s.add(lib)
        await s.flush()
        s.add(ScanRun(library_id=lib.id, status="finished", rel_path="x"))
        await s.commit()
    body = (await wired["client"].get(LIBS)).json()[0]
    assert body["last_scan"] is None
    assert body["last_scoped_scan"]["rel_path"] == "x"


async def test_last_scan_carries_reconcile_note(wired):
    async with wired["maker"]() as s:
        lib = Library(name="R", root_path="/r")
        s.add(lib)
        await s.flush()
        s.add(ScanRun(library_id=lib.id, status="failed", stats={"reconcile_note": "orphan"}))
        await s.commit()
    last = (await wired["client"].get(LIBS)).json()[0]["last_scan"]
    assert last["error"] is None
    assert last["reconcile_note"] == "orphan"


# --------------------------------------------------------------------------- #
# B11 — trigger_scan's pre-defer reap records stats.error                      #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("queue_reachable", [True, False])
async def test_trigger_reap_records_error_full_runs_only(wired, monkeypatch, queue_reachable):
    from filearr.api import libraries as lib_api

    async with wired["maker"]() as s:
        lib = Library(name="T", root_path="/t")
        s.add(lib)
        await s.flush()
        orphan = ScanRun(library_id=lib.id, status="running", stats={"seen": 5})
        stopping = ScanRun(library_id=lib.id, status="stopping")
        scoped = ScanRun(library_id=lib.id, status="running", rel_path="hot")
        s.add_all([orphan, stopping, scoped])
        await s.commit()
        lib_id, ids = lib.id, (orphan.id, stopping.id, scoped.id)

    @contextlib.asynccontextmanager
    async def _pool():
        if not queue_reachable:
            raise OSError("queue unreachable")
        yield

    async def _not_pending(library_id, rel_path=None):
        return False

    async def _fake_defer(library_id, *, force=False, force_empty=False):
        return 42

    monkeypatch.setattr(lib_api, "open_pool_if_needed", _pool)
    monkeypatch.setattr(lib_api, "scan_job_pending", _not_pending)
    monkeypatch.setattr(lib_api, "defer_scan", _fake_defer)

    r = await wired["client"].post(f"{LIBS}/{lib_id}/scan")
    assert r.status_code == 202, r.text
    assert r.json() == {"job_id": 42}

    async with wired["maker"]() as s:
        rows = {
            run.id: run
            for run in (await s.execute(select(ScanRun).where(ScanRun.id.in_(ids)))).scalars()
        }
    for rid in ids[:2]:
        run = rows[rid]
        assert run.status == "failed"
        assert run.finished_at is not None
        assert run.stats["reaped"] is True
        if queue_reachable:
            assert run.stats["error"].startswith("no queued or running scan job remained")
        else:
            assert run.stats["error"].startswith("job queue unreachable")
    assert rows[ids[0]].stats["seen"] == 5  # existing stats preserved (merge)
    # a scoped run's job was never checked -> left for the FIX-15 reconciler
    assert rows[ids[2]].status == "running"
    assert "error" not in (rows[ids[2]].stats or {})


# --------------------------------------------------------------------------- #
# Library description — optional note, stripped, blank -> null, capped         #
# --------------------------------------------------------------------------- #
async def test_create_with_description_strips_and_lists(wired):
    c = wired["client"]
    r = await c.post(
        LIBS,
        json={"name": "Docs", "root_path": "/d", "description": "  Scanned PDFs\nfrom the NAS  "},
    )
    assert r.status_code == 201, r.text
    assert r.json()["description"] == "Scanned PDFs\nfrom the NAS"
    listed = {lib["name"]: lib for lib in (await c.get(LIBS)).json()}
    assert listed["Docs"]["description"] == "Scanned PDFs\nfrom the NAS"


async def test_create_without_or_blank_description_is_null(wired):
    c = wired["client"]
    a = (await c.post(LIBS, json={"name": "A", "root_path": "/a"})).json()
    b = (await c.post(LIBS, json={"name": "B", "root_path": "/b", "description": "   "})).json()
    assert a["description"] is None
    assert b["description"] is None


async def test_patch_description_sets_and_blank_clears(wired):
    c = wired["client"]
    lib = (await c.post(LIBS, json={"name": "M", "root_path": "/m"})).json()
    r = await c.patch(f"{LIBS}/{lib['id']}", json={"description": " Films "})
    assert r.status_code == 200, r.text
    assert r.json()["description"] == "Films"
    # A patch of another field leaves the description alone.
    r = await c.patch(f"{LIBS}/{lib['id']}", json={"watch_mode": False})
    assert r.json()["description"] == "Films"
    r = await c.patch(f"{LIBS}/{lib['id']}", json={"description": ""})
    assert r.status_code == 200, r.text
    assert r.json()["description"] is None
    async with wired["maker"]() as s:
        row = (await s.execute(select(Library).where(Library.name == "M"))).scalar_one()
        assert row.description is None


async def test_description_length_cap(wired):
    from filearr.schemas import LIBRARY_DESCRIPTION_MAX

    c = wired["client"]
    at_cap = "x" * LIBRARY_DESCRIPTION_MAX
    ok = await c.post(LIBS, json={"name": "Cap", "root_path": "/c", "description": at_cap})
    assert ok.status_code == 201, ok.text
    # Surrounding whitespace does not count against the cap.
    ok2 = await c.patch(f"{LIBS}/{ok.json()['id']}", json={"description": f"  {at_cap}  "})
    assert ok2.status_code == 200, ok2.text
    over = "x" * (LIBRARY_DESCRIPTION_MAX + 1)
    r = await c.post(LIBS, json={"name": "Over", "root_path": "/o", "description": over})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == "description"
    r = await c.patch(f"{LIBS}/{ok.json()['id']}", json={"description": over})
    assert r.status_code == 422, r.text
    assert r.json()["detail"][0]["loc"][-1] == "description"
