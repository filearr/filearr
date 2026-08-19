"""Roadmap §5 P3 provenance (2026-08-19): download-source xattrs -> metadata."""

from __future__ import annotations

import os
import plistlib

import pytest

from filearr import file_origin as provenance


def test_clean_url_filters_schemes_and_junk():
    assert provenance.clean_url("https://example.com/a?b=1") == "https://example.com/a?b=1"
    assert provenance.clean_url(b"http://x.test/\x00bad\x07") == "http://x.test/bad"
    assert provenance.clean_url("javascript:alert(1)") is None
    assert provenance.clean_url("file:///etc/passwd") is None
    assert provenance.clean_url("   ") is None
    assert provenance.clean_url(42) is None
    assert len(provenance.clean_url("https://h.test/" + "x" * 5000)) == provenance.URL_MAX_CHARS


def _xattr_ok(path) -> bool:
    try:
        os.setxattr(path, "user.filearr.probe", b"1")
        os.removexattr(path, "user.filearr.probe")
        return True
    except OSError:
        return False


@pytest.fixture
def xfile(tmp_path):
    p = tmp_path / "dl.bin"
    p.write_bytes(b"x")
    if not hasattr(os, "setxattr") or not _xattr_ok(p):
        pytest.skip("user xattrs unsupported on this tmp filesystem")
    return p


def test_xdg_origin_and_referrer(xfile):
    os.setxattr(xfile, "user.xdg.origin.url", b"https://dl.example.com/f.bin")
    os.setxattr(xfile, "user.xdg.referrer.url", b"https://example.com/page")
    assert provenance.provenance_metadata(str(xfile)) == {
        "origin_url": "https://dl.example.com/f.bin",
        "referrer_url": "https://example.com/page",
    }


def test_referrer_equal_to_origin_is_dropped(xfile):
    os.setxattr(xfile, "user.xdg.origin.url", b"https://dl.example.com/f.bin")
    os.setxattr(xfile, "user.xdg.referrer.url", b"https://dl.example.com/f.bin")
    assert provenance.provenance_metadata(str(xfile)) == {
        "origin_url": "https://dl.example.com/f.bin"
    }


def test_macos_wherefroms_bplist_decoder():
    # Linux refuses the com.apple.* xattr namespace, so exercise the decoder
    # (the only macOS-specific piece) directly.
    blob = plistlib.dumps(
        ["https://cdn.example.org/x.dmg", "https://example.org/"], fmt=plistlib.FMT_BINARY
    )
    assert provenance._wherefroms(blob) == ("https://cdn.example.org/x.dmg", "https://example.org/")
    assert provenance._wherefroms(b"garbage") == (None, None)
    assert provenance._wherefroms(plistlib.dumps("https://one.test/", fmt=plistlib.FMT_BINARY)) == (
        "https://one.test/",
        None,
    )


def test_no_xattrs_is_empty_and_missing_file_never_raises(tmp_path):
    p = tmp_path / "plain"
    p.write_bytes(b"")
    assert provenance.provenance_metadata(str(p)) == {}
    assert provenance.provenance_metadata(str(tmp_path / "nope")) == {}


def test_build_doc_projects_origin_url():
    from datetime import UTC, datetime
    from uuid import uuid4

    from filearr.models import Item, ItemStatus
    from filearr.search import build_doc

    item = Item(
        id=uuid4(),
        library_id=uuid4(),
        file_category="document",
        file_group="doc",
        path="/d/a.pdf",
        rel_path="a.pdf",
        filename="a.pdf",
        extension="pdf",
        size=1,
        mtime=datetime.now(UTC),
        status=ItemStatus.active,
        metadata_={"origin_url": "https://example.com/a.pdf"},
        user_metadata={},
        tags=[],
    )
    assert build_doc(item)["origin_url"] == "https://example.com/a.pdf"
