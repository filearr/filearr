"""Per-agent About / dependency report — ``GET /agents/{agent_id}/about``
(2026-08-11).

The console could answer "which build is this SERVER running" (the About page)
and knew exactly one string about a remote agent's software: ``agent_version``.
This surface answers the same question about an agent — build stack, Go module
dependencies, host tools with version AND resolved path AND a verdict against
the published minimums.

What these tests defend, in order of how expensive the mistake would be:

1. **One comparator.** Every verdict comes from ``filearr.toolversions``, the
   same function that judges central's own tools on the About page. A second
   implementation anywhere (Go, TypeScript, or a copy in this module) is the
   failure this whole design is arranged to prevent.
2. **No drift between the two About surfaces.** ``about.py`` and
   ``agent_about.py`` describe the same seven tools and must share one
   purpose/url table, not two copies of it.
3. **Never blank, never zero, never a 500.** An agent that has never polled is
   ``reported: false`` with empty sections, not an error; a tool that would not
   state its version is ``unknown``, not ``outdated``; a hostile advertisement
   degrades rather than raising.

Harness mirrors test_agents_p5t1.py (migrated pgserver Postgres).
"""

from __future__ import annotations

import logging
import uuid
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy import text
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import about as about_mod
from filearr import agent_about, toolversions
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app
from filearr.models import Agent

# No module-level asyncio mark: half of these are pure composition tests with no
# event loop involved, and the suite runs in ``asyncio_mode = "auto"``.
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
async def client(db_maker, monkeypatch):
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings
    app.dependency_overrides.clear()


#: A realistic advertisement from a current agent. Deliberately mixed: one tool
#: below its minimum, one present but silent about its version, one absent, and
#: one this central release has never heard of.
FULL_CAPS = {
    "inventory_collectors": ["owner", "perms", "placeholder", "stat"],
    "inventory_version": 1,
    "ffmpeg": True,
    "container": False,
    "extract": True,
    "extract_schema": 1,
    "tools": {
        "ffmpeg": True,
        "ffprobe": True,
        "tesseract": True,
        "exiftool": True,
        "pdfinfo": False,
        "pdftotext": False,
        "pdftoppm": False,
        "qpdf": True,  # a tool a NEWER agent build knows about and we do not
    },
    "tool_versions": {
        "ffmpeg": "7.1",
        "ffprobe": "7.1",
        "tesseract": "4.1.1",  # below the 5.0.0 minimum
        # exiftool present but silent — no entry at all, by design
        "qpdf": "11.9.0",
    },
    "tool_paths": {
        "ffmpeg": r"C:\Program Files\ffmpeg\bin\ffmpeg.exe",
        "ffprobe": r"C:\Program Files\ffmpeg\bin\ffprobe.exe",
        "tesseract": r"C:\Program Files\Tesseract-OCR\tesseract.exe",
        "exiftool": r"C:\Program Files\ExifTool\exiftool.exe",
        "qpdf": r"C:\Program Files\qpdf\bin\qpdf.exe",
    },
    "build": {
        "go_version": "go1.26.5",
        "goos": "windows",
        "goarch": "amd64",
        "os_version": "Windows 10.0 (build 26100)",
        "vcs_revision": "0123456789abcdef0123456789abcdef01234567",
        "vcs_time": "2026-08-11T12:00:00Z",
        "vcs_modified": False,
        "num_cpu": 16,
    },
    "modules": [
        {"path": "github.com/filearr/filearr/agent"},
        {"path": "golang.org/x/crypto", "version": "v0.54.0"},
    ],
    "formats": ["archive", "audio", "document", "image", "video"],
}


async def _mk_agent(maker, **kw) -> Agent:
    async with maker() as s:
        agent = Agent(
            name=kw.pop("name", "filer-01"),
            hostname=kw.pop("hostname", "filer-01"),
            platform=kw.pop("platform", "windows"),
            **kw,
        )
        s.add(agent)
        await s.commit()
        return agent


