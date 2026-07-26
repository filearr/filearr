"""Move/rename detection (T2).

Runs at scan end, *before* tombstoning, and *before* sidecar association. When a
file is renamed or relocated between scans it appears to the diff as one vanished
item (its old ``rel_path`` was not seen) and one brand-new item (the new
``rel_path``). Left alone, the diff would tombstone the old row and create a fresh
one, losing the original identity: id, tags, ``user_metadata``, ``external_ids``,
``first_seen``. This pass reunites the two: it transfers identity onto the
surviving *original* row and drops the freshly-inserted duplicate, so the item id
and every user edit survive the move.

Matching tiers (research-backed, cheap -> strict):
  1. ``(quick_hash, size)`` -- quick_hash is first+last 64 KiB xxh3, so collisions
     are plausible; size narrows but does not eliminate them.
  2. ``content_hash`` confirmation -- when BOTH sides carry a full hash, a mismatch
     *vetoes* the match outright (different bytes, coincidental quick_hash+size).
  3. ``mid_hash`` rescue (roadmap §13) -- when content_hash is unavailable on
     either side (quick_only policy / pre-ceiling large files), a 64 KiB
     midpoint sample stands in: a mid mismatch vetoes exactly like a content
     mismatch, and a mid match may pin a unique partner in a multi-way bucket
     that content_hash alone would have refused as ambiguous. content_hash,
     when present on both sides, always dominates (a full hash outranks any
     sample). mid_hash is NULL for files <=128 KiB (quick_hash already covers
     every byte) and for rows hashed before the column existed — an absent
     sample never confirms and never vetoes.

Integrity first (architect decision, integrity > reliability > speed):
  * A match is transferred ONLY when it is unambiguous. If a ``(quick_hash, size)``
    bucket holds more than one vanished candidate or more than one new item and
    ``content_hash`` cannot single out exactly one pair, NOTHING in that bucket is
    transferred -- the items fall back to the normal tombstone+create path. The
    counts land in ``ScanRun.stats`` (``moved`` / ``move_ambiguous``).
  * Sidecar rows never carry a quick_hash (T3 skips their extraction), so they are
    naturally excluded here; sidecar re-linking after a move is handled by the T3
    association pass, which runs *after* this transfer so it sees surviving ids.

Ordering / the unique index ``(library_id, rel_path)``:
  New-item rows are inserted during the walk with their *new* rel_path, so the
  target rel_path is already occupied by the duplicate we are about to delete.
  Transfer therefore deletes the duplicate first (freeing the rel_path), flushes,
  then rewrites the surviving row's identity columns. Swap cases (A->B while B->A)
  fall out for free: each direction is an independent (quick_hash, size) bucket
  handled in isolation, and because the duplicate is deleted before the survivor
  is repointed, no two live rows ever hold the same rel_path mid-transfer.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select

from filearr import rbac
from filearr.models import Item, ItemStatus, Library
from filearr.tasks.extract import full_hash, mid_hash, quick_hash


@dataclass
class MovePlan:
    """A confirmed, unambiguous rename: keep ``survivor`` (the original row, id
    preserved), delete ``duplicate`` (the row inserted this scan), repointing the
    survivor at the duplicate's new location."""

    survivor: Item  # original (candidate tombstone) -- identity kept
    duplicate: Item  # freshly-created row for the new path -- removed


@dataclass
class MovedOut:
    """A confirmed relocation, surfaced to the scan pipeline (P8-T5 alert capture).

    ``detect_moves`` appends one of these per transferred rename when the caller
    passes a ``moved_out`` collector. It carries exactly what the alert layer
    needs: the surviving item's id (identity preserved across the move), its NEW
    ``rel_path``, and the id of the freshly-inserted duplicate row that was
    dropped — so the scan can (a) suppress the spurious ``created`` alert draft
    the duplicate produced during the walk and (b) emit a ``moved`` event for the
    survivor's new location. Purely additive: the ``detect_moves`` return contract
    is unchanged."""

    survivor_id: str
    survivor_rel_path: str
    duplicate_id: str
    library_id: str | None


def _key(item: Item) -> tuple[str, int] | None:
    if item.quick_hash is None or item.size is None:
        return None
    return (item.quick_hash, item.size)


def _content_match(a: Item, b: Item) -> bool | None:
    """Tri-state: True/False when BOTH sides have a content_hash, else None
    (unknown -- cannot confirm nor veto)."""
    if a.content_hash is not None and b.content_hash is not None:
        return a.content_hash == b.content_hash
    return None


