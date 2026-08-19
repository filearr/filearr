"""P3-T13 — archive member listing WITHOUT extraction (untrusted input).

Lists the member *names* + declared uncompressed *sizes* of an archive by reading
ONLY its index — the zip central directory, or streamed tar headers — so a search
"which file CONTAINS a member called X" is answerable without ever unpacking a
byte to disk. No member payload is decompressed to a file and, critically, **no
member name is ever normalised, resolved, or joined to a filesystem path**: member
names are untrusted STRINGS, stored verbatim (``../evil``, ``/etc/passwd`` and the
like are kept exactly as declared) and only sanitised of control characters +
length-capped for safe JSON/index storage.

Guard discipline (mirrors P3-T5/T6 — the SAME central-directory zip-bomb guard):

  * **zip family** (zip / cbz / jar): :func:`documents.guard_decompression` runs
    FIRST — it inspects the central directory (declared ``file_size`` vs
    ``compress_size``) and REJECTS a crafted ratio-bomb *before a single member is
    enumerated*. Only then are the member entries read (still index-only).

  * **tar family** (tar / tar.gz|tgz / tar.bz2|tbz2 / tar.xz|txz): tar has no
    central directory, so headers are STREAMED. A compressed tar must decompress
    the *stream* (never the member contents to disk) to walk the headers, so the
    read is bounded two ways — a member-count cap (``FILEARR_ARCHIVE_MAX_MEMBERS``)
    AND a compressed-bytes ceiling (``FILEARR_ARCHIVE_SCAN_MAX_BYTES``, default
    64 MiB) past which listing stops CLEANLY (truncated), so a decompression-bomb
    tar can never force unbounded work.

7z / rar (roadmap §15, 2026-08-19) list through ``py7zr`` / ``rarfile`` --
header-only in both cases (rarfile parses RAR3/RAR5 headers in pure Python; its
external unrar tool is only for EXTRACTION, which never happens here). A 7z
listing decompresses the (small) end header, never a member. The same
decompression-total / ratio guard as zip is applied to the DECLARED sizes after
listing, so a declared bomb is recorded as a guard error rather than indexed.
Encrypted headers (7z/RAR "encrypt file names") raise a ``corrupt``-class error
("encrypted headers") -- nothing is listed. ``.cbr``/``.cb7`` comic archives
ride the same readers.

Emitted schema (merged into ``metadata_`` by the extract wrapper, all optional):
  ``archive``:            {member_count:int, total_uncompressed:int,
                           members:list[{name:str, size:int}] (<= max_stored),
                           truncated:bool, format:str}
  ``archive_members``:    newline-joined member names, char-capped — the flat,
                          SEARCHABLE projection target (Meili attribute, added LAST
                          so a NAME/title hit outranks an archive-member hit).
"""

from __future__ import annotations

import os
import tarfile
import zipfile
from pathlib import PurePath
from typing import Any

from filearr.tasks.documents import (
    DEFAULT_DECOMPRESSED_MAX,
    DEFAULT_DECOMPRESSION_RATIO,
    DEFAULT_DECOMPRESSION_RATIO_MIN_BYTES,
    DocumentError,
    guard_decompression,
)


class ArchiveError(RuntimeError):
    """An archive could not be listed. Message is safe to store under
    ``_extract_error`` (no raw member content, no unbounded parser output).

    ``kind`` classifies the failure for the errors surface: ``corrupt``
    (default), ``guard`` (decompression ceiling), ``error`` (I/O).
    """

    def __init__(self, message: str, *, kind: str = "corrupt") -> None:
        super().__init__(message)
        self.kind = kind


# Default caps (overridable via config at call time; module constants keep the
# pure listing functions self-contained + unit-testable).
DEFAULT_MAX_MEMBERS = 10_000  # enumeration ceiling (count of listed members)
DEFAULT_MAX_STORED = 1_000  # {name,size} entries persisted in metadata_.archive
DEFAULT_INDEX_CHARS = 20_000  # cap on the flat, searchable archive_members string
DEFAULT_SCAN_MAX_BYTES = 64 * 1024 * 1024  # 64 MiB compressed-stream ceiling (tar)

# Per-member-name storage cap — a hostile archive can declare arbitrarily long
# names; cap for safe row/index storage (the name is never used as a path).
_MEMBER_NAME_CAP = 1024

