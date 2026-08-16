"""Library diagnosis report (2026-08-16): verdict shape and the main causes."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime
from pathlib import Path

import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import diagnose
from filearr.models import Library, ScanRun

BACKEND_DIR = Path(__file__).resolve().parent.parent


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(pg_uri.replace("postgresql://", "postgresql+psycopg://", 1))
    async with engine.begin() as conn:
        for t in ("scan_runs", "items", "libraries"):
            await conn.execute(text(f"DELETE FROM {t}"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


def _codes(rep):
    return [v["code"] for v in rep["verdicts"]]


async def _mk_lib(maker, root: str, **kw) -> Library:
    async with maker() as s:
        lib = Library(name=kw.pop("name", "L"), root_path=root, **kw)
        s.add(lib)
        await s.commit()
        await s.refresh(lib)
        return lib


async def test_missing_root_and_never_scanned(db_maker):
    lib = await _mk_lib(db_maker, "/definitely/not/here")
    async with db_maker() as s:
        rep = await diagnose.diagnose_library(s, lib)
    codes = _codes(rep)
    assert codes[0] == "path-missing"  # errors sort first
    assert "never-scanned" in codes and "extract-ok" in codes
    assert rep["verdicts"][0]["doc"].endswith("#path-missing")
    assert rep["path"]["exists"] is False


async def test_empty_root_and_tombstoned_all_scan(db_maker, tmp_path):
    root = tmp_path / "empty"
    root.mkdir()
    lib = await _mk_lib(db_maker, str(root))
    async with db_maker() as s:
        s.add(
            ScanRun(
                library_id=lib.id,
                status="finished",
                started_at=datetime.now(UTC),
                finished_at=datetime.now(UTC),
                stats={"seen": 0, "missing": 120, "new": 0},
            )
        )
        await s.commit()
    async with db_maker() as s:
        rep = await diagnose.diagnose_library(s, lib)
    codes = _codes(rep)
    assert "path-empty" in codes and "scan-tombstoned-all" in codes
    assert rep["scans"][0]["stats"]["missing"] == 120


async def test_failed_scan_classified_and_crash_loop(db_maker, tmp_path):
    root = tmp_path / "ok"
    root.mkdir()
    (root / "a.txt").write_text("x")
    lib = await _mk_lib(db_maker, str(root))
    async with db_maker() as s:
        for _ in range(2):
            s.add(
                ScanRun(
                    library_id=lib.id,
                    status="failed",
                    started_at=datetime.now(UTC),
                    finished_at=datetime.now(UTC),
                    stats={"error": "PermissionError: [Errno 13] Permission denied: '/x/y'"},
                )
            )
        await s.commit()
    async with db_maker() as s:
        rep = await diagnose.diagnose_library(s, lib)
    codes = _codes(rep)
    assert "scan-failed-permission" in codes and "scan-crash-loop" in codes
    assert "path-ok" in codes
    assert rep["scans"][0]["error"].startswith("PermissionError")


async def test_extract_error_breakdown(db_maker, tmp_path):
    root = tmp_path / "e"
    root.mkdir()
    (root / "a.txt").write_text("x")
    lib = await _mk_lib(db_maker, str(root))
    async with db_maker() as s:
        for i, kind in enumerate(["dependency", "dependency", "corrupt", None]):
            meta = {"_extract_error": f"boom {kind}"}
            if kind:
                meta["_extract_error_kind"] = kind
            await s.execute(
                text(
                    "INSERT INTO items "
                    "(id, library_id, rel_path, path, filename, size, mtime, status, metadata) "
                    "VALUES (:id, :lib, :rp, :p, :fn, 1, now(), 'active', CAST(:m AS jsonb))"
                ),
                {
                    "id": str(uuid.uuid4()),
                    "lib": str(lib.id),
                    "rp": f"f{i}",
                    "p": f"/x/f{i}",
                    "fn": f"f{i}",
                    "m": json.dumps(meta),
                },
            )
        await s.commit()
    async with db_maker() as s:
        rep = await diagnose.diagnose_library(s, lib)
    codes = _codes(rep)
    assert "extract-dependency" in codes and "extract-corrupt" in codes and "extract-error" in codes
    assert rep["extract_errors"]["count"] == 4
    assert rep["extract_errors"]["by_kind"]["dependency"] == 2
    assert rep["extract_errors"]["top_messages"][0]["count"] == 2