# --------------------------------------------------------------------------- #
# Composition (pure, no HTTP)                                                  #
# --------------------------------------------------------------------------- #
def _agent_row(**kw) -> Agent:
    """A detached Agent instance — agent_about() is pure, so no DB is needed to
    exercise every composition branch."""
    row = Agent(
        id=uuid.uuid4(),
        name=kw.pop("name", "filer-01"),
        hostname=kw.pop("hostname", "filer-01"),
        platform=kw.pop("platform", "windows"),
    )
    for k, v in kw.items():
        setattr(row, k, v)
    return row


def test_full_capabilities_populate_every_section():
    about = agent_about.agent_about(_agent_row(capabilities=FULL_CAPS))

    assert about["reported"] is True
    assert about["build"]["go_version"] == "go1.26.5"
    assert about["build"]["os_version"] == "Windows 10.0 (build 26100)"
    assert about["build"]["vcs_modified"] is False
    # Allowlisted build keys are always PRESENT (as null when unreported), so
    # the console never has to distinguish a missing key from a null one.
    assert "main_version" in about["build"]

    assert about["extract"]["schema"] == 1
    assert about["extract"]["collectors"] == ["owner", "perms", "placeholder", "stat"]

    mods = {m["path"]: m for m in about["modules"]}
    assert mods["golang.org/x/crypto"]["version"] == "v0.54.0"
    assert mods["golang.org/x/crypto"]["url"] == "https://pkg.go.dev/golang.org/x/crypto"
    # A module with no recorded version is null, never "" — the console renders
    # "version unknown" from the null.
    assert mods["github.com/filearr/filearr/agent"]["version"] is None
    assert about["modules_omitted"] is False


def test_host_tool_rows_are_ordered_judged_and_located():
    rows = {r["name"]: r for r in agent_about.host_tool_rows(FULL_CAPS)}
    order = [r["name"] for r in agent_about.host_tool_rows(FULL_CAPS)]

    # HOST_TOOLS order first, then anything newer the agent knows about.
    assert order[: len(toolversions.HOST_TOOLS)] == list(toolversions.HOST_TOOLS)
    assert order[-1] == "qpdf"

    # The verdicts are central's, from the one comparator.
    assert rows["ffmpeg"]["verdict"] == "ok"
    assert rows["tesseract"]["verdict"] == "outdated"  # 4.1.1 < 5.0.0
    assert rows["tesseract"]["minimum_version"] == "5.0.0"
    assert rows["tesseract"]["impact"]
    # Present but silent about its version: unjudged, NOT outdated.
    assert rows["exiftool"]["present"] is True
    assert rows["exiftool"]["version"] is None
    assert rows["exiftool"]["verdict"] == "unknown"
    assert rows["pdfinfo"]["verdict"] == "absent"
    # A tool this release has no opinion about is unknown, never ok.
    assert rows["qpdf"]["verdict"] == "unknown"
    assert rows["qpdf"]["minimum_version"] is None
    assert rows["qpdf"]["purpose"] is None  # no invented description

    # The locations, which are the point of the "pull the tool locations also"
    # request — and the visible proof of the agent's machine-wide-only rule.
    assert rows["exiftool"]["path"] == r"C:\Program Files\ExifTool\exiftool.exe"
    assert rows["pdfinfo"]["path"] is None  # absent tools have no location
    for row in rows.values():
        if row["path"]:
            assert "\\Users\\" not in row["path"], row


def test_verdicts_match_the_shared_comparator():
    """No second comparator: every verdict this module produces must equal what
    ``toolversions.tool_verdict`` says for the same inputs."""
    for row in agent_about.host_tool_rows(FULL_CAPS):
        assert row["verdict"] == toolversions.tool_verdict(
            row["name"], row["present"], row["version"]
        )


def test_never_reported_agent_is_empty_not_broken():
    about = agent_about.agent_about(_agent_row(capabilities=None))
    assert about["reported"] is False
    assert about["build"] is None
    assert about["host_tools"] == []
    # None, not [] — an empty list would claim the binary has no dependencies.
    assert about["modules"] is None
    assert about["modules_omitted"] is False
    assert about["extract"]["formats"] == []


def test_modules_omitted_flag_is_honoured():
    """The agent trims its own module list to stay inside its poll budget and
    says so. That flag has to survive to the console, or an empty table reads as
    "this binary has no dependencies"."""
    caps = dict(FULL_CAPS)
    caps.pop("modules")
    caps["modules_omitted"] = True
    about = agent_about.agent_about(_agent_row(capabilities=caps))
    assert about["modules"] is None
    assert about["modules_omitted"] is True