# zip-family extensions (single suffix). cbz is a comic-book zip; jar is a zip.
_ZIP_EXTS: frozenset[str] = frozenset({"zip", "cbz", "jar"})
# 7z / rar families (roadmap §15): cb7 / cbr are the comic-book wrappers.
_SEVENZ_EXTS: frozenset[str] = frozenset({"7z", "cb7"})
_RAR_EXTS: frozenset[str] = frozenset({"rar", "cbr"})
# tar-family suffixes (checked against the full lower-cased filename so the
# compound ``.tar.gz`` forms match before the bare ``.gz`` suffix would).
_TAR_SUFFIXES: tuple[str, ...] = (
    "tar.gz",
    "tar.bz2",
    "tar.xz",
    "tar",
    "tgz",
    "tbz2",
    "tbz",
    "txz",
)


def detect_archive(path: str) -> str | None:
    """Return the archive FAMILY (``"zip"`` / ``"tar"`` / ``"7z"`` / ``"rar"``) for
    ``path``, else None.

    Keys off the EXTENSION only (never opens the file). Compound tar suffixes
    (``.tar.gz`` etc.) are matched against the whole filename so they win over the
    bare ``.gz`` single-suffix."""
    name = PurePath(path).name.lower()
    for suffix in _TAR_SUFFIXES:
        if name.endswith("." + suffix):
            return "tar"
    ext = PurePath(path).suffix.lstrip(".").lower()
    if ext in _ZIP_EXTS:
        return "zip"
    if ext in _SEVENZ_EXTS:
        return "7z"
    if ext in _RAR_EXTS:
        return "rar"
    return None


def is_archive(path: str) -> bool:
    """True when ``path`` is a supported (zip- or tar-family) archive by extension."""
    return detect_archive(path) is not None


def _archive_format(path: str) -> str:
    """Human/machine format tag for the stored ``archive.format`` (e.g. ``tar.gz``)."""
    name = PurePath(path).name.lower()
    for suffix in _TAR_SUFFIXES:
        if name.endswith("." + suffix):
            return suffix
    return PurePath(path).suffix.lstrip(".").lower()


def _clean_member_name(name: Any) -> str:
    """Sanitise an untrusted member name for safe JSON/index storage.

    Strips C0/C1 control characters (injection hygiene — the flat string is fed to
    the search index and the name is rendered in the UI) and caps length. The path
    STRUCTURE is preserved verbatim: ``../evil`` / ``/etc/passwd`` are kept exactly
    as declared — the name is a display/search STRING and is NEVER resolved,
    normalised, or joined to a filesystem path."""
    out: list[str] = []
    for ch in str(name):
        o = ord(ch)
        if o < 0x20 or 0x7F <= o <= 0x9F:
            continue  # drop control chars (NUL/ANSI/etc.)
        out.append(ch)
    return "".join(out)[:_MEMBER_NAME_CAP]


class _CountingReader:
    """A read-only, forward-only wrapper that counts bytes pulled from the raw
    (compressed) file and hard-stops at ``cap`` bytes by reporting EOF.

    Used ONLY for the tar-family stream so the compressed bytes decompressed to
    walk headers are bounded — a decompression-bomb tar cannot force unbounded
    reads. Once the cap is hit, ``read`` returns ``b""`` (EOF) and ``capped`` is
    set; the caller treats the resulting short stream as a clean truncation."""

    def __init__(self, fh: Any, cap: int) -> None:
        self._fh = fh
        self._cap = cap
        self.count = 0
        self.capped = False

    def read(self, size: int = -1) -> bytes:
        if self.count >= self._cap:
            self.capped = True
            return b""
        if size is None or size < 0:
            size = 65536
        remaining = self._cap - self.count
        chunk = self._fh.read(min(size, remaining))
        self.count += len(chunk)
        return chunk


