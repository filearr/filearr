"""Minimum recommended versions for the extraction host tools, and the verdict.

Agents report which host tools they found AND which build
(``capabilities.tools`` / ``capabilities.tool_versions``, from
``agent/internal/inventory/toolversions.go``); :mod:`filearr.about` probes the
same seven binaries on central's own machine. Until this module existed, both
surfaces could say only *present* or *absent* — and "present" is not the answer
an operator needs. A host running tesseract 4.1.1 reads a scanned page on an
engine line that upstream stopped maintaining in 2021, and it rendered as the
same cheerful green chip as a 5.3.4 next to it.

So: a documented data table of minimums, in the spirit of
:data:`filearr.agent_config.COLLECTOR_CATALOGUE` — prose an operator can read,
not a validation gate. Nothing here rejects, disables or blocks anything. The
only output is a *verdict* the console can colour.

**Why the table lives centrally rather than in the agent.** A minimum is an
opinion that changes when the world does — a new CVE, a new codec, an upstream
branch going end-of-life. Central-side, revising it is a release of one Python
file; agent-side it would be a fleet-wide binary rollout to change a number.
The agent's job stays "report what is installed"; judging it is ours.

**Every minimum below cites WHY.** A bare version number with no justification
rots into cargo cult within two releases: nobody remembers whether 4.3 was
load-bearing or somebody's guess, so nobody dares raise it and nobody trusts it.
Where no defensible reason could be found, the right entry is ``minimum: None``
— an unjustified threshold that flags healthy installs is strictly worse than no
threshold, because the first false alarm teaches operators to ignore the amber
chip that will one day be real.
"""

from __future__ import annotations

from typing import Any, Literal

from filearr.versioncmp import is_below

#: The verdict vocabulary, shared by the agent capability matrix and the About
#: page so the two surfaces cannot drift apart:
#:
#: * ``ok``       — installed, states a version, and that version is >= the minimum.
#: * ``outdated`` — installed and DEMONSTRABLY below the minimum. The only state
#:                  that warns, and it is only ever reached by an actual numeric
#:                  comparison of two parseable versions.
#: * ``unknown``  — installed, but we cannot judge it: it would not state a
#:                  version, its version is not comparable (an ffmpeg git build),
#:                  or Filearr publishes no minimum for it. NOT a problem, and
#:                  deliberately rendered as neither good nor bad news.
#: * ``absent``   — not installed. A pre-existing, well-understood state that the
#:                  capability matrix already showed; included so a single field
#:                  describes every tool.
Verdict = Literal["ok", "outdated", "unknown", "absent"]

#: The seven tools, in the order both surfaces render them (mirrors
#: ``CAPABILITY_TOOLS`` in frontend/src/lib/agentPolicyDoc.ts and the row order
#: of :func:`filearr.about.host_tools`).
HOST_TOOLS: tuple[str, ...] = (
    "ffmpeg",
    "ffprobe",
    "tesseract",
    "exiftool",
    "pdfinfo",
    "pdftotext",
    "pdftoppm",
)

#: What each tool is FOR, and where its documentation lives, as
#: ``{name: (purpose, url)}``. Curated by necessity — a host binary carries no
#: metadata to introspect the way a Python distribution does.
#:
#: WHY IT LIVES HERE and not in the module that renders it: two surfaces now
#: describe the same seven tools — :func:`filearr.about.host_tools` (central's
#: own machine) and :func:`filearr.agent_about.agent_about` (a remote agent's) —
#: and a table copied into both would drift on the first edit. It would drift
#: silently, too: nothing fails when one page calls pdftoppm "PDF page
#: rasterisation (OCR)" and the other calls it something else, it just quietly
#: teaches operators that the two pages are describing different things.
#: :mod:`filearr.toolversions` is already the single registry both consult for
#: order and minimums, so the prose belongs beside them.
HOST_TOOL_INFO: dict[str, tuple[str, str]] = {
    "ffprobe": ("Video/audio stream metadata", "https://ffmpeg.org/ffprobe.html"),
    "ffmpeg": ("Thumbnail poster frames", "https://ffmpeg.org/ffmpeg.html"),
    "exiftool": ("Image/EXIF metadata", "https://exiftool.org/"),
    "tesseract": ("OCR for scanned documents", "https://tesseract-ocr.github.io/"),
    "pdfinfo": ("PDF document properties", "https://poppler.freedesktop.org/"),
    "pdftotext": ("PDF text extraction", "https://poppler.freedesktop.org/"),
    "pdftoppm": ("PDF page rasterisation (OCR)", "https://poppler.freedesktop.org/"),
}


