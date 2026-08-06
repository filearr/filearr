"""Content-sniffing for extensionless files (roadmap §4, OPS-T4 follow-up).

The live corpus carries thousands of extensionless files that the extension-only
taxonomy can never classify — they sit at ``(other, other)`` with zero signal.
This task magic-sniffs a BOUNDED prefix of each candidate with libmagic
(python-magic — a declared dependency, first used here) and reclassifies the
item when the detected MIME maps onto the taxonomy.

Design constraints (all from the roadmap entry, deliberately):

* **Opt-in, off-scan.** Gated on ``FILEARR_CONTENT_SNIFF_ENABLED`` (default
  false) and exposed as an on-demand maintenance action — NEVER inline in the
  scan walk (sniffing thousands of files over SMB/NFS during a walk is a real
  cost the default scan path must not pay).
* **Extensionless set only.** Candidates are active, non-sidecar,
  centrally-readable items with ``extension IS NULL`` still classified
  ``other`` — mis-extensioned files are out of scope (their extension is a
  deliberate user signal; overriding it is a different, riskier feature).
* **Agent items excluded.** ``source_agent_id IS NULL`` — central cannot open
  files on an agent's filesystem (same rule as extraction/NFO parsing).
* **Idempotent + resumable.** Every sniffed item is stamped
  ``metadata_["_sniffed_mime"]`` (even when the MIME maps to nothing), so a
  re-run skips it; one run processes at most ``content_sniff_batch`` items
  and reports ``remaining`` so the operator can simply run it again.

The MIME map names a **file_group key only**; the category is resolved through
the live taxonomy's ``group_to_category`` rollup, so an operator-customized
taxonomy stays coherent and a mapping onto a deleted group is skipped rather
than inventing vocabulary.
"""

from __future__ import annotations

import logging

from sqlalchemy import select

from filearr.config import get_settings
from filearr.models import Item, ItemStatus

log = logging.getLogger("filearr.sniff")

# libmagic MIME -> taxonomy file_group KEY (category rolls up from the live
# taxonomy). Exact matches first; conservative prefix fallbacks below. Anything
# unmapped (application/octet-stream above all) stays (other, other).
MIME_GROUP_MAP: dict[str, str] = {
    # video
    "video/x-matroska": "video",
    "video/mp4": "video",
    "video/quicktime": "video",
    "video/x-msvideo": "video",
    "video/webm": "video",
    "video/mpeg": "video",
    "video/mp2t": "video",
    # audio
    "audio/mpeg": "audio-lossy",
    "audio/mp4": "audio-lossy",
    "audio/aac": "audio-lossy",
    "audio/ogg": "audio-lossy",
    "audio/flac": "audio-lossless",
    "audio/x-flac": "audio-lossless",
    "audio/x-wav": "audio-lossless",
    "audio/wav": "audio-lossless",
    "audio/x-aiff": "audio-lossless",
    # image
    "image/jpeg": "raster-photo",
    "image/png": "raster-photo",
    "image/webp": "raster-photo",
    "image/tiff": "raster-photo",
    "image/bmp": "raster-photo",
    "image/heic": "raster-photo",
    "image/gif": "animated-image",
    "image/svg+xml": "vector-image",
    # documents
    "application/pdf": "pdf",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "document-office",
    "application/msword": "document-office",
    "application/vnd.oasis.opendocument.text": "document-office",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": "spreadsheet",
    "application/vnd.ms-excel": "spreadsheet",
    "application/vnd.oasis.opendocument.spreadsheet": "spreadsheet",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": "presentation",
    "application/vnd.ms-powerpoint": "presentation",
    "application/epub+zip": "ebook",
    "text/html": "markup",
    "text/xml": "markup",
    "application/xml": "markup",
    "text/csv": "spreadsheet",
    "text/plain": "document-text",
    "text/rtf": "document-text",
    # archives / images-of-disks
    "application/zip": "archive",
    "application/x-tar": "archive",
    "application/gzip": "archive",
    "application/x-xz": "archive",
    "application/x-bzip2": "archive",
    "application/x-7z-compressed": "archive",
    "application/x-rar": "archive",
    "application/vnd.rar": "archive",
    "application/zstd": "archive",
    "application/x-iso9660-image": "disk-image",
    # development / config
    "application/json": "config-data",
    "application/x-yaml": "config-data",
    "text/x-python": "script",
    "text/x-shellscript": "script",
    # system
    "application/x-dosexec": "executable-binary",
    "application/x-executable": "executable-binary",
    "application/x-pie-executable": "executable-binary",
    "application/x-sharedlib": "executable-binary",
    "application/x-mach-binary": "executable-binary",
    "application/x-sqlite3": "database",
    "application/vnd.sqlite3": "database",
    "font/ttf": "font",
    "font/otf": "font",
    "font/woff": "font",
    "font/woff2": "font",
    "application/font-sfnt": "font",
    "message/rfc822": "email",
}

