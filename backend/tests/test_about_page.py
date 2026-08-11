"""The About page endpoint (``GET /system/about``) — 2026-08-10.

The console had no way to answer "which versions is this deployment actually
running", which is the first question of every incident. The rule these tests
exist to defend is that every number on that page is READ FROM THE LIVE SYSTEM:
so the dependency assertions compare against ``importlib.metadata`` rather than
literal version strings (a hardcoded "0.141.1" here would pass while the
endpoint returned a stale pin, which is precisely the bug), and the probe tests
assert that a dead service degrades to a REASON rather than a 500 or a blank.

No real ffprobe/tesseract is invoked: CI has none installed, and the parsing is
pinned against captured banner text instead.

Harness mirrors test_features_panel.py (migrated pgserver Postgres).
"""

from __future__ import annotations

import importlib.metadata as im
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import about as about_mod
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def db_maker(pg_uri):
    cfg = Config(str(BACKEND_DIR / "alembic.ini"))
    command.upgrade(cfg, "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    async with engine.begin() as conn:
        await conn.execute(text("DELETE FROM agents"))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    yield maker
    await engine.dispose()


@pytest.fixture
async def settings(monkeypatch):
    get_settings.cache_clear()
    s = get_settings()
    monkeypatch.setattr(s, "auth_enabled", False)
    yield s
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def _clear_about_caches():
    """The module memoises the dependency table and every tool probe for the
    life of the process; a test that monkeypatches either must not inherit (or
    leak) another test's cached answer."""
    about_mod.python_dependencies.cache_clear()
    about_mod._probe_tool_version.cache_clear()
    yield
    about_mod.python_dependencies.cache_clear()
    about_mod._probe_tool_version.cache_clear()


@pytest.fixture
async def client(db_maker, settings, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker
    app.dependency_overrides.clear()


async def _about(c) -> dict:
    r = await c.get("/api/v1/system/about")
    assert r.status_code == 200, r.text
    return r.json()


# --------------------------------------------------------------------------- #
# Sections                                                                     #
# --------------------------------------------------------------------------- #
async def test_about_returns_every_section(client):
    c, _ = client
    body = await _about(c)
    assert set(body) >= {
        "application",
        "services",
        "python_packages",
        "host_tools",
        "agents",
        "embedding",
    }

    app = body["application"]
    from filearr import __version__

    assert app["app_version"] == __version__
    assert app["license"] == "AGPL-3.0-or-later"
    # Read from the live interpreter, never from a config value.
    import platform as _platform

    assert app["python_version"] == _platform.python_version()
    assert app["machine"] == _platform.machine()
    assert app["source_url"] == get_settings().source_url
    # A dev checkout has no deploy stamp; the field must still be present.
    assert "build_stamp" in app


async def test_services_are_named_and_probed(client):
    c, _ = client
    services = {s["name"]: s for s in (await _about(c))["services"]}
    assert {"PostgreSQL", "Meilisearch", "Procrastinate", "SQLAlchemy"} <= set(services)
    for s in services.values():
        assert s["url"].startswith("https://")
        # Exactly one of the two is populated: a version OR a reason. A row with
        # neither is the blank cell this page exists to prevent.
        assert (s["version"] is not None) != (s["error"] is not None), s

    # The two library-backed rows are facts about the running process.
    assert services["Procrastinate"]["version"] == im.version("procrastinate")
    assert services["SQLAlchemy"]["version"] == im.version("sqlalchemy")
    # Postgres is genuinely up in this harness, and the paragraph is trimmed.
    pg = services["PostgreSQL"]
    assert pg["version"] and pg["version"][0].isdigit(), pg
    assert "compiled by" not in (pg["detail"] or "")


async def test_meili_probe_failure_degrades_to_a_reason(client, monkeypatch):
    """Meilisearch being down is INFORMATION, not a 500 — the whole page must
    still render, with the one row saying why it is empty."""
    c, _ = client

    def _boom():
        raise ConnectionRefusedError("nothing listening on 7700")

    monkeypatch.setattr("filearr.search.client", _boom)
    body = await _about(c)
    meili = next(s for s in body["services"] if s["name"] == "Meilisearch")
    assert meili["version"] is None
    assert "unreachable" in meili["error"]
    assert "ConnectionRefusedError" in meili["error"]
    # Every other section survived.
    assert body["python_packages"]
    assert next(s for s in body["services"] if s["name"] == "SQLAlchemy")["version"]


class _BrokenSession:
    """A session whose every statement fails, for the degradation path. Tracks
    the rollback because a failed statement poisons a real session — without
    it the sections that run afterwards would fail too, turning one dead probe
    into a dead page."""

    def __init__(self) -> None:
        self.rolled_back = False

    async def execute(self, *args, **kwargs):
        raise RuntimeError("connection closed")

    async def rollback(self) -> None:
        self.rolled_back = True


async def test_postgres_probe_failure_degrades_to_a_reason():
    session = _BrokenSession()
    row = await about_mod._postgres_service(session)  # type: ignore[arg-type]
    assert row["name"] == "PostgreSQL"
    assert row["version"] is None
    assert "unreachable" in row["error"]
    assert "RuntimeError" in row["error"]
    assert session.rolled_back is True


async def test_endpoint_survives_a_degraded_postgres_probe(client, monkeypatch):
    """A degraded probe never becomes a 500: the page still renders with the
    one row explaining itself."""
    c, _ = client
    real = about_mod._postgres_service

    async def _pg(_session):
        return await real(_BrokenSession())  # type: ignore[arg-type]

    monkeypatch.setattr(about_mod, "_postgres_service", _pg)
    body = await _about(c)
    pg = next(s for s in body["services"] if s["name"] == "PostgreSQL")
    assert pg["version"] is None and pg["error"]
    assert body["python_packages"] and body["host_tools"]


# --------------------------------------------------------------------------- #
# Python dependencies                                                          #
# --------------------------------------------------------------------------- #
async def test_dependencies_report_installed_versions_not_pins(client):
    """Asserted against importlib.metadata rather than literals, so the test
    does not rot on the next dependency bump — and so it would FAIL if the
    endpoint ever started echoing pyproject's pins."""
    c, _ = client
    packages = {p["name"]: p for p in (await _about(c))["python_packages"]}

    # A representative spread: web, db, queue, search, extraction.
    for name in ("fastapi", "sqlalchemy", "procrastinate", "meilisearch-python-sdk", "pillow"):
        assert name in packages, sorted(packages)
        assert packages[name]["version"] == im.version(name)

    for p in packages.values():
        assert p["url"].startswith("https://"), p
        assert p["optional"] is False or p["version"] is not None


def test_requirement_name_strips_specifiers():
    assert about_mod.requirement_name("sqlalchemy[asyncio]==2.0.51") == "sqlalchemy"
    assert about_mod.requirement_name("trimesh>=4.12.2,<5") == "trimesh"
    assert about_mod.requirement_name('pgserver>=0.1.4; python_version < "3.13"') == "pgserver"
    assert about_mod.requirement_name("  fastapi  ") == "fastapi"
    assert about_mod.requirement_name("") is None


def test_dependency_url_prefers_metadata_then_curated_then_pypi(monkeypatch):
    # Real metadata wins (fastapi publishes a Documentation Project-URL).
    assert about_mod.dependency_url("fastapi").startswith("https://")

    # No metadata at all -> curated fallback, then PyPI for anything unknown.
    monkeypatch.setattr(about_mod, "_metadata_url", lambda dist: None)
    assert about_mod.dependency_url("python-magic") == about_mod._CURATED_URLS["python-magic"]
    assert (
        about_mod.dependency_url("nonexistent-pkg")
        == "https://pypi.org/project/nonexistent-pkg/"
    )


def test_pg_version_split():
    raw = (
        "PostgreSQL 18.4 (Debian 18.4-1.pgdg13+1) on x86_64-pc-linux-gnu, "
        "compiled by gcc (Debian 12.2.0-14) 12.2.0, 64-bit"
    )
    version, detail = about_mod.split_pg_version(raw)
    assert version == "18.4"
    assert detail == "PostgreSQL 18.4 (Debian 18.4-1.pgdg13+1)"
    # Unrecognised banner: no invented version, but the text is still shown.
    version, detail = about_mod.split_pg_version("CockroachDB CCL v23.1")
    assert version is None
    assert detail == "CockroachDB CCL v23.1"


# --------------------------------------------------------------------------- #
# Host tools                                                                   #
# --------------------------------------------------------------------------- #
def test_parse_tool_version_covers_every_real_shape():
    """The three output shapes the agent's Go probe documents, mirrored here."""
    assert (
        about_mod.parse_tool_version(
            "ffmpeg", "ffmpeg version 6.1.1-3ubuntu5 Copyright (c) 2000-2023 the FFmpeg developers"
        )
        == "6.1.1-3ubuntu5"
    )
    assert (
        about_mod.parse_tool_version("tesseract", "tesseract 5.3.4\n leptonica-1.82.0") == "5.3.4"
    )
    assert about_mod.parse_tool_version("exiftool", "12.76\n") == "12.76"
    assert about_mod.parse_tool_version("pdfinfo", "pdfinfo version 22.12.0") == "22.12.0"
    # A usage banner is NOT a version.
    assert about_mod.parse_tool_version("exiftool", "Usage: exiftool [OPTIONS] FILE") is None
    assert about_mod.parse_tool_version("ffprobe", "") is None
    assert about_mod.parse_tool_version("ffprobe", "\n\n  \n") is None


def test_host_tools_report_absent_tools_without_raising(monkeypatch):
    """Nothing on PATH is a completely normal state (a stripped image, a dev
    box) and must produce full rows, not an exception or a short list."""
    monkeypatch.setattr(about_mod.shutil, "which", lambda cmd: None)
    rows = about_mod.host_tools()
    assert {r["name"] for r in rows} == set(about_mod._TOOL_VERSION_ARGS)
    for r in rows:
        assert r["present"] is False
        assert r["version"] is None
        assert r["path"] is None
        assert r["purpose"] and r["url"].startswith("https://")


def test_host_tool_probe_is_cached_per_process(monkeypatch):
    """One subprocess per tool per process — the page must not spawn seven
    processes on every request (it is polled by a browser tab)."""
    calls: list[list[str]] = []

    class _Result:
        stdout = "ffprobe version 7.1.1 Copyright (c) 2007-2024"
        stderr = ""

    def _fake_run(argv, **kwargs):
        calls.append(argv)
        return _Result()

    monkeypatch.setattr(about_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(about_mod.subprocess, "run", _fake_run)

    first = about_mod.host_tools()
    second = about_mod.host_tools()
    assert first == second
    assert len(calls) == len(about_mod._TOOL_VERSION_ARGS), calls
    assert next(r for r in first if r["name"] == "ffprobe")["version"] == "7.1.1"


def test_host_tool_present_but_silent_is_not_absent(monkeypatch):
    """"Installed, version unknown" is a distinct state from "not installed"."""

    class _Result:
        stdout = ""
        stderr = "Segmentation fault"

    monkeypatch.setattr(about_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")
    monkeypatch.setattr(about_mod.subprocess, "run", lambda argv, **kw: _Result())
    row = next(r for r in about_mod.host_tools() if r["name"] == "tesseract")
    assert row["present"] is True
    assert row["version"] is None
    assert row["path"] == "/usr/bin/tesseract"


def test_host_tool_probe_survives_a_missing_binary(monkeypatch):
    """which() can win a race the exec then loses (a binary deleted between the
    two). The probe must report unknown, never propagate OSError."""
    monkeypatch.setattr(about_mod.shutil, "which", lambda cmd: f"/usr/bin/{cmd}")

    def _boom(argv, **kwargs):
        raise FileNotFoundError(argv[0])

    monkeypatch.setattr(about_mod.subprocess, "run", _boom)
    assert all(r["version"] is None for r in about_mod.host_tools())


# --------------------------------------------------------------------------- #
# Embedding model                                                              #
# --------------------------------------------------------------------------- #
async def test_embedding_reports_not_downloaded(client, settings, monkeypatch, tmp_path):
    """The default state: semantic search off, nothing ever fetched. It must
    read as normal — populated fields, explicit nulls — not as a fault."""
    c, _ = client
    monkeypatch.setattr(settings, "embed_model_cache", str(tmp_path / "empty"))
    e = (await _about(c))["embedding"]
    assert e["enabled"] is False
    assert e["downloaded"] is False
    assert e["revision"] is None
    assert e["revision_url"] is None
    assert e["size"] is None
    assert e["downloaded_at"] is None
    # The configured identity is still fully reported, with a working link.
    assert e["repo"] == settings.embed_model_repo
    assert e["file"] == settings.embed_model_file
    assert e["model_url"] == f"https://huggingface.co/{settings.embed_model_repo}"


async def test_embedding_extracts_the_commit_sha_from_the_hf_cache(
    client, settings, monkeypatch, tmp_path
):
    """huggingface_hub stores a repo as models--{org}--{name}/snapshots/{sha}/,
    so the snapshot directory name IS the commit of the revision on disk —
    better provenance than any date, and the thing the page links to."""
    c, _ = client
    sha = "9c1f2a3b4d5e6f708192a3b4c5d6e7f809112233"
    repo = settings.embed_model_repo
    snap = tmp_path / about_mod.hf_repo_dirname(repo) / "snapshots" / sha
    snap.mkdir(parents=True)
    (snap / settings.embed_model_file).write_bytes(b"x" * 4096)

    monkeypatch.setattr(settings, "embed_model_cache", str(tmp_path))
    monkeypatch.setattr(settings, "semantic_enabled", True)

    e = (await _about(c))["embedding"]
    assert e["downloaded"] is True
    assert e["revision"] == sha
    assert e["revision_url"] == f"https://huggingface.co/{repo}/tree/{sha}"
    assert e["size"] == 4096
    # The "date information" the operator asked for is the LOCAL download time.
    assert e["downloaded_at"] and e["downloaded_at"].endswith("+00:00")
    assert e["path"].endswith(settings.embed_model_file)


def test_hf_cache_prefers_the_newest_snapshot(monkeypatch, tmp_path):
    """An upgrade can leave two revisions cached; the newest is the one a fresh
    load would resolve to, so it is the one reported."""
    import os

    repo, filename = "Org/Model", "model.onnx"
    base = tmp_path / about_mod.hf_repo_dirname(repo) / "snapshots"
    for name, mtime in (("a" * 40, 1_700_000_000), ("b" * 40, 1_800_000_000)):
        d = base / name
        d.mkdir(parents=True)
        (d / filename).write_bytes(b"z")
        os.utime(d / filename, (mtime, mtime))

    got = about_mod._inspect_hf_cache(str(tmp_path), repo, filename)
    assert got["revision"] == "b" * 40
    assert got["downloaded"] is True

    # A repo directory that exists but holds no snapshot of the wanted file.
    (tmp_path / about_mod.hf_repo_dirname("Other/Repo") / "snapshots").mkdir(parents=True)
    missing = about_mod._inspect_hf_cache(str(tmp_path), "Other/Repo", filename)
    assert missing["downloaded"] is False
    assert missing["revision"] is None


# --------------------------------------------------------------------------- #
# Agent fleet                                                                  #
# --------------------------------------------------------------------------- #
async def test_agents_section_absent_when_the_feature_is_off(client):
    c, _ = client
    assert (await _about(c))["agents"] is None


async def test_agent_fleet_groups_versions(client, settings, monkeypatch):
    """A rollout in flight shows as more than one row, with a count each."""
    c, maker = client
    monkeypatch.setattr(settings, "agents_enabled", True)

    from filearr.models import Agent

    async with maker() as s:
        for name, version in (
            ("a1", "0.4.1"),
            ("a2", "0.4.1"),
            ("a3", "0.3.9"),
            ("a4", None),  # enrolled, never polled
        ):
            s.add(
                Agent(
                    name=name,
                    hostname=f"{name}.example",
                    platform="linux",
                    agent_version=version,
                )
            )
        await s.commit()

    fleet = (await _about(c))["agents"]
    assert fleet["total"] == 4
    counts = {v["version"]: v["count"] for v in fleet["versions"]}
    assert counts == {"0.4.1": 2, "0.3.9": 1, None: 1}