def test_hostile_advertisement_degrades_rather_than_raising():
    """``capabilities`` is third-party JSON from a machine we do not control.
    Every wrong type must produce a missing section, never an exception."""
    caps = {
        "tools": "not-a-dict",
        "tool_versions": 42,
        "tool_paths": [1, 2, 3],
        "build": "nope",
        "modules": [{"path": 5}, "junk", {"nothing": True}],
        "extract_schema": "one",
        "formats": [1, "audio", None],
        "inventory_collectors": "stat",
    }
    about = agent_about.agent_about(_agent_row(capabilities=caps))
    assert about["reported"] is True
    assert about["build"] is None
    assert about["modules"] is None
    assert about["extract"]["schema"] is None
    assert about["extract"]["formats"] == ["audio"]
    assert about["extract"]["collectors"] == []
    # The tool matrix still renders — as seven absent rows, which is what a
    # nonsense `tools` value honestly implies.
    assert [r["name"] for r in about["host_tools"]] == list(toolversions.HOST_TOOLS)
    assert all(r["verdict"] == "absent" for r in about["host_tools"])


def test_untrusted_strings_are_capped():
    caps = {
        "tools": {"ffmpeg": True},
        "tool_versions": {"ffmpeg": "v" * 5000},
        "tool_paths": {"ffmpeg": "C:\\" + "x" * 5000},
    }
    row = agent_about.host_tool_rows(caps)[0]
    assert len(row["version"]) <= agent_about._MAX_TEXT
    assert len(row["path"]) <= agent_about._MAX_TEXT


# --------------------------------------------------------------------------- #
# The two About surfaces must not drift                                        #
# --------------------------------------------------------------------------- #
def test_host_tool_prose_is_shared_with_the_server_about_page():
    """One table, imported by both. Two copies would drift on the first edit and
    nothing would fail — the pages would just quietly describe the same binary
    differently, which is how operators stop believing either of them."""
    assert about_mod._TOOL_INFO is toolversions.HOST_TOOL_INFO

    server_rows = {r["name"]: r for r in about_mod.host_tools()}
    agent_rows = {r["name"]: r for r in agent_about.host_tool_rows(FULL_CAPS)}
    assert set(server_rows) == set(toolversions.HOST_TOOLS)
    for name in toolversions.HOST_TOOLS:
        assert server_rows[name]["purpose"] == agent_rows[name]["purpose"]
        assert server_rows[name]["url"] == agent_rows[name]["url"]
        assert server_rows[name]["minimum_version"] == agent_rows[name]["minimum_version"]
        assert server_rows[name]["impact"] == agent_rows[name]["impact"]
    # Same keys, so one frontend cell helper renders both tables.
    assert set(server_rows["ffmpeg"]) == set(agent_rows["ffmpeg"])


# --------------------------------------------------------------------------- #
# Endpoint                                                                     #
# --------------------------------------------------------------------------- #
async def test_endpoint_returns_the_report(client):
    c, maker, _ = client
    agent = await _mk_agent(
        maker,
        capabilities=FULL_CAPS,
        capabilities_at=datetime(2026, 8, 11, 12, 0, tzinfo=UTC),
        agent_version="0.9.1",
        last_auth_mode="mtls",
    )

    r = await c.get(f"/api/v1/agents/{agent.id}/about")
    assert r.status_code == 200, r.text
    body = r.json()

    assert body["agent"]["hostname"] == "filer-01"
    assert body["agent"]["agent_version"] == "0.9.1"
    assert body["agent"]["auth_mode"] == "mtls"
    assert body["agent"]["capabilities_at"].startswith("2026-08-11T12:00:00")
    assert body["build"]["goarch"] == "amd64"
    assert body["extract"]["schema"] == 1
    tools = {t["name"]: t for t in body["host_tools"]}
    assert tools["tesseract"]["verdict"] == "outdated"
    assert tools["ffprobe"]["path"].endswith("ffprobe.exe")
    assert body["reported"] is True


