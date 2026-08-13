"""IN-T1..T4 (2026-08-13) — the "insight features" backend half.

Governing principle under test (user-stated): *Filearr provides insight, it never
manages the files.* Nothing here touches a byte on disk — the value delivered is
a per-copy report an operator's OWN script can act on safely, so the tests are
correspondingly obsessed with the properties a script depends on:

* **IN-T1 ``duplicate_files_detail``** — window-query ranking (newest first),
  a DETERMINISTIC tie-break (a script must pick the same "keep" file on every
  re-run, or it deletes a different copy each night), group contiguity + biggest-
  waste-first ordering (a *limited* export must still be usable), per-row
  ``hash_tier`` (a sampled quick_hash group is not a delete-without-verifying
  group), and the same base predicate as the aggregate report.
* **IN-T2 ``stale_files``** — the parameterized threshold, its 1..36500 API
  bounds, its rejection on reports that declare none, and its round-trip through
  a BACKGROUND export's ``params`` (a queued export silently running at the
  default while the UI showed the operator "3650" is the failure mode).
* **IN-T3 ``GET /reports/folder-tree``** — one drill level: children-of-parent
  aggregation, the reserved ``"."`` files-here child, ``has_children``,
  all-libraries root mode, RBAC scoping, and limit/truncated.
* **IN-T4 ``POST /items/batch``** — null-pops-key parity with the single PATCH
  (a bulk "clear this field" previously wrote a literal JSON null into every row)
  and the 500-key request cap.
"""

from __future__ import annotations

import uuid as _uuid
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Item, Library

BACKEND_DIR = Path(__file__).resolve().parent.parent

REPORTS = "/api/v1/reports"
ITEMS = "/api/v1/items"


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def api(pg_uri, monkeypatch):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM item_versions"))
        await conn.execute(text("DELETE FROM report_exports"))
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)

    # A successful metadata write defers an index-sync job; the procrastinate
    # queue is not wired to this throwaway DB, so stub the defer to a no-op.
    import filearr.api.items as items_mod

    async def _noop_defer(ids):
        return None

    monkeypatch.setattr(items_mod, "defer_index_sync", _noop_defer)

    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()
    await engine.dispose()


async def _mk_lib(maker, name="Lib", *, native_prefix=None, share_prefix=None):
    async with maker() as s:
        lib = Library(
            name=name,
            root_path="/data/l",
            native_prefix=native_prefix,
            share_prefix=share_prefix,
        )
        s.add(lib)
        await s.commit()
        return lib.id


async def _mk_item(
    maker,
    library_id,
    rel_path,
    *,
    status="active",
    size=100,
    mtime=None,
    content_hash=None,
    quick_hash=None,
    path_scope=None,
    user_metadata=None,
):
    async with maker() as s:
        item = Item(
            library_id=library_id,
            file_category="other",
            file_group="other",
            status=status,
            path=f"/data/l/{rel_path}",
            rel_path=rel_path,
            filename=rel_path.rsplit("/", 1)[-1],
            extension="bin",
            size=size,
            mtime=mtime or datetime.now(UTC),
            metadata_={},
            user_metadata=user_metadata or {},
            external_ids={},
            content_hash=content_hash,
            quick_hash=quick_hash,
            path_scope=path_scope,
            tags=[],
        )
        s.add(item)
        await s.commit()
        return str(item.id)


