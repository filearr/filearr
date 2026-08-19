"""Roadmap §12 (2026-08-19): JRiver *_JRSidecar.xml parsing."""

from __future__ import annotations

from filearr.jriver import parse_jrsidecar_bytes

SAMPLE = b"""<?xml version="1.0" encoding="UTF-8"?>
<MPL Version="2.0" Title="MPL" PathSeparator="\\">
<Item>
<Field Name="Filename">D:\\Movies\\Heat (1995)\\Heat.mkv</Field>
<Field Name="Name">Heat</Field>
<Field Name="Year">1995</Field>
<Field Name="Date">34700.5</Field>
<Field Name="Genre">Crime; Drama; Thriller</Field>
<Field Name="Director">Michael Mann</Field>
<Field Name="Actors">Al Pacino; Robert De Niro; Val Kilmer</Field>
<Field Name="Description">A group of professional bank robbers...</Field>
<Field Name="Rating">4.5</Field>
<Field Name="MPAA Rating">R</Field>
<Field Name="Media Type">Video</Field>
<Field Name="Media Sub Type">Movie</Field>
<Field Name="IMDb ID">tt0113277</Field>
<Field Name="TMDb ID">949</Field>
<Field Name="Duration">10200</Field>
<Field Name="Some Custom Field">ignored</Field>
<Field Name="Playback Count">3</Field>
</Item>
<Item><Field Name="Name">second item is ignored</Field></Item>
</MPL>
"""


def test_parses_known_fields_and_ext_ids():
    out = parse_jrsidecar_bytes(SAMPLE)
    assert out["title"] == "Heat" and out["year"] == 1995
    assert out["genre"] == ["Crime", "Drama", "Thriller"]
    assert out["director"] == "Michael Mann"
    assert out["actors"] == ["Al Pacino", "Robert De Niro", "Val Kilmer"]
    assert out["plot"].startswith("A group")
    assert out["rating"] == 4.5 and out["mpaa"] == "R"
    assert out["media_sub_type"] == "Movie" and out["runtime_seconds"] == 10200.0
    assert out["external_ids"] == {"imdb": "tt0113277", "tmdb": "949"}
    assert "some_custom_field" not in out and "playback_count" not in out
    assert "date" not in out  # JRiver's serial date is not trusted


def test_rejects_non_mpl_and_garbage():
    assert parse_jrsidecar_bytes(b"") == {}
    assert parse_jrsidecar_bytes(b"not xml") == {}
    assert parse_jrsidecar_bytes(b"<movie><title>x</title></movie>") == {}
    assert parse_jrsidecar_bytes(b"<MPL></MPL>") == {}
    assert parse_jrsidecar_bytes(b"<MPL><Item></Item></MPL>") == {}
    assert parse_jrsidecar_bytes(b"x" * (600 * 1024)) == {}
    # entity bomb / DTD is refused by defusedxml
    bomb = (
        b'<?xml version="1.0"?><!DOCTYPE lolz [<!ENTITY lol "lol"><!ENTITY lol2 "&lol;&lol;">]>'
        b'<MPL><Item><Field Name="Name">&lol2;</Field></Item></MPL>'
    )
    assert parse_jrsidecar_bytes(bomb) == {}


def test_invalid_control_chars_rejected_and_list_capped():
    # \x01 is not a legal XML character: the parser refuses the whole document
    bad = b'<MPL><Item><Field Name="Name">Bad\x01Name</Field></Item></MPL>'
    assert parse_jrsidecar_bytes(bad) == {}
    raw = b'<MPL><Item><Field Name="Genre">' + b"a;" * 200 + b"</Field></Item></MPL>"
    out = parse_jrsidecar_bytes(raw)
    assert len(out["genre"]) == 50
