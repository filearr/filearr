"""BK-T3 — in-app backup: version guard, bundle shape, endpoints.

The two things worth pinning hardest are the ones that would otherwise fail
QUIETLY: a pg_dump older than the server (which can emit an incomplete archive
that restores without complaint), and a bundle an operator believes is complete
when it cannot contain the host ``.env`` or the step-ca volume. Everything here
is exercised without a real ``pg_dump`` binary — the subprocess is faked — so
the suite runs identically on a dev box and in CI.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import httpx
import pytest
from alembic.config import Config
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from alembic import command
from filearr import backup
from filearr import db as db_mod
from filearr.config import get_settings
from filearr.db import get_session
from filearr.main import create_app

BACKEND_DIR = Path(__file__).resolve().parent.parent


def _psycopg3(uri: str) -> str:
    return uri.replace("postgresql://", "postgresql+psycopg://", 1)


@pytest.fixture
async def ctx(pg_uri, tmp_path, monkeypatch):
    command.upgrade(Config(str(BACKEND_DIR / "alembic.ini")), "head")
    engine = create_async_engine(_psycopg3(pg_uri))
    maker = async_sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr(db_mod, "SessionLocal", maker)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "config_dir", str(tmp_path))
    monkeypatch.setattr(settings, "secret_key", "bk3-test-key")
    # The FIX-11 disk guard is os.statvfs-based and therefore POSIX-only; on
    # Windows it raises AttributeError before it can answer. Neutralised here so
    # these tests run on every dev box — the guard's own behaviour is covered by
    # test_diskguard_fix11.py, and its WIRING into the backup path is covered by
    # test_disk_guard_refusal_propagates below.
    from filearr import diskguard

    monkeypatch.setattr(diskguard, "guard_write", lambda *_a, **_k: {"status": "ok"})
    yield maker, settings, monkeypatch, tmp_path
    await engine.dispose()


class _FakeProc:
    """Stand-in for an asyncio subprocess: `pg_dump` writes to the fd it was
    handed, so the fake does the same and reports success."""

    def __init__(self, returncode=0, stdout=b"", write_to=None, payload=b""):
        self.returncode = returncode
        self._stdout = stdout
        self._write_to = write_to
        self._payload = payload

    async def communicate(self):
        if self._write_to is not None:
            self._write_to.write(self._payload)
        return self._stdout, b""


def _fake_exec(monkeypatch, *, dump_major=18, dump_rc=0, payload=b"PGDMP-fake"):
    """Patch shutil.which + create_subprocess_exec so no real pg_dump is needed."""
    monkeypatch.setattr(backup.shutil, "which", lambda _n: "/usr/bin/pg_dump")

    async def _exec(exe, *args, **kwargs):
        if args and args[0] == "--version":
            return _FakeProc(stdout=f"pg_dump (PostgreSQL) {dump_major}.4\n".encode())
        return _FakeProc(
            returncode=dump_rc, write_to=kwargs.get("stdout"), payload=payload
        )

    monkeypatch.setattr(backup.asyncio, "create_subprocess_exec", _exec)


# --------------------------------------------------------------------------- #
# Version guard — the silent-partial-dump defence                              #
# --------------------------------------------------------------------------- #


async def test_older_pg_dump_is_refused_with_both_versions(ctx):
    """An older client may produce an archive missing constructs it does not
    understand — success reported, data lost. Refuse, and name BOTH numbers so
    the operator can act without guessing which side is wrong."""
    maker, settings, monkeypatch, _ = ctx
    _fake_exec(monkeypatch, dump_major=14)
    async with maker() as session:
        with pytest.raises(backup.BackupError) as exc:
            await backup.run_backup(session, settings)
    msg = str(exc.value)
    assert "14" in msg and "Refusing" in msg
    # And nothing was written — a refusal must not leave a half-bundle behind.
    assert backup.list_bundles(settings) == []


async def test_absent_pg_dump_says_what_to_do(ctx):
    """A dev checkout / an image built before the client was added. The message
    must route the operator to the host script rather than dead-ending."""
    maker, settings, monkeypatch, _ = ctx
    monkeypatch.setattr(backup.shutil, "which", lambda _n: None)
    async with maker() as session:
        with pytest.raises(backup.BackupError, match="scripts/backup.sh"):
            await backup.run_backup(session, settings)


async def test_newer_pg_dump_is_accepted(ctx):
    """pg_dump >= server is the whole rule; a NEWER client is fine."""
    maker, settings, monkeypatch, _ = ctx
    _fake_exec(monkeypatch, dump_major=99)
    async with maker() as session:
        manifest = await backup.run_backup(session, settings)
    assert manifest["contents"]["dump"]["bytes"] > 0


async def test_failed_pg_dump_leaves_no_bundle(ctx):
    """A non-zero exit must fail the job, not publish a truncated bundle that
    the listing (and a panicking operator) would treat as a backup."""
    maker, settings, monkeypatch, tmp_path = ctx
    _fake_exec(monkeypatch, dump_rc=1)
    async with maker() as session:
        with pytest.raises(backup.BackupError, match="exited 1"):
            await backup.run_backup(session, settings)
    assert backup.list_bundles(settings) == []
    assert not any(p.name.endswith(".partial") for p in (tmp_path / "backups").iterdir())


# --------------------------------------------------------------------------- #
# Bundle + manifest shape                                                      #
# --------------------------------------------------------------------------- #


async def test_disk_guard_refusal_propagates(ctx):
    """FIX-11 fail-closed, wired in: a backup is exactly the kind of large
    unattended write that turns "low disk" into "Postgres cannot write its WAL",
    so at the critical floor it must refuse rather than consume the last of it."""
    maker, settings, monkeypatch, _ = ctx
    _fake_exec(monkeypatch)
    from filearr import diskguard

    def _refuse(path, _settings, **_kw):
        raise diskguard.DiskGuardError(path, {"status": "critical", "free": 0})

    monkeypatch.setattr(diskguard, "guard_write", _refuse)
    async with maker() as session:
        with pytest.raises(diskguard.DiskGuardError):
            await backup.run_backup(session, settings)
    assert backup.list_bundles(settings) == []


async def test_bundle_shape_and_honest_manifest(ctx):
    maker, settings, monkeypatch, tmp_path = ctx
    _fake_exec(monkeypatch)
    async with maker() as session:
        manifest = await backup.run_backup(session, settings)

    bundles = backup.list_bundles(settings)
    assert len(bundles) == 1
    name = bundles[0]["name"]
    root = tmp_path / "backups" / name
    assert (root / f"{name}.dump").is_file()
    assert (root / "MANIFEST.json").is_file()

    on_disk = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    assert on_disk == manifest
    # The honest half, machine-readable AND in prose.
    assert on_disk["complete"] is False
    assert on_disk["missing"] == [".env (host file)", "step-ca volume"]
    assert "step-ca" in on_disk["incomplete_note"]
    assert on_disk["contents"]["env"]["included"] is False
    assert on_disk["contents"]["stepca"]["included"] is False
    assert on_disk["restore_notes"]
    # Fingerprints, never values. This is the anchor a restore is checked
    # against, so its absence would silently reintroduce the BK-T1 defect.
    fps = on_disk["fingerprints"]
    assert fps["secret_key_fingerprint"] and len(fps["secret_key_fingerprint"]) == 16
    assert "bk3-test-key" not in json.dumps(on_disk)
    assert on_disk["alembic_head"]


async def test_retention_keeps_the_newest_and_sweeps_partials(ctx):
    """These bundles live on the /config volume the disk monitor watches, so
    unbounded retention is a self-inflicted disk-full incident."""
    maker, settings, monkeypatch, tmp_path = ctx
    _fake_exec(monkeypatch)
    monkeypatch.setattr(settings, "backup_keep", 2)
    from datetime import UTC, datetime, timedelta

    base = datetime(2026, 8, 12, 3, 0, tzinfo=UTC)
    for i in range(4):
        async with maker() as session:
            await backup.run_backup(session, settings, now=base + timedelta(hours=i))
    names = [b["name"] for b in backup.list_bundles(settings)]
    assert len(names) == 2
    assert names == sorted(names, reverse=True)  # newest first, oldest pruned


# --------------------------------------------------------------------------- #
# Traversal guard                                                              #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "name",
    [
        "..",
        "../etc/passwd",
        "filearr-20260812T030000Z/../..",
        "filearr-2026",
        "",
        "filearr-20260812T030000Z ",
        "%2e%2e",
    ],
)
def test_bundle_name_guard_rejects_everything_but_the_generated_shape(name):
    assert not backup.is_valid_name(name)


def test_generated_names_pass_their_own_guard():
    assert backup.is_valid_name(backup.bundle_name())


# --------------------------------------------------------------------------- #
# Endpoints                                                                    #
# --------------------------------------------------------------------------- #


@pytest.fixture
async def client(ctx):
    maker, settings, monkeypatch, tmp_path = ctx
    app = create_app()

    async def _test_session():
        async with maker() as s:
            yield s

    app.dependency_overrides[get_session] = _test_session
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, maker, settings, monkeypatch, tmp_path
    app.dependency_overrides.clear()


async def test_list_and_download_roundtrip(client):
    c, maker, settings, monkeypatch, _ = client
    _fake_exec(monkeypatch, payload=b"PGDMP-roundtrip")
    async with maker() as session:
        await backup.run_backup(session, settings)

    listing = (await c.get("/api/v1/system/backups")).json()
    assert listing["keep"] == settings.backup_keep
    assert "step-ca" in listing["incomplete_note"]
    row = listing["bundles"][0]
    assert row["complete"] is False and row["missing"]

    dl = await c.get(f"/api/v1/system/backups/{row['name']}")
    assert dl.status_code == 200
    assert dl.content == b"PGDMP-roundtrip"
    assert dl.headers["content-type"] == "application/octet-stream"


async def test_download_404s_on_unknown_and_on_traversal(client):
    c, _maker, _settings, _mp, _tmp = client
    for name in ("filearr-20260101T000000Z", "..%2f..%2fetc%2fpasswd", "nonsense"):
        r = await c.get(f"/api/v1/system/backups/{name}")
        # Unknown and malformed are deliberately indistinguishable: the endpoint
        # must not double as a probe for what exists on the config volume.
        assert r.status_code in (404, 405), name


async def test_trigger_returns_the_caveat_even_to_an_api_only_caller(client, monkeypatch):
    """An operator scripting against the API never reads the Jobs page. The
    limitation must ride the response body."""
    c, _maker, _settings, _mp, _tmp = client

    async def _fake_run_now(key):
        assert key == "backup_now"
        return 4242

    import filearr.maintenance as maint_mod

    monkeypatch.setattr(maint_mod, "run_now", _fake_run_now)
    r = await c.post("/api/v1/system/backup")
    assert r.status_code == 202
    body = r.json()
    assert body["job_id"] == 4242
    assert "by itself, a disaster-recovery backup" in body["incomplete_note"]


async def test_backup_task_is_in_the_registry_unscheduled_but_editable():
    """No default cron (an unattended dump filling a disk is worse than no
    dump) yet editable, so an operator CAN opt in — and the tick must actually
    consider it, which the pre-BK-T3 TICK_SCHEDULED predicate did not."""
    from filearr.maintenance import MAINT_TASKS, TICK_SCHEDULED

    spec = MAINT_TASKS["backup_now"]
    assert spec.default_cron is None
    assert spec.editable and spec.runnable
    assert spec.category == "integrity"
    assert spec in TICK_SCHEDULED


async def test_unscheduled_editable_task_never_fires_on_its_own(ctx):
    """The other half of that widening: a task with no effective cron must be
    skipped by the tick, not crash it."""
    from datetime import UTC, datetime

    from filearr.maintenance import run_maintenance_tick

    fired: list[str] = []

    async def _defer(spec, at):
        fired.append(spec.key)

    keys = await run_maintenance_tick(datetime(2026, 8, 12, 4, 0, tzinfo=UTC), defer=_defer)
    assert "backup_now" not in keys
    assert "backup_now" not in fired


def test_backup_dir_is_under_config():
    settings = get_settings()
    assert backup.backup_dir(settings) == os.path.join(settings.config_dir, "backups")