class _Accumulator:
    """Collects members under three independent caps: an enumeration COUNT cap
    (``max_members``), a STORED-list cap (``max_stored`` {name,size} entries kept
    in ``metadata_``), and a flat searchable STRING cap (``index_chars``). The
    flat string may hold more names than the stored list (short names pack denser)
    to maximise searchability within the char budget."""

    def __init__(self, *, max_members: int, max_stored: int, index_chars: int) -> None:
        self.max_members = max_members
        self.max_stored = max_stored
        self.index_chars = index_chars
        self.members: list[dict[str, Any]] = []
        self.count = 0
        self.total = 0
        self.truncated = False
        self._flat: list[str] = []
        self._flat_len = 0

    def full(self) -> bool:
        """True once the enumeration COUNT cap is reached (stop + mark truncated)."""
        return self.count >= self.max_members

    def add(self, name: Any, size: Any) -> None:
        clean = _clean_member_name(name)
        isize = int(size) if isinstance(size, int) and size > 0 else 0
        self.count += 1
        self.total += isize
        if len(self.members) < self.max_stored:
            self.members.append({"name": clean, "size": isize})
        if self._flat_len < self.index_chars:
            piece = clean + "\n"
            self._flat.append(piece)
            self._flat_len += len(piece)

    def flat_string(self) -> str | None:
        s = "".join(self._flat)
        if len(s) > self.index_chars:
            s = s[: self.index_chars]
        return s.strip("\n") or None


def _list_zip(
    path: str,
    acc: _Accumulator,
    *,
    decompressed_max: int,
    ratio_limit: float,
    ratio_min_bytes: int,
) -> None:
    # Guard FIRST — reject a ratio-bomb from the central directory before ANY
    # member is enumerated (reuses the exact P3-T5/T6 discipline).
    try:
        guard_decompression(
            path,
            decompressed_max=decompressed_max,
            ratio_limit=ratio_limit,
            ratio_min_bytes=ratio_min_bytes,
        )
    except DocumentError as exc:
        # Preserve the guard/corrupt classification the shared guard assigned.
        raise ArchiveError(str(exc), kind=getattr(exc, "kind", "corrupt")) from exc

    try:
        with zipfile.ZipFile(path) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if acc.full():
                    acc.truncated = True
                    break
                acc.add(info.filename, info.file_size)
    except zipfile.BadZipFile as exc:
        raise ArchiveError(f"not a valid zip archive: {exc}") from exc
    except OSError as exc:
        raise ArchiveError(f"cannot read zip archive: {exc}", kind="error") from exc


def _list_tar(path: str, acc: _Accumulator, *, scan_max_bytes: int) -> None:
    try:
        raw = open(path, "rb")
    except OSError as exc:
        raise ArchiveError(f"cannot open tar archive: {exc}", kind="error") from exc
    reader = _CountingReader(raw, scan_max_bytes)
    tar = None
    try:
        # Stream mode "r|*": forward-only, auto-detects compression, and reads via
        # our counting fileobj so the compressed bytes decompressed to walk the
        # headers are bounded by ``scan_max_bytes`` (bomb protection). Never seeks,
        # never extracts a member payload to disk.
        try:
            tar = tarfile.open(fileobj=reader, mode="r|*")
        except tarfile.TarError as exc:
            raise ArchiveError(f"not a valid tar archive: {exc}") from exc
        try:
            for info in tar:
                if acc.full() or reader.count >= scan_max_bytes:
                    acc.truncated = True
                    break
                if not info.isfile():
                    continue  # skip dirs/symlinks/devices from the member list
                acc.add(info.name, info.size)
        except (tarfile.TarError, EOFError, OSError) as exc:
            # A short/partial stream (e.g. the compressed-byte ceiling cut the
            # stream mid-member) is a CLEAN truncation once we already have
            # members; only a failure before any member is a real parse error.
            if acc.count == 0:
                raise ArchiveError(f"tar listing failed: {exc}") from exc
            acc.truncated = True
    finally:
        if tar is not None:
            try:
                tar.close()
            except Exception:
                pass
        raw.close()
    if reader.capped:
        acc.truncated = True


def _guard_declared(
    acc: _Accumulator,
    compressed: int,
    *,
    decompressed_max: int,
    ratio_limit: float,
    ratio_min_bytes: int,
) -> None:
    """Post-listing guard for readers that expose only DECLARED sizes (7z/rar):
    same ceilings as the zip central-directory guard, applied to what the headers
    claim. Nothing was decompressed to get here; this just refuses to INDEX a
    declared bomb (the extract records a guard error instead)."""
    if acc.total > decompressed_max:
        raise ArchiveError(
            f"archive declares {acc.total} uncompressed bytes (> {decompressed_max})",
            kind="guard",
        )
    if compressed >= ratio_min_bytes and compressed > 0 and acc.total / compressed > ratio_limit:
        raise ArchiveError(
            f"archive decompression ratio {acc.total / compressed:.0f}:1 exceeds {ratio_limit:g}:1",
            kind="guard",
        )