def tool_purpose(tool: str) -> str | None:
    """What ``tool`` is used for, or ``None`` for a tool we do not know.

    ``None`` is a real answer, not a gap to paper over: a NEWER agent build can
    advertise a host tool this central release has never heard of, and the
    honest render is the tool's name with no description rather than an invented
    one."""
    entry = HOST_TOOL_INFO.get(tool)
    return entry[0] if entry else None


def tool_url(tool: str) -> str | None:
    """The upstream documentation link for ``tool``, or ``None``. See
    :func:`tool_purpose` on why an unknown tool gets no guess."""
    entry = HOST_TOOL_INFO.get(tool)
    return entry[1] if entry else None

# --- The minimums (researched 2026-08-11) ----------------------------------- #
#
# Two of the four justifications below are SECURITY ones, and that is not an
# accident: these binaries are handed files that arrived from wherever the
# library's contents came from, and they are the largest attack surface Filearr
# has. The other two are capability cliffs — a version below which the tool
# simply cannot see something Filearr asks it about.
#
# Each entry:
#   name      — the binary, as both the agent and about.py key it.
#   minimum   — the version string, or None for "we publish no minimum".
#   reason    — WHY that number, with the evidence. Read this before changing it.
#   impact    — one operator-readable sentence: what is worse below the minimum.
#               This is the tooltip text, so it must stand alone.
HOST_TOOL_MINIMUMS: tuple[dict[str, Any], ...] = (
    {
        "name": "ffmpeg",
        "minimum": "4.3",
        "reason": (
            "Paired with ffprobe below, whose 4.3 cliff is the binding one — they "
            "ship as one project and an operator upgrades them together. 4.3 also "
            "comfortably clears AV1 decoding through libdav1d, which arrived in "
            "4.1 (FFmpeg Changelog, 'AV1 decoding support through libdav1d'); "
            "before that, an AV1 video simply produces no poster frame."
        ),
        "impact": (
            "Older builds cannot decode newer codecs such as AV1, so those videos "
            "fall back to the placeholder icon instead of a poster-frame thumbnail."
        ),
    },
    {
        "name": "ffprobe",
        "minimum": "4.3",
        "reason": (
            "4.3 is the first release in which the Dolby Vision configuration "
            "record is a reportable side-data type: AV_PKT_DATA_DOVI_CONF and "
            "AVDOVIDecoderConfigurationRecord were added 2020-04-22 (lavc "
            "58.81.100 / lavu 56.43.100, doc/APIchanges), and 4.3 is the release "
            "that shipped them. An older ffprobe parses an HDR/DV file happily "
            "and just never mentions the DV layer, which is the worst failure "
            "shape there is: a complete-looking answer that is quietly missing a "
            "field. Full DV RPU metadata (AV_FRAME_DATA_DOVI_METADATA, 2022-01-04) "
            "came later still, but Filearr reads the configuration record, so 4.3 "
            "is the honest threshold rather than the highest one we could pick."
        ),
        "impact": (
            "Dolby Vision and some HDR side data are not reported at all, so those "
            "videos are indexed as ordinary SDR files with no indication anything "
            "was missed."
        ),
    },
    {
        "name": "tesseract",
        "minimum": "5.0.0",
        "reason": (
            "4.1.3 (2021-11-15) was the FINAL release of the 4.x line; 5.0.0 "
            "followed a fortnight later (2021-11-30) and every engine fix since "
            "has landed only there (tesseract ReleaseNotes). The honest framing "
            "matters: the neural LSTM engine itself arrived in 4.0, so a 4.1 host "
            "is not pre-neural and the raw accuracy gap is modest. What a 4.1 host "
            "is, is frozen on an unmaintained 2019 build — 5.0 moved LSTM "
            "inference to 32-bit floats with wider SIMD (AVX2/NEON) for a large "
            "speed and memory win, and the page-segmentation and crash fixes of "
            "the last four years exist only on 5.x. Ubuntu 22.04 still ships "
            "4.1.1, so this minimum does flag real hosts — deliberately: that is "
            "exactly the machine whose OCR output looks inexplicably worse than "
            "its neighbour's."
        ),
        "impact": (
            "OCR runs on an engine line upstream abandoned in 2021 — slower, "
            "heavier, and missing four years of recognition and crash fixes."
        ),
    },
    {
        "name": "exiftool",
        "minimum": "12.24",
        "reason": (
            "Security, and an unusually clear-cut one. CVE-2021-22204: ExifTool "
            "7.44 through 12.23 evaluates attacker-controlled Perl from the DjVu "
            "annotation parser — arbitrary code execution triggerable from a "
            "variety of ordinary-looking image files, which is how it was found in "
            "GitLab. Fixed in 12.24. Filearr runs exiftool over whatever files a "
            "library contains, which is precisely the exposure the CVE describes. "
            "No functional cliff is claimed: exiftool adds format support "
            "monthly and nothing Filearr reads needs a specific release, so the "
            "threshold is set at the fix and no higher."
        ),
        "impact": (
            "Versions below 12.24 carry CVE-2021-22204: a crafted image in a "
            "scanned library can execute code as the user running the extractor."
        ),
    },
    # poppler-utils: one package, three binaries, one version number.
    #
    # The three entries share a minimum because they share a libpoppler — the
    # version pdfinfo prints IS the poppler release, and an operator upgrades
    # the package, never one binary. Honesty about the difference between them:
    # pdftoppm rasterises page content and pdftotext walks content streams, so
    # both reach the image decoders where CVE-2022-38784 lives; pdfinfo reads
    # the document catalogue and is far less exposed. Its minimum is therefore a
    # PROXY — it flags a vulnerable poppler package on the host, which is the
    # actionable fact, rather than a claim that pdfinfo itself is exploitable.
    {
        "name": "pdfinfo",
        "minimum": "22.09.0",
        "reason": (
            "The poppler package minimum (see the pdftotext/pdftoppm entries): "
            "CVE-2022-38784, an integer overflow in the JBIG2 decoder affecting "
            "poppler up to and including 22.08.0, fixed in 22.09.0. pdfinfo itself "
            "reads the document catalogue rather than decoding images, so this is "
            "a package-version proxy, not a claim that pdfinfo is the exploitable "
            "binary — but the number it prints is the version of the libpoppler "
            "the other two use, and upgrading is a single package operation."
        ),
        "impact": (
            "Reports a poppler older than 22.09.0 on this host, which means "
            "pdftotext and pdftoppm on the same machine carry CVE-2022-38784."
        ),
    },
    {
        "name": "pdftotext",
        "minimum": "22.09.0",
        "reason": (
            "CVE-2022-38784: an integer overflow in libpoppler's JBIG2 decoder "
            "(JBIG2Stream::readTextRegionSeg) affecting poppler up to and "
            "including 22.08.0, fixed in 22.09.0 — a crafted PDF can crash the "
            "process or execute code. Text extraction walks the content streams of "
            "untrusted PDFs, so this is on the path Filearr actually runs. No "
            "text-quality cliff is claimed on top of it: poppler's extraction has "
            "improved incrementally (21.03 fixed text parsing in some broken PDFs, "
            "24.04 sped it up) with no single release that changes the answer."
        ),
        "impact": (
            "Versions below 22.09.0 carry CVE-2022-38784: a crafted PDF in a "
            "scanned library can crash the extractor or execute code."
        ),
    },
    {
        "name": "pdftoppm",
        "minimum": "22.09.0",
        "reason": (
            "Same libpoppler CVE-2022-38784 as pdftotext, and pdftoppm is the most "
            "exposed of the three: rasterising a page decodes its images, which is "
            "exactly the JBIG2 path the overflow is in. Fixed in 22.09.0."
        ),
        "impact": (
            "Versions below 22.09.0 carry CVE-2022-38784: rasterising a crafted "
            "scanned PDF for OCR can crash the extractor or execute code."
        ),
    },
)