# =========================================================================== #
# IN-T1 — duplicate_files_detail                                              #
# =========================================================================== #
async def test_detail_ranks_newest_first_and_marks_keep_hint(api):
    """rank 0 = the NEWEST copy by mtime; keep_hint mirrors it. This is the whole
    contract a delete script leans on: it acts ONLY on 'candidate' rows."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    async def _copy(rel, days_ago):
        return await _mk_item(
            maker, lib, rel, size=100, content_hash="dead",
            mtime=now - timedelta(days=days_ago),
        )

    await _copy("old.bin", 10)
    await _copy("new.bin", 0)
    await _copy("mid.bin", 1)

    rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
    assert [r["rel_path"] for r in rows] == ["new.bin", "mid.bin", "old.bin"]
    assert [r["group_rank"] for r in rows] == [0, 1, 2]
    assert [r["keep_hint"] for r in rows] == ["keep", "candidate", "candidate"]
    assert {r["copies_in_group"] for r in rows} == {3}
    assert {r["group_key"] for r in rows} == {"dead"}


async def test_detail_tie_break_is_stable_across_runs(api):
    """Identical mtimes (a `cp -p`/`rsync -a` copy keeps the timestamp) must not
    make the ranking non-deterministic: item_id is the documented tie-break, so
    re-running the report returns the SAME 'keep' row every time. Without it
    Postgres may reorder freely and a nightly script would delete a different
    copy each night."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    same = datetime.now(UTC)
    ids = [
        await _mk_item(maker, lib, f"t{i}.bin", size=100, content_hash="tie", mtime=same)
        for i in range(4)
    ]
    runs = []
    for _ in range(3):
        rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
        runs.append([r["item_id"] for r in rows])
    assert runs[0] == runs[1] == runs[2]
    # ...and the order IS ascending item_id (the declared tie-break), not chance.
    assert runs[0] == sorted(ids)


async def test_detail_orders_groups_by_wasted_bytes_and_keeps_them_contiguous(api):
    """Groups stream biggest-waste-first and never interleave — so an export
    truncated at N rows is still whole-groups-first and remains actionable."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    # small group: 2 x 10 bytes  -> wasted 10
    for i in range(2):
        await _mk_item(maker, lib, f"s{i}.bin", size=10, content_hash="small", mtime=now)
    # big group: 3 x 1000 bytes -> wasted 2000
    for i in range(3):
        await _mk_item(maker, lib, f"b{i}.bin", size=1000, content_hash="big", mtime=now)

    rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
    keys = [r["group_key"] for r in rows]
    assert keys == ["big", "big", "big", "small", "small"]  # sorted + contiguous


async def test_detail_shares_the_aggregate_reports_base_predicate(api):
    """Same membership rules as ``duplicate_files``: singletons excluded,
    quick_hash+size fallback grouping, zero-byte files excluded outright (QH-T5 —
    every empty file trivially shares a hash), non-active rows excluded."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    await _mk_item(maker, lib, "solo.bin", size=999, content_hash="lonely")  # singleton
    await _mk_item(maker, lib, "q1.bin", size=50, quick_hash="qq")  # fallback pair
    await _mk_item(maker, lib, "q2.bin", size=50, quick_hash="qq")
    for n in ("e1.bin", "e2.bin", "e3.bin"):  # zero-byte cluster
        await _mk_item(maker, lib, n, size=0, quick_hash="empty")
    await _mk_item(maker, lib, "t1.bin", size=7, content_hash="tomb", status="trashed")
    await _mk_item(maker, lib, "t2.bin", size=7, content_hash="tomb", status="trashed")

    rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
    assert {r["group_key"] for r in rows} == {"qq:50"}
    assert {r["rel_path"] for r in rows} == {"q1.bin", "q2.bin"}

    # And the two reports agree on copy counts for the group they both see.
    agg = {r["dup_key"]: r for r in (await client.get(f"{REPORTS}/duplicate_files")).json()["rows"]}
    assert agg["qq:50"]["copies"] == rows[0]["copies_in_group"]


