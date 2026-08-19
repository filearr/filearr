# ruff: noqa: E501
"""P2-T7 (2026-08-19): exclusion preset bundles as data -- custom bundles
survive restart (they live in preset_bundles), builtins are fork-not-mutate,
a code builtin never overwrites a same-named custom bundle, and a bundle
enabled on a library cannot be deleted."""

from __future__ import annotations

import pytest
from sqlalchemy import select

from filearr import preset_registry
from filearr.models import Library, PresetBundleRow
from filearr.presets import BUILTIN_PRESET_BUNDLES, PRESET_BUNDLES

from .test_roles_account_settings import _bootstrap_admin, client, db_maker  # noqa: F401, F811

pytestmark = pytest.mark.asyncio


@pytest.fixture(autouse=True)
async def _clean(db_maker):  # noqa: F811
    from sqlalchemy import text

    async with db_maker() as s:
        await s.execute(text("DELETE FROM preset_bundles"))
        await s.execute(text("DELETE FROM libraries"))
        await s.commit()
    preset_registry.reset_for_tests()
    yield
    preset_registry.reset_for_tests()


async def test_seed_and_custom_lifecycle(client):  # noqa: F811
    c, maker, _ = client
    await _bootstrap_admin(c)
    async with maker() as s:
        assert await preset_registry.seed_builtins(s) == len(BUILTIN_PRESET_BUNDLES)
        assert await preset_registry.seed_builtins(s) == 0  # idempotent

    r = await c.get("/api/v1/presets")
    names = {p["name"]: p for p in r.json()["presets"]}
    assert set(names) == set(BUILTIN_PRESET_BUNDLES) and all(p["builtin"] for p in names.values())

    # create: bad name / bad pattern / collision with builtin
    assert (await c.post("/api/v1/presets", json={"name": "Bad Name", "label": "x", "patterns": ["a"]})).status_code == 422
    assert (await c.post("/api/v1/presets", json={"name": "empty", "label": "x", "patterns": ["  "]})).status_code == 422
    assert (await c.post("/api/v1/presets", json={"name": "system_files", "label": "x", "patterns": ["a"]})).status_code == 409
    r = await c.post(
        "/api/v1/presets",
        json={"name": "no-raw", "label": "No RAW photos", "patterns": ["*.cr2", "*.nef", "# comment", "RAW/"]},
    )
    assert r.status_code == 201, r.text
    assert r.json()["patterns"] == ["*.cr2", "*.nef", "RAW/"] and r.json()["builtin"] is False
    # visible in the live mapping the walk reads, after builtins
    assert list(PRESET_BUNDLES)[-1] == "no-raw" and PRESET_BUNDLES["no-raw"].exclude == ("*.cr2", "*.nef", "RAW/")

    # builtin is read-only; fork works
    assert (await c.patch("/api/v1/presets/system_files", json={"label": "x"})).status_code == 409
    assert (await c.delete("/api/v1/presets/system_files")).status_code == 409
    r = await c.post("/api/v1/presets/system_files/fork", json={"name": "system_files_plus"})
    assert r.status_code == 201 and r.json()["patterns"] == list(BUILTIN_PRESET_BUNDLES["system_files"].exclude)
    r = await c.patch("/api/v1/presets/system_files_plus", json={"patterns": ["*.tmp"], "caveat": "mine"})
    assert r.status_code == 200 and r.json()["patterns"] == ["*.tmp"] and r.json()["caveat"] == "mine"

    # a library enabling the custom bundle blocks its deletion
    r = await c.post("/api/v1/libraries", json={"name": "L", "root_path": "/tmp/x", "enabled_presets": ["no-raw"]})
    assert r.status_code in (200, 201), r.text
    assert (await c.delete("/api/v1/presets/no-raw")).status_code == 409
    async with maker() as s:
        lib = (await s.execute(select(Library))).scalar_one()
        lib.enabled_presets = []
        await s.commit()
    assert (await c.delete("/api/v1/presets/no-raw")).status_code == 204
    assert "no-raw" not in PRESET_BUNDLES

    # 'restart': a fresh registry reload sees the surviving custom bundle
    preset_registry.reset_for_tests()
    async with maker() as s:
        await preset_registry.ensure_loaded(s, force=True)
    assert "system_files_plus" in PRESET_BUNDLES and "no-raw" not in PRESET_BUNDLES


async def test_builtin_seed_never_overwrites_same_named_custom(client):  # noqa: F811
    c, maker, _ = client
    async with maker() as s:
        # An operator bundle that PREDATES a builtin of the same name (simulate
        # by inserting a custom row using a builtin's name before seeding).
        s.add(PresetBundleRow(name="caches_temp", label="MINE", exclude=["mine/"], is_builtin=False))
        await s.commit()
        await preset_registry.seed_builtins(s)
        row = await s.get(PresetBundleRow, "caches_temp")
        assert row.label == "MINE" and row.is_builtin is False and row.exclude == ["mine/"]
        await preset_registry.ensure_loaded(s, force=True)
    # the live mapping still serves the CODE builtin for that name (code is
    # authoritative for builtins) -- the custom row is preserved, not lost
    assert PRESET_BUNDLES["caches_temp"] is BUILTIN_PRESET_BUNDLES["caches_temp"]
