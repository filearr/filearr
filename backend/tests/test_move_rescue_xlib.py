"""Roadmap §13 + §19 scan hardening:

  * ``mid_hash`` — the 64 KiB midpoint sampling tier that rescues (or vetoes)
    moves when ``content_hash`` is unavailable (quick_only policy);
  * cross-library move identity transfer (tombstone in X revived into Y);
  * the N->0 empty-mount guard (a walk that sees an empty tree over a
    previously-populated library fails the run instead of tombstoning it).

Mirrors the ``test_move_detection`` harness: pure planner tests on fake items,
then end-to-end ``_scan_body`` integration over real Postgres + real files.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.config import get_settings
from filearr.models import Item, ItemStatus, Library
from filearr.tasks.extract import QUICK_CHUNK, mid_hash
from filearr.tasks.move import plan_moves

BACKEND_DIR = Path(__file__).resolve().parent.parent


# --------------------------------------------------------------------------- #
# mid_hash — the sampling function                                            #
# --------------------------------------------------------------------------- #
def test_mid_hash_none_for_small_files(tmp_path):
    """<=128 KiB is fully covered by quick_hash — no midpoint sample."""
    p = tmp_path / "small.bin"
    p.write_bytes(b"x" * (QUICK_CHUNK * 2))
    assert mid_hash(str(p), QUICK_CHUNK * 2) is None
    assert mid_hash(str(p), None) is None


def test_mid_hash_discriminates_shared_head_tail(tmp_path):
    """Files sharing head+tail+size (the quick_hash collision family) get
    DIFFERENT mid hashes when their middles differ, and equal ones when the
    bytes match."""
    head, tail = b"H" * QUICK_CHUNK, b"T" * QUICK_CHUNK
    a = tmp_path / "a.bin"
    b = tmp_path / "b.bin"
    c = tmp_path / "c.bin"
    a.write_bytes(head + b"M1" * QUICK_CHUNK + tail)
    b.write_bytes(head + b"M2" * QUICK_CHUNK + tail)
    c.write_bytes(head + b"M1" * QUICK_CHUNK + tail)
    size = a.stat().st_size
    assert b.stat().st_size == size
    ha, hb, hc = (mid_hash(str(p), size) for p in (a, b, c))
    assert ha is not None
    assert ha != hb
    assert ha == hc


# --------------------------------------------------------------------------- #
# planner — mid tier semantics (pure)                                         #
# --------------------------------------------------------------------------- #
@dataclass
class _FakeItem:
    rel_path: str
    quick_hash: str | None
    size: int | None
    content_hash: str | None = None
    mid_hash: str | None = None


def test_planner_mid_veto_on_1to1():
    """Same (quick_hash, size), no content_hash anywhere: a mid mismatch vetoes
    the previously-blind 1:1 transfer."""
    cand = [_FakeItem("old.bin", "qh", 10, mid_hash="m1")]
    new = [_FakeItem("new.bin", "qh", 10, mid_hash="m2")]
    plans, ambiguous = plan_moves(cand, new)
    assert plans == []
    assert ambiguous == 1


def test_planner_mid_rescues_multiway_bucket():
    """A 2x2 (quick_hash, size) bucket with no content hashes used to be fully
    ambiguous; distinct mid samples now pin both pairs."""
    c1 = _FakeItem("a_old", "qh", 10, mid_hash="mA")
    c2 = _FakeItem("b_old", "qh", 10, mid_hash="mB")
    n1 = _FakeItem("a_new", "qh", 10, mid_hash="mA")
    n2 = _FakeItem("b_new", "qh", 10, mid_hash="mB")
    plans, ambiguous = plan_moves([c1, c2], [n1, n2])
    assert ambiguous == 0
    pairs = {(p.survivor.rel_path, p.duplicate.rel_path) for p in plans}
    assert pairs == {("a_old", "a_new"), ("b_old", "b_new")}


def test_planner_content_hash_outranks_mid():
    """A full-hash mismatch vetoes even when the midpoint samples agree — the
    full hash is authoritative byte evidence, the sample is not."""
    cand = [_FakeItem("old", "qh", 10, content_hash="c1", mid_hash="same")]
    new = [_FakeItem("new", "qh", 10, content_hash="c2", mid_hash="same")]
    plans, ambiguous = plan_moves(cand, new)
    assert plans == []
    assert ambiguous == 1


def test_planner_absent_mid_keeps_legacy_behaviour():
    """No content hash AND no mid samples (pre-column rows): the 1:1 bucket
    still transfers on (quick_hash, size) alone — §13 adds evidence, it never
    removes a previously-valid match."""
    cand = [_FakeItem("old", "qh", 10)]
    new = [_FakeItem("new", "qh", 10)]
    plans, ambiguous = plan_moves(cand, new)
    assert len(plans) == 1
    assert ambiguous == 0


# --------------------------------------------------------------------------- #
# Integration harness (mirrors test_move_detection)                           #
# --------------------------------------------------------------------------- #
def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _run_scan(session, library, *, force_empty=False):
    from filearr.models import ScanRun
    from filearr.tasks import scan as scan_mod

    async def _noop_defer(item_ids, scan_run_id=None):
        return None

    async def _noop_reindex(sess, lib_id):
        return None

    orig_defer = scan_mod._defer_extract_batch
    orig_reindex = scan_mod._reindex_library
    scan_mod._defer_extract_batch = _noop_defer
    scan_mod._reindex_library = _noop_reindex
    try:
        run = ScanRun(library_id=library.id, stats={})
        session.add(run)
        await session.commit()
        return await scan_mod._scan_body(
            session, library, run, force_empty=force_empty
        )
    finally:
        scan_mod._defer_extract_batch = orig_defer
        scan_mod._reindex_library = orig_reindex


def _write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)


async def _hash_all(session, lib, *, content=True):
    """Mimic the extract worker: populate quick/mid (and optionally content)
    hashes on the library's rows, as production does asynchronously."""
    from filearr.sidecar import classify
    from filearr.tasks.extract import full_hash, quick_hash
    from filearr.tasks.extract import mid_hash as mid_fn

    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars()
        .all()
    )
    for r in rows:
        if r.sidecar_of is not None or classify(r.rel_path) is not None:
            continue
        try:
            r.quick_hash = quick_hash(r.path, r.size)
            r.mid_hash = mid_fn(r.path, r.size)
            if content:
                r.content_hash = full_hash(r.path)
        except OSError:
            pass
    await session.commit()


