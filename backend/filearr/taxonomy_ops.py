"""Shared taxonomy maintenance operations (2026-08-21).

The seed-sync and reclassify logic behind BOTH the admin endpoints
(``POST /taxonomy/sync-seed``, ``POST /system/reclassify-extensions``) and the
watermark-guarded ``taxonomy_upkeep`` maintenance task. Two ``app_settings``
watermarks make the periodic task event-driven rather than timer-driven — the
review conclusion behind this design: a raw nightly reclassify would issue
dozens of guarded-but-real UPDATE scans over a million-row items table for
nothing, and a nightly seed sync is a guaranteed no-op between deploys.

* ``taxonomy_seed_fingerprint`` — a hash of the shipped seed payload
  (:func:`seed_fingerprint`). It changes only when a deploy ships a different
  seed, so the upkeep task runs the add-only sync exactly once per such deploy
  — fixing the "edited taxonomy is frozen at its original seed" silent failure
  without any operator action.
* ``taxonomy_reclassified_version`` — the ``taxonomy_state.version`` the
  catalogue was last reclassified under. Every taxonomy edit (GUI or seed
  sync) bumps the version; the upkeep task reconverges once per bump. This
  also debounces a burst of GUI edits into a single reclassify.

Both operations remain idempotent and safe to run concurrently with scans
(reclassify's UPDATEs are guarded with ``IS DISTINCT FROM``; seed sync is
add-only with operator placements winning), so the manual buttons and the
upkeep task can coexist freely.
"""

from __future__ import annotations

import hashlib
import json
import logging

from sqlalchemy import or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from filearr import app_settings, file_groups, taxonomy
from filearr.models import (
    FileCategoryModel,
    FileGroupExtension,
    FileGroupModel,
    Item,
    ItemStatus,
)

log = logging.getLogger("filearr.taxonomy_ops")

#: Batch size for deferring index_sync jobs after a reclassify pass.
RECLASSIFY_SYNC_BATCH = 1000


def seed_fingerprint() -> str:
    """A stable digest of the shipped seed taxonomy — changes exactly when a
    deploy ships different seed content (categories, groups, or extensions)."""
    payload = file_groups.taxonomy_seed_payload()
    canon = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(canon.encode()).hexdigest()


async def sync_seed_now(session: AsyncSession, dry_run: bool = False) -> dict:
    """Adopt extensions/groups the shipped SEED has gained, without disturbing
    any operator edit. ADD-ONLY and idempotent — see the endpoint docstring in
    :mod:`filearr.api.taxonomy` for the full contract. On a real (non-dry) run
    the seed fingerprint watermark is stored so the upkeep task knows this
    seed has been adopted. The caller owns audit emission."""
    db_exts = {
        row.ext: row
        for row in (await session.execute(select(FileGroupExtension))).scalars().all()
    }
    db_groups = {
        g.key: g for g in (await session.execute(select(FileGroupModel))).scalars().all()
    }
    db_cats = {
        c.key: c
        for c in (await session.execute(select(FileCategoryModel))).scalars().all()
    }

    created_categories: list[str] = []
    created_groups: list[str] = []
    added: dict[str, list[str]] = {}
    skipped: list[dict[str, str]] = []

    # --- categories/groups the seed has and the DB does not -------------------
    for cat_key, cat in file_groups.FILE_CATEGORIES.items():
        if cat_key in db_cats:
            continue
        created_categories.append(cat_key)
        if not dry_run:
            row = FileCategoryModel(
                key=cat_key,
                label=cat.label,
                description=cat.description,
                extractor=cat.extractor,
                is_builtin=True,
            )
            session.add(row)
            db_cats[cat_key] = row
    if created_categories and not dry_run:
        await session.flush()  # category ids are needed for the group FK below

    for group_key, group in file_groups.FILE_GROUPS.items():
        if group_key in db_groups:
            continue
        cat_key = file_groups._GROUP_CATEGORY.get(group_key)
        parent = db_cats.get(cat_key) if cat_key else None
        if parent is None:
            # A seed group whose category we could neither find nor create: skip
            # rather than invent a parent. Reported so it is not invisible.
            skipped.append({"group": group_key, "reason": "no such category in the taxonomy"})
            continue
        created_groups.append(group_key)
        if not dry_run:
            row = FileGroupModel(
                key=group_key,
                label=group.label,
                description=group.description or "",
                category_id=parent.id,
                is_builtin=True,
            )
            session.add(row)
            db_groups[group_key] = row
    if created_groups and not dry_run:
        await session.flush()  # group ids are needed for the extension FK below

    # --- extensions ------------------------------------------------------------
    for ext, group_key in file_groups.EXT_GROUP_MAP.items():
        existing = db_exts.get(ext)
        if existing is not None:
            # Already mapped. Report it ONLY when the operator's placement differs
            # from the seed's — an ext already sitting where the seed wants it is
            # not interesting, and listing all ~1200 would bury the signal.
            current = next(
                (k for k, g in db_groups.items() if g.id == existing.group_id), None
            )
            if current is not None and current != group_key:
                skipped.append({"ext": ext, "kept_in": current, "seed_wants": group_key})
            continue
        target = db_groups.get(group_key)
        if target is None:
            skipped.append({"ext": ext, "reason": f"group {group_key!r} missing"})
            continue
        added.setdefault(group_key, []).append(ext)
        if not dry_run:
            session.add(FileGroupExtension(ext=ext, group_id=target.id))

    total_added = sum(len(v) for v in added.values())
    version: int | None = None
    if not dry_run and (total_added or created_groups or created_categories):
        version = await taxonomy.bump_version(session)
        await session.commit()
    elif not dry_run:
        # Nothing to do: do NOT bump the version. A no-op sync that invalidated
        # every agent's cached taxonomy would be a fleet-wide refetch for nothing.
        await session.rollback()

    if not dry_run:
        # Watermark the adopted seed either way — "already covered" is adopted too.
        await app_settings.set_value(
            session, app_settings.KEY_TAXONOMY_SEED_FINGERPRINT, seed_fingerprint(),
            updated_by=None,
        )
        await session.commit()

    return {
        "dry_run": dry_run,
        "added_count": total_added,
        "added": {k: sorted(v) for k, v in sorted(added.items())},
        "created_categories": created_categories,
        "created_groups": created_groups,
        "skipped": skipped,
        "version": version,
        "note": (
            "Existing items keep their stored classification. Run "
            "POST /system/reclassify-extensions to apply this to rows already in "
            "the catalogue — no rescan required."
        ),
    }


