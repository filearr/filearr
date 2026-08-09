"""Docs surfaces: custom /api/docs (self-hosted Swagger) + bundled /docs site.

No DB involved — create_app() route wiring only. The swagger asset check and
the docs-site mount both resolve RELATIVE paths against the cwd (WORKDIR /app
in the image), so each test chdirs into tmp_path BEFORE create_app().
"""

from __future__ import annotations

import httpx
import pytest

from filearr.config import get_settings
from filearr.main import create_app


def _make_client(app):
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://t"
    )


@pytest.fixture
def app_factory(tmp_path, monkeypatch):
    """chdir into a scratch dir, disable auth, and hand back (tmp_path, build)."""
    monkeypatch.chdir(tmp_path)
    get_settings.cache_clear()
    settings = get_settings()
    monkeypatch.setattr(settings, "auth_enabled", False)
    return tmp_path


async def test_api_docs_serves_local_swagger_assets(app_factory):
    tmp_path = app_factory
    static = tmp_path / "static" / "swagger-ui"
    static.mkdir(parents=True)
    (static / "swagger-ui-bundle.js").write_text("// bundle", encoding="utf-8")
    (static / "swagger-ui.css").write_text("/* css */", encoding="utf-8")
    (tmp_path / "static" / "index.html").write_text("<html></html>", encoding="utf-8")

    async with _make_client(create_app()) as c:
        r = await c.get("/api/docs")
        assert r.status_code == 200, r.text
        assert "/swagger-ui/swagger-ui-bundle.js" in r.text
        assert "/swagger-ui/swagger-ui.css" in r.text
        assert "cdn.jsdelivr.net" not in r.text
        # the assets themselves come off the SPA static mount
        r = await c.get("/swagger-ui/swagger-ui-bundle.js")
        assert r.status_code == 200
        assert r.text == "// bundle"


async def test_api_docs_cdn_fallback_without_static(app_factory):
    # bare cwd: no static/ dir at all — dev checkout behavior
    async with _make_client(create_app()) as c:
        r = await c.get("/api/docs")
        assert r.status_code == 200, r.text
        assert "cdn.jsdelivr.net" in r.text


async def test_bundled_docs_site_mount(app_factory):
    tmp_path = app_factory
    site = tmp_path / "docs-site-html"
    site.mkdir()
    (site / "index.html").write_text("<h1>Filearr manual</h1>", encoding="utf-8")

    async with _make_client(create_app()) as c:
        r = await c.get("/docs/")
        assert r.status_code == 200
        assert "Filearr manual" in r.text
        # bare /docs redirects to /docs/ (Starlette redirect_slashes)
        r = await c.get("/docs", follow_redirects=False)
        assert r.status_code in (301, 307)
        assert r.headers["location"].rstrip("/").endswith("/docs") or r.headers[
            "location"
        ].endswith("/docs/")


async def test_docs_mount_absent_in_dev_is_clean(app_factory):
    # no docs-site-html dir: create_app must not raise and /docs must not 500
    async with _make_client(create_app()) as c:
        r = await c.get("/docs/")
        assert r.status_code == 404


async def test_openapi_spec_still_published(app_factory):
    async with _make_client(create_app()) as c:
        r = await c.get("/api/openapi.json")
        assert r.status_code == 200
        assert r.json()["info"]["title"] == "Filearr"