def _mid_match(a: Item, b: Item) -> bool | None:
    """Tri-state midpoint-sample comparison (roadmap §13), same contract as
    ``_content_match``: unknown unless BOTH sides carry a sample."""
    a_mid = getattr(a, "mid_hash", None)
    b_mid = getattr(b, "mid_hash", None)
    if a_mid is not None and b_mid is not None:
        return a_mid == b_mid
    return None


def _match(a: Item, b: Item) -> bool | None:
    """Combined confirmation verdict: content_hash when both sides have one (a
    full hash outranks any sample), else the midpoint sample, else unknown."""
    verdict = _content_match(a, b)
    if verdict is not None:
        return verdict
    return _mid_match(a, b)


def plan_moves(
    candidates: list[Item], new_items: list[Item]
) -> tuple[list[MovePlan], int]:
    """Pure planner. ``candidates`` = vanished original rows that carry a
    quick_hash; ``new_items`` = rows created this scan that carry a quick_hash.

    Returns ``(plans, ambiguous)``. ``plans`` are unambiguous 1:1 renames;
    ``ambiguous`` counts new items that had a plausible (quick_hash, size) match
    but could not be resolved to exactly one candidate (and are thus left to
    tombstone+create).
    """
    cand_buckets: dict[tuple[str, int], list[Item]] = {}
    for c in candidates:
        k = _key(c)
        if k is not None:
            cand_buckets.setdefault(k, []).append(c)

    new_buckets: dict[tuple[str, int], list[Item]] = {}
    for n in new_items:
        k = _key(n)
        if k is not None:
            new_buckets.setdefault(k, []).append(n)

    plans: list[MovePlan] = []
    ambiguous = 0

    for key, news in new_buckets.items():
        cands = cand_buckets.get(key)
        if not cands:
            continue  # genuinely new file -- no vanished twin

        if len(news) == 1 and len(cands) == 1:
            n, c = news[0], cands[0]
            # content_hash — or, absent one on either side, the §13 midpoint
            # sample — may VETO (bytes differ despite quick_hash+size collision).
            if _match(c, n) is False:
                ambiguous += 1
                continue
            plans.append(MovePlan(survivor=c, duplicate=n))
            continue

        # Multi-way bucket: only rescue pairs that content_hash (or the §13
        # midpoint sample when content_hash is unavailable) pins down to a
        # unique partner on BOTH sides; anything else is ambiguous -> skip.
        remaining_cands = list(cands)
        for n in news:
            confirmed = [c for c in remaining_cands if _match(c, n) is True]
            if len(confirmed) == 1:
                c = confirmed[0]
                rival_news = [
                    m for m in news if m is not n and _match(c, m) is True
                ]
                if rival_news:
                    ambiguous += 1
                    continue
                plans.append(MovePlan(survivor=c, duplicate=n))
                remaining_cands.remove(c)
            else:
                ambiguous += 1
    return plans, ambiguous


def _ensure_hashes(item: Item, full_max_bytes: int, compute_content: bool) -> None:
    """Compute quick_hash (and, when the T7 policy allows and the file fits the
    size ceiling, content_hash) for a row that has not been through extraction yet.
    New-item rows are hashed on demand here so move detection can run at scan end
    without waiting on the extract queue. IO errors (dead mount, race) leave the
    hashes None -> the item simply won't match and falls through to normal creation.

    When ``compute_content`` is False (quick_only policy, e.g. a network library),
    content_hash is deliberately left None: such a library CANNOT content-confirm a
    move, so a (quick_hash, size) collision that would need content_hash to
    disambiguate is refused by plan_moves and counted as move_ambiguous -- integrity
    is preserved, we simply never falsely transfer identity without proof."""
    if item.quick_hash is not None:
        return
    try:
        item.quick_hash = quick_hash(item.path, item.size)
        # §13 midpoint sample (None for <=128 KiB files). Computed even when
        # content_hash follows — the survivor stores both, so a FUTURE move of
        # this file under a quick_only policy can still mid-confirm.
        item.mid_hash = mid_hash(item.path, item.size)
        if compute_content and item.size is not None and item.size <= full_max_bytes:
            item.content_hash = full_hash(item.path, item.size)
    except OSError:
        pass


