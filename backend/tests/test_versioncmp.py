"""The version comparator (:mod:`filearr.versioncmp`) against real-world strings.

Every input here is a shape an actual extraction host prints. That is the point
of the file: the comparator's failure mode is not "raises" but "quietly answers
wrong about a string nobody thought of", and the string nobody thinks of is
always the ffmpeg git build. A false "outdated" on the most current host in the
fleet is worse than no warning at all, because it teaches operators that the
amber chip means nothing.
"""

from __future__ import annotations

import pytest

from filearr.versioncmp import compare_versions, is_below, numeric_version


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        # Plain releases, as tesseract / poppler / exiftool print them.
        ("5.3.4", (5, 3, 4)),
        ("12.76", (12, 76)),
        ("7.1", (7, 1)),
        ("22.09.0", (22, 9, 0)),
        # poppler's leading zero is cosmetic: 24.02.0 must be a bigger number
        # than 22.09.0, not a differently formatted string.
        ("24.02.0", (24, 2, 0)),
        # A distro rebuild. The suffix orders nothing on a scale we share with
        # other distros, so only the upstream part is compared.
        ("6.1.1-3ubuntu5", (6, 1, 1)),
        ("4.4.2-0ubuntu0.22.04.1", (4, 4, 2)),
        ("5.1.4-0+deb12u1", (5, 1, 4)),
        # Some builds print a v; a git-describe suffix is ordinary trailing junk.
        ("v7.1", (7, 1)),
        ("5.3.4-99-gafe1e0a", (5, 3, 4)),
        ("3.4.13~ubuntu18", (3, 4, 13)),
        # A single component is still a version.
        ("13", (13,)),
        # Trailing separators must not produce a phantom zero component: 5.3.4.
        # is 5.3.4, and comparing it as (5,3,4,0) would still be right but
        # (5,3,4) is what it means.
        ("5.3.4.", (5, 3, 4)),
    ],
)
def test_numeric_version_parses_real_shapes(raw, expected):
    assert numeric_version(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        # THE case this module exists for: ffmpeg built from git. There is no
        # release number in here at all, and 113579 is a commit counter, not a
        # major version.
        "N-113579-g1c2d3e4",
        "n7.1-dev",  # ffmpeg's other git flavour
        "git-2020-06-12-4a35c2f",
        "unknown",
        "Usage: exiftool [OPTIONS] FILE",
        "",
        "   ",
        None,
        # Not a version with a prefix — just a word starting with v.
        "version",
        # A component too wide to be a version: a build id or a timestamp.
        "1234567890.1",
    ],
)
def test_numeric_version_refuses_what_it_cannot_read(raw):
    assert numeric_version(raw) is None


@pytest.mark.parametrize(
    ("a", "b", "expected"),
    [
        ("5.3.4", "5.0.0", 1),
        ("4.1.1", "5.0.0", -1),
        ("5.0.0", "5.0.0", 0),
        # Padding: a two-component version equals its zero-extended self, which
        # is what lets exiftool (12.76) and ffmpeg (6.1.1) share one comparator.
        ("7.1", "7.1.0", 0),
        ("7.1", "7.0.9", 1),
        ("12.76", "12.24", 1),
        ("12.23", "12.24", -1),
        ("24.02.0", "22.09.0", 1),
        ("22.08.0", "22.09.0", -1),
        # The distro suffix is not part of the ordering.
        ("6.1.1-3ubuntu5", "4.3", 1),
        ("4.2.7-0ubuntu0.1", "4.3", -1),
    ],
)
def test_compare_versions_orders_releases(a, b, expected):
    assert compare_versions(a, b) == expected


def test_compare_versions_is_none_when_either_side_is_unreadable():
    """Unanswerable is a THIRD answer, not a falsy second one."""
    assert compare_versions("N-113579-g1c2d3e4", "4.3") is None
    assert compare_versions("4.3", "N-113579-g1c2d3e4") is None
    assert compare_versions(None, "4.3") is None
    assert compare_versions("4.3", None) is None


def test_is_below_never_calls_a_git_build_old():
    """An ffmpeg git build is usually NEWER than every tagged release. Reporting
    it as below the minimum would put an amber warning on the best-maintained
    machine in the fleet."""
    assert is_below("N-113579-g1c2d3e4", "4.3") is None
    assert is_below("6.1.1-3ubuntu5", "4.3") is False
    assert is_below("4.2.2", "4.3") is True
    # No minimum published -> unanswerable, not "fine".
    assert is_below("6.1.1", None) is None
