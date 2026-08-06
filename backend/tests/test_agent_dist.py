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
