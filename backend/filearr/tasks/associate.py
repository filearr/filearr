"""Sidecar association pass (T3).

Runs after the scan walk has committed all item rows for a library. Recomputes
``items.sidecar_of`` for every active item from scratch (idempotent) and, for
Kodi ``.nfo`` sidecars, folds parsed metadata into the *parent's* extracted
``metadata`` column. Parent resolution:

  * per-stem sidecar  → the sibling non-sidecar item in the SAME directory whose
    stem matches (``Movie (2020)-thumb.jpg`` → ``Movie (2020).mkv``);
  * ``<stem>.nfo``     → same, matching stem;
  * directory artwork / ``movie.nfo`` / ``tvshow.nfo`` → the directory's *primary*
    media item (largest media file in that directory, deterministic tie-break).

Because it derives purely from the current row set, a rescan, rename, or
sidecar-seen-before-parent ordering all converge to the same result.
"""

from __future__ import annotations

import os
import uuid as uuid_mod
from typing import NamedTuple

from sqlalchemy import select, update

from filearr.models import Item, ItemStatus
from filearr.nfo import parse_nfo_bytes
from filearr.sidecar import classify

# W8-B: taxonomy ``file_category`` values considered "primary" content candidates
# for directory-level artwork (the real-media categories with an extractor). The
# old audiobook/sample/spreadsheet media types fold into audio/document. Non-primary
# categories (development/archive/system/other) are never a directory primary.
_PRIMARY_CATEGORIES = frozenset(
    {"video", "audio", "image", "document", "three-d-cad"}
)


def _dir_of(rel_path: str) -> str:
    return os.path.dirname(rel_path)


def _stem_of(rel_path: str) -> str:
    return os.path.splitext(os.path.basename(rel_path))[0].lower()


def resolve_links(items: list[Item]) -> dict[str, str | None]:
    """Pure planner: given the active item rows for a library, return a map of
    {sidecar_item_id: parent_item_id_or_None}. Does not touch the DB.

    Only sidecars appear as keys. A sidecar whose parent can't be resolved maps to
    None (kept, but still marked a sidecar via a self-noop? no — see caller: an
    unresolved sidecar stays sidecar_of=None and thus visible; the classify() call
    result is what the caller uses to force-hide). Here we only assign real parents.
    """
    # Index non-sidecar candidates by (dir, stem) and by directory (for primary).
    by_dir_stem: dict[tuple[str, str], Item] = {}
    dir_primaries: dict[str, list[Item]] = {}

    classified: dict[str, object] = {}
    for it in items:
        info = classify(it.rel_path)
        classified[str(it.id)] = info
        if info is None:  # a real media file — eligible parent
            key = (_dir_of(it.rel_path), _stem_of(it.rel_path))
            # Prefer a primary content category on stem collision (e.g. .mkv over
            # .srt). Both are keyed on the taxonomy file_category now.
            existing = by_dir_stem.get(key)
            if existing is None or (
                it.file_category in _PRIMARY_CATEGORIES
                and existing.file_category not in _PRIMARY_CATEGORIES
            ):
                by_dir_stem[key] = it
            if it.file_category in _PRIMARY_CATEGORIES:
                dir_primaries.setdefault(_dir_of(it.rel_path), []).append(it)

    def primary_for(directory: str) -> Item | None:
        cands = dir_primaries.get(directory)
        if not cands:
            return None
        # Deterministic: largest file, tie-break on rel_path.
        return max(cands, key=lambda i: (i.size or 0, i.rel_path))

    links: dict[str, str | None] = {}
    for it in items:
        info = classified[str(it.id)]
        if info is None:
            continue
        parent: Item | None = None
        if info.parent_stem is not None:  # type: ignore[union-attr]
            key = (info.directory, info.parent_stem.lower())  # type: ignore[union-attr]
            parent = by_dir_stem.get(key)
            if parent is None:
                # Double-extension convention (digiKam et al.): "photo.jpg.xmp"
                # carries parent_stem "photo.jpg", but the sibling photo indexes
                # under stem "photo" — retry with the secondary extension trimmed
                # before giving up on an exact-stem match.
                trimmed = os.path.splitext(info.parent_stem)[0]  # type: ignore[union-attr]
                if trimmed and trimmed != info.parent_stem:
                    parent = by_dir_stem.get((info.directory, trimmed.lower()))  # type: ignore[union-attr]
            if parent is None:
                # Fall back to directory primary if the exact stem sibling is gone.
                parent = primary_for(info.directory)  # type: ignore[union-attr]
        else:
            parent = primary_for(info.directory)  # type: ignore[union-attr]
        # Never let a sidecar point at itself.
        if parent is not None and parent.id != it.id:
            links[str(it.id)] = str(parent.id)
        else:
            links[str(it.id)] = None
    return links


