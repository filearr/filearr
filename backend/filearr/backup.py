"""BK-T3 — in-app backup: a bundle an operator can produce without a shell.

WHY THIS EXISTS. `scripts/backup.sh` is the complete backup, but it requires SSH
to the Docker host and a compose project. On Unraid there is no compose, and for
a large share of self-hosters "open a terminal on the server" is the step that
never happens. A console button that produces a real, downloadable, restorable
dump is worth a great deal more than a perfect procedure nobody runs.

WHY IT IS NOT A REPLACEMENT, stated here and repeated in the manifest, the API
response and the Jobs page — because a backup an operator wrongly believes is
complete is worse than one they know is partial:

* A container cannot read the **step-ca volume** (it is mounted into another
  container). Lose it and step-ca mints a NEW root on next start; every
  certificate it ever issued stops validating and every agent re-enrolls.
* A container cannot read the host **.env**. That file holds
  ``FILEARR_SECRET_KEY`` — the envelope key for alert-channel secrets — and a
  restore under a different key succeeds while leaving every one of those
  secrets permanently undecryptable. We can record its FINGERPRINT (that much
  the process knows) but not the value.

So this writes: the dump, a MANIFEST.json that says exactly what is and is not
inside, and nothing else.

VERSION DISCIPLINE. ``pg_dump`` refuses nothing when it is OLDER than the server
— it produces a dump that may silently omit newer catalogue constructs. That is
the same shape of failure as the secret-key one (success reported, data lost),
so the task reads ``SHOW server_version_num``, compares it against
``pg_dump --version``, and FAILS LOUDLY with both numbers rather than writing a
file an operator will trust.

DISK DISCIPLINE. A dump is written through :func:`filearr.diskguard.guard_write`
and to a ``.partial`` path renamed on success, exactly like the report exports —
an unattended backup that fills the disk is worse than no backup, which is also
why this task ships with NO default schedule.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
from datetime import UTC, datetime

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

log = logging.getLogger("filearr.backup")

#: Bundle directory name pattern. Timestamped, no operator string anywhere in
#: it — the download endpoint's traversal guard leans on this being a closed
#: vocabulary rather than on sanitising a user-supplied name.
_NAME_RE = re.compile(r"^filearr-\d{8}T\d{6}Z$")

MANIFEST_NAME = "MANIFEST.json"

#: Repeated verbatim in the manifest, the API and the UI. One string, so the
#: honest caveat cannot drift out of one of the three places an operator reads.
INCOMPLETE_NOTE = (
    "This bundle was produced INSIDE the application container and is NOT, by "
    "itself, a disaster-recovery backup. A container cannot read the step-ca "
    "volume (losing it forces a new CA root and full agent re-enrollment) or "
    "the host .env (which holds FILEARR_SECRET_KEY — restoring this dump under "
    "a different key succeeds while leaving every encrypted alert-channel "
    "secret permanently undecryptable). Copy .env and the step-ca volume "
    "separately, or run scripts/backup.sh on the host, which does both."
)


class BackupError(RuntimeError):
    """A refused or failed backup. The message is operator-facing and is what
    lands on the failed job — it must name the fix, not just the fault."""


def backup_dir(settings) -> str:
    """Where in-app bundles live: ``{config_dir}/backups``.

    Inside ``{config}`` on purpose here, unlike the host script's default. A
    container has nowhere else it can write, and the honest consequence — that
    these bundles sit on the same volume they protect — is precisely why the
    download endpoint exists and why the docs tell operators to pull them off
    the box."""
    return os.path.join(settings.config_dir, "backups")


def bundle_name(now: datetime | None = None) -> str:
    return f"filearr-{(now or datetime.now(UTC)).strftime('%Y%m%dT%H%M%SZ')}"


def is_valid_name(name: str) -> bool:
    """Traversal guard for the download endpoint (mirrors the export-artifact
    discipline in :mod:`filearr.exports`: no operator string ever reaches a
    path). Rejects ``..``, separators, and anything not matching the generated
    timestamp shape."""
    return bool(_NAME_RE.match(name))


# --------------------------------------------------------------------------- #
# Version guard                                                                #
# --------------------------------------------------------------------------- #


async def _pg_dump_version() -> tuple[int, str]:
    """``(major, raw banner)`` of the pg_dump on PATH.

    Raises :class:`BackupError` when pg_dump is absent — which is the honest
    answer on a dev checkout or an image built before the client was added."""
    exe = shutil.which("pg_dump")
    if not exe:
        raise BackupError(
            "pg_dump is not installed in this image, so an in-app backup cannot "
            "run. Use scripts/backup.sh on the Docker host, or update to an "
            "image built with the postgresql-client package."
        )
    proc = await asyncio.create_subprocess_exec(
        exe, "--version", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    banner = (out or b"").decode("utf-8", "replace").strip()
    m = re.search(r"(\d+)(?:\.(\d+))?", banner)
    if not m:
        raise BackupError(f"could not parse the pg_dump version from {banner!r}")
    return int(m.group(1)), banner


async def _server_version(session: AsyncSession) -> tuple[int, str]:
    """``(major, display)`` of the server we are about to dump."""
    num = (await session.execute(text("SHOW server_version_num"))).scalar()
    disp = (await session.execute(text("SHOW server_version"))).scalar()
    return int(num) // 10000, str(disp)


async def check_versions(session: AsyncSession) -> dict:
    """Refuse the backup when pg_dump is OLDER than the server.

    The failure this prevents is the quiet one: an older pg_dump does not
    necessarily error, it can emit an archive missing catalogue constructs it
    does not understand, and the operator discovers that only at restore. So
    the mismatch is a hard failure carrying BOTH version numbers — the two facts
    needed to fix it."""
    client_major, client_banner = await _pg_dump_version()
    server_major, server_disp = await _server_version(session)
    if client_major < server_major:
        raise BackupError(
            f"pg_dump is version {client_major} but the server is "
            f"{server_disp} (major {server_major}). pg_dump must be at least "
            "the server's major version; an older client can write a silently "
            "incomplete dump. Refusing to write one. Update the Filearr image, "
            "or take the backup with scripts/backup.sh on the host."
        )
    return {
        "pg_dump_major": client_major,
        "pg_dump_version": client_banner,
        "server_major": server_major,
        "server_version": server_disp,
    }


# --------------------------------------------------------------------------- #
# The dump                                                                     #
# --------------------------------------------------------------------------- #


def _dump_env(settings) -> dict[str, str]:
    """Environment for the pg_dump subprocess.

    ``PGPASSWORD`` is passed through the environment rather than in the DSN
    because a DSN would appear in the process table for anyone with a shell on
    the host. Nothing here is ever logged."""
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    env = dict(os.environ)
    if url.password:
        env["PGPASSWORD"] = str(url.password)
    return env


def _dump_args(settings) -> list[str]:
    from sqlalchemy.engine import make_url

    url = make_url(settings.database_url)
    args = ["-Fc", "--no-owner"]
    if url.host:
        args += ["-h", str(url.host)]
    if url.port:
        args += ["-p", str(url.port)]
    if url.username:
        args += ["-U", str(url.username)]
    args.append(url.database or "filearr")
    return args


async def run_backup(session: AsyncSession, settings, *, now=None) -> dict:
    """Produce one bundle; return its manifest dict.

    Raises :class:`BackupError` on any refusal or failure — the caller (the
    Procrastinate task) lets that fail the job so it is visible on the Jobs page
    rather than being swallowed into a "succeeded" run with no file."""
    from filearr import diskguard

    versions = await check_versions(session)

    directory = backup_dir(settings)
    os.makedirs(directory, exist_ok=True)
    # FIX-11 fail-closed: at the critical free-space floor this raises rather
    # than filling the last of the disk with a dump. A backup is exactly the
    # kind of large unattended write that turns "low disk" into "Postgres
    # cannot write its WAL".
    diskguard.guard_write(directory, settings)

    name = bundle_name(now)
    work = os.path.join(directory, name + ".partial")
    final = os.path.join(directory, name)
    if os.path.exists(work):
        shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)

    dump_file = f"{name}.dump"
    dump_path = os.path.join(work, dump_file)
    try:
        exe = shutil.which("pg_dump")
        assert exe  # check_versions already proved it exists
        with open(dump_path, "wb") as fh:
            proc = await asyncio.create_subprocess_exec(
                exe,
                *_dump_args(settings),
                stdout=fh,
                stderr=asyncio.subprocess.PIPE,
                env=_dump_env(settings),
            )
            _, err = await proc.communicate()
        if proc.returncode != 0:
            detail = (err or b"").decode("utf-8", "replace").strip()[:500]
            raise BackupError(f"pg_dump exited {proc.returncode}: {detail}")

        size = os.path.getsize(dump_path)
        head = (
            await session.execute(text("SELECT version_num FROM alembic_version LIMIT 1"))
        ).scalar()
        items = (await session.execute(text("SELECT count(*) FROM items"))).scalar()
        manifest = build_manifest(
            settings,
            name=name,
            dump_file=dump_file,
            dump_bytes=size,
            alembic_head=head,
            item_count=int(items or 0),
            versions=versions,
        )
        with open(os.path.join(work, MANIFEST_NAME), "w", encoding="utf-8") as fh:
            json.dump(manifest, fh, indent=2, sort_keys=False)
            fh.write("\n")
        # Atomic publish: a crash mid-dump leaves only a .partial, which the
        # listing ignores and the next run replaces.
        os.replace(work, final)
    except Exception:
        shutil.rmtree(work, ignore_errors=True)
        raise
    prune(settings)
    log.info(
        "backup: wrote %s (%d bytes, %d items, head %s)",
        final,
        manifest["contents"]["dump"]["bytes"],
        manifest["item_count"],
        manifest["alembic_head"],
    )
    return manifest


def build_manifest(
    settings,
    *,
    name: str,
    dump_file: str,
    dump_bytes: int,
    alembic_head,
    item_count: int,
    versions: dict,
) -> dict:
    """The bundle's MANIFEST.json.

    Same shape as the host script's so one restore procedure and one verifier
    read both, with ``complete: false`` and the explicit ``missing`` list that
    say what a container could not reach. The fingerprints are
    ``sha256(value)[:16]`` from :mod:`filearr.keyguard` — never values."""
    from filearr import __version__, keyguard

    return {
        "bundle_version": 1,
        "created_at": datetime.now(UTC).isoformat(),
        "created_by": "in-app (POST /system/backup)",
        "app_version": __version__,
        "alembic_head": alembic_head,
        "postgres_server_version": versions.get("server_version"),
        "pg_dump_version": versions.get("pg_dump_version"),
        "item_count": item_count,
        "contents": {
            "dump": {"file": dump_file, "bytes": dump_bytes},
            "env": {"file": None, "included": False},
            "stepca": {"file": None, "included": False, "bytes": 0},
        },
        # The honest half. `complete` is a machine-readable flag so a future
        # verifier/UI never has to parse prose to know this is partial.
        "complete": False,
        "missing": [".env (host file)", "step-ca volume"],
        "incomplete_note": INCOMPLETE_NOTE,
        "fingerprints": {
            "_note": (
                "sha256(value)[:16] hex. NEVER a value. Compare against the "
                "target deployment BEFORE trusting a restore."
            ),
            **keyguard.fingerprints_for_manifest(settings),
        },
        "restore_notes": [
            "Verify before you trust: scripts/verify-backup.sh <bundle>.",
            (
                "Set FILEARR_SECRET_KEY on the target to the ORIGINAL value "
                "before starting the app. A different key restores cleanly and "
                "leaves every encrypted alert-channel secret permanently "
                "undecryptable, with no error anywhere. The fingerprint above "
                "is what this database's ciphertext was written under."
            ),
            (
                "Restore the step-ca volume separately if this deployment runs "
                "agents: an empty volume makes step-ca generate a NEW root and "
                "every enrolled agent must re-enroll."
            ),
            (
                "Load order: pg_restore --clean --if-exists, THEN "
                "scripts/init_db.py, THEN start the stack, THEN POST "
                "/api/v1/system/rebuild-index."
            ),
        ],
    }


# --------------------------------------------------------------------------- #
# Listing / retention                                                          #
# --------------------------------------------------------------------------- #


def _read_manifest(path: str) -> dict | None:
    try:
        with open(os.path.join(path, MANIFEST_NAME), encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def list_bundles(settings) -> list[dict]:
    """Newest-first listing of complete bundles. ``.partial`` directories are
    invisible here by design — they are never valid backups and showing one
    would invite an operator to download it mid-incident."""
    directory = backup_dir(settings)
    out: list[dict] = []
    try:
        names = os.listdir(directory)
    except OSError:
        return out
    for name in names:
        path = os.path.join(directory, name)
        if not is_valid_name(name) or not os.path.isdir(path):
            continue
        manifest = _read_manifest(path)
        total = 0
        dump_file = None
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except OSError:
                    pass
                if f.endswith(".dump"):
                    dump_file = f
        out.append(
            {
                "name": name,
                "bytes": total,
                "dump_file": dump_file,
                "created_at": (manifest or {}).get("created_at"),
                "item_count": (manifest or {}).get("item_count"),
                "alembic_head": (manifest or {}).get("alembic_head"),
                "app_version": (manifest or {}).get("app_version"),
                # False for every in-app bundle; surfaced per-row so the UI
                # never has to assume.
                "complete": bool((manifest or {}).get("complete", False)),
                "missing": (manifest or {}).get("missing", []),
            }
        )
    out.sort(key=lambda r: r["name"], reverse=True)
    return out


def prune(settings) -> int:
    """Keep the newest ``FILEARR_BACKUP_KEEP`` bundles; sweep abandoned
    ``.partial`` directories. Returns how many bundles were removed.

    Retention matters more here than for the host script: these bundles live on
    ``{config}``, the same volume the thumbnail cache and the disk monitor watch,
    so an unbounded pile of dumps is a self-inflicted disk-full incident."""
    keep = max(1, int(getattr(settings, "backup_keep", 7) or 7))
    directory = backup_dir(settings)
    try:
        names = sorted(
            (n for n in os.listdir(directory) if is_valid_name(n)), reverse=True
        )
    except OSError:
        return 0
    removed = 0
    for name in names[keep:]:
        shutil.rmtree(os.path.join(directory, name), ignore_errors=True)
        removed += 1
    for name in os.listdir(directory):
        if name.endswith(".partial"):
            path = os.path.join(directory, name)
            try:
                age = datetime.now(UTC).timestamp() - os.path.getmtime(path)
            except OSError:
                continue
            # An hour of grace so a long-running dump is never swept from under
            # a concurrent run.
            if age > 3600:
                shutil.rmtree(path, ignore_errors=True)
    return removed


def bundle_path(settings, name: str) -> str | None:
    """Absolute path of one bundle's dump file, or ``None``.

    Both guards, deliberately: the name must match the generated pattern AND the
    resolved path must stay under the backup directory. The pattern alone is
    sufficient today; the containment check is what survives a future change to
    the pattern."""
    if not is_valid_name(name):
        return None
    directory = os.path.realpath(backup_dir(settings))
    path = os.path.realpath(os.path.join(directory, name))
    if os.path.commonpath([directory, path]) != directory:
        return None
    if not os.path.isdir(path):
        return None
    for f in sorted(os.listdir(path)):
        if f.endswith(".dump"):
            return os.path.join(path, f)
    return None
