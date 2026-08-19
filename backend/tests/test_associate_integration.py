"""End-to-end sidecar association against a real Postgres (pgserver).

Creates library + item rows and on-disk NFO files, runs the async association
pass, and asserts: sidecars link to parents, NFO metadata lands in the PARENT's
extracted `metadata` (never user_metadata), and a rescan is idempotent.
"""

from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr.models import Item, ItemStatus, Library
from filearr.tasks.associate import associate_sidecars

BACKEND_DIR = Path(__file__).resolve().parent.parent

MOVIE_NFO = b"""<movie>
  <title>Dune</title>
  <year>2021</year>
  <plot>War for a desert planet.</plot>
</movie>
"""


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def session(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    # clean slate between tests
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM items"))
        await conn.execute(text("DELETE FROM libraries"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    async with maker() as s:
        yield s
    await engine.dispose()


async def _mk_item(session, lib, rel, mt, path, size=100):
    it = Item(
        library_id=lib.id,
        file_category=mt,
        path=path,
        rel_path=rel,
        filename=rel.split("/")[-1],
        extension=rel.rsplit(".", 1)[-1] if "." in rel else None,
        size=size,
        mtime=datetime.now(UTC),
        status=ItemStatus.active,
    )
    session.add(it)
    await session.flush()
    return it


async def test_nfo_and_thumb_link_and_metadata(session, tmp_path):
    lib = Library(name="movies", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()

    d = tmp_path / "Dune (2021)"
    d.mkdir()
    (d / "Dune (2021).nfo").write_bytes(MOVIE_NFO)

    video = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021).mkv", "video",
        str(d / "Dune (2021).mkv"), size=5_000_000,
    )
    nfo = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021).nfo", "other",
        str(d / "Dune (2021).nfo"),
    )
    thumb = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021)-thumb.jpg", "image",
        str(d / "Dune (2021)-thumb.jpg"),
    )
    await session.commit()

    stats = await associate_sidecars(session, lib.id)
    await session.commit()

    await session.refresh(nfo)
    await session.refresh(thumb)
    await session.refresh(video)

    # links
    assert nfo.sidecar_of == video.id
    assert thumb.sidecar_of == video.id
    assert video.sidecar_of is None

    # NFO metadata folded into PARENT's extracted metadata (not user_metadata)
    assert video.metadata_.get("nfo_title") == "Dune"
    assert video.metadata_.get("nfo_year") == 2021
    assert "War for a desert planet." in video.metadata_.get("nfo_plot", "")
    assert video.user_metadata == {}  # extractors never touch user_metadata
    assert video.title == "Dune"  # promoted to typed column (was empty)
    assert video.year == 2021

    assert stats["sidecars"] == 2
    assert stats["linked"] == 2
    assert stats["nfo_parsed"] == 1


async def test_rescan_idempotent(session, tmp_path):
    lib = Library(name="movies2", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "X"
    d.mkdir()
    (d / "X.nfo").write_bytes(MOVIE_NFO)
    video = await _mk_item(session, lib, "X/X.mkv", "video", str(d / "X.mkv"), size=9)
    nfo = await _mk_item(session, lib, "X/X.nfo", "other", str(d / "X.nfo"))
    await session.commit()

    s1 = await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(nfo)
    first_parent = nfo.sidecar_of

    s2 = await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(nfo)

    assert nfo.sidecar_of == first_parent == video.id
    assert s1["linked"] == s2["linked"] == 1


async def test_directory_poster_links_to_primary(session, tmp_path):
    lib = Library(name="movies3", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "Dir"
    d.mkdir()
    big = await _mk_item(
        session, lib, "Dir/big.mkv", "video", str(d / "big.mkv"), size=10**9
    )
    poster = await _mk_item(session, lib, "Dir/poster.jpg", "image", str(d / "poster.jpg"))
    await session.commit()

    await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(poster)
    assert poster.sidecar_of == big.id


JR_SIDECAR = b"""<?xml version="1.0" encoding="UTF-8"?>
<MPL Version="2.0" Title="MPL">
<Item>
<Field Name="Name">Heat</Field>
<Field Name="Year">1995</Field>
<Field Name="Genre">Crime; Drama</Field>
<Field Name="Director">Michael Mann</Field>
<Field Name="IMDb ID">tt0113277</Field>
</Item>
</MPL>
"""


async def test_jriver_sidecar_links_and_parses(session, tmp_path):
    """Roadmap §12 (2026-08-19): *_JRSidecar.xml is parsed into jr_* keys."""
    lib = Library(name="jr", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "Heat"
    d.mkdir()
    (d / "Heat_JRSidecar.xml").write_bytes(JR_SIDECAR)
    video = await _mk_item(session, lib, "Heat/Heat.mkv", "video", str(d / "Heat.mkv"), size=9_000)
    side = await _mk_item(
        session, lib, "Heat/Heat_JRSidecar.xml", "other", str(d / "Heat_JRSidecar.xml")
    )
    await session.commit()

    stats = await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(side)
    await session.refresh(video)
    assert side.sidecar_of == video.id
    assert video.metadata_["jr_title"] == "Heat" and video.metadata_["jr_year"] == 1995
    assert video.metadata_["jr_genre"] == ["Crime", "Drama"]
    assert video.metadata_["jr_director"] == "Michael Mann"
    assert video.external_ids.get("imdb") == "tt0113277"
    assert video.title == "Heat" and video.year == 1995
    assert stats["jriver_parsed"] == 1 and stats["nfo_parsed"] == 0


async def test_sidecar_priority_override(session, tmp_path, monkeypatch):
    """Roadmap §12: FILEARR_SIDECAR_METADATA_PRIORITY=sidecar lets the NFO
    overwrite an already-set title/year (default 'fill' only fills empties)."""
    from filearr.config import get_settings

    lib = Library(name="prio", root_path=str(tmp_path))
    session.add(lib)
    await session.flush()
    d = tmp_path / "Dune (2021)"
    d.mkdir()
    (d / "Dune (2021).nfo").write_bytes(MOVIE_NFO)
    video = await _mk_item(
        session, lib, "Dune (2021)/Dune (2021).mkv", "video", str(d / "Dune (2021).mkv"), size=5
    )
    video.title = "Dune.2021.2160p.REMUX"
    video.year = 2020
    await _mk_item(session, lib, "Dune (2021)/Dune (2021).nfo", "other", str(d / "Dune (2021).nfo"))
    await session.commit()

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "sidecar_metadata_priority", "fill")
    await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(video)
    assert video.title == "Dune.2021.2160p.REMUX" and video.year == 2020  # fill: untouched

    monkeypatch.setattr(get_settings(), "sidecar_metadata_priority", "sidecar")
    await associate_sidecars(session, lib.id)
    await session.commit()
    await session.refresh(video)
    assert video.title == "Dune" and video.year == 2021