async def detect_moves(
    session,
    candidates: list[Item],
    new_items: list[Item],
    full_max_bytes: int,
    compute_content: bool = True,
    moved_out: list[MovedOut] | None = None,
) -> dict[str, int]:
    """Compute hashes for the new rows, match against vanished candidates, and
    transfer identity for unambiguous renames.

    ``candidates`` MUST already carry quick_hash (they are prior-scan rows). Only
    the new rows are hashed here. ``compute_content`` reflects the library's
    resolved T7 hash policy: when False (quick_only), new rows get no content_hash
    and ambiguous (quick_hash, size) buckets that need it stay ambiguous rather
    than transferring on weaker evidence. Returns ``{'moved': n,
    'move_ambiguous': n}``.
    """
    if not candidates or not new_items:
        return {"moved": 0, "move_ambiguous": 0}

    for n in new_items:
        _ensure_hashes(n, full_max_bytes, compute_content)

    plans, ambiguous = plan_moves(candidates, new_items)

    now = datetime.now(UTC)

    # The unique index (library_id, rel_path) makes a naive "repoint survivor onto
    # the duplicate's rel_path" unsafe in *cyclic* renames (A->B while B->C): the
    # target rel_path can still be held by another vanished-but-not-yet-moved row.
    # Three phases inside one transaction avoid every transient collision:
    #   1. delete all duplicate rows, freeing the target rel_paths they occupied;
    #   2. park every survivor at a guaranteed-unique sentinel rel_path (so no two
    #      survivors, and no survivor-vs-lingering-original, ever clash);
    #   3. rewrite survivors to their final rel_paths + identity columns.
    for plan in plans:
        await session.delete(plan.duplicate)
    await session.flush()

    for i, plan in enumerate(plans):
        # Sentinel is unique per survivor (uuid pk) and shaped so it cannot collide
        # with any scanned rel_path: it is absolute-anchored with a private marker
        # segment that os.path.relpath never emits. (Postgres text forbids NUL, so
        # a NUL sentinel is not an option.)
        plan.survivor.rel_path = f"\uffff__filearr_move_pending__/{i}/{plan.survivor.id}"
    await session.flush()

    for plan in plans:
        s, d = plan.survivor, plan.duplicate
        # Identity columns (id, tags, user_metadata, external_ids, first_seen,
        # title/year, metadata_) are deliberately left untouched — only the
        # location + freshly-computed hashes move onto the surviving original.
        s.path = d.path
        s.rel_path = d.rel_path
        # P6-T2: the item moved to a new rel_path -> restamp its ltree scope key.
        s.path_scope = rbac.path_to_ltree(d.rel_path, library_id=s.library_id)
        s.filename = d.filename
        s.extension = d.extension
        s.size = d.size
        s.mtime = d.mtime
        s.status = d.status  # active
        s.last_seen = now
        s.quick_hash = d.quick_hash
        s.content_hash = d.content_hash
        s.mid_hash = d.mid_hash
        if moved_out is not None:
            # survivor now carries the NEW rel_path; duplicate is about to be gone.
            moved_out.append(
                MovedOut(
                    survivor_id=str(s.id),
                    survivor_rel_path=s.rel_path,
                    duplicate_id=str(d.id),
                    library_id=str(s.library_id) if s.library_id is not None else None,
                )
            )
    await session.flush()

    return {"moved": len(plans), "move_ambiguous": ambiguous}


