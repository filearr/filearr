"""Unprobeable-video ``unsupported`` gate + the extension-taxonomy gap fill.

Two related changes, tested together because the gate's member set is drawn from
extensions the taxonomy only learned in the same commit.

* **Taxonomy gaps** — a user's Blue Iris ``.bvr`` recordings classified as ``other``
  because the extension was unmapped. Auditing that turned up systematic holes: the
  entire MIDI/tracker family, the legacy QuickTime/RealMedia siblings, raw elementary
  streams, packet captures, calendar/contact interchange, and more. Every extension
  added to :data:`filearr.file_groups._GROUP_EXTENSIONS` is pinned here to its
  intended ``(file_category, file_group)`` through the REAL classifier path, so a
  future re-shuffle of the authoring rows cannot silently move one.

  The global "no extension in two groups" invariant is NOT re-implemented here — it
  already exists twice over: ``file_groups._invert`` raises at import, and
  ``tests/test_file_groups.py::test_no_duplicate_extension_across_groups`` re-checks
  the authoring source explicitly. This module only asserts the new rows landed.

* **The gate** — ``extract_video`` had no extension gate at all: EVERY
  ``file_category=video`` item invoked ffprobe unconditionally, and each failure
  persisted as ``_extract_error`` on that item. For a CCTV library that is tens of
  thousands of permanently-red rows. Known-unprobeable extensions now return
  ``{"unsupported": True}`` (the marker documents.py/model3d.py already use) WITHOUT
  invoking the probe — asserted here by patching the ffprobe seam and checking it was
  never called, not merely by checking the output dict.
"""

from __future__ import annotations

import pytest

from filearr import taxonomy
from filearr.file_groups import EXT_GROUP_MAP, detect_category, detect_group
from filearr.tasks.extract import _UNPROBEABLE_VIDEO_EXTS, extract_video

# --------------------------------------------------------------------------- #
# The gap fill: ext -> (file_category, file_group), as intended.               #
# --------------------------------------------------------------------------- #
ADDED_EXTENSIONS: dict[str, tuple[str, str]] = {
    # -- video: legacy QuickTime / RealMedia / Vivo siblings ------------------ #
    **{
        e: ("video", "video")
        for e in (
            "moov", "movie", "viv", "vivo", "rv", "rvx", "qtvr", "fxm",
            "rmj", "rmm", "rms", "rmx",
            # raw elementary streams (ffmpeg's own raw demuxers own these names)
            "264", "265", "avc", "h26l",
            # CCTV / NVR / dashcam recorder containers
            "bvr", "g64", "g64x", "dvr", "gmp4", "rcd", "fmp4", "fmpi",
            "h3r", "n3r", "mcg", "hm4",
        )
    },
    # -- audio-project: MIDI, tracker modules, chiptune ----------------------- #
    **{
        e: ("audio", "audio-project")
        for e in (
            "mid", "midi", "kar", "rmi", "xmf",
            "669", "it", "m15", "med", "mtm", "s3m", "stm", "ult", "uni", "xi", "mo3",
            "sid", "minipsf", "psflib",
        )
    },
    # -- the remaining groups ------------------------------------------------- #
    **{e: ("audio", "audio-lossy") for e in ("f4a", "mpga", "rax", "oma", "aa3", "at3", "ogx")},
    "voc": ("audio", "audio-lossless"),
    "f4b": ("audio", "audiobook"),
    **{e: ("audio", "playlist") for e in ("wmx", "mpls")},
    **{
        e: ("image", "raster-photo")
        for e in ("icns", "iff", "ilbm", "jpc", "xwd", "qtif", "mdi")
    },
    "dcm": ("image", "raw-photo"),
    **{e: ("document", "pdf") for e in ("dvi", "pcl")},
    "pgn": ("document", "document-text"),
    **{e: ("development", "source-code") for e in ("diff", "patch")},
    **{e: ("system", "log") for e in ("pcap", "pcapng")},
    **{
        e: ("development", "config-data")
        for e in ("torrent", "ics", "vcs", "vcf", "gcrd", "vct")
    },
}

# Deliberate NON-additions, pinned so a well-meaning future PR has to argue with a
# failing test rather than quietly re-introducing a collision:
#   pef  — Pelco NVR, but ``pef`` is ALREADY Pentax RAW here; the collision also
#          discredits its single blog-post source.
#   psf  — a genuine coin flip (Linux console font vs PlayStation Sound Format).
#   blk/pic/sec — far too generic to hand to one CCTV vendor (``pic`` in particular
#          is a long-established raster-image extension).
REJECTED_EXTENSIONS = {
    "pef": "raw-photo",   # stays Pentax RAW
    "psf": None,          # stays unmapped
    "blk": None,
    "pic": None,
    "sec": None,
}


@pytest.mark.parametrize("ext,expected", sorted(ADDED_EXTENSIONS.items()))
def test_added_extension_resolves_through_the_taxonomy(ext, expected):
    """Each new extension classifies to its intended (category, group) on BOTH real
    classifier paths: the pure seed functions used by the search projection, and the
    ``Taxonomy`` snapshot the runtime service falls back to on an unseeded DB."""
    category, group = expected
    path = f"/data/media/recording.{ext}"

    assert detect_group(path) == group
    assert detect_category(path) == category
    # Same answer through the runtime classifier's seed snapshot (session-free).
    assert taxonomy._seed_snapshot().detect(path) == (category, group)


def test_added_extensions_are_not_other():
    """The whole point of the change: none of these fall into the catch-all."""
    stragglers = [e for e in ADDED_EXTENSIONS if detect_group(f"a.{e}") == "other"]
    assert stragglers == []