async def test_endpoint_on_a_never_polled_agent_is_200(client):
    """A pending enrollment is a normal state. 404-ing it would send an operator
    hunting for an agent that is sitting right there in the table."""
    c, maker, _ = client
    agent = await _mk_agent(maker, name="pending", hostname="pending")

    r = await c.get(f"/api/v1/agents/{agent.id}/about")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["reported"] is False
    assert body["build"] is None and body["modules"] is None
    assert body["host_tools"] == []
    assert body["agent"]["capabilities_at"] is None


async def test_endpoint_404_on_unknown_agent(client):
    c, _, _ = client
    r = await c.get(f"/api/v1/agents/{uuid.uuid4()}/about")
    assert r.status_code == 404


async def test_endpoint_404_when_agents_disabled(client, monkeypatch):
    c, maker, settings = client
    agent = await _mk_agent(maker, name="off", hostname="off")
    monkeypatch.setattr(settings, "agents_enabled", False)
    r = await c.get(f"/api/v1/agents/{agent.id}/about")
    assert r.status_code == 404


async def test_endpoint_requires_admin_scope(db_maker, monkeypatch):
    """Admin, deliberately narrower than ``/system/about``'s read scope: this
    exposes filesystem paths from someone else's machine."""
    monkeypatch.setattr(db_mod, "SessionLocal", maker := db_maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", True)
    monkeypatch.setattr(settings, "agents_enabled", True)
    app = create_app()

    async def _s():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _s
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        r = await c.get(f"/api/v1/agents/{uuid.uuid4()}/about")
        assert r.status_code == 401
    app.dependency_overrides.clear()


# --------------------------------------------------------------------------- #
# Oversize advertisements: dropped, but no longer silently                     #
# --------------------------------------------------------------------------- #
async def test_oversize_capabilities_are_dropped_with_a_warning(client, caplog, monkeypatch):
    """The drop is deliberate and stays (the command drain must not depend on an
    advertisement). The SILENCE was the trap: an agent whose advertisement grew
    past the cap polls happily forever while the console shows stale data."""
    c, maker, settings = client
    agent = await _mk_agent(maker, name="chatty", hostname="chatty")
    async with maker() as s:
        row = await s.get(Agent, agent.id)
        row.cert_fingerprint = "fp-chatty"
        await s.commit()

    oversize = {"tools": {"ffmpeg": True}, "junk": "x" * 200}
    # 32 bytes: any real advertisement is oversize against it.
    monkeypatch.setattr(settings, "agent_capabilities_max_bytes", 32)

    with caplog.at_level(logging.WARNING, logger="filearr.api.agent_commands"):
        r = await c.post(
            f"/api/v1/agents/{agent.id}/commands/poll",
            json={"max": 5, "capabilities": oversize},
            headers={"Authorization": "Bearer fp-chatty"},
        )
    # The poll still succeeds — dropping an advertisement must never break
    # command delivery.
    assert r.status_code == 200, r.text
    assert any(
        "oversize capabilities" in rec.getMessage() and str(agent.id) in rec.getMessage()
        for rec in caplog.records
    ), [rec.getMessage() for rec in caplog.records]

    async with maker() as s:
        row = await s.get(Agent, agent.id)
        assert row.capabilities is None  # unchanged
        assert row.capabilities_at is None  # and the clock did NOT advance


async def test_accepted_capabilities_stamp_their_arrival(client):
    """``capabilities_at`` is stamped on the poll that STORED the advertisement —
    its own column precisely so a dropped body cannot advance it."""
    c, maker, _ = client
    agent = await _mk_agent(maker, name="fresh", hostname="fresh")
    async with maker() as s:
        row = await s.get(Agent, agent.id)
        row.cert_fingerprint = "fp-fresh"
        await s.commit()

    r = await c.post(
        f"/api/v1/agents/{agent.id}/commands/poll",
        json={"max": 5, "capabilities": FULL_CAPS},
        headers={"Authorization": "Bearer fp-fresh"},
    )
    assert r.status_code == 200, r.text

    async with maker() as s:
        row = await s.get(Agent, agent.id)
        assert row.capabilities["build"]["goos"] == "windows"
        assert row.capabilities_at is not None

    body = (await c.get(f"/api/v1/agents/{agent.id}/about")).json()
    assert body["agent"]["capabilities_at"] is not None
    assert body["reported"] is True
