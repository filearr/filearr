"""Host-tool minimums and the verdict mapping (:mod:`filearr.toolversions`).

The verdict is what an operator sees as a colour, so the tests below are mostly
about the states that must NOT be green and must NOT be amber. Two rules carry
the feature:

* nothing is called ``outdated`` without an actual numeric comparison of two
  parseable versions;
* nothing is called ``ok`` unless we hold a published opinion it satisfies —
  "we never had a minimum for this tool" is ``unknown``.
"""

from __future__ import annotations

import pytest

from filearr import toolversions as tv


# --------------------------------------------------------------------------- #
# The table itself                                                             #
# --------------------------------------------------------------------------- #
def test_every_host_tool_has_a_catalogue_entry():
    """A tool the agent reports but the table forgets would silently render as
    unjudged forever; the seven are known and finite, so pin them."""
    assert {e["name"] for e in tv.HOST_TOOL_MINIMUMS} == set(tv.HOST_TOOLS)
    assert len(tv.HOST_TOOL_MINIMUMS) == len(tv.HOST_TOOLS)


def test_every_published_minimum_is_parseable_and_justified():
    """A minimum the comparator cannot read would make every host 'unknown' —
    a threshold that silently judges nothing. And a number with no stated reason
    is the cargo cult this table exists to prevent, so `reason` is mandatory
    wherever a number is."""
    from filearr.versioncmp import numeric_version

    for entry in tv.HOST_TOOL_MINIMUMS:
        assert entry["impact"], entry["name"]
        if entry["minimum"] is None:
            # Allowed — and then the entry must say so rather than be blank.
            assert entry["reason"], entry["name"]
            continue
        assert numeric_version(entry["minimum"]) is not None, entry
        assert entry["reason"] and len(entry["reason"]) > 40, entry["name"]


def test_lookups_are_none_for_an_unknown_tool():
    assert tv.minimum_version("pandoc") is None
    assert tv.minimum_impact("pandoc") is None


# --------------------------------------------------------------------------- #
# The verdict mapping                                                          #
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    ("tool", "present", "version", "expected"),
    [
        # absent — nothing else can be said about it.
        ("tesseract", False, None, "absent"),
        ("tesseract", False, "5.3.4", "absent"),  # a version without the binary is noise
        # present, no version: an old build, a wrapper script, a segfaulting probe.
        ("tesseract", True, None, "unknown"),
        ("tesseract", True, "", "unknown"),
        # below / at / above the published minimum.
        ("tesseract", True, "4.1.1", "outdated"),
        ("tesseract", True, "5.0.0", "ok"),
        ("tesseract", True, "5.3.4", "ok"),
        # exiftool's two-component scheme and the CVE-2021-22204 fix line.
        ("exiftool", True, "12.23", "outdated"),
        ("exiftool", True, "12.24", "ok"),
        ("exiftool", True, "12.76", "ok"),
        # poppler's calendar versioning, across the epoch change: the old 0.x
        # scheme must compare as older, not as a different universe.
        ("pdftotext", True, "22.08.0", "outdated"),
        ("pdftotext", True, "22.09.0", "ok"),
        ("pdftotext", True, "24.02.0", "ok"),
        ("pdftoppm", True, "0.86.1", "outdated"),
        # ffmpeg distro builds.
        ("ffmpeg", True, "6.1.1-3ubuntu5", "ok"),
        ("ffprobe", True, "4.2.7-0ubuntu0.1", "outdated"),
        ("ffprobe", True, "7.1", "ok"),
        # An ffmpeg built from git states no release number. It is almost always
        # NEWER than any tag, so it must never come back 'outdated'.
        ("ffmpeg", True, "N-113579-g1c2d3e4", "unknown"),
        # A tool with no published minimum is unjudged — never 'ok'.
        ("pandoc", True, "3.1.2", "unknown"),
    ],
)
def test_tool_verdict(tool, present, version, expected):
    assert tv.tool_verdict(tool, present, version) == expected


def test_a_git_build_is_never_outdated_for_any_tool():
    """Blanket statement of the rule, so a future tool added to the table
    inherits it rather than needing its own test."""
    for name in tv.HOST_TOOLS:
        assert tv.tool_verdict(name, True, "N-113579-g1c2d3e4") == "unknown"


# --------------------------------------------------------------------------- #
# Agent capability advertisements                                              #
# --------------------------------------------------------------------------- #
def test_capability_verdicts_covers_every_tool_the_agent_answered_for():
    caps = {
        "extract": True,
        "tools": {
            "ffmpeg": True,
            "ffprobe": True,
            "tesseract": True,
            "exiftool": False,
            "pdfinfo": True,
            "pdftotext": True,
            "pdftoppm": False,
        },
        "tool_versions": {
            "ffmpeg": "6.1.1-3ubuntu5",
            "ffprobe": "6.1.1-3ubuntu5",
            "tesseract": "4.1.1",
            "pdfinfo": "22.02.0",
            "pdftotext": "22.02.0",
        },
    }
    assert tv.capability_verdicts(caps) == {
        "ffmpeg": "ok",
        "ffprobe": "ok",
        "tesseract": "outdated",
        "exiftool": "absent",
        "pdfinfo": "outdated",
        "pdftotext": "outdated",
        "pdftoppm": "absent",
    }


def test_capability_verdicts_are_empty_when_nothing_was_advertised():
    """A pending enrollment, or a build older than the capability channel. The
    console renders "not reported yet"; seven 'absent' verdicts would assert an
    observation we never made."""
    assert tv.capability_verdicts(None) == {}
    assert tv.capability_verdicts({}) == {}
    assert tv.capability_verdicts({"extract": True}) == {}


def test_capability_verdicts_tolerate_a_hostile_advertisement():
    """`capabilities` is third-party JSON from a machine central does not
    control. Wrong types must degrade, never raise inside a list response."""
    assert tv.capability_verdicts({"tools": ["ffmpeg"]}) == {}
    caps = {
        "tools": {"ffmpeg": "yes", "tesseract": True, 7: True},
        "tool_versions": "6.1.1",
    }
    verdicts = tv.capability_verdicts(caps)
    # "yes" is not True — the agent contract is a bool, and anything else is not
    # an assertion of presence.
    assert verdicts["ffmpeg"] == "absent"
    # tools present, tool_versions unusable -> installed but unjudgeable.
    assert verdicts["tesseract"] == "unknown"


def test_capability_verdicts_include_a_tool_this_release_never_heard_of():
    """A newer agent build can report a tool central has no minimum for. It must
    appear (so the console can render it) as unjudged."""
    caps = {"tools": {"exiftool": True, "mediainfo": True}, "tool_versions": {"mediainfo": "24.06"}}
    verdicts = tv.capability_verdicts(caps)
    assert verdicts["mediainfo"] == "unknown"
    assert verdicts["exiftool"] == "unknown"  # present, no version reported
