"""JRiver Media Center ``*_JRSidecar.xml`` parsing (roadmap §12, 2026-08-19).

JRiver writes a per-file sidecar in its MPL ("Media Playlist") XML dialect::

    <?xml version="1.0" encoding="UTF-8"?>
    <MPL Version="2.0" Title="MPL">
    <Item>
    <Field Name="Filename">D:\\Movies\\Heat (1995).mkv</Field>
    <Field Name="Name">Heat</Field>
    <Field Name="Year">1995</Field>
    <Field Name="Genre">Crime; Drama</Field>
    <Field Name="Director">Michael Mann</Field>
    <Field Name="Actors">Al Pacino; Robert De Niro</Field>
    <Field Name="Description">...</Field>
    <Field Name="Rating">5</Field>
    <Field Name="Media Sub Type">Movie</Field>
    <Field Name="IMDb ID">tt0113277</Field>
    ...
    </Item>
    </MPL>

There is no published schema — the field NAMES are JRiver's library field
names, so the mapping below is a conservative, reverse-engineered subset of the
stable, well-known ones (the same fields JRiver's own MPL import/export
documents). Everything else is preserved nowhere: unknown fields are ignored,
never stored, so a custom JRiver field can't inflate an item's metadata.

Same untrusted-input posture as ``filearr.nfo``: ``defusedxml`` (no DTD, no
entities), a size cap, and ``{}`` on anything malformed. Output keys use
central's vocabulary (``title`` / ``year`` / ``genre`` / ...) so the association
pass can namespace them as ``jr_*`` exactly like ``nfo_*``.
"""

from __future__ import annotations

from typing import Any
from xml.etree.ElementTree import ParseError

from defusedxml.common import DefusedXmlException
from defusedxml.ElementTree import fromstring as _defused_fromstring

MAX_JRSIDECAR_BYTES = 512 * 1024
_TEXT_CAP = 4000
_LIST_CAP = 50

# JRiver field name (lower-cased) -> (our key, kind). kind: str | int | float |
# list (semicolon-separated) | ext:<id-kind>.
_FIELDS: dict[str, tuple[str, str]] = {
    "name": ("title", "str"),
    "year": ("year", "int"),
    "genre": ("genre", "list"),
    "description": ("plot", "str"),
    "director": ("director", "list"),
    "actors": ("actors", "list"),
    "writer": ("writer", "list"),
    "studio": ("studio", "str"),
    "keywords": ("keywords", "list"),
    "series": ("series", "str"),
    "season": ("season", "int"),
    "episode": ("episode", "int"),
    "media type": ("media_type", "str"),
    "media sub type": ("media_sub_type", "str"),
    "rating": ("rating", "float"),
    "mpaa rating": ("mpaa", "str"),
    "duration": ("runtime_seconds", "float"),
    "album": ("album", "str"),
    "artist": ("artist", "str"),
    "album artist": ("album_artist", "str"),
    "composer": ("composer", "str"),
    "track #": ("track", "int"),
    "disc #": ("disc", "int"),
    "comment": ("comment", "str"),
    "imdb id": ("ext:imdb", "ext"),
    "tmdb id": ("ext:tmdb", "ext"),
    "tvdb id": ("ext:tvdb", "ext"),
    "musicbrainz id": ("ext:musicbrainz", "ext"),
}


def _clean(text: str | None) -> str | None:
    if text is None:
        return None
    s = "".join(ch for ch in text.strip() if ch >= " " or ch in "\n\t")
    if not s:
        return None
    return s[:_TEXT_CAP]


def _int(raw: str | None) -> int | None:
    if raw is None:
        return None
    digits = ""
    for ch in raw.strip():
        if ch.isdigit():
            digits += ch
        else:
            break
    try:
        return int(digits) if digits else None
    except ValueError:
        return None


def _float(raw: str | None) -> float | None:
    try:
        return float(raw) if raw is not None and raw.strip() else None
    except ValueError:
        return None


def _list(raw: str | None) -> list[str]:
    if raw is None:
        return []
    parts = [p.strip() for p in raw.replace("\n", ";").split(";")]
    return [p for p in parts if p][:_LIST_CAP]


def parse_jrsidecar_bytes(data: bytes) -> dict[str, Any]:
    """Parse a JRiver sidecar into a metadata dict. Never raises.

    Returns ``{}`` for non-MPL / malformed / oversized / malicious input and for
    an MPL with no ``<Item>``. Only the FIRST ``<Item>`` is read (a sidecar
    describes one file)."""
    if not data or len(data) > MAX_JRSIDECAR_BYTES:
        return {}
    try:
        root = _defused_fromstring(data)
    except (DefusedXmlException, ParseError, ValueError):
        return {}
    except Exception:  # noqa: BLE001 - any other parser failure is "no metadata"
        return {}
    if (root.tag or "").lower() != "mpl":
        return {}
    item = root.find("Item")
    if item is None:
        item = root.find("item")
    if item is None:
        return {}

    out: dict[str, Any] = {}
    ext_ids: dict[str, str] = {}
    for field in item:
        if (field.tag or "").lower() != "field":
            continue
        name = (field.get("Name") or field.get("name") or "").strip().lower()
        spec = _FIELDS.get(name)
        if spec is None:
            continue
        key, kind = spec
        raw = _clean(field.text)
        if raw is None:
            continue
        if kind == "str":
            out[key] = raw
        elif kind == "int":
            v = _int(raw)
            if v is not None:
                out[key] = v
        elif kind == "float":
            v = _float(raw)
            if v is not None:
                out[key] = v
        elif kind == "list":
            vals = _list(raw)
            if vals:
                out[key] = vals if len(vals) > 1 else vals[0]
        elif kind == "ext":
            ext_ids[key.split(":", 1)[1]] = raw[:128]
    if not out and not ext_ids:
        return {}
    # JRiver's "Date" is its own serial; "Year" is what we trust. A bare
    # Media Sub Type of "TV Show"/"Movie" is kept as provenance only.
    if ext_ids:
        out["external_ids"] = ext_ids
    return out
