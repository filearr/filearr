"""UR-T2 — regression guard for ``backend/scripts/docker-entrypoint.sh``.

The entrypoint is the only place in the product where process supervision is
implemented, and it is implemented in POSIX sh (no supervisord/s6 — see the
script header). Shell has no type checker and no import-time errors, so a typo
in the merged-mode supervisor would ship silently and only surface as "the
Unraid container is healthy but the API is dead". These tests drive the REAL
script with stub ``uvicorn`` / ``procrastinate`` / ``python`` executables on
PATH, so they exercise the actual argv, the actual signal forwarding, and the
actual exit codes without needing Docker or a database.

Four behaviours are load-bearing and each maps to a live incident or invariant:

  * **Bootstrap runs exactly once, before either child.** Procrastinate's
    ``apply_schema`` is not idempotent across concurrent processes; the whole
    ``argv[1] == "uvicorn"`` gate exists for that reason and merged mode must
    not widen it into a race with itself.
  * **SIGTERM reaches BOTH children and we wait for them.**
    ``docker-compose.yml`` sets ``stop_grace_period: 60s`` on the worker
    because Docker's default 10s regularly cut Procrastinate jobs off
    mid-transaction during redeploys. Unraid gives 10s and no knob, so the
    supervisor itself has to hold the door open (``FILEARR_STOP_GRACE_SECONDS``).
  * **Either child dying takes the container down, non-zero — even if the child
    exited 0.** A supervisor limping along on one child hides a dead API behind
    a "running" container.
  * **Mode 1 (pass-through) is byte-for-byte unchanged.** Compose and the
    Proxmox deploy run it; merged mode must be purely additive.

POSIX-only: the script is /bin/sh and the tests fork, signal and reap real
processes. Skipped on Windows, where the documented local baseline already
excludes the POSIX-only suites.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="POSIX-only: drives /bin/sh, fork and signals"
)

ENTRYPOINT = (
    Path(__file__).resolve().parents[1] / "scripts" / "docker-entrypoint.sh"
)

# Stub children. Each records the argv it was called with, then either dies
# immediately (crash simulation) or parks in an interruptible sleep loop that
# records the TERM it receives. `sleep & wait` rather than plain `sleep` for the
# same reason the entrypoint itself uses it: POSIX defers a trapped signal until
# the current foreground command completes, and `wait` is the one builtin
# specified to be interruptible.
_STUB_PYTHON = """#!/bin/sh
echo "python $*" >> "$MARKER_DIR/bootstrap.log"
exit "${STUB_BOOTSTRAP_RC:-0}"
"""

_STUB_UVICORN = """#!/bin/sh
echo "$*" > "$MARKER_DIR/uvicorn.args"
trap 'echo term > "$MARKER_DIR/uvicorn.term"; exit 0' TERM
if [ "${STUB_API_DIE:-}" = "1" ]; then exit "${STUB_API_RC:-7}"; fi
while :; do sleep 0.2 & wait $!; done
"""

_STUB_PROCRASTINATE = """#!/bin/sh
echo "$*" > "$MARKER_DIR/worker.args"
if [ "${STUB_WORKER_IGNORES_TERM:-}" = "1" ]; then
  trap 'echo ignored >> "$MARKER_DIR/worker.term"' TERM
else
  trap 'echo term > "$MARKER_DIR/worker.term"; exit 0' TERM
fi
if [ "${STUB_WORKER_DIE:-}" = "1" ]; then exit "${STUB_WORKER_RC:-9}"; fi
while :; do sleep 0.2 & wait $!; done
"""

_ONE_SHOT = """#!/bin/sh
echo "$*" > "$MARKER_DIR/%s.args"
exit 0
"""


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A scratch cwd with stub uvicorn/procrastinate/python ahead on PATH."""
    binv = tmp_path / "bin"
    binv.mkdir()
    for name, body in (
        ("python", _STUB_PYTHON),
        ("uvicorn", _STUB_UVICORN),
        ("procrastinate", _STUB_PROCRASTINATE),
    ):
        p = binv / name
        p.write_text(body)
        p.chmod(0o755)
    # The entrypoint runs `python scripts/init_db.py` relative to cwd (WORKDIR
    # /app in the image); the stub never opens it, but keep the shape honest.
    (tmp_path / "scripts").mkdir()
    return tmp_path


