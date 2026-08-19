"""File origin — "where did this file come from?" (roadmap §5 P3 provenance, 2026-08-19).

(Named ``file_origin`` because ``filearr.provenance`` already holds the P4-T7
config-fingerprint helpers.)

Browsers and download tools stamp the source URL onto the file itself:

* **Linux** (Firefox, Chrome, wget, curl ≥ 7.?? with ``--xattr``, KDE/GNOME
  downloaders): the freedesktop xattrs ``user.xdg.origin.url`` and
  ``user.xdg.referrer.url``.
* **macOS** (Safari, Chrome, Finder copies keep it): ``com.apple.metadata:
  kMDItemWhereFroms``, a binary plist holding ``[origin, referrer]``.
* **Windows**: the ``Zone.Identifier`` alternate data stream (``HostUrl`` /
  ``ReferrerUrl``) — read AGENT-side (``agent/internal/extract/provenance_*``);
  central's scanner does not run on Windows.

This module is the central-side reader for the first two. It costs one
``listxattr`` per file (and a ``getxattr`` per present key), skips silently on
filesystems without user xattrs (cifs without ``user_xattr``, FAT, ...) and never
raises. Output is flat metadata in central's vocabulary — ``origin_url`` /
``referrer_url`` — so the agent's Windows reader emits the very same keys.
Values are length-capped, control-stripped and restricted to http(s)/ftp(s)
schemes (a ``file:`` or ``javascript:`` origin is not provenance anyone wants
rendered as a link)."""

from __future__ import annotations

import os
import plistlib
from typing import Any
from urllib.parse import urlsplit

ORIGIN_KEY = "origin_url"
REFERRER_KEY = "referrer_url"

URL_MAX_CHARS = 2048
_ALLOWED_SCHEMES = frozenset({"http", "https", "ftp", "ftps", "sftp"})

_XATTR_ORIGIN = "user.xdg.origin.url"
_XATTR_REFERRER = "user.xdg.referrer.url"
_XATTR_WHEREFROMS = "com.apple.metadata:kMDItemWhereFroms"


def clean_url(raw: object) -> str | None:
    """Validate + normalise one provenance URL (None when unusable)."""
    if isinstance(raw, bytes):
        raw = raw.decode("utf-8", errors="replace")
    if not isinstance(raw, str):
        return None
    s = "".join(ch for ch in raw.strip() if ch >= " " and ch != "\x7f")
    if not s:
        return None
    if len(s) > URL_MAX_CHARS:
        s = s[:URL_MAX_CHARS]
    try:
        parts = urlsplit(s)
    except ValueError:
        return None
    if parts.scheme.lower() not in _ALLOWED_SCHEMES or not parts.netloc:
        return None
    return s


def _wherefroms(blob: bytes) -> tuple[str | None, str | None]:
    """Decode macOS ``kMDItemWhereFroms`` (bplist array of strings)."""
    try:
        val = plistlib.loads(blob)
    except Exception:  # noqa: BLE001 - malformed xattr, ignore
        return None, None
    if isinstance(val, str):
        return clean_url(val), None
    if isinstance(val, (list, tuple)):
        urls = [clean_url(v) for v in val]
        origin = urls[0] if urls else None
        referrer = urls[1] if len(urls) > 1 else None
        return origin, referrer
    return None, None


def provenance_metadata(path: str) -> dict[str, Any]:
    """Read provenance xattrs for ``path``. ``{}`` when none / unsupported."""
    if not hasattr(os, "listxattr"):
        return {}
    try:
        names = set(os.listxattr(path, follow_symlinks=False))
    except OSError:
        return {}
    out: dict[str, Any] = {}
    origin = referrer = None
    if _XATTR_ORIGIN in names or _XATTR_REFERRER in names:
        for name, key in ((_XATTR_ORIGIN, "o"), (_XATTR_REFERRER, "r")):
            if name not in names:
                continue
            try:
                val = clean_url(os.getxattr(path, name, follow_symlinks=False))
            except OSError:
                val = None
            if key == "o":
                origin = val
            else:
                referrer = val
    elif _XATTR_WHEREFROMS in names:
        try:
            origin, referrer = _wherefroms(
                os.getxattr(path, _XATTR_WHEREFROMS, follow_symlinks=False)
            )
        except OSError:
            origin = referrer = None
    if origin:
        out[ORIGIN_KEY] = origin
    if referrer and referrer != origin:
        out[REFERRER_KEY] = referrer
    return out