async def detect_cross_library_moves(
    session,
    new_items: list[Item],
    *,
    library_id,
    full_max_bytes: int,
    compute_content: bool,
) -> tuple[dict[str, int], list[str]]:
    """Cross-library move identity transfer (roadmap §13, deferred from T2).

    A file relocated from library X to library Y used to tombstone in X and be
    created fresh in Y — identity (id, tags, ``user_metadata``,
    ``external_ids``, ``first_seen``) was lost at the library boundary. This
    pass runs at the end of Y's scan, AFTER intra-library detection (which has
    first claim on this scan's new rows): each still-unmatched new row is
    matched against ``missing`` tombstones in OTHER libraries and, when the
    match is proven, the tombstone row is revived INTO library Y at the new
    location (same survivor-keeps-identity mechanics as ``detect_moves``) and
    the fresh duplicate row is dropped.

    Deliberately STRICTER than the intra-library pass (integrity first — a
    cross-library claim lacks the within-scan co-occurrence signal that makes
    an intra 1:1 bucket trustworthy):

      * ``(quick_hash, size)`` alone NEVER transfers. A positive byte-evidence
        confirmation is required — ``content_hash`` equality, or (when either
        side lacks one) a ``mid_hash`` match. Unknown is a refusal here, not a
        pass.
      * Exactly one tombstone must confirm for a new row, and that tombstone
        must confirm no other new row in the batch; anything else counts as
        ``cross_move_ambiguous`` and falls back to plain creation.
      * Only an actual tombstone matches — a file COPIED to Y (original still
        active in X) never matches, because the X row is not ``missing``.
      * Tombstones in agent-sourced libraries are excluded: those rows belong
        to the agent replication protocol (reconcile sweeps, purge watermarks)
        and central must not rewrite their ``library_id`` out from under it.

    Returns ``({'cross_moved': n, 'cross_move_ambiguous': n}, survivor_ids)``.
    The caller re-queues the survivors for extraction: they changed library
    (policy stamp) and location (search projection), so a fresh extract both
    re-applies Y's hash policy and refreshes the Meili document. Alert capture
    is intentionally untouched — from Y's perspective the file genuinely
    appeared, so the walk's ``created`` event stands.
    """
    stats = {"cross_moved": 0, "cross_move_ambiguous": 0}

    # Size prefilter BEFORE any hashing: equal size is a hard precondition of
    # the (quick_hash, size) key, and tombstone sizes are one cheap indexed
    # query — so a scan whose new files match no tombstone size anywhere
    # (the overwhelmingly common case, including every first scan) does zero
    # scan-thread hashing here. This keeps the T2 "don't front-load the
    # extract queue's IO into the walk" discipline.
    tomb_sizes = set(
        (
            await session.execute(
                select(Item.size)
                .join(Library, Item.library_id == Library.id)
                .where(
                    Item.library_id != library_id,
                    Item.status == ItemStatus.missing,
                    Item.quick_hash.is_not(None),
                    Item.sidecar_of.is_(None),
                    Library.source_agent_id.is_(None),
                )
                .distinct()
            )
        ).scalars()
    )
    if not tomb_sizes:
        return stats, []

    hashed: list[Item] = []
    for n in new_items:
        if n.size not in tomb_sizes:
            continue
        _ensure_hashes(n, full_max_bytes, compute_content)
        if n.quick_hash is not None and n.size is not None:
            hashed.append(n)
    if not hashed:
        return stats, []

    tombs = (
        (
            await session.execute(
                select(Item)
                .join(Library, Item.library_id == Library.id)
                .where(
                    Item.library_id != library_id,
                    Item.status == ItemStatus.missing,
                    Item.quick_hash.in_({n.quick_hash for n in hashed}),
                    Item.sidecar_of.is_(None),
                    Library.source_agent_id.is_(None),
                )
            )
        )
        .scalars()
        .all()
    )
    if not tombs:
        return stats, []

    tomb_buckets: dict[tuple[str, int], list[Item]] = {}
    for t in tombs:
        k = _key(t)
        if k is not None:
            tomb_buckets.setdefault(k, []).append(t)

    plans: list[MovePlan] = []
    claimed: set = set()
    for n in hashed:
        cands = tomb_buckets.get((n.quick_hash, n.size), [])
        cands = [c for c in cands if c.id not in claimed]
        if not cands:
            continue
        confirmed = [c for c in cands if _match(c, n) is True]
        if len(confirmed) != 1:
            stats["cross_move_ambiguous"] += 1
            continue
        c = confirmed[0]
        rivals = [m for m in hashed if m is not n and _match(c, m) is True]
        if rivals:
            stats["cross_move_ambiguous"] += 1
            continue
        claimed.add(c.id)
        plans.append(MovePlan(survivor=c, duplicate=n))

    if not plans:
        return stats, []

    now = datetime.now(UTC)
    # Same three-phase discipline as detect_moves: free the target rel_paths,
    # park survivors at collision-proof sentinels, then rewrite — except here
    # the survivor ALSO changes library_id (revived out of its tombstone).
    for plan in plans:
        await session.delete(plan.duplicate)
    await session.flush()
    for i, plan in enumerate(plans):
        plan.survivor.rel_path = (
            f"￿__filearr_xmove_pending__/{i}/{plan.survivor.id}"
        )
    await session.flush()
    for plan in plans:
        s, d = plan.survivor, plan.duplicate
        s.library_id = library_id
        s.path = d.path
        s.rel_path = d.rel_path
        s.path_scope = rbac.path_to_ltree(d.rel_path, library_id=library_id)
        s.filename = d.filename
        s.extension = d.extension
        s.size = d.size
        s.mtime = d.mtime
        s.status = ItemStatus.active
        s.last_seen = now
        s.deleted_at = None
        s.quick_hash = d.quick_hash
        s.content_hash = d.content_hash
        s.mid_hash = d.mid_hash
    await session.flush()

    stats["cross_moved"] = len(plans)
    return stats, [str(p.survivor.id) for p in plans]