async def test_detail_hash_tier_is_per_row_and_group_uniform(api):
    """hash_tier rides EVERY row (a script must not have to join back to the
    aggregate to learn whether a group is byte-verified), and it is computed as a
    window max so all rows of one group report the same tier the aggregate does."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    await _mk_item(maker, lib, "c1.bin", size=100, content_hash="cc")
    await _mk_item(maker, lib, "c2.bin", size=100, content_hash="cc")
    await _mk_item(maker, lib, "q1.bin", size=50, quick_hash="qq")
    await _mk_item(maker, lib, "q2.bin", size=50, quick_hash="qq")

    rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
    tiers = {}
    for r in rows:
        tiers.setdefault(r["group_key"], set()).add(r["hash_tier"])
    assert tiers == {"cc": {"content_hash"}, "qq:50": {"quick_hash"}}


async def test_detail_carries_item_id_and_full_path_context(api):
    """Everything a script needs to translate a catalog row into a real path on
    the source machine: item_id (UI link) + path/native_path/share_url/share_unc."""
    client, maker = api
    lib = await _mk_lib(
        maker, name="A", native_prefix="/mnt/user/media", share_prefix="\\\\tower\\media"
    )
    fid = await _mk_item(maker, lib, "Movies/x.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "Backup/x.bin", size=100, content_hash="dead")

    rows = (await client.get(f"{REPORTS}/duplicate_files_detail")).json()["rows"]
    by_path = {r["rel_path"]: r for r in rows}
    row = by_path["Movies/x.bin"]
    assert row["item_id"] == fid
    assert row["native_path"] == "/mnt/user/media/Movies/x.bin"
    assert row["share_unc"] == "\\\\tower\\media\\Movies\\x.bin"
    assert row["path"] == "/data/l/Movies/x.bin"
    assert row["content_hash"] == "dead" and row["quick_hash"] is None
    assert row["mtime"] is not None and row["size"] == 100


async def test_detail_row_link_is_item_and_csv_export_carries_the_action_columns(api):
    """row_link='item' (B renders it through the existing generic table) and the
    CSV export — the thing the documented scripts consume — actually contains
    keep_hint/group_rank/native_path."""
    client, maker = api
    lib = await _mk_lib(maker, name="A", native_prefix="/mnt/user/media")
    await _mk_item(maker, lib, "a.bin", size=100, content_hash="dead")
    await _mk_item(maker, lib, "b.bin", size=100, content_hash="dead")

    meta = {r["id"]: r for r in (await client.get(REPORTS)).json()["reports"]}
    assert meta["duplicate_files_detail"]["row_link"] == "item"
    assert meta["duplicate_files_detail"]["supports_threshold"] is False

    r = await client.get(f"{REPORTS}/duplicate_files_detail?format=csv")
    header = r.text.splitlines()[0].split(",")
    assert header[:5] == ["group_key", "group_rank", "copies_in_group", "hash_tier", "keep_hint"]
    assert "native_path" in header and "share_unc" in header
    assert "item_id" not in header  # item_id stays out of spreadsheets (P11 rule)


async def test_detail_rbac_scope_is_pushed_into_the_subquery(api):
    """The statement selects FROM a subquery (a window function is illegal in
    WHERE), so the scope predicate MUST be pushed down. An outer .where() would
    re-add `items` to the FROM list and cartesian-join — wrong rows AND a
    silently ineffective ACL. Asserted at the statement level because that is
    exactly where the trap is."""
    from filearr import rbac, rbac_sql
    from filearr.models import Item as _Item
    from filearr.reports import ReportParams, get_report

    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    visible = rbac.path_to_ltree("Movies/a.bin", library_id=lib)
    hidden = rbac.path_to_ltree("Private/b.bin", library_id=lib)
    await _mk_item(maker, lib, "Movies/a.bin", size=100, content_hash="dead",
                   mtime=now, path_scope=visible)
    await _mk_item(maker, lib, "Movies/a2.bin", size=100, content_hash="dead",
                   mtime=now - timedelta(days=1),
                   path_scope=rbac.path_to_ltree("Movies/a2.bin", library_id=lib))
    await _mk_item(maker, lib, "Private/b.bin", size=100, content_hash="dead",
                   mtime=now - timedelta(days=2), path_scope=hidden)

    # Resolve text-vs-ltree from the LIVE column, as production does
    # (security.py) — pgserver has no contrib so local runs see text,
    # while CI's postgres created a real ltree column (2026-08-13 CI
    # failure: starts_with(ltree, varchar) does not exist).
    async with maker() as _s:
        use_ltree = await rbac_sql.path_scope_uses_ltree(_s)
    clause = rbac_sql.scope_where_clause(
        rbac.Role.USER,
        # path_to_ltree encodes each segment ("Movies" -> "_4dovies"); a grant
        # built from the raw name would be a corrupt scope and fail closed.
        [rbac.PathGrant(
            path=rbac.path_to_ltree("Movies", library_id=lib),
            action="search_metadata",
            allow=True,
        )],
        action="search_metadata",
        column=_Item.path_scope,
        use_ltree=use_ltree,
    )
    report = get_report("duplicate_files_detail")
    stmt = report.statement(ReportParams(limit=100), clause)
    # No cartesian product: `items` appears exactly once, inside the subquery.
    assert str(stmt.compile()).count("FROM items") == 1
    async with maker() as s:
        rows = [report.row(r) for r in (await s.execute(stmt)).all()]
    # The denied copy neither appears NOR inflates copies_in_group for the rest.
    assert {r["rel_path"] for r in rows} == {"Movies/a.bin", "Movies/a2.bin"}
    assert {r["copies_in_group"] for r in rows} == {2}
    _ = client


# =========================================================================== #
# IN-T2 — stale_files + the threshold parameter                               #
# =========================================================================== #
async def test_stale_files_default_threshold_and_age_days(api):
    """Default 730 days; age_days is floor-days computed in SQL (so it stays
    correct across a long streaming export rather than drifting per row)."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    await _mk_item(maker, lib, "ancient.bin", mtime=now - timedelta(days=1000))
    await _mk_item(maker, lib, "old.bin", mtime=now - timedelta(days=800))
    await _mk_item(maker, lib, "recent.bin", mtime=now - timedelta(days=100))

    body = (await client.get(f"{REPORTS}/stale_files")).json()
    rows = body["rows"]
    assert [r["rel_path"] for r in rows] == ["ancient.bin", "old.bin"]  # oldest first
    assert rows[0]["age_days"] == 1000
    assert rows[1]["age_days"] == 800
    assert body["report"]["default_threshold_days"] == 730
    assert body["report"]["supports_threshold"] is True
    assert body["report"]["threshold_label"] == "Not modified in the last (days)"


