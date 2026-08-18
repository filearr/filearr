"""pypdf's per-object repair WARNINGs are swallowed and counted (2026-08-18):
one sloppy PDF used to emit dozens of "Ignoring wrong pointing object N 0"
lines into the console Logs panel. Now: nothing from pypdf at WARNING reaches
the ROOT handlers (which is where the DB log recorder and stderr live), the
file's metadata carries ``pdf_repaired_objects``, the worker logs ONE info
line, and pypdf ERROR records still propagate.

NOTE: pytest's ``caplog`` (>= 8.4) hooks non-propagating loggers directly, so
it would "see" the swallowed records; these tests observe a handler on the
root logger instead, which is what production does."""

from __future__ import annotations

import logging
from pathlib import Path

import pypdf
import pytest

from filearr.tasks import documents


class _RootSink(logging.Handler):
    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


@pytest.fixture
def root_sink():
    root = logging.getLogger()
    sink = _RootSink()
    prev = root.level
    root.setLevel(logging.DEBUG)
    root.addHandler(sink)
    try:
        yield sink
    finally:
        root.removeHandler(sink)
        root.setLevel(prev)


@pytest.fixture
def sloppy_pdf(tmp_path: Path) -> Path:
    """A valid PDF with a wrong startxref pointer: pypdf (strict=False) rebuilds
    the xref and warns while doing so ('incorrect startxref pointer' and/or
    'Ignoring wrong pointing object')."""
    w = pypdf.PdfWriter()
    w.add_blank_page(width=72, height=72)
    w.add_metadata({"/Title": "sloppy"})
    p = tmp_path / "sloppy.pdf"
    with p.open("wb") as fh:
        w.write(fh)
    data = p.read_bytes()
    i = data.rfind(b"startxref")
    assert i > 0
    j = data.find(b"%%EOF", i)
    p.write_bytes(data[:i] + b"startxref\n999999\n" + data[j:])
    return p


def test_pypdf_warnings_are_swallowed_and_counted(sloppy_pdf, root_sink):
    meta = documents.extract_pdf(str(sloppy_pdf), max_bytes=10_000_000)
    assert meta.get("title") == "sloppy"  # pypdf did repair and read it
    leaked = [r for r in root_sink.records if r.name.startswith("pypdf")]
    assert leaked == [], [r.getMessage() for r in leaked]
    ours = [
        r for r in root_sink.records
        if r.name == documents.__name__ and "pypdf repaired" in r.getMessage()
    ]
    assert meta.get("pdf_repaired_objects", 0) >= 1
    assert len(ours) == 1 and ours[0].levelno == logging.INFO


def test_pypdf_error_records_still_propagate(root_sink):
    logging.getLogger("pypdf._reader").error("boom from pypdf")
    assert any(r.getMessage() == "boom from pypdf" for r in root_sink.records)


def test_pypdf_warning_outside_extraction_is_dropped(root_sink):
    logging.getLogger("pypdf._reader").warning("Ignoring wrong pointing object 1 0 (offset 0)")
    assert not any("wrong pointing" in r.getMessage() for r in root_sink.records)