def _spawn(workdir: Path, *args: str, **env: str) -> subprocess.Popen:
    environ = dict(os.environ)
    environ["PATH"] = f"{workdir / 'bin'}{os.pathsep}{environ['PATH']}"
    environ["MARKER_DIR"] = str(workdir)
    environ.update(env)
    return subprocess.Popen(
        ["/bin/sh", str(ENTRYPOINT), *args],
        cwd=workdir,
        env=environ,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )


def _await_file(path: Path, timeout: float = 10.0) -> str:
    """Poll for a marker file; the children are real processes, not mocks."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if path.exists():
            return path.read_text().strip()
        time.sleep(0.05)
    raise AssertionError(f"{path.name} never appeared within {timeout}s")


# --------------------------------------------------------------------------- #
# Merged mode (`all`)                                                          #
# --------------------------------------------------------------------------- #


def test_all_mode_bootstraps_once_then_starts_both_children(workdir: Path):
    """Bootstrap exactly once, THEN both children, with the compose argv."""
    proc = _spawn(
        workdir,
        "all",
        FILEARR_WORKER_CONCURRENCY="6",
        FILEARR_WORKER_QUEUES="scan,index",
    )
    try:
        api_args = _await_file(workdir / "uvicorn.args")
        worker_args = _await_file(workdir / "worker.args")
    finally:
        proc.terminate()
        proc.wait(timeout=30)

    # One bootstrap, not two — the whole reason the uvicorn gate exists.
    assert (workdir / "bootstrap.log").read_text().splitlines() == [
        "python scripts/init_db.py"
    ]
    assert api_args == "filearr.main:app --host 0.0.0.0 --port 8000"
    # Byte-identical to docker-compose.yml's worker command mapping.
    assert worker_args == (
        "--app=filearr.worker.proc_app worker --concurrency 6 --queues scan,index"
    )


def test_all_mode_worker_flags_default_to_the_compose_defaults(workdir: Path):
    """Unset knobs -> concurrency 4 and an empty --queues (== all queues)."""
    proc = _spawn(workdir, "all")
    try:
        worker_args = _await_file(workdir / "worker.args")
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    assert worker_args == "--app=filearr.worker.proc_app worker --concurrency 4 --queues"


def test_all_mode_appends_extra_argv_to_uvicorn(workdir: Path):
    """`all --proxy-headers ...` extends the API command, not the worker's."""
    proc = _spawn(workdir, "all", "--proxy-headers")
    try:
        api_args = _await_file(workdir / "uvicorn.args")
        worker_args = _await_file(workdir / "worker.args")
    finally:
        proc.terminate()
        proc.wait(timeout=30)
    assert api_args.endswith("--port 8000 --proxy-headers")
    assert "--proxy-headers" not in worker_args


def test_all_mode_forwards_sigterm_to_both_children_and_exits_zero(workdir: Path):
    """The stop_grace_period lesson: BOTH children get SIGTERM, then we wait."""
    proc = _spawn(workdir, "all")
    _await_file(workdir / "uvicorn.args")
    _await_file(workdir / "worker.args")

    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=30)[0]

    assert (workdir / "uvicorn.term").read_text().strip() == "term"
    assert (workdir / "worker.term").read_text().strip() == "term"
    # A requested stop is not a failure.
    assert proc.returncode == 0, out


def test_all_mode_api_death_tears_down_the_worker_and_propagates_status(
    workdir: Path,
):
    proc = _spawn(workdir, "all", STUB_API_DIE="1", STUB_API_RC="7")
    out = proc.communicate(timeout=60)[0]

    assert proc.returncode == 7, out
    assert (workdir / "worker.term").read_text().strip() == "term"
    assert "api exited (status 7)" in out


def test_all_mode_clean_worker_exit_still_fails_the_container(workdir: Path):
    """A child exiting 0 is still a container-level failure: never exit 0."""
    proc = _spawn(workdir, "all", STUB_WORKER_DIE="1", STUB_WORKER_RC="0")
    out = proc.communicate(timeout=60)[0]

    assert proc.returncode == 1, out
    assert (workdir / "uvicorn.term").read_text().strip() == "term"
    assert "worker exited (status 0)" in out