async def test_stale_files_honours_the_threshold_param(api):
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    await _mk_item(maker, lib, "d900.bin", mtime=now - timedelta(days=900))
    await _mk_item(maker, lib, "d400.bin", mtime=now - timedelta(days=400))
    await _mk_item(maker, lib, "d10.bin", mtime=now - timedelta(days=10))

    def _names(body):
        return [r["rel_path"] for r in body["rows"]]

    assert _names((await client.get(f"{REPORTS}/stale_files?threshold_days=365")).json()) == [
        "d900.bin", "d400.bin",
    ]
    assert _names((await client.get(f"{REPORTS}/stale_files?threshold_days=1")).json()) == [
        "d900.bin", "d400.bin", "d10.bin",
    ]
    assert _names((await client.get(f"{REPORTS}/stale_files?threshold_days=5000")).json()) == []


async def test_threshold_validation_bounds(api):
    """1..36500 inclusive. 0 is rejected on purpose (it means 'every file' — never
    what an operator meant); a negative would invert the predicate into the
    future-dated query ``bad_mtime`` already owns."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    await _mk_item(maker, lib, "x.bin", mtime=datetime.now(UTC) - timedelta(days=900))
    for bad in (0, -1, 36501, 999999):
        r = await client.get(f"{REPORTS}/stale_files?threshold_days={bad}")
        assert r.status_code == 422, (bad, r.text)
    for ok in (1, 730, 36500):
        r = await client.get(f"{REPORTS}/stale_files?threshold_days={ok}")
        assert r.status_code == 200, (ok, r.text)


async def test_threshold_rejected_for_reports_that_declare_none(api):
    """A typo'd param must 422, not silently do nothing — the same reasoning as
    the existing library_id-unsupported check."""
    client, _ = api
    r = await client.get(f"{REPORTS}/largest_files?threshold_days=30")
    assert r.status_code == 422
    assert "threshold_days" in r.text


async def test_only_stale_files_declares_a_threshold(api):
    """The UI renders the numeric input off meta(); no other report may grow a
    stray control by accident."""
    client, _ = api
    reports = (await client.get(REPORTS)).json()["reports"]
    declared = {r["id"] for r in reports if r["supports_threshold"]}
    assert declared == {"stale_files"}
    for r in reports:
        assert {"supports_threshold", "threshold_label", "default_threshold_days"} <= set(r)


async def test_stale_files_streaming_export_honours_threshold(api):
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    await _mk_item(maker, lib, "d900.bin", mtime=now - timedelta(days=900))
    await _mk_item(maker, lib, "d100.bin", mtime=now - timedelta(days=100))
    r = await client.get(f"{REPORTS}/stale_files?format=ndjson&threshold_days=50")
    lines = [ln for ln in r.text.splitlines() if ln.strip()]
    assert len(lines) == 2
    r = await client.get(f"{REPORTS}/stale_files?format=ndjson&threshold_days=500")
    assert len([ln for ln in r.text.splitlines() if ln.strip()]) == 1


async def test_background_export_round_trips_threshold_days(api, monkeypatch, tmp_path):
    """The enqueue -> params -> job hop. Without the round-trip a queued export
    runs at the report DEFAULT while the UI showed the operator their number."""
    import filearr.tasks.reports as trep
    from filearr import diskguard
    from filearr import exports as exports_mod

    client, maker = api
    settings = get_settings()
    monkeypatch.setattr(settings, "export_dir", str(tmp_path / "exports"))
    monkeypatch.setattr(diskguard, "guard_write", lambda *a, **k: None)

    async def _noop(_id):
        return 1

    monkeypatch.setattr(trep, "defer_export_job", _noop)

    lib = await _mk_lib(maker, name="A")
    now = datetime.now(UTC)
    await _mk_item(maker, lib, "d900.bin", mtime=now - timedelta(days=900))
    await _mk_item(maker, lib, "d100.bin", mtime=now - timedelta(days=100))

    r = await client.post(f"{REPORTS}/stale_files/export?format=ndjson&threshold_days=50")
    assert r.status_code == 202, r.text
    export_id = r.json()["id"]

    # Stored verbatim on the row...
    async with maker() as s:
        stored = (
            await s.execute(
                text("SELECT params FROM report_exports WHERE id = :i"), {"i": export_id}
            )
        ).scalar_one()
    assert stored["threshold_days"] == 50

    # ...and rebuilt by the job: 50 days catches BOTH files (the 730 default
    # would have caught only one, which is the bug this guards).
    async with maker() as s:
        res = await exports_mod.run_export(s, _uuid.UUID(export_id), settings)
    assert res["status"] == "complete"
    assert res["rows"] == 2


async def test_background_export_rejects_out_of_range_threshold(api):
    """The background path applies the SAME validation as the sync path — an
    export must not be a way to smuggle a bad value into the same builder."""
    client, _ = api
    r = await client.post(f"{REPORTS}/stale_files/export?format=csv&threshold_days=0")
    assert r.status_code == 422
    r = await client.post(f"{REPORTS}/largest_files/export?format=csv&threshold_days=30")
    assert r.status_code == 422


# =========================================================================== #
# IN-T3 — GET /reports/folder-tree                                            #
# =========================================================================== #
async def test_folder_tree_children_of_a_parent(api):
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "Movies/2019/a.mkv", size=1000)
    await _mk_item(maker, lib, "Movies/2019/b.mkv", size=500)
    await _mk_item(maker, lib, "Movies/2020/c.mkv", size=2000)
    await _mk_item(maker, lib, "Music/song.flac", size=10)

    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}")).json()
    assert body["parent"] == "" and body["library_id"] == str(lib)
    assert body["truncated"] is False
    kids = {c["name"]: c for c in body["children"]}
    assert set(kids) == {"Movies", "Music"}
    assert kids["Movies"]["total_bytes"] == 3500 and kids["Movies"]["file_count"] == 3
    assert kids["Movies"]["folder"] == "Movies"
    assert kids["Movies"]["has_children"] is True  # 2019/2020 sit deeper
    assert kids["Music"]["has_children"] is False  # song.flac is one segment down
    assert kids["Music"]["library"] == "F" and kids["Music"]["library_id"] == str(lib)
    assert [c["name"] for c in body["children"]] == ["Movies", "Music"]  # bytes DESC

    # drill one level
    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&parent=Movies")).json()
    assert body["parent"] == "Movies"
    kids = {c["name"]: c for c in body["children"]}
    assert set(kids) == {"2019", "2020"}
    assert kids["2019"]["folder"] == "Movies/2019" and kids["2019"]["total_bytes"] == 1500
    assert kids["2020"]["has_children"] is False


async def test_folder_tree_files_here_child(api):
    """A file living directly IN the parent aggregates under the reserved '.'
    child, whose folder is the parent itself and which never claims children (the
    UI renders it but must not drill into it)."""
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "Movies/loose.mkv", size=700)
    await _mk_item(maker, lib, "Movies/also-loose.mkv", size=300)
    await _mk_item(maker, lib, "Movies/2019/a.mkv", size=100)

    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&parent=Movies")).json()
    kids = {c["name"]: c for c in body["children"]}
    assert set(kids) == {".", "2019"}
    assert kids["."]["file_count"] == 2 and kids["."]["total_bytes"] == 1000
    assert kids["."]["folder"] == "Movies"
    assert kids["."]["has_children"] is False


async def test_folder_tree_root_files_land_in_the_dot_child(api):
    """At a library root the same rule applies — root-level files are NOT
    silently dropped the way ``largest_folders`` drops them (that report has no
    folder to attribute them to; the treemap does)."""
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "rootfile.bin", size=42)
    await _mk_item(maker, lib, "Movies/a.mkv", size=10)
    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}")).json()
    kids = {c["name"]: c for c in body["children"]}
    assert kids["."]["total_bytes"] == 42
    assert kids["."]["folder"] == ""  # parent is the library root


async def test_folder_tree_all_libraries_root_mode(api):
    """No library_id => one child per LIBRARY, so the top view is library-sized
    rectangles. Each child pins library_id for the next drill."""
    client, maker = api
    small = await _mk_lib(maker, name="Small")
    big = await _mk_lib(maker, name="Big")
    empty = await _mk_lib(maker, name="Empty")
    await _mk_item(maker, small, "a.bin", size=10)
    await _mk_item(maker, big, "Movies/b.bin", size=5000)
    await _mk_item(maker, big, "Movies/c.bin", size=5000)

    body = (await client.get(f"{REPORTS}/folder-tree")).json()
    assert body["parent"] == "" and body["library_id"] is None
    names = [c["name"] for c in body["children"]]
    assert names == ["Big", "Small"]  # bytes DESC; an EMPTY library has no row
    assert str(empty) not in {c["library_id"] for c in body["children"]}
    big_row = body["children"][0]
    assert big_row["library_id"] == str(big) and big_row["folder"] == ""
    assert big_row["total_bytes"] == 10000 and big_row["file_count"] == 2
    assert big_row["has_children"] is True

    # the child's library_id is what the next call uses
    nxt = (await client.get(f"{REPORTS}/folder-tree?library_id={big_row['library_id']}")).json()
    assert [c["name"] for c in nxt["children"]] == ["Movies"]


async def test_folder_tree_limit_and_truncated(api):
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    for i in range(5):
        await _mk_item(maker, lib, f"dir{i}/f.bin", size=100 - i)
    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&limit=2")).json()
    assert len(body["children"]) == 2
    assert body["truncated"] is True
    assert [c["name"] for c in body["children"]] == ["dir0", "dir1"]
    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&limit=50")).json()
    assert body["truncated"] is False and len(body["children"]) == 5


async def test_folder_tree_param_validation(api):
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "Movies/a.bin", size=1)
    tree = f"{REPORTS}/folder-tree"
    assert (await client.get(f"{tree}?library_id={lib}&limit=0")).status_code == 422
    assert (await client.get(f"{tree}?library_id={lib}&limit=501")).status_code == 422
    # a parent without a library is ambiguous (two libraries can both have
    # "Movies" and they are different folders) — 422, never a guess.
    assert (await client.get(f"{tree}?parent=Movies")).status_code == 422
    assert (await client.get(f"{tree}?library_id={_uuid.uuid4()}")).status_code == 404


async def test_folder_tree_prefix_match_is_folder_boundary_exact(api):
    """"Movies/" must not pick up "MoviesOld/..." — and a folder name containing a
    LIKE wildcard (% or _) must be matched literally, not as a pattern."""
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "Movies/in.bin", size=10)
    await _mk_item(maker, lib, "MoviesOld/out.bin", size=999)
    await _mk_item(maker, lib, "100%_done/x.bin", size=7)
    await _mk_item(maker, lib, "100Xydone/y.bin", size=8)

    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&parent=Movies")).json()
    assert [c["name"] for c in body["children"]] == ["."]
    assert body["children"][0]["total_bytes"] == 10

    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}&parent=100%_done")).json()
    assert [c["total_bytes"] for c in body["children"]] == [7]  # not 7+8


async def test_folder_tree_ignores_tombstoned_items(api):
    client, maker = api
    lib = await _mk_lib(maker, name="F")
    await _mk_item(maker, lib, "Movies/live.bin", size=10)
    await _mk_item(maker, lib, "Movies/gone.bin", size=9999, status="missing")
    body = (await client.get(f"{REPORTS}/folder-tree?library_id={lib}")).json()
    assert body["children"][0]["total_bytes"] == 10


async def test_folder_tree_applies_rbac_scope(api):
    """A denied file contributes to no child's count or bytes. Exercised at the
    helper level (building a scoped PermissionContext end to end would mean
    standing up sessions/grants for what is a one-line clause hand-off)."""
    from filearr import rbac, rbac_sql
    from filearr.api.reports import _folder_tree_children, _folder_tree_libraries
    from filearr.models import Item as _Item

    client, maker = api
    lib = await _mk_lib(maker, name="F")
    for rel, size in (("Movies/a.bin", 100), ("Movies/b.bin", 200), ("Private/c.bin", 5000)):
        await _mk_item(
            maker, lib, rel, size=size, path_scope=rbac.path_to_ltree(rel, library_id=lib)
        )
    # Resolve text-vs-ltree from the LIVE column, as production does
    # (security.py) — pgserver has no contrib so local runs see text,
    # while CI's postgres created a real ltree column (2026-08-13 CI
    # failure: starts_with(ltree, varchar) does not exist).
    async with maker() as _s:
        use_ltree = await rbac_sql.path_scope_uses_ltree(_s)
    clause = rbac_sql.scope_where_clause(
        rbac.Role.USER,
        # path_to_ltree encodes each segment ("Movies" -> "_4dovies"); a grant
        # built from the raw name would be a corrupt scope and fail closed.
        [rbac.PathGrant(
            path=rbac.path_to_ltree("Movies", library_id=lib),
            action="search_metadata",
            allow=True,
        )],
        action="search_metadata",
        column=_Item.path_scope,
        use_ltree=use_ltree,
    )
    async with maker() as s:
        body = await _folder_tree_children(s, lib, "", 100, clause)
        assert [c["name"] for c in body["children"]] == ["Movies"]
        assert body["children"][0]["total_bytes"] == 300  # Private/c.bin excluded
        roots = await _folder_tree_libraries(s, 100, clause)
        assert roots["children"][0]["total_bytes"] == 300
    _ = client


# =========================================================================== #
# IN-T4 — POST /items/batch hardening                                         #
# =========================================================================== #
async def test_batch_null_pops_the_key_like_the_single_patch(api):
    """Parity with PATCH /items/{id}: an explicit null CLEARS a key (pops it),
    an absent key is untouched. Before IN-T4 the batch wrote a literal JSON null,
    so a bulk 'clear this field' poisoned every row it touched — and the null
    then flowed into the Meili projection and every export."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    one = await _mk_item(maker, lib, "one.bin", user_metadata={"note": "x", "keep": "y"})
    two = await _mk_item(maker, lib, "two.bin", user_metadata={"note": "z"})

    r = await client.post(
        f"{ITEMS}/batch",
        json={
            one: {"user_metadata": {"note": None}},
            two: {"user_metadata": {"note": None, "added": "new"}},
        },
    )
    assert r.status_code == 200, r.text
    assert r.json()["results"] == {one: "ok", two: "ok"}

    async with maker() as s:
        rows = {
            str(i.id): i.user_metadata
            for i in (await s.execute(text("SELECT id, user_metadata FROM items"))).all()
        }
    # the key is GONE, not present-as-null; untouched keys survive
    assert rows[one] == {"keep": "y"}
    assert rows[two] == {"added": "new"}