def test_midi_is_mapped():
    """MIDI was entirely unmapped until this change — the single most common
    sequence format on earth was landing in ``other``."""
    for ext in ("mid", "midi"):
        assert EXT_GROUP_MAP[ext] == "audio-project"


def test_case_insensitive_like_the_rest_of_the_taxonomy():
    assert detect_group("CAM1.BVR") == "video"
    assert detect_group("Track01.MID") == "audio-project"


@pytest.mark.parametrize("ext,expected_group", sorted(REJECTED_EXTENSIONS.items()))
def test_rejected_extensions_were_not_claimed(ext, expected_group):
    """See REJECTED_EXTENSIONS for the reasoning behind each."""
    assert EXT_GROUP_MAP.get(ext) == expected_group


# --------------------------------------------------------------------------- #
# The unsupported gate                                                         #
# --------------------------------------------------------------------------- #
class _ProbeSpy:
    """Stand-in for ``ffprobe.extract_video_tech`` that records its invocations."""

    def __init__(self, result: dict | None = None):
        self.calls: list[str] = []
        self.result = result if result is not None else {}

    def __call__(self, path, **kwargs):
        self.calls.append(path)
        return dict(self.result)


@pytest.fixture
def probe_spy(monkeypatch):
    """Patch the ffprobe seam. ``extract_video`` imports ``extract_video_tech``
    INSIDE the function body, so patching the module attribute is what the call
    actually resolves through."""
    import filearr.tasks.ffprobe as ffprobe_mod

    spy = _ProbeSpy({"video_codec": "h264", "resolution": "320x240"})
    monkeypatch.setattr(ffprobe_mod, "extract_video_tech", spy)
    return spy


def test_gate_membership_is_the_documented_set():
    """Pinned so a member cannot be added/removed without a deliberate edit here.
    Moving one OUT (a real sample proved ffprobe handles it) = delete the string in
    both places; nothing else is coupled to the set."""
    assert _UNPROBEABLE_VIDEO_EXTS == frozenset(
        {"bvr", "g64", "g64x", "dvr", "gmp4", "rcd", "fmp4", "fmpi", "h3r", "n3r", "mcg", "hm4"}
    )


def test_gate_members_all_classify_as_video():
    """A gate member that did not reach the video extractor would be dead config."""
    for ext in _UNPROBEABLE_VIDEO_EXTS:
        assert detect_category(f"a.{ext}") == "video"


@pytest.mark.parametrize("ext", sorted(_UNPROBEABLE_VIDEO_EXTS))
def test_gated_extension_is_unsupported_and_never_probes(ext, probe_spy, tmp_path):
    f = tmp_path / f"cam.{ext}"
    f.write_bytes(b"\x00" * 64)

    meta = extract_video(str(f))

    assert meta["unsupported"] is True
    # The seam itself was never touched — not just "no error in the output".
    assert probe_spy.calls == []
    # ``unsupported`` is a FACT, not a failure: no error sentinel is recorded, so
    # the item stays out of the errors surface.
    assert "_extract_error" not in meta
    assert "_extract_error_kind" not in meta


def test_gated_extension_still_returns_the_filename_parse(probe_spy, tmp_path):
    """An unprobeable recording must still get whatever guessit yields from its
    name — exactly what extract_video already does when ffprobe FAILS."""
    f = tmp_path / "The.Great.Escape.1963.bvr"
    f.write_bytes(b"\x00" * 64)

    meta = extract_video(str(f))

    assert meta["unsupported"] is True
    assert meta["title"] == "The Great Escape"
    assert meta["year"] == 1963
    assert probe_spy.calls == []


def test_gated_episode_fields_survive(probe_spy, tmp_path):
    f = tmp_path / "Some.Show.S02E05.g64"
    f.write_bytes(b"\x00" * 64)

    meta = extract_video(str(f))

    assert meta["unsupported"] is True
    assert meta["season"] == 2
    assert meta["episode"] == 5


@pytest.mark.parametrize("ext", ["mkv", "mp4", "264", "265", "avc", "h26l"])
def test_ungated_extension_still_probes(ext, probe_spy, tmp_path):
    """The raw elementary-stream extensions are deliberately NOT gated: ffmpeg's own
    raw demuxers register exactly ``h26l,h264,264,avc`` (libavformat/h264dec.c) and
    the hevcdec.c equivalent, so they take the normal probe path."""
    f = tmp_path / f"clip.{ext}"
    f.write_bytes(b"\x00" * 64)

    meta = extract_video(str(f))

    assert probe_spy.calls == [str(f)]
    assert meta["video_codec"] == "h264"
    assert meta["resolution"] == "320x240"
    assert "unsupported" not in meta


def test_ungated_probe_failure_still_records_extract_error(monkeypatch, tmp_path):
    """Regression guard: the gate must not have swallowed the pre-existing
    ffprobe-failure path (error recorded, guessit parse preserved, no raise)."""
    import filearr.tasks.ffprobe as ffprobe_mod

    def _boom(path, **kwargs):
        raise ffprobe_mod.FfprobeError("probe exploded")

    monkeypatch.setattr(ffprobe_mod, "extract_video_tech", _boom)

    f = tmp_path / "Movie.2001.mkv"
    f.write_bytes(b"\x00" * 64)
    meta = extract_video(str(f))

    assert meta["_extract_error"] == "probe exploded"
    assert meta["_extract_error_kind"] == "error"
    assert meta["year"] == 2001
    assert "unsupported" not in meta