# Conservative type-family fallbacks for MIMEs not in the exact map. text/* is
# deliberately ABSENT: libmagic labels countless binary-adjacent things text/…,
# and a wrong "document" beats "other" by nothing.
MIME_PREFIX_MAP: dict[str, str] = {
    "video/": "video",
    "audio/": "audio-lossy",
    "image/": "raster-photo",
}

SNIFFED_KEY = "_sniffed_mime"


def resolve_group(mime: str) -> str | None:
    """Map a libmagic MIME onto a taxonomy file_group key, or None (stay other)."""
    m = (mime or "").split(";", 1)[0].strip().lower()
    if not m:
        return None
    exact = MIME_GROUP_MAP.get(m)
    if exact is not None:
        return exact
    for prefix, group in MIME_PREFIX_MAP.items():
        if m.startswith(prefix):
            return group
    return None


def _magic_available() -> bool:
    """Whether python-magic + a working native libmagic are importable.

    Package seam (monkeypatched in tests): importing ``magic`` LOADS the native
    library via ctypes, and an incompatible DLL found on a dev host's PATH can
    hard-crash the process rather than raise — so tests never touch the real
    thing, and the feature is targeted at the Linux image (which ships a known
    ``libmagic1``). Windows workers: leave the feature off (its default).
    """
    try:
        import magic  # noqa: F401

        return True
    except ImportError:  # pragma: no cover - environment-dependent
        return False


def _sniff_bytes(head: bytes) -> str | None:
    """MIME of a bounded prefix via libmagic; None when unavailable/undetectable.

    python-magic needs the native libmagic (present in the Docker image + CI;
    a bare Windows dev host may lack the DLL) — an ImportError degrades to
    "feature unavailable", never an exception out of the task.
    """
    try:
        import magic
    except ImportError:  # pragma: no cover - environment-dependent
        return None
    try:
        return magic.from_buffer(head, mime=True)
    except Exception:  # noqa: BLE001 - hostile bytes must never kill the run
        return None


async def sniff_extensionless(session) -> dict:
    """One bounded content-sniff pass over the extensionless candidates.

    Returns counters: ``{"candidates", "sniffed", "reclassified", "unmapped",
    "read_errors", "remaining", "changed_ids"}`` — the CALLER pops
    ``changed_ids`` (re-project + re-extract) before persisting the stats.
    """
    from sqlalchemy import func

    from filearr import taxonomy

    settings = get_settings()

    # Probe libmagic ONCE up front: without it the run can do nothing useful,
    # and stamping candidates as sniffed-to-nothing would poison later runs.
    if not _magic_available():
        log.warning("content sniff: python-magic/libmagic unavailable on this host; skipping run")
        return {
            "candidates": 0, "sniffed": 0, "reclassified": 0, "unmapped": 0,
            "read_errors": 0, "remaining": 0, "changed_ids": [],
            "skipped": "libmagic unavailable",
        }

    tax = await taxonomy.load(session)

    candidate_filter = (
        (Item.status == ItemStatus.active)
        & Item.extension.is_(None)
        & (Item.file_category == "other")
        & Item.sidecar_of.is_(None)
        & Item.source_agent_id.is_(None)
        & ~Item.metadata_.has_key(SNIFFED_KEY)
    )
    total = (
        await session.execute(select(func.count()).select_from(Item).where(candidate_filter))
    ).scalar_one()
    batch = settings.content_sniff_batch
    rows = list(
        (
            await session.execute(
                select(Item).where(candidate_filter).order_by(Item.id).limit(batch)
            )
        ).scalars()
    )
    remaining = max(0, int(total) - len(rows))

    sniffed = reclassified = unmapped = read_errors = 0
    changed_ids: list[str] = []
    read_bytes = settings.content_sniff_read_bytes

    for item in rows:
        try:
            # Small bounded read; mirrors the synchronous file IO the extract
            # task already uses (worker context, no event-loop starvation risk
            # worth a thread hop per 64 KiB read).
            with open(item.path, "rb") as fh:  # noqa: ASYNC230
                head = fh.read(read_bytes)
        except OSError:
            # Unreadable right now (dead mount, permissions, vanished): leave it
            # unstamped so a later run retries once the path is readable again.
            read_errors += 1
            continue

        mime = _sniff_bytes(head)
        sniffed += 1
        group = resolve_group(mime or "")
        category = tax.group_to_category.get(group) if group else None
        # Stamp the sniff result either way (idempotence: a re-run skips this
        # item). Extracted-metadata column only — invariant 2 untouched.
        item.metadata_ = {**item.metadata_, SNIFFED_KEY: mime or "unknown"}
        if group is None or category is None:
            unmapped += 1
            continue
        item.file_category = category
        item.file_group = group
        reclassified += 1
        changed_ids.append(str(item.id))

    await session.commit()
    return {
        "candidates": len(rows),
        "sniffed": sniffed,
        "reclassified": reclassified,
        "unmapped": unmapped,
        "read_errors": read_errors,
        "remaining": remaining,
        "changed_ids": changed_ids,
    }