def test_all_mode_sigkills_after_the_grace_budget(workdir: Path):
    """A child that ignores SIGTERM is killed once the grace budget is spent."""
    proc = _spawn(
        workdir,
        "all",
        STUB_WORKER_IGNORES_TERM="1",
        FILEARR_STOP_GRACE_SECONDS="3",
    )
    _await_file(workdir / "worker.args")

    started = time.monotonic()
    proc.send_signal(signal.SIGTERM)
    out = proc.communicate(timeout=60)[0]
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, out
    assert "SIGKILL" in out
    # Held the door open for the full budget rather than killing immediately —
    # that difference IS the mid-transaction-job regression.
    assert elapsed >= 3, f"gave up after {elapsed:.1f}s"


def test_all_mode_does_not_start_children_until_the_bootstrap_succeeds(
    workdir: Path,
):
    """A failing bootstrap must retry, not start a half-migrated stack."""
    proc = _spawn(workdir, "all", STUB_BOOTSTRAP_RC="1")
    try:
        # The retry loop is 30 attempts x 2s; sample it mid-flight.
        time.sleep(3)
        assert not (workdir / "uvicorn.args").exists()
        assert not (workdir / "worker.args").exists()
        assert (workdir / "bootstrap.log").read_text().count("init_db") >= 1
    finally:
        proc.kill()
        proc.wait(timeout=30)


# --------------------------------------------------------------------------- #
# Mode 1 pass-through — the compose/Proxmox path. Purely a regression guard.   #
# --------------------------------------------------------------------------- #


@pytest.fixture
def oneshot_workdir(workdir: Path) -> Path:
    """Same stubs, but the children exit immediately (we only inspect argv)."""
    for name in ("uvicorn", "procrastinate"):
        p = workdir / "bin" / name
        p.write_text(_ONE_SHOT % ("uvicorn" if name == "uvicorn" else "worker"))
        p.chmod(0o755)
    return workdir


def test_pass_through_uvicorn_still_bootstraps_and_execs(oneshot_workdir: Path):
    proc = _spawn(oneshot_workdir, "uvicorn", "filearr.main:app", "--port", "8000")
    proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert (oneshot_workdir / "bootstrap.log").read_text().count("init_db") == 1
    assert (
        oneshot_workdir / "uvicorn.args"
    ).read_text().strip() == "filearr.main:app --port 8000"


def test_pass_through_worker_command_never_bootstraps(oneshot_workdir: Path):
    """The compose worker service must not race the app's migration."""
    proc = _spawn(
        oneshot_workdir, "procrastinate", "--app=filearr.worker.proc_app", "worker"
    )
    proc.communicate(timeout=30)
    assert proc.returncode == 0
    assert not (oneshot_workdir / "bootstrap.log").exists()
    assert (
        oneshot_workdir / "worker.args"
    ).read_text().strip() == "--app=filearr.worker.proc_app worker"


@pytest.mark.parametrize("mode", ["uvicorn", "all"])
def test_auto_init_db_false_opts_out_of_the_bootstrap_in_both_modes(
    oneshot_workdir: Path, mode: str
):
    proc = _spawn(oneshot_workdir, mode, FILEARR_AUTO_INIT_DB="false")
    try:
        if mode == "all":
            _await_file(oneshot_workdir / "uvicorn.args")
        else:
            proc.communicate(timeout=30)
        assert not (oneshot_workdir / "bootstrap.log").exists()
    finally:
        proc.kill()
        proc.wait(timeout=30)


def test_entrypoint_is_posix_sh_clean():
    """`sh -n` the shipped script — this file's other tests all fork it."""
    r = subprocess.run(
        ["/bin/sh", "-n", str(ENTRYPOINT)], capture_output=True, text=True
    )
    assert r.returncode == 0, r.stderr


def test_entrypoint_never_hardcodes_a_stop_grace_other_than_60(workdir: Path):
    """60s mirrors docker-compose.yml's stop_grace_period; keep them in step."""
    text = ENTRYPOINT.read_text()
    assert 'FILEARR_STOP_GRACE_SECONDS:-60' in text
    compose = ENTRYPOINT.parents[2] / "docker-compose.yml"
    assert "stop_grace_period: 60s" in compose.read_text()
