"""First-install agent distribution surface (/api/v1/agent-dist).

No DB involved — the router serves baked binaries from a directory. Covers the
feature gate, manifest shape + sha256, download + .sha256 digest, traversal
refusal, and the templated install scripts.
"""

from __future__ import annotations

import hashlib

import httpx
import pytest

from filearr.config import get_settings
from filearr.main import create_app


@pytest.fixture
async def client(tmp_path, monkeypatch):
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    monkeypatch.setattr(settings, "agents_enabled", True)
    monkeypatch.setattr(settings, "agent_dist_dir", str(tmp_path))

    (tmp_path / "filearr-agent-linux-amd64").write_bytes(b"ELF-fake-linux")
    (tmp_path / "filearr-agent-windows-amd64.exe").write_bytes(b"MZ-fake-windows")
    (tmp_path / "VERSION").write_text("v9.9.9 (abc1234)\n", encoding="utf-8")
    # a non-matching file must never be listed or downloadable
    (tmp_path / "notes.txt").write_text("secret", encoding="utf-8")

    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c, settings, tmp_path


async def test_gate_404_when_agents_disabled(client, monkeypatch):
    c, settings, _ = client
    monkeypatch.setattr(settings, "agents_enabled", False)
    for path in (
        "/api/v1/agent-dist",
        "/api/v1/agent-dist/install.sh",
        "/api/v1/agent-dist/filearr-agent-linux-amd64",
    ):
        r = await c.get(path)
        assert r.status_code == 404, path


async def test_manifest_lists_artifacts_with_sha256(client):
    c, _, tmp_path = client
    r = await c.get("/api/v1/agent-dist")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["version"] == "v9.9.9 (abc1234)"
    by_name = {a["filename"]: a for a in body["artifacts"]}
    assert set(by_name) == {
        "filearr-agent-linux-amd64",
        "filearr-agent-windows-amd64.exe",
    }
    lin = by_name["filearr-agent-linux-amd64"]
    assert (lin["os"], lin["arch"]) == ("linux", "amd64")
    assert lin["sha256"] == hashlib.sha256(b"ELF-fake-linux").hexdigest()
    assert lin["size"] == len(b"ELF-fake-linux")
    assert lin["url"].endswith("/api/v1/agent-dist/filearr-agent-linux-amd64")
    win = by_name["filearr-agent-windows-amd64.exe"]
    assert (win["os"], win["arch"]) == ("windows", "amd64")


async def test_download_and_digest(client):
    c, _, _ = client
    r = await c.get("/api/v1/agent-dist/filearr-agent-linux-amd64")
    assert r.status_code == 200
    assert r.content == b"ELF-fake-linux"
    assert r.headers["content-type"] == "application/octet-stream"

    r = await c.get("/api/v1/agent-dist/filearr-agent-linux-amd64.sha256")
    assert r.status_code == 200
    assert r.text.strip() == hashlib.sha256(b"ELF-fake-linux").hexdigest()


async def test_unknown_and_traversal_refused(client):
    c, _, _ = client
    for path in (
        "/api/v1/agent-dist/filearr-agent-plan9-mips",   # regex ok, absent -> 404
        "/api/v1/agent-dist/notes.txt",                   # present, wrong shape
        "/api/v1/agent-dist/VERSION",
        "/api/v1/agent-dist/..%2fVERSION",
        "/api/v1/agent-dist/%2e%2e%2fVERSION",
    ):
        r = await c.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"


async def test_manifest_404_when_dir_empty(client, tmp_path_factory, monkeypatch):
    c, settings, _ = client
    empty = tmp_path_factory.mktemp("empty-dist")
    monkeypatch.setattr(settings, "agent_dist_dir", str(empty))
    r = await c.get("/api/v1/agent-dist")
    assert r.status_code == 404


async def test_install_scripts_templated_with_central_url(client):
    c, _, _ = client
    r = await c.get("/api/v1/agent-dist/install.sh")
    assert r.status_code == 200
    assert 'BASE="http://t"' in r.text
    assert "__CENTRAL_URL__" not in r.text
    assert "install --config filearr-agent.json" in r.text
    assert "sha256" in r.text  # the script verifies the digest

    r = await c.get("/api/v1/agent-dist/install.ps1")
    assert r.status_code == 200
    assert '$base = "http://t"' in r.text
    assert "__CENTRAL_URL__" not in r.text
    assert "Get-FileHash" in r.text