async def reclassify_now(session: AsyncSession) -> dict:
    """Recompute every active item's ``(file_category, file_group)`` from the
    CURRENT taxonomy and re-sync the changed docs — the endpoint body of
    ``POST /system/reclassify-extensions`` (see its docstring for the design).
    Guarded UPDATEs (``IS DISTINCT FROM``) so a converged catalogue changes
    zero rows. Stores the reclassified-version watermark on completion."""
    from filearr import worker

    tax = await taxonomy.load(session)
    # Group extensions by their (category, group) target so each target is one
    # bounded set-based UPDATE.
    targets: dict[tuple[str, str], list[str]] = {}
    for ext, group in tax.ext_to_group.items():
        category = tax.group_to_category.get(group, taxonomy.CATEGORY_OTHER)
        targets.setdefault((category, group), []).append(ext)
    all_mapped = list(tax.ext_to_group.keys())

    counts: dict[str, int] = {}
    changed_ids: list[str] = []

    for (category, group), exts in targets.items():
        result = await session.execute(
            update(Item)
            .where(
                Item.status == ItemStatus.active,
                Item.extension.in_(exts),
                or_(
                    Item.file_category.is_distinct_from(category),
                    Item.file_group.is_distinct_from(group),
                ),
            )
            .values(file_category=category, file_group=group)
            .returning(Item.id)
        )
        ids = [str(r[0]) for r in result]
        if ids:
            counts[category] = counts.get(category, 0) + len(ids)
            changed_ids.extend(ids)

    # Reconciliation: an item whose extension is NULL or no longer mapped falls back
    # to (other, other) (matches taxonomy.detect). ``NOT IN`` is NULL-blind, so the
    # explicit ``IS NULL`` arm is required to catch extensionless files.
    result = await session.execute(
        update(Item)
        .where(
            Item.status == ItemStatus.active,
            or_(Item.extension.is_(None), Item.extension.notin_(all_mapped)),
            or_(
                Item.file_category.is_distinct_from(taxonomy.CATEGORY_OTHER),
                Item.file_group.is_distinct_from(taxonomy.GROUP_OTHER),
            ),
        )
        .values(file_category=taxonomy.CATEGORY_OTHER, file_group=taxonomy.GROUP_OTHER)
        .returning(Item.id)
    )
    other_ids = [str(r[0]) for r in result]
    if other_ids:
        counts[taxonomy.CATEGORY_OTHER] = counts.get(taxonomy.CATEGORY_OTHER, 0) + len(other_ids)
        changed_ids.extend(other_ids)

    # Watermark the version this pass converged on, in the same commit as the
    # row updates (a crash before commit retries the whole pass — idempotent).
    await app_settings.set_value(
        session, app_settings.KEY_TAXONOMY_RECLASSIFIED_VERSION, tax.version,
        updated_by=None,
    )
    await session.commit()

    # Re-project changed rows through the normal incremental index path, in
    # bounded batches (invariant 1: Meili is a rebuildable projection of PG).
    for i in range(0, len(changed_ids), RECLASSIFY_SYNC_BATCH):
        await worker.defer_index_sync(changed_ids[i : i + RECLASSIFY_SYNC_BATCH])

    return {"changed": len(changed_ids), "by_category": counts}


async def upkeep_now(session: AsyncSession) -> dict:
    """One watermark-guarded upkeep pass (the ``taxonomy_upkeep`` maintenance
    task body): adopt a changed shipped seed, then reconverge the catalogue if
    the taxonomy version moved. Both checks are two cheap reads; the expensive
    work runs only on actual drift."""
    # Pre-migration boot window: the taxonomy tables may not exist yet.
    if (
        await session.execute(text("SELECT to_regclass('file_group_extensions')"))
    ).scalar() is None:
        return {"skipped": "taxonomy tables not migrated yet"}

    out: dict = {"seed_synced": None, "reclassified": None}

    fp = seed_fingerprint()
    stored_fp = await app_settings.get_value(session, app_settings.KEY_TAXONOMY_SEED_FINGERPRINT)
    if stored_fp != fp:
        result = await sync_seed_now(session, dry_run=False)
        out["seed_synced"] = {
            "added_count": result["added_count"],
            "created_groups": result["created_groups"],
            "created_categories": result["created_categories"],
        }
        log.info(
            "taxonomy upkeep: seed changed — adopted %d ext(s), %d group(s), "
            "%d category(ies)",
            result["added_count"], len(result["created_groups"]),
            len(result["created_categories"]),
        )

    version = (
        await session.execute(text("SELECT version FROM taxonomy_state LIMIT 1"))
    ).scalar()
    last = await app_settings.get_value(
        session, app_settings.KEY_TAXONOMY_RECLASSIFIED_VERSION
    )
    if version is not None and version != last:
        result = await reclassify_now(session)
        out["reclassified"] = result
        log.info(
            "taxonomy upkeep: version %s -> reclassified %d item(s)",
            version, result["changed"],
        )
    return out