async def associate_sidecars(session, library_id) -> dict[str, int]:
    """Recompute sidecar links for a library and parse NFO metadata into parents.

    Returns stats {'sidecars': n, 'linked': n, 'nfo_parsed': n}.
    """
    items = list(
        (
            await session.execute(
                select(Item).where(
                    Item.library_id == library_id,
                    Item.status == ItemStatus.active,
                )
            )
        ).scalars()
    )
    by_id = {str(i.id): i for i in items}
    links = resolve_links(items)

    sidecars = 0
    linked = 0
    nfo_parsed = 0
    touched_parents: set[str] = set()

    changed_ids: list[str] = []
    for sid, pid in links.items():
        sidecar = by_id[sid]
        sidecars += 1
        # Update FK only on change (keeps rescans cheap / idempotent).
        current = str(sidecar.sidecar_of) if sidecar.sidecar_of else None
        if current != pid:
            sidecar.sidecar_of = pid
            changed_ids.append(sid)
        if pid is not None:
            linked += 1

    # Parse NFO sidecars into their parent's extracted metadata.
    for sid, pid in links.items():
        if pid is None:
            continue
        sidecar = by_id[sid]
        info = classify(sidecar.rel_path)
        if info is None or info.kind != "nfo":
            continue
        parent = by_id.get(pid)
        if parent is None:
            continue
        try:
            # Small NFO read; mirrors the synchronous file IO the extract task
            # already uses. Media mounts are read-only and NFO files are tiny.
            with open(sidecar.path, "rb") as fh:  # noqa: ASYNC230
                data = fh.read()
        except OSError:
            continue
        parsed = parse_nfo_bytes(data)
        if not parsed:
            continue
        nfo_parsed += 1
        ext_ids = parsed.pop("external_ids", None)
        # Extractors write ONLY metadata_ (never user_metadata). Merge, don't clobber
        # sibling extracted keys; NFO is authoritative for the keys it provides.
        parent.metadata_ = {**parent.metadata_, **{f"nfo_{k}": v for k, v in parsed.items()}}
        # Promote a few high-value fields to typed columns when still empty.
        if not parent.title and parsed.get("title"):
            parent.title = str(parsed["title"])
        if not parent.year and parsed.get("year"):
            parent.year = int(parsed["year"])
        if ext_ids:
            parent.external_ids = {**parent.external_ids, **ext_ids}
        touched_parents.add(pid)

    return {
        "sidecars": sidecars,
        "linked": linked,
        "nfo_parsed": nfo_parsed,
        "parents_updated": len(touched_parents),
        # Sidecars whose FK link changed this pass: the caller MUST re-project
        # these to Meili (is_sidecar/sidecar_of live in the doc) and MUST pop
        # this list before persisting the stats dict anywhere (ScanRun.stats).
        "changed_ids": changed_ids,
    }


class _LightItem(NamedTuple):
    """Column-only stand-in for :class:`Item` with exactly the attributes
    :func:`resolve_links` reads — streaming these instead of full ORM rows keeps
    a ~900k-item agent library at tens of MB instead of GB (reconcile.py's
    ``yield_per`` pattern)."""

    id: uuid_mod.UUID
    rel_path: str
    file_category: str | None
    size: int
    sidecar_of: uuid_mod.UUID | None


async def associate_sidecars_light(session, library_id) -> dict:
    """Link-only sidecar association for AGENT-backed libraries.

    Replicated items never pass through the scan task, so nothing sets their
    ``sidecar_of`` (agentsync deliberately never touches the column) — a bulk
    .xmp export on an agent share lands as 400k first-class "other" items
    (live 2026-08: the July timeline bar). This pass recomputes the links the
    same way :func:`associate_sidecars` does but:

      * streams only the columns :func:`resolve_links` needs (``yield_per``) —
        never materializes full ORM rows for a whole agent library;
      * skips NFO parsing entirely — central cannot open agent files
        (``sidecar.path`` is a path on the AGENT's filesystem);
      * bulk-updates only the rows whose link actually changed.

    Returns ``{"sidecars", "linked", "changed", "changed_ids"}`` — the caller
    commits and re-projects ``changed_ids`` to Meili.
    """
    rows: list[_LightItem] = []
    result = await session.stream(
        select(
            Item.id, Item.rel_path, Item.file_category, Item.size, Item.sidecar_of
        )
        .where(Item.library_id == library_id, Item.status == ItemStatus.active)
        .execution_options(yield_per=5000)
    )
    async for r in result:
        rows.append(_LightItem(r.id, r.rel_path, r.file_category, r.size or 0, r.sidecar_of))

    links = resolve_links(rows)
    current = {str(r.id): (str(r.sidecar_of) if r.sidecar_of else None) for r in rows}
    changed = [(sid, pid) for sid, pid in links.items() if current.get(sid) != pid]

    for i in range(0, len(changed), 1000):
        chunk = changed[i : i + 1000]
        await session.execute(
            update(Item),
            [
                {
                    "id": uuid_mod.UUID(sid),
                    "sidecar_of": uuid_mod.UUID(pid) if pid else None,
                }
                for sid, pid in chunk
            ],
        )

    return {
        "sidecars": len(links),
        "linked": sum(1 for v in links.values() if v is not None),
        "changed": len(changed),
        "changed_ids": [sid for sid, _ in changed],
    }