async def test_install_ps1_installs_tools_machine_scope(client):
    """The Windows agent runs as a LocalSystem service, so host tools MUST be
    installed machine-wide: winget defaults to user scope, whose payloads live in
    the operator's %LOCALAPPDATA% and are unreachable for LocalSystem (and are
    ignored on purpose — executing from a user-writable directory as SYSTEM is a
    privilege-escalation path). Regression guard for a live 2026-08-11 report of
    exiftool/poppler reading as absent right after the installer ran."""
    c, _, _ = client
    r = await c.get("/api/v1/agent-dist/install.ps1")
    ps = r.text

    assert "--scope machine" in ps
    # Presence is asked as the SERVICE sees it: the MACHINE PATH, never the
    # operator's merged $env:PATH and never Get-Command on the tool.
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in ps
    for cmd in ("ffprobe", "pdftotext", "exiftool", "tesseract"):
        assert f"Get-Command {cmd}" not in ps
    # No user-profile location is probed for a tool (the words may appear in the
    # comments explaining WHY; what must not appear is an expansion of them).
    assert "$env:LOCALAPPDATA" not in ps
    assert "$env:USERPROFILE" not in ps
    # Machine winget locations ARE probed, at both portable-payload depths.
    assert r"$env:ProgramFiles\WinGet\Links" in ps
    assert r"$env:ProgramFiles\WinGet\Packages\*\*\Library\bin" in ps
    assert r"$env:ProgramFiles\WinGet\Packages\*\*\bin" in ps
    # ...and nothing outside Program Files (tightened 2026-08-11, mirroring
    # agent/internal/inventory/wellknown_windows.go). %ProgramData% and C:\ let
    # Authenticated Users create subdirectories, so a well-known path under
    # them that does not exist yet is attacker-creatable and then run by
    # SYSTEM. Chocolatey/Scoop installs are still found via the MACHINE PATH
    # read asserted above, which is why dropping these costs nothing.
    for probe in (
        r"$env:ProgramData\chocolatey\bin",
        r"$env:ProgramData\scoop\shims",
        r"C:\ffmpeg\bin",
        r"C:\exiftool",
        r"C:\poppler*\Library\bin",
    ):
        assert probe not in ps, f"{probe} is not admin-only; SYSTEM must not probe it"
    # Machine scope needs elevation, and a package that refuses it must warn
    # rather than quietly land in a profile.
    assert "Test-Elevated" in ps
    assert "refused machine scope" in ps


async def test_manage_windows_agent_script_served_templated(client):
    """The unified Windows lifecycle script is served with THIS central's URL
    baked into the -CentralUrl default; the split-placeholder runtime guard
    survives templating untouched (so a repo copy still demands -CentralUrl).
    Registered before the {filename} catch-all — a 404 here would mean route
    ordering regressed."""
    c, _, _ = client
    r = await c.get("/api/v1/agent-dist/manage-windows-agent.ps1")
    assert r.status_code == 200, r.text
    assert '[string]$CentralUrl = "http://t"' in r.text
    assert "__CENTRAL_URL__" not in r.text
    # The guard's split halves are untouched by templating.
    assert '"__CENTRAL" + "_URL__"' in r.text
    # Both lifecycle halves present: provisioning and update markers.
    assert "enrollment-tokens" in r.text
    assert "Stop-Service filearr-agent" in r.text
    assert "Get-FileHash" in r.text
    # Same machine-scope contract as install.ps1 — the two surfaces must not
    # drift, because this one is what repairs an already-provisioned box.
    assert "--scope machine" in r.text
    assert '[Environment]::GetEnvironmentVariable("Path", "Machine")' in r.text
    assert r"$env:ProgramFiles\WinGet\Packages\*\*\Library\bin" in r.text
    # Same Program-Files-only probe list, for the same reason (see the
    # install.ps1 test above). The two scripts and the agent must agree about
    # what "visible to the service" means.
    for probe in (
        r"$env:ProgramData\chocolatey\bin",
        r"$env:ProgramData\scoop\shims",
        r"C:\ffmpeg\bin",
        r"C:\exiftool",
        r"C:\poppler*\Library\bin",
    ):
        assert probe not in r.text, f"{probe} is not admin-only; SYSTEM must not probe it"
