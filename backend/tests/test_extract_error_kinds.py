"""Fix batch 2026-07-24: SVG/vector extraction routing + error-kind
classification.

The live incident: every SVG in a 77k-file documents library produced
"cannot identify image file" (Pillow has no vector decoder), and missing
trimesh lazy-deps (networkx / charset-normalizer) rendered as generic model
errors — indistinguishable from corrupt files in the errors UI. These tests
pin the SVG XML parse, the vector `unsupported` marker, and the
dependency/guard/corrupt/error taxonomy end to end.
"""

from __future__ import annotations

import gzip

import pytest

from filearr.tasks.archives import ArchiveError
from filearr.tasks.documents import DocumentError, guard_decompression
from filearr.tasks.extract import (
    _error_kind,
    extract_document,
    extract_image,
    extract_model3d,
)
from filearr.tasks.model3d import Model3DError

SVG = (
    '<?xml version="1.0" encoding="UTF-8"?>\n'
    '<svg xmlns="http://www.w3.org/2000/svg" width="640" height="480px" '
    'viewBox="0 0 640 480"><rect width="640" height="480"/></svg>'
)
SVG_VIEWBOX_ONLY = (
    '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024.5 768"/>'
)
SVG_RELATIVE = '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%"/>'


# --------------------------------------------------------------------------- #
# SVG / vector routing in extract_image                                        #
# --------------------------------------------------------------------------- #
def test_svg_dimensions_from_attributes(tmp_path):
    p = tmp_path / "logo.svg"
    p.write_text(SVG, encoding="utf-8")
    meta = extract_image(str(p))
    assert meta["format"] == "SVG"
    assert meta["width"] == 640
    assert meta["height"] == 480
    assert "_extract_error" not in meta


def test_svg_dimensions_fall_back_to_viewbox(tmp_path):
    p = tmp_path / "icon.svg"
    p.write_text(SVG_VIEWBOX_ONLY, encoding="utf-8")
    meta = extract_image(str(p))
    assert meta["width"] == 1024  # 1024.5 rounded (banker's) — stable int
    assert meta["height"] == 768


def test_svg_relative_units_yield_no_dimensions(tmp_path):
    """Percent/contextual units would store misleading numbers — omit instead."""
    p = tmp_path / "fluid.svg"
    p.write_text(SVG_RELATIVE, encoding="utf-8")
    meta = extract_image(str(p))
    assert meta == {"format": "SVG"}


def test_svgz_gzip_wrapped(tmp_path):
    p = tmp_path / "logo.svgz"
    p.write_bytes(gzip.compress(SVG.encode()))
    meta = extract_image(str(p))
    assert meta["width"] == 640 and meta["height"] == 480


def test_malformed_svg_classified_corrupt(tmp_path):
    """Broken XML raises ParseError (a SyntaxError) -> kind `corrupt` via the
    blanket classifier, never a Pillow 'cannot identify image file'."""
    p = tmp_path / "broken.svg"
    p.write_text("<svg width='1'", encoding="utf-8")
    with pytest.raises(Exception) as ei:
        extract_image(str(p))
    assert _error_kind(ei.value) == "corrupt"


def test_other_vector_formats_are_unsupported_not_errors(tmp_path):
    """eps/wmf/ai/... have no Pillow decoder: `unsupported` marker (a fact),
    NOT a guaranteed UnidentifiedImageError per file (the live incident)."""
    for name in ("draw.eps", "clip.wmf", "art.ai", "diagram.drawio"):
        p = tmp_path / name
        p.write_bytes(b"binary vector bytes")
        assert extract_image(str(p)) == {"unsupported": True}


def test_raster_path_unchanged(tmp_path):
    """A real raster image still goes through Pillow."""
    from PIL import Image

    p = tmp_path / "dot.png"
    Image.new("RGB", (3, 2)).save(p)
    meta = extract_image(str(p))
    assert (meta["width"], meta["height"], meta["format"]) == (3, 2, "PNG")


