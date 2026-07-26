"""/version identifies the running build (package version + deploy stamp)."""

import httpx
import pytest

from filearr.main import create_app


@pytest.fixture
async def client(monkeypatch):
    from filearr.config import get_settings

    get_settings.cache_clear()
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        yield c


async def test_version_returns_app_version_and_stamp_field(client):
    r = await client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["app_version"]
    assert "build_stamp" in body  # null in dev checkouts, string in deployed images


async def test_version_reads_stamp_file(client, monkeypatch):
    # simulate a deployed image by stubbing the stamp reader
    import filearr.api.system as system_mod

    monkeypatch.setattr(
        system_mod, "_read_stamp", lambda: "abc123def456-20260710T000000Z"
    )
    r = await client.get("/api/v1/version")
    assert r.json()["build_stamp"] == "abc123def456-20260710T000000Z"


async def test_version_exposes_source_url(client):
    """FIX-8 (AGPL §13): /version carries source_url so the footer Source link can
    point at a fork's modified source at runtime (FILEARR_SOURCE_URL)."""
    r = await client.get("/api/v1/version")
    body = r.json()
    # The shipped default must point at the REAL public repo — the footer went
    # live to github.com/filearr/filearr (nonexistent) before this pin.
    assert body["source_url"] == "https://github.com/pwsh/filearr"


async def test_version_source_url_honours_setting(client, monkeypatch):
    from filearr.config import get_settings

    monkeypatch.setattr(get_settings(), "source_url", "https://example.test/mysrc")
    r = await client.get("/api/v1/version")
    assert r.json()["source_url"] == "https://example.test/mysrc"