#: Indexed form of the table above. A tool absent from it has no published
#: minimum, which yields ``unknown`` — never ``ok``. That distinction is the
#: whole reason this is a lookup rather than ``.get(name, "0")``: claiming a tool
#: is fine because we forgot to have an opinion about it is the failure mode this
#: module exists to avoid.
MINIMUM_BY_TOOL: dict[str, dict[str, Any]] = {e["name"]: e for e in HOST_TOOL_MINIMUMS}


def minimum_version(tool: str) -> str | None:
    """The published minimum for ``tool``, or ``None`` when there is none."""
    entry = MINIMUM_BY_TOOL.get(tool)
    return entry["minimum"] if entry else None


def minimum_impact(tool: str) -> str | None:
    """The one-sentence "what is worse below the minimum" line, if any."""
    entry = MINIMUM_BY_TOOL.get(tool)
    return entry["impact"] if entry else None


def tool_verdict(tool: str, present: bool, version: str | None) -> Verdict:
    """Judge one tool. See :data:`Verdict` for what each answer means.

    The precedence is deliberate and the order is the argument:

    1. not installed  -> ``absent`` (nothing else can be said);
    2. no version     -> ``unknown`` (installed but silent: an old build, a
       wrapper script, a binary that segfaulted on ``--version``);
    3. no minimum     -> ``unknown``, NOT ``ok``. We have not judged it, and
       saying "ok" would be asserting an opinion we do not hold;
    4. version not comparable -> ``unknown``. This is the ffmpeg git build
       (``N-113579-g1c2d3e4``), which is usually NEWER than any release;
       reporting it as outdated would train operators to ignore the warning;
    5. only now, an actual numeric comparison decides ``outdated`` vs ``ok``.
    """
    if not present:
        return "absent"
    if not version:
        return "unknown"
    minimum = minimum_version(tool)
    if not minimum:
        return "unknown"
    below = is_below(version, minimum)
    if below is None:
        return "unknown"
    return "outdated" if below else "ok"


def capability_verdicts(capabilities: dict | None) -> dict[str, Verdict]:
    """Per-tool verdicts for one agent's advertised ``capabilities``.

    Returns ``{}`` when the agent has never advertised anything (a pending
    enrollment, or a build older than the capability channel). Empty is the
    honest answer there: the console already renders "not reported yet", and
    seven ``absent`` chips would state as fact something we have not observed.

    Everything in ``capabilities`` is third-party JSON from a machine we do not
    control, so each field is type-checked rather than trusted. Tools the agent
    reports that this release has never heard of are included too — they get
    ``unknown`` (no minimum), which is exactly right for a capability added by a
    newer agent build.
    """
    if not isinstance(capabilities, dict):
        return {}
    tools = capabilities.get("tools")
    if not isinstance(tools, dict):
        return {}
    versions = capabilities.get("tool_versions")
    if not isinstance(versions, dict):
        versions = {}
    names = list(HOST_TOOLS) + sorted(
        n for n in tools if isinstance(n, str) and n not in HOST_TOOLS
    )
    out: dict[str, Verdict] = {}
    for name in names:
        present = tools.get(name) is True
        raw = versions.get(name)
        version = raw if isinstance(raw, str) else None
        out[name] = tool_verdict(name, present, version)
    return out