# --------------------------------------------------------------------------- #
# Error-kind taxonomy                                                          #
# --------------------------------------------------------------------------- #
def test_error_kind_classifier():
    assert _error_kind(ModuleNotFoundError("No module named 'networkx'")) == "dependency"
    assert _error_kind(ImportError("cannot import name x")) == "dependency"
    assert _error_kind(SyntaxError("bad xml")) == "corrupt"
    import zipfile

    assert _error_kind(zipfile.BadZipFile("File is not a zip file")) == "corrupt"
    assert _error_kind(OSError("io broke")) == "error"
    assert _error_kind(RuntimeError("anything else")) == "error"


def test_document_guard_kinds(tmp_path):
    # Not-a-zip -> corrupt (default kind).
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"this is not a zip")
    with pytest.raises(DocumentError) as ei:
        guard_decompression(str(fake))
    assert ei.value.kind == "corrupt"

    # Declared-size ceiling -> guard.
    import zipfile

    bomb = tmp_path / "bomb.docx"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("word/document.xml", b"\0" * 4096)
    with pytest.raises(DocumentError) as ei:
        guard_decompression(str(bomb), decompressed_max=1024)
    assert ei.value.kind == "guard"


def test_model3d_kinds(tmp_path, monkeypatch):
    # Size ceiling -> guard.
    big = tmp_path / "big.stl"
    big.write_bytes(b"\0" * 2048)
    from filearr.tasks import model3d as m3d

    with pytest.raises(Model3DError) as ei:
        m3d.extract_model3d(str(big), max_bytes=1024)
    assert ei.value.kind == "guard"

    # A missing trimesh lazy-import -> dependency (the live deployment bug).
    def _no_module(*a, **k):
        raise ModuleNotFoundError("No module named 'networkx'")

    import trimesh

    monkeypatch.setattr(trimesh, "load", _no_module)
    small = tmp_path / "small.glb"
    small.write_bytes(b"glTF fake")
    with pytest.raises(Model3DError) as ei:
        m3d.extract_model3d(str(small), max_bytes=10_000)
    assert ei.value.kind == "dependency"


def test_extract_wrappers_record_kind(tmp_path, monkeypatch):
    """The task-level wrappers persist `_extract_error_kind` beside the error."""
    fake = tmp_path / "fake.docx"
    fake.write_bytes(b"not a zip")
    meta = extract_document(str(fake))
    assert meta["_extract_error_kind"] == "corrupt"
    assert "_extract_error" in meta


    # The model3d wrapper runs the parser in a CHILD process (2026-08-22
    # isolation), so an in-process monkeypatch of the parser can't reach it —
    # trip the real size guard inside the child via the settings it is handed.
    from filearr.config import get_settings

    big = tmp_path / "any.stl"
    big.write_bytes(b"solid fake\n" * 64)
    monkeypatch.setattr(get_settings(), "model3d_max_bytes", 1)
    meta = extract_model3d(str(big))
    assert meta["_extract_error_kind"] == "guard"
    assert "too large" in meta["_extract_error"]


def test_archive_error_preserves_guard_kind(tmp_path):
    """The archive lister re-wraps the shared decompression guard's DocumentError
    — the guard/corrupt classification must survive the re-wrap."""
    import zipfile

    from filearr.tasks.archives import list_archive_members

    bomb = tmp_path / "big.zip"
    with zipfile.ZipFile(bomb, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("payload.bin", b"\0" * 4096)
    with pytest.raises(ArchiveError) as ei:
        list_archive_members(str(bomb), decompressed_max=1024)
    assert ei.value.kind == "guard"


def test_raised_dependency_error_defaults():
    """New shipped guard defaults (user-approved): doc 1 GiB / model3d 512 MiB."""
    from filearr.config import Settings

    s = Settings()
    assert s.doc_decompressed_max == 1_073_741_824
    assert s.model3d_max_bytes == 536_870_912

# --------------------------------------------------------------------------- #
# Extract wall-clock budget (live incident 2026-07-27)                        #
# --------------------------------------------------------------------------- #
def test_extract_timeout_setting_default():
    from filearr.config import get_settings

    assert get_settings().extract_timeout_seconds == 300