def _list_7z(path: str, acc: _Accumulator, **guard: Any) -> None:
    try:
        import py7zr
        from py7zr.exceptions import PasswordRequired
    except ImportError as exc:  # pragma: no cover - dependency pinned
        raise ArchiveError("7z reader unavailable (py7zr not installed)", kind="error") from exc
    try:
        with py7zr.SevenZipFile(path, mode="r") as zf:
            for info in zf.list():
                if getattr(info, "is_directory", False):
                    continue
                if acc.full():
                    acc.truncated = True
                    break
                acc.add(info.filename, int(getattr(info, "uncompressed", 0) or 0))
    except PasswordRequired as exc:
        raise ArchiveError("7z archive has encrypted headers") from exc
    except OSError as exc:
        raise ArchiveError(f"cannot read 7z archive: {exc}", kind="error") from exc
    except Exception as exc:  # noqa: BLE001 - py7zr raises a zoo of parse errors
        raise ArchiveError(f"not a valid 7z archive: {type(exc).__name__}") from exc
    _guard_declared(acc, os.path.getsize(path), **guard)


def _list_rar(path: str, acc: _Accumulator, **guard: Any) -> None:
    try:
        import rarfile
    except ImportError as exc:  # pragma: no cover - dependency pinned
        raise ArchiveError("rar reader unavailable (rarfile not installed)", kind="error") from exc
    try:
        with rarfile.RarFile(path) as rf:
            if rf.needs_password():
                raise ArchiveError("rar archive has encrypted headers")
            for info in rf.infolist():
                if info.is_dir():
                    continue
                if acc.full():
                    acc.truncated = True
                    break
                acc.add(info.filename, int(info.file_size or 0))
    except ArchiveError:
        raise
    except rarfile.NeedFirstVolume as exc:
        raise ArchiveError("rar volume is not the first of its set") from exc
    except rarfile.Error as exc:
        raise ArchiveError(f"not a valid rar archive: {type(exc).__name__}") from exc
    except OSError as exc:
        raise ArchiveError(f"cannot read rar archive: {exc}", kind="error") from exc
    _guard_declared(acc, os.path.getsize(path), **guard)


def list_archive_members(
    path: str,
    *,
    max_members: int = DEFAULT_MAX_MEMBERS,
    max_stored: int = DEFAULT_MAX_STORED,
    index_chars: int = DEFAULT_INDEX_CHARS,
    scan_max_bytes: int = DEFAULT_SCAN_MAX_BYTES,
    decompressed_max: int = DEFAULT_DECOMPRESSED_MAX,
    ratio_limit: float = DEFAULT_DECOMPRESSION_RATIO,
    ratio_min_bytes: int = DEFAULT_DECOMPRESSION_RATIO_MIN_BYTES,
) -> dict[str, Any]:
    """List an archive's members index-only, returning the ``metadata_`` fragment.

    Returns ``{}`` for an unrecognised (non-archive) path. Raises
    :class:`ArchiveError` on a hard parse failure OR a decompression-guard
    rejection (the caller records ``_extract_error`` without discarding other
    already-parsed metadata). Never unpacks a member to disk; never touches a
    member name as a filesystem path."""
    family = detect_archive(path)
    if family is None:
        return {}
    acc = _Accumulator(max_members=max_members, max_stored=max_stored, index_chars=index_chars)
    guard = {
        "decompressed_max": decompressed_max,
        "ratio_limit": ratio_limit,
        "ratio_min_bytes": ratio_min_bytes,
    }
    if family == "zip":
        _list_zip(path, acc, **guard)
    elif family == "7z":
        _list_7z(path, acc, **guard)
    elif family == "rar":
        _list_rar(path, acc, **guard)
    else:
        _list_tar(path, acc, scan_max_bytes=scan_max_bytes)

    out: dict[str, Any] = {
        "archive": {
            "member_count": acc.count,
            "total_uncompressed": acc.total,
            "members": acc.members,
            "truncated": acc.truncated,
            "format": _archive_format(path),
        }
    }
    flat = acc.flat_string()
    if flat:
        out["archive_members"] = flat
    return out
