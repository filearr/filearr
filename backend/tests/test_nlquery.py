"""Natural-language → DSL translation (roadmap §5 P2, 2026-08-06).

Pure-module tests: the heuristic engine is deterministic, so translations are
asserted exactly; every produced DSL string must survive ``querydsl.parse``
(the translator's contract). The Ollama engine is exercised only through its
fallback path (no network in the suite).
"""

from __future__ import annotations

import pytest

from filearr.nlquery import Translation, translate, translate_heuristic
from filearr.querydsl import parse


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        (
            "videos larger than 2GB modified last week not tagged archived",
            "-tag:archived size:>2G modified:<7d kind:video",
        ),
        ("huge mkv or mp4 movies from 2023",
         "modified:2023-01-01..2023-12-31 size:>1G kind:video ext:mkv;mp4"),
        ('pdf documents created since 2024-03-01 "tax return"',
         'created:>=2024-03-01 kind:document ext:pdf "tax return"'),
        ("photos taken in the last 30 days smaller than 5 mb",
         "size:<5M created:<30d kind:image"),
        ("4k video files older than 2 years", "modified:>730d meta.height:>=2000 kind:video"),
        ("spreadsheets this month", "modified:<30d group:spreadsheet"),
        ("yesterday screenshots", "modified:1d..2d kind:image"),
        ("between 100mb and 1gb archives", "size:100M..1G kind:archive"),
        ("everything except zip files", "-ext:zip"),
        ("songs at least 20 mb", "size:>=20M kind:audio"),
        ("ebooks before 2020", "modified:<2020-01-01 group:ebook"),
        ("subtitles for dune", "group:subtitle dune"),
    ],
)
def test_heuristic_translations(text, expected):
    result = translate_heuristic(text)
    assert result.dsl == expected
    parse(result.dsl)  # the contract: output always parses


def test_free_text_passthrough_and_quotes():
    r = translate_heuristic('random gibberish "exact phrase"')
    assert '"exact phrase"' in r.dsl
    assert "random" in r.terms and "gibberish" in r.terms
    parse(r.dsl)


def test_multiple_kinds_noted_not_anded():
    r = translate_heuristic("photos and videos")
    # AND-ing two kinds would match nothing — only the first is emitted.
    assert r.dsl.count("kind:") == 1
    assert r.notes and "AND" in r.notes[0]


def test_fractional_size_converts_to_integer_mantissa():
    r = translate_heuristic("files bigger than 1.5 gb")
    assert r.dsl == "size:>1536M"
    parse(r.dsl)


def test_empty_input():
    assert translate_heuristic("").dsl == ""


@pytest.mark.asyncio
async def test_translate_falls_back_without_ollama():
    # No URL configured -> heuristic directly.
    r = await translate("videos this week")
    assert isinstance(r, Translation)
    assert r.source == "heuristic"
    assert r.dsl == "modified:<7d kind:video"


@pytest.mark.asyncio
async def test_translate_ollama_failure_falls_back(monkeypatch):
    # A configured-but-unreachable model degrades to the heuristic silently.
    r = await translate(
        "videos this week", ollama_url="http://127.0.0.1:1", ollama_model="m"
    )
    assert r.source == "heuristic"
    assert r.dsl == "modified:<7d kind:video"
