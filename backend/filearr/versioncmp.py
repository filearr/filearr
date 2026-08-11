"""Comparing real-world tool version strings without lying about them.

This exists for one job: deciding whether the ``tesseract 4.1.1`` an agent found
on its host is older than the minimum Filearr recommends. That sounds like a
two-line function and is not, because the strings these binaries print are not
version numbers — they are marketing, packaging and git state in a trench coat::

    5.3.4              tesseract, a plain release
    12.76              exiftool, two components and it will never grow a third
    24.02.0            poppler, calendar-ish versioning that starts a new epoch
    7.1                ffmpeg from Homebrew
    6.1.1-3ubuntu5     ffmpeg as Debian/Ubuntu rebuilt it
    N-113579-g1c2d3e4  ffmpeg built from git — no release number exists AT ALL

The design rule, from which everything else follows: **an unparseable version is
never "outdated".** The git-build case is the whole reason. An ``N-113579``
ffmpeg is almost always NEWER than any tagged release, so a comparator that
guessed "older" there would light up an amber warning on the most up-to-date
host in the fleet — and an operator who is warned wrongly once stops reading
warnings. So every function here returns ``None`` for "cannot say", callers must
handle it, and "cannot say" is rendered as its own neutral state rather than
being folded into either good or bad news.

The parsing rule is correspondingly narrow: take the LEADING dotted-numeric run
and ignore whatever the packager appended. ``6.1.1-3ubuntu5`` compares as
``6.1.1`` because the distro revision orders nothing meaningful for us — Ubuntu's
``3ubuntu5`` and Fedora's ``2.fc39`` are not on a common scale, and upstream's
own numbers already answer the question we are asking. What we must NOT do is
try to be clever about the suffix (PEP 440 / semver pre-release ordering, where
``5.0.0-rc1`` sorts BEFORE ``5.0.0``): a release candidate of the minimum is not
the failure mode this feature was built to catch, and the ordering rules of four
packaging ecosystems are not something to reimplement by eye. A leading ``v`` is
tolerated because some builds print one; nothing else is.

Pure and dependency-free on purpose — this is the logic that will be wrong at the
edges if it is not pinned, so it lives in its own module with its own tests
(``backend/tests/test_versioncmp.py``).
"""

from __future__ import annotations

#: Components past this are dropped. No tool in the catalogue uses four, and the
#: cap keeps an absurd input (untrusted third-party output, remember: this string
#: comes from a binary on a machine we do not control) from turning into a
#: thousand-element tuple.
_MAX_COMPONENTS = 4

#: A single component wider than this is not a version number — it is a build
#: id, a timestamp or garbage — so the whole string is treated as unparseable
#: rather than silently compared as an enormous integer.
_MAX_COMPONENT_DIGITS = 9


def numeric_version(raw: str | None) -> tuple[int, ...] | None:
    """The leading dotted-numeric components of ``raw``, or ``None``.

    ``None`` means "this string carries no comparable version", which is a
    legitimate and common answer (an ffmpeg git build), NOT an error::

        numeric_version("6.1.1-3ubuntu5")     -> (6, 1, 1)
        numeric_version("24.02.0")            -> (24, 2, 0)
        numeric_version("7.1")                -> (7, 1)
        numeric_version("N-113579-g1c2d3e4")  -> None

    Note ``24.02`` parses to ``24, 2``: leading zeros are packaging cosmetics,
    and poppler's ``24.02.0`` must compare greater than its ``22.09.0``, which
    integer components give for free.
    """
    if not raw:
        return None
    s = raw.strip()
    # Some builds print "v7.1". Tolerated, but only when a digit follows — "vX"
    # is not a version with a stripped prefix, it is not a version.
    if s[:1] in ("v", "V") and s[1:2].isdigit():
        s = s[1:]
    if not s[:1].isdigit():
        return None

    parts: list[int] = []
    for chunk in s.split("."):
        digits = ""
        for ch in chunk:
            if not ch.isdigit():
                break
            digits += ch
        if not digits:
            break  # ".foo" — the numeric run ended at the previous component
        if len(digits) > _MAX_COMPONENT_DIGITS:
            return None
        parts.append(int(digits))
        if len(digits) != len(chunk):
            break  # trailing junk ("1-3ubuntu5"): this component is the last one
        if len(parts) >= _MAX_COMPONENTS:
            break
    return tuple(parts) or None


def compare_versions(found: str | None, other: str | None) -> int | None:
    """Order two version strings: ``-1`` / ``0`` / ``1``, or ``None``.

    ``None`` when EITHER side carries no comparable version. The comparison pads
    the shorter tuple with zeros, so ``7.1`` == ``7.1.0`` and ``12.76`` > ``12.24``
    — exiftool's two-component scheme and ffmpeg's three-component one therefore
    need no special casing at the call site.
    """
    a = numeric_version(found)
    b = numeric_version(other)
    if a is None or b is None:
        return None
    width = max(len(a), len(b))
    pa = a + (0,) * (width - len(a))
    pb = b + (0,) * (width - len(b))
    if pa < pb:
        return -1
    return 0 if pa == pb else 1


def is_below(found: str | None, minimum: str | None) -> bool | None:
    """Is ``found`` older than ``minimum``? ``None`` = unanswerable.

    The tri-state is the point. A boolean return would force every caller to
    pick a default for the unanswerable case, and the tempting default (``False``
    — "assume fine") and the cautious one (``True`` — "assume old") are both
    wrong: one hides a real problem, the other cries wolf at a git build. Making
    the third state unrepresentable-as-a-boolean pushes that decision up to the
    verdict layer, where it is made once and named (:mod:`filearr.toolversions`).
    """
    cmp = compare_versions(found, minimum)
    if cmp is None:
        return None
    return cmp < 0
