"""Roadmap §20 — STL/3MF preview thumbnails via the in-process software
rasterizer (thumbs.generate_model3d_thumb): headless (no GL context), fully
vectorized point-splat + Lambert shading, fed through the shared WebP ladder.
"""

from __future__ import annotations

import os

import pytest

from filearr.config import get_settings
from filearr.thumbs import (
    TIER_GRID,
    TIER_PREVIEW,
    generate_model3d_thumb,
)

trimesh = pytest.importorskip("trimesh")


@pytest.fixture(scope="module")
def stl_path(tmp_path_factory):
    d = tmp_path_factory.mktemp("models")
    sphere = trimesh.creation.icosphere(subdivisions=3)
    box = trimesh.creation.box(extents=[1.2, 1.2, 1.2])
    box.apply_translation([0.8, 0, 0])
    mesh = trimesh.util.concatenate([sphere, box])
    p = str(d / "part.stl")
    mesh.export(p)
    return p


def test_grid_and_preview_render_within_caps(stl_path):
    s = get_settings()
    for tier in (TIER_GRID, TIER_PREVIEW):
        tb = generate_model3d_thumb(stl_path, tier, s)
        assert tb is not None, f"tier {tier} produced no thumbnail"
        edge = s.thumbnail_grid_px if tier == TIER_GRID else s.thumbnail_preview_px
        cap = (
            s.thumbnail_grid_max_bytes
            if tier == TIER_GRID
            else s.thumbnail_preview_max_bytes
        )
        assert max(tb.width, tb.height) == edge
        assert 0 < len(tb.data) <= cap
        # Not a blank background: a flat fill encodes to a few hundred bytes.
        assert len(tb.data) > 1000


def test_3mf_container_renders(stl_path, tmp_path):
    mesh = trimesh.load(stl_path)
    p = str(tmp_path / "part.3mf")
    mesh.export(p)
    tb = generate_model3d_thumb(p, TIER_GRID, get_settings())
    assert tb is not None


def test_hostile_inputs_yield_none(tmp_path):
    s = get_settings()
    junk = tmp_path / "junk.stl"
    junk.write_bytes(b"not a mesh at all")
    assert generate_model3d_thumb(str(junk), TIER_GRID, s) is None

    empty = tmp_path / "empty.stl"
    empty.write_bytes(b"")
    assert generate_model3d_thumb(str(empty), TIER_GRID, s) is None

    assert generate_model3d_thumb(str(tmp_path / "missing.stl"), TIER_GRID, s) is None


def test_non_geometry_extensions_skipped(tmp_path):
    """fbx/blend/step have no safe pure loader — skip, don't attempt."""
    s = get_settings()
    for name in ("scene.fbx", "scene.blend", "part.step"):
        p = tmp_path / name
        p.write_bytes(b"whatever")
        assert generate_model3d_thumb(str(p), TIER_GRID, s) is None


def test_size_ceiling_enforced(stl_path, monkeypatch):
    """model3d_max_bytes is the load guard — trimesh reads the whole mesh into
    RAM, so an oversized source is rejected before the parser opens it."""
    s = get_settings()
    monkeypatch.setattr(s, "model3d_max_bytes", os.path.getsize(stl_path) - 1)
    assert generate_model3d_thumb(stl_path, TIER_GRID, s) is None
