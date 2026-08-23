"""model3d: 3MF central-directory guard + killable child-process isolation
(live 2026-08-22: multi-part .3mf print bundles pinned every worker slot at the
300 s extract timeout, and the abandoned parser threads kept burning CPU)."""

from __future__ import annotations

import json
import subprocess
import sys
import zipfile

import pytest

from filearr.tasks import extract as ex
from filearr.tasks.model3d import Model3DError, extract_model3d

BIG = 1 << 40


def test_3mf_uncompressed_ceiling_guards_before_trimesh(tmp_path):
    """A 3MF whose DECLARED uncompressed total exceeds max_bytes is refused from
    the zip central directory alone — no member is decompressed, trimesh never
    runs (the on-disk size is small; the inflated XML is what costs minutes)."""
    bundle = tmp_path / "huge.3mf"
    with zipfile.ZipFile(bundle, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("3D/3dmodel.model", b"<model>" + b"0" * (3 << 20) + b"</model>")
    assert bundle.stat().st_size < (1 << 20)  # highly compressible on disk
    with pytest.raises(Model3DError) as ei:
        extract_model3d(str(bundle), max_bytes=1 << 20)
    assert ei.value.kind == "guard"
    assert "uncompressed" in str(ei.value)


def test_isolated_entry_roundtrips_json(stl_cube):
    proc = subprocess.run(
        [sys.executable, "-m", "filearr.tasks.model3d", str(stl_cube), "--max-bytes", str(BIG)],
        capture_output=True, text=True, timeout=120, check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["ok"]["triangles"] == 12
    assert payload["ok"]["file_format"] == "stl"


def test_isolated_entry_reports_guard_as_error_payload(stl_cube):
    proc = subprocess.run(
        [sys.executable, "-m", "filearr.tasks.model3d", str(stl_cube), "--max-bytes", "10"],
        capture_output=True, text=True, timeout=120, check=True,
    )
    payload = json.loads(proc.stdout)
    assert payload["error"]["kind"] == "guard"
    assert "too large" in payload["error"]["message"]


def test_wrapper_runs_child_and_returns_meta(stl_cube):
    meta = ex.extract_model3d(str(stl_cube))
    assert meta["triangles"] == 12 and "_extract_error" not in meta


def test_wrapper_timeout_kills_child_and_records_guard(monkeypatch, stl_cube):
    calls: list[dict] = []

    def _boom(cmd, **kw):
        calls.append(kw)
        raise subprocess.TimeoutExpired(cmd, kw.get("timeout"))

    monkeypatch.setattr(subprocess, "run", _boom)
    meta = ex.extract_model3d(str(stl_cube))
    assert meta["_extract_error_kind"] == "guard"
    assert "terminated" in meta["_extract_error"]
    # The child's budget sits BELOW the extract timeout so it always fires first
    # (the outer wait_for must never be the one to give up and abandon the thread).
    assert calls[0]["timeout"] < ex.effective_settings().extract_timeout_seconds


def test_wrapper_garbage_child_output_is_error_not_crash(monkeypatch, stl_cube):
    class _P:
        returncode = 1
        stdout = ""
        stderr = "Traceback...\nMemoryError: boom"

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: _P())
    meta = ex.extract_model3d(str(stl_cube))
    assert meta["_extract_error_kind"] == "error"
    assert "MemoryError" in meta["_extract_error"]
