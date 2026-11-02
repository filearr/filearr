"""/version identifies the running build (package version + never-null build identity)."""

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


async def test_version_returns_app_version_and_build(client):
    r = await client.get("/api/v1/version")
    assert r.status_code == 200
    body = r.json()
    assert body["app_version"]
    assert "build_stamp" not in body
    build = body["build"]
    assert build["id"]
    assert build["source"] in {"deploy", "image", "git", "runtime"}


@pytest.fixture
def fresh_build_info(monkeypatch):
    from filearr import buildinfo

    buildinfo.build_info.cache_clear()
    for k in ("FILEARR_BUILD_COMMIT", "FILEARR_BUILD_REF", "FILEARR_BUILD_DATE"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setattr(buildinfo, "read_stamp", lambda: None)
    yield buildinfo
    buildinfo.build_info.cache_clear()


def test_build_info_prefers_deploy_stamp(fresh_build_info, monkeypatch):
    monkeypatch.setattr(
        fresh_build_info, "read_stamp", lambda: "abc123def456-20260710T000000Z"
    )
    b = fresh_build_info.build_info()
    assert b["source"] == "deploy"
    assert b["id"] == "abc123def456-20260710T000000Z"
    assert b["built_at"] == "2026-07-10T00:00:00Z"


def test_build_info_from_image_build_args(fresh_build_info, monkeypatch):
    sha = "940000d9e573194c3df1fa19480429bca8604b70"
    monkeypatch.setenv("FILEARR_BUILD_COMMIT", sha)
    monkeypatch.setenv("FILEARR_BUILD_REF", "main")
    monkeypatch.setenv("FILEARR_BUILD_DATE", "2026-11-01T19:58:14Z")
    b = fresh_build_info.build_info()
    assert b == {
        "id": "main-940000d",
        "commit": sha,
        "ref": "main",
        "built_at": "2026-11-01T19:58:14Z",
        "source": "image",
    }


def test_build_info_ignores_garbage_commit_env(fresh_build_info, monkeypatch):
    monkeypatch.setenv("FILEARR_BUILD_COMMIT", "not-a-sha")
    monkeypatch.setattr(fresh_build_info, "_from_git", lambda: None)
    b = fresh_build_info.build_info()
    assert b["source"] == "runtime"
    assert b["id"].endswith("+unidentified")


def test_build_info_never_null_without_any_source(fresh_build_info, monkeypatch):
    monkeypatch.setattr(fresh_build_info, "_from_git", lambda: None)
    b = fresh_build_info.build_info()
    assert b["id"] and b["source"] == "runtime"


async def test_version_exposes_source_url(client):
    """FIX-8 (AGPL §13): /version carries source_url so the footer Source link can
    point at a fork's modified source at runtime (FILEARR_SOURCE_URL)."""
    r = await client.get("/api/v1/version")
    body = r.json()
    # The shipped default must point at the REAL public repo — the footer went
    # live to github.com/filearr/filearr (nonexistent) before this pin.
    assert body["source_url"] == "https://github.com/filearr/filearr"


async def test_version_source_url_honours_setting(client, monkeypatch):
    from filearr.config import get_settings

    monkeypatch.setattr(get_settings(), "source_url", "https://example.test/mysrc")
    r = await client.get("/api/v1/version")
    assert r.json()["source_url"] == "https://example.test/mysrc"