async def test_single_patch_and_batch_agree_on_null_clear(api):
    """The regression guard proper: run the SAME patch through both endpoints and
    assert byte-identical resulting user_metadata."""
    client, maker = api
    lib = await _mk_lib(maker, name="A")
    a = await _mk_item(maker, lib, "a.bin", user_metadata={"note": "x", "keep": 1})
    b = await _mk_item(maker, lib, "b.bin", user_metadata={"note": "x", "keep": 1})

    patch = {"user_metadata": {"note": None, "extra": "e"}}
    assert (await client.patch(f"{ITEMS}/{a}", json=patch)).status_code == 200
    assert (await client.post(f"{ITEMS}/batch", json={b: patch})).status_code == 200

    async with maker() as s:
        got = {
            str(i.id): i.user_metadata
            for i in (await s.execute(text("SELECT id, user_metadata FROM items"))).all()
        }
    assert got[a] == got[b] == {"keep": 1, "extra": "e"}


async def test_batch_rejects_over_the_key_cap_with_413(api):
    """Unbounded maps are a DoS-shaped hole: every key costs a SELECT, an RBAC
    evaluation, a validation pass and an ItemVersion insert inside ONE
    transaction. 413 (request size), not 422 (malformed value). The UI chunks at
    exactly this size, so it never trips."""
    from filearr.api.items import MAX_BATCH_PATCH_ITEMS

    client, maker = api
    lib = await _mk_lib(maker, name="A")
    real = await _mk_item(maker, lib, "real.bin")

    over = {str(_uuid.uuid4()): {"year": 2000} for _ in range(MAX_BATCH_PATCH_ITEMS + 1)}
    r = await client.post(f"{ITEMS}/batch", json=over)
    assert r.status_code == 413, r.text
    assert str(MAX_BATCH_PATCH_ITEMS) in r.text
    # ...and nothing was applied (the cap is checked before any row is touched).
    async with maker() as s:
        year = (
            await s.execute(text("SELECT year FROM items WHERE id = :i"), {"i": real})
        ).scalar_one()
    assert year is None

    # exactly at the cap is allowed
    at_cap = {str(_uuid.uuid4()): {"year": 2000} for _ in range(MAX_BATCH_PATCH_ITEMS - 1)}
    at_cap[real] = {"year": 1999}
    r = await client.post(f"{ITEMS}/batch", json=at_cap)
    assert r.status_code == 200, r.text
    assert r.json()["results"][real] == "ok"