async def _mk_library(session, root, name, **kw):
    lib = Library(name=name, root_path=str(root), enabled_categories=[], **kw)
    session.add(lib)
    await session.commit()
    return lib


HEAD, TAIL = b"H" * QUICK_CHUNK, b"T" * QUICK_CHUNK
SHARED_A = HEAD + b"M1" * QUICK_CHUNK + TAIL  # same head/tail/size as SHARED_B
SHARED_B = HEAD + b"M2" * QUICK_CHUNK + TAIL
BODY = b"AAAA" * 40_000


# --------------------------------------------------------------------------- #
# §13 integration — mid rescue under quick_only                               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_mid_rescues_quick_only_collision_moves(session, tmp_path):
    """Two files sharing head+tail+size under a quick_only policy (no
    content_hash ever computed): both renamed in one pass. quick_hash alone
    saw one ambiguous 2x2 bucket and refused; the midpoint samples pin the
    pairs, so both identities survive the rename."""
    root = tmp_path / "lib"
    _write(root / "a.bin", SHARED_A)
    _write(root / "b.bin", SHARED_B)
    lib = await _mk_library(session, root, "l-mid", hash_policy="quick_only")

    await _run_scan(session, lib)
    rows = {
        r.rel_path: r
        for r in (
            await session.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()
    }
    ids = {rel: r.id for rel, r in rows.items()}
    assert rows["a.bin"].quick_hash is None  # extraction hasn't run yet
    await _hash_all(session, lib, content=False)

    (root / "a.bin").rename(root / "a_renamed.bin")
    (root / "b.bin").rename(root / "b_renamed.bin")
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 2
    assert stats["move_ambiguous"] == 0
    assert stats["missing"] == 0
    after = {
        r.rel_path: r
        for r in (
            await session.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()
    }
    assert after["a_renamed.bin"].id == ids["a.bin"]
    assert after["b_renamed.bin"].id == ids["b.bin"]
    assert after["a_renamed.bin"].content_hash is None  # policy still honoured


@pytest.mark.asyncio
async def test_mid_vetoes_quick_only_false_transfer(session, tmp_path):
    """A quick_only rename where the 'new' file is actually DIFFERENT bytes in
    the collision family (same head/tail/size): the mid sample vetoes the
    1:1 transfer that (quick_hash, size) alone would have made."""
    root = tmp_path / "lib"
    _write(root / "a.bin", SHARED_A)
    lib = await _mk_library(session, root, "l-veto", hash_policy="quick_only")
    await _run_scan(session, lib)
    await _hash_all(session, lib, content=False)
    original_id = (
        await session.execute(select(Item).where(Item.library_id == lib.id))
    ).scalar_one().id

    # replace with the OTHER family member under a new name: vanished + new,
    # same (quick_hash, size), different middle bytes.
    (root / "a.bin").unlink()
    _write(root / "b.bin", SHARED_B)
    stats = await _run_scan(session, lib)

    assert stats["moved"] == 0
    assert stats["move_ambiguous"] == 1
    assert stats["missing"] == 1
    after = {
        r.rel_path: r
        for r in (
            await session.execute(select(Item).where(Item.library_id == lib.id))
        ).scalars()
    }
    assert after["a.bin"].status == ItemStatus.missing
    assert after["b.bin"].id != original_id


# --------------------------------------------------------------------------- #
# §13 integration — cross-library move identity                               #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_cross_library_move_revives_tombstone(session, tmp_path):
    """File tombstoned in X, identical bytes appearing in Y: Y's scan revives
    the X tombstone into Y — id, tags, user_metadata, first_seen intact — and
    drops the fresh duplicate row."""
    root_x = tmp_path / "libx"
    root_y = tmp_path / "liby"
    _write(root_x / "keep" / "movie.mkv", BODY)
    root_y.mkdir()
    lib_x = await _mk_library(session, root_x, "l-x")
    lib_y = await _mk_library(session, root_y, "l-y")

    await _run_scan(session, lib_x)
    item = (
        await session.execute(select(Item).where(Item.library_id == lib_x.id))
    ).scalar_one()
    original_id, original_first_seen = item.id, item.first_seen
    item.tags = ["favourite"]
    item.user_metadata = {"note": "keep me"}
    await session.commit()
    await _hash_all(session, lib_x)

    # relocate across the library boundary
    _write(root_y / "incoming" / "movie.mkv", BODY)
    (root_x / "keep" / "movie.mkv").unlink()
    stats_x = await _run_scan(session, lib_x, force_empty=True)
    assert stats_x["missing"] == 1

    stats_y = await _run_scan(session, lib_y)
    assert stats_y["cross_moved"] == 1
    assert stats_y["new"] == 0  # a revival, not a new file

    survivor = (
        await session.execute(select(Item).where(Item.id == original_id))
    ).scalar_one()
    assert survivor.library_id == lib_y.id
    assert survivor.rel_path == "incoming/movie.mkv"
    assert survivor.status == ItemStatus.active
    assert survivor.tags == ["favourite"]
    assert survivor.user_metadata == {"note": "keep me"}
    assert survivor.first_seen == original_first_seen
    # X holds nothing anymore; Y holds exactly the survivor.
    assert (
        await session.execute(
            select(Item).where(Item.library_id == lib_x.id)
        )
    ).scalars().all() == []
    y_rows = (
        (await session.execute(select(Item).where(Item.library_id == lib_y.id)))
        .scalars()
        .all()
    )
    assert [r.id for r in y_rows] == [original_id]


@pytest.mark.asyncio
async def test_cross_library_copy_never_transfers(session, tmp_path):
    """A COPY (original still active in X) must not steal X's identity — only
    an actual tombstone matches."""
    root_x = tmp_path / "libx"
    root_y = tmp_path / "liby"
    _write(root_x / "movie.mkv", BODY)
    root_y.mkdir()
    lib_x = await _mk_library(session, root_x, "l-x2")
    lib_y = await _mk_library(session, root_y, "l-y2")

    await _run_scan(session, lib_x)
    await _hash_all(session, lib_x)
    x_id = (
        await session.execute(select(Item).where(Item.library_id == lib_x.id))
    ).scalar_one().id

    _write(root_y / "movie.mkv", BODY)
    stats_y = await _run_scan(session, lib_y)
    assert stats_y["cross_moved"] == 0
    assert stats_y["new"] == 1
    x_item = (
        await session.execute(select(Item).where(Item.library_id == lib_x.id))
    ).scalar_one()
    assert x_item.id == x_id
    assert x_item.status == ItemStatus.active


@pytest.mark.asyncio
async def test_cross_library_requires_byte_confirmation(session, tmp_path):
    """Under quick_only with NO mid/content evidence stored on the tombstone
    (pre-§13 rows), a (quick_hash, size) match alone must NOT cross the
    library boundary — unknown is a refusal here, unlike the intra pass."""
    root_x = tmp_path / "libx"
    root_y = tmp_path / "liby"
    _write(root_x / "a.bin", SHARED_A)
    root_y.mkdir()
    lib_x = await _mk_library(session, root_x, "l-x3", hash_policy="quick_only")
    lib_y = await _mk_library(session, root_y, "l-y3", hash_policy="quick_only")

    await _run_scan(session, lib_x)
    await _hash_all(session, lib_x, content=False)
    # Simulate a pre-§13 row: quick_hash present, no mid sample stored.
    x_item = (
        await session.execute(select(Item).where(Item.library_id == lib_x.id))
    ).scalar_one()
    x_id = x_item.id
    x_item.mid_hash = None
    await session.commit()

    (root_x / "a.bin").unlink()
    _write(root_y / "a.bin", SHARED_A)
    await _run_scan(session, lib_x, force_empty=True)
    stats_y = await _run_scan(session, lib_y)

    assert stats_y["cross_moved"] == 0
    assert stats_y["cross_move_ambiguous"] == 1
    assert stats_y["new"] == 1
    tomb = (
        await session.execute(select(Item).where(Item.id == x_id))
    ).scalar_one()
    assert tomb.library_id == lib_x.id
    assert tomb.status == ItemStatus.missing


# --------------------------------------------------------------------------- #
# §19 — N->0 empty-mount guard                                                #
# --------------------------------------------------------------------------- #
@pytest.mark.asyncio
async def test_empty_walk_over_populated_library_fails(session, tmp_path):
    """The dead-mount presentation: the root still exists and is readable but
    the walk sees NOTHING. Refused — run raises (-> failed), zero tombstones."""
    from filearr.tasks.scan import ScanRootError

    root = tmp_path / "lib"
    _write(root / "a" / "one.mkv", BODY)
    _write(root / "b" / "two.mkv", BODY)
    lib = await _mk_library(session, root, "l-guard")
    await _run_scan(session, lib)
    await _hash_all(session, lib)

    # empty the tree entirely (stale-empty mountpoint presentation)
    for p in sorted(root.rglob("*"), reverse=True):
        p.unlink() if p.is_file() else p.rmdir()

    with pytest.raises(ScanRootError, match="force_empty"):
        await _run_scan(session, lib)

    rows = (
        (await session.execute(select(Item).where(Item.library_id == lib.id)))
        .scalars()
        .all()
    )
    assert all(r.status == ItemStatus.active for r in rows)  # nothing tombstoned


@pytest.mark.asyncio
async def test_empty_walk_forced_or_guard_disabled(session, tmp_path, monkeypatch):
    """force_empty consents to the N->0 tombstoning; the config kill switch
    does the same globally."""
    root = tmp_path / "lib"
    _write(root / "one.mkv", BODY)
    lib = await _mk_library(session, root, "l-guard2")
    await _run_scan(session, lib)
    await _hash_all(session, lib)
    (root / "one.mkv").unlink()

    stats = await _run_scan(session, lib, force_empty=True)
    assert stats["missing"] == 1

    # guard disabled: a second empty walk (everything already missing) is a
    # no-op, and even a repopulate->empty cycle passes without force.
    _write(root / "two.mkv", BODY)
    await _run_scan(session, lib)
    await _hash_all(session, lib)
    (root / "two.mkv").unlink()
    monkeypatch.setattr(get_settings(), "scan_empty_guard", False)
    stats = await _run_scan(session, lib)
    assert stats["missing"] == 1


@pytest.mark.asyncio
async def test_empty_but_filtered_walk_passes_guard(session, tmp_path):
    """seen == 0 with EXCLUDED entries proves the tree is readable — the guard
    must not trip (the mount is alive; the files are merely filtered)."""
    root = tmp_path / "lib"
    _write(root / "movie.mkv", BODY)
    lib = await _mk_library(session, root, "l-guard3")
    await _run_scan(session, lib)
    await _hash_all(session, lib)

    # Replace the media with a dotfile: walk sees an entry but excludes it.
    (root / "movie.mkv").unlink()
    _write(root / ".hidden", b"x")
    stats = await _run_scan(session, lib)
    assert stats["missing"] == 1
