"""Console update check (filearr.updatecheck): comparison logic against the
GitHub repository head, changelog shaping, caching, offline degradation, and
the admin endpoints. No test ever touches the network — the GitHub fetch and
the local-identity probe are seams."""

from __future__ import annotations

from datetime import UTC, datetime

import httpx
import pytest

from filearr import updatecheck
from filearr.config import get_settings
from filearr.main import create_app

HEAD_SHA = "cb6cf05aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"
HEAD_DATE = datetime(2026, 8, 8, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _fresh_cache(monkeypatch):
    monkeypatch.setattr(updatecheck, "_cached", None)


async def test_release_part_and_stamp_time():
    assert updatecheck._release_part("1.5.0-3303638") == (1, 5, 0)
    assert updatecheck._release_part("v1.5.0") == (1, 5, 0)
    assert updatecheck._release_part("1.5.0@abcdef0") == (1, 5, 0)
    assert updatecheck._release_part("0.0.0-dev") == (0, 0, 0)
    assert updatecheck._release_part("main-1a2b3c4") is None
    ts = updatecheck._stamp_time("3303638699ed-20260807T203554Z")
    assert ts == datetime(2026, 8, 7, 20, 35, 54, tzinfo=UTC)
    assert updatecheck._stamp_time("1.5.0-1a2b3c4") is None


async def test_central_row_stamp_timestamp_heuristic():
    older = updatecheck._central_row(
        "3303638699ed-20260807T203554Z", None, HEAD_SHA, HEAD_DATE
    )
    assert older["update_available"] is True  # head commit postdates the build
    newer = updatecheck._central_row(
        "3303638699ed-20260809T000000Z", None, HEAD_SHA, HEAD_DATE
    )
    assert newer["update_available"] is False


async def test_central_row_dist_sha_identity():
    same = updatecheck._central_row(None, "1.5.0-cb6cf05", HEAD_SHA, HEAD_DATE)
    assert same["update_available"] is False
    diff = updatecheck._central_row(None, "1.5.0-0000000", HEAD_SHA, HEAD_DATE)
    assert diff["update_available"] is True
    unknown = updatecheck._central_row(None, None, HEAD_SHA, HEAD_DATE)
    assert unknown["update_available"] is None


async def test_agent_row_semver():
    assert updatecheck._agent_row("1.5.0-3303638", "1.5.0")["update_available"] is False
    assert updatecheck._agent_row("1.5.0-3303638", "1.6.0")["update_available"] is True
    assert updatecheck._agent_row("0.0.0-dev", "1.5.0")["update_available"] is True
    assert updatecheck._agent_row("main-1a2b3c4", "1.5.0")["update_available"] is None
    assert updatecheck._agent_row("1.5.0-x", None)["update_available"] is None


def _commits():
    return [
        {
            "sha": HEAD_SHA,
            "date": "2026-08-08T12:00:00Z",
            "subject": "newest change",
            "body": "details of the newest change",
        },
        {
            "sha": "1234567" + "b" * 33,
            "date": "2026-08-01T00:00:00Z",
            "subject": "older change",
            "body": "",
        },
    ]


async def test_check_shapes_result_and_caches(monkeypatch):
    async def fake_fetch(settings):
        return _commits(), "1.6.0"

    monkeypatch.setattr(updatecheck, "_fetch_github", fake_fetch)
    monkeypatch.setattr(
        updatecheck,
        "_local_identity",
        lambda: ("3303638699ed-20260807T203554Z", "1.5.0-3303638"),
    )
    result = await updatecheck.check(force=True)
    central, agent = result["components"]
    assert central["component"] == "central" and central["update_available"] is True
    assert agent["component"] == "agent binaries" and agent["update_available"] is True
    # changelog: shas truncated, is_new computed against the build timestamp
    assert [c["sha"] for c in result["changelog"]] == [HEAD_SHA[:7], "1234567"]
    assert [c["is_new"] for c in result["changelog"]] == [True, False]

    # cached: a second non-forced call returns the same object, no refetch
    async def boom(settings):
        raise AssertionError("must not refetch while cache is fresh")

    monkeypatch.setattr(updatecheck, "_fetch_github", boom)
    assert (await updatecheck.check()) is result
    assert updatecheck.cached() is result


async def test_check_degrades_offline(monkeypatch):
    async def fake_fetch(settings):
        raise httpx.ConnectError("no route to host")

    monkeypatch.setattr(updatecheck, "_fetch_github", fake_fetch)
    result = await updatecheck.check(force=True)
    assert "could not reach GitHub" in result["error"]
    assert result["components"] == [] and result["changelog"] == []
    assert updatecheck.cached() is None  # an error is never cached


async def test_endpoints(monkeypatch):
    monkeypatch.setattr(get_settings(), "auth_enabled", False)
    monkeypatch.setattr(get_settings(), "update_check_auto", False)
    app = create_app()
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c:
        # GET before any check: empty state, no network
        r = await c.get("/api/v1/system/update-check")
        assert r.status_code == 200
        assert r.json()["checked_at"] is None

        async def fake_fetch(settings):
            return _commits(), "1.5.0"

        monkeypatch.setattr(updatecheck, "_fetch_github", fake_fetch)
        monkeypatch.setattr(
            updatecheck, "_local_identity", lambda: (None, "1.5.0-cb6cf05")
        )
        r = await c.post("/api/v1/system/update-check")
        assert r.status_code == 200
        body = r.json()
        assert body["components"][0]["update_available"] is False  # sha == head
        assert body["changelog"][0]["subject"] == "newest change"

        # GET now serves the cache
        r = await c.get("/api/v1/system/update-check")
        assert r.json()["checked_at"] == body["checked_at"]
