"""agent config groups: fold legacy settings.scan_schedule_cron into policy.scan_cron

Revision ID: 3c8a6f1d9e24
Revises: 9b4e7d2c1a55
Create Date: 2026-11-02

The config-group ``settings`` key ``scan_schedule_cron`` predates the policy key
``scan_cron`` and did the same job one precedence rung lower. The agent resolved
its scan cron as::

    merged policy scan_cron (non-null)  >  merged settings scan_schedule_cron
    (non-null)  >  local override  >  env

where each "merged" value is the layered per-key merge over the agent's groups
in ``(priority, name, id)`` order (a later group's key — even an explicit
``null`` — replaces an earlier one). After this revision only ``scan_cron``
exists, so every group's legacy value is either MOVED into that group's policy
``scan_cron`` or DROPPED, chosen so that no agent's effective schedule changes:

* the group's own policy already sets a non-null ``scan_cron`` -> drop (the
  policy key already outranked it everywhere this group applies);
* the legacy value is ``null`` -> drop (it could only ever un-set an earlier
  legacy value, which this migration moves/drops on its own);
* otherwise -> move, unless the value is dead for EVERY member agent (shadowed
  by some group's policy ``scan_cron`` or a later group's legacy key), in which
  case moving it would newly activate it, so it is dropped.

Every agent's before/after schedule is then simulated over the full layered
merge. If any agent would change (a shape that per-group documents cannot
express, e.g. a group whose legacy value is live for some members and shadowed
for others), the migration REFUSES with the affected agents listed rather than
silently moving a schedule — fix those groups in the console and re-run.

Delivery reads immutable version snapshots, so a group whose delivered snapshot
(``current_version``) carries the key gets a NEW forward-only version (actor
``migration``) that becomes current. That bumps the global ``seq`` generation
and the content hash, so every member's policy ETag changes and agents re-fetch
on their next poll. Historical snapshots are left untouched (they are the audit
trail); rolling back to one now 422s because the settings validator rejects the
removed key. The live authoring columns are rewritten with the same decision.
A live (scheduled/running) rollout makes the delivered version per-agent, so
the migration refuses while one exists anywhere and a group still carries the
key — complete or cancel it first.

The downgrade is a no-op: the data move is not reversed (``scan_cron`` is the
equivalent key and the pre-revision agent reads it at a higher precedence).
"""

from __future__ import annotations

import json
from typing import Any

import sqlalchemy as sa

from alembic import op

revision = "3c8a6f1d9e24"
down_revision = "9b4e7d2c1a55"
branch_labels = None
depends_on = None

LEGACY = "scan_schedule_cron"
KEY = "scan_cron"


# --------------------------------------------------------------------------- #
# Pure planning helpers (copied logic, no app imports — tested via importlib)  #
# --------------------------------------------------------------------------- #
def _chain(groups: list[dict[str, Any]], explicit: set[Any]) -> list[dict[str, Any]]:
    """The agent's groups in merge order (``groups`` is already ordered)."""
    return [g for g in groups if g["is_system"] or g["id"] in explicit]


def effective_before(chain: list[tuple[Any, dict, dict]]) -> tuple[Any, Any]:
    """Today's agent-side resolution: ``(cron, legacy_source_group_id)``.
    ``legacy_source_group_id`` is set only when the legacy key is the winner."""
    pol: Any = None
    leg: Any = None
    leg_src: Any = None
    for gid, settings, policy in chain:
        if KEY in policy:
            pol = policy[KEY]
        if LEGACY in settings:
            leg = settings[LEGACY]
            leg_src = gid
    if pol is not None:
        return pol, None
    if leg is not None:
        return leg, leg_src
    return None, None


def effective_after(chain: list[tuple[Any, dict, dict]]) -> Any:
    """Post-migration resolution: the merged policy ``scan_cron`` alone."""
    val: Any = None
    for _gid, _settings, policy in chain:
        if KEY in policy:
            val = policy[KEY]
    return val


def transform(settings: dict, policy: dict, *, move: bool) -> tuple[dict, dict]:
    """Remove the legacy key; when ``move``, copy it into policy ``scan_cron``."""
    new_settings = {k: v for k, v in settings.items() if k != LEGACY}
    new_policy = dict(policy)
    if move:
        new_policy[KEY] = settings[LEGACY]
    return new_settings, new_policy


def _movable(settings: dict, policy: dict) -> bool:
    return settings.get(LEGACY) is not None and policy.get(KEY) is None


def plan(
    groups: list[dict[str, Any]], members: dict[Any, set[Any]]
) -> dict[Any, tuple[dict, dict]]:
    """Decide the new DELIVERED ``(settings, policy)`` for each affected group.

    ``groups``: dicts with ``id``, ``name``, ``is_system``, ``settings``,
    ``policy`` (the delivered snapshot), in merge order. ``members``: every
    agent id -> its explicit group ids (Global is implicit). Returns only the
    groups that change. Raises ``RuntimeError`` if any agent's effective scan
    cron would differ afterwards."""
    chains = {
        aid: [(g["id"], g["settings"], g["policy"]) for g in _chain(groups, explicit)]
        for aid, explicit in members.items()
    }
    winners = {aid: effective_before(ch) for aid, ch in chains.items()}

    out: dict[Any, tuple[dict, dict]] = {}
    for g in groups:
        settings, policy = g["settings"], g["policy"]
        if LEGACY not in settings:
            continue
        move = False
        if _movable(settings, policy):
            in_group = [aid for aid, ch in chains.items() if any(c[0] == g["id"] for c in ch)]
            live = any(winners[aid][1] == g["id"] for aid in in_group)
            move = live or not in_group
        out[g["id"]] = transform(settings, policy, move=move)

    after_groups = [
        {**g, "settings": out[g["id"]][0], "policy": out[g["id"]][1]} if g["id"] in out else g
        for g in groups
    ]
    problems: list[str] = []
    for aid, explicit in members.items():
        ch = [(g["id"], g["settings"], g["policy"]) for g in _chain(after_groups, explicit)]
        before, after = winners[aid][0], effective_after(ch)
        if before != after:
            problems.append(f"agent {aid}: {before!r} -> {after!r}")
    if problems:
        raise RuntimeError(
            "cannot fold settings.scan_schedule_cron into policy.scan_cron without "
            "changing these agents' scan schedules; set scan_cron explicitly on the "
            "affected config groups and remove scan_schedule_cron, then re-run: "
            + "; ".join(problems)
        )
    return out


# --------------------------------------------------------------------------- #
def _doc(v: Any) -> dict:
    if v is None:
        return {}
    if isinstance(v, str):
        return json.loads(v)
    return dict(v)


def upgrade() -> None:
    conn = op.get_bind()
    rows = conn.execute(
        sa.text(
            "SELECT g.id, g.name, g.is_system, g.current_version, "
            "       g.settings AS col_settings, g.policy AS col_policy, "
            "       v.settings AS snap_settings, v.policy AS snap_policy, "
            "       v.seq AS snap_seq "
            "FROM agent_config_groups g "
            "LEFT JOIN agent_config_group_versions v "
            "  ON v.group_id = g.id AND v.version = g.current_version "
            "ORDER BY g.priority, g.name, g.id"
        )
    ).all()
    if not rows:
        return

    groups: list[dict[str, Any]] = []
    for r in rows:
        col_s, col_p = _doc(r.col_settings), _doc(r.col_policy)
        # Delivery falls back to the live columns when the snapshot row is
        # missing (agent_config.resolve_effective_config) — mirror that.
        has_snap = r.snap_seq is not None
        groups.append(
            {
                "id": r.id,
                "name": r.name,
                "is_system": r.is_system,
                "has_snap": has_snap,
                "settings": _doc(r.snap_settings) if has_snap else col_s,
                "policy": _doc(r.snap_policy) if has_snap else col_p,
                "col_settings": col_s,
                "col_policy": col_p,
            }
        )

    if not any(LEGACY in g["settings"] or LEGACY in g["col_settings"] for g in groups):
        return

    live_rollouts = conn.execute(
        sa.text(
            "SELECT count(*) FROM agent_config_rollouts WHERE status IN ('scheduled', 'running')"
        )
    ).scalar()
    if live_rollouts:
        raise RuntimeError(
            "a config group still sets settings.scan_schedule_cron and a config "
            "rollout is live; complete or cancel the rollout, then re-run the "
            "migration"
        )

    members: dict[Any, set[Any]] = {
        aid: set() for (aid,) in conn.execute(sa.text("SELECT id FROM agents")).all()
    }
    for aid, gid in conn.execute(
        sa.text("SELECT agent_id, group_id FROM agent_config_group_members")
    ).all():
        members.setdefault(aid, set()).add(gid)

    delivered = plan(groups, members)

    for g in groups:
        gid = g["id"]
        new_delivered = delivered.get(gid)

        # Live authoring columns. Normally identical to the delivered snapshot;
        # a cancelled rollout can leave them ahead, in which case apply the
        # plain per-group rule to the draft.
        col_s, col_p = g["col_settings"], g["col_policy"]
        new_cols: tuple[dict, dict] | None = None
        if LEGACY in col_s:
            if new_delivered is not None and (col_s, col_p) == (g["settings"], g["policy"]):
                new_cols = new_delivered
            else:
                new_cols = transform(col_s, col_p, move=_movable(col_s, col_p))

        if new_delivered is not None and g["has_snap"]:
            nxt = conn.execute(
                sa.text(
                    "SELECT coalesce(max(version), 0) + 1 FROM "
                    "agent_config_group_versions WHERE group_id = :g"
                ),
                {"g": gid},
            ).scalar()
            conn.execute(
                sa.text(
                    "INSERT INTO agent_config_group_versions "
                    "  (group_id, version, settings, policy, actor, note) "
                    "VALUES (:g, :v, CAST(:s AS jsonb), CAST(:p AS jsonb), "
                    "        'migration', :n)"
                ),
                {
                    "g": gid,
                    "v": nxt,
                    "s": json.dumps(new_delivered[0]),
                    "p": json.dumps(new_delivered[1]),
                    "n": "migrated legacy settings.scan_schedule_cron to policy.scan_cron",
                },
            )
            conn.execute(
                sa.text(
                    "UPDATE agent_config_groups SET current_version = :v, "
                    "updated_at = now() WHERE id = :g"
                ),
                {"g": gid, "v": nxt},
            )
        elif new_delivered is not None:
            # No snapshot: the columns ARE what is delivered.
            new_cols = new_delivered

        if new_cols is not None:
            conn.execute(
                sa.text(
                    "UPDATE agent_config_groups SET settings = CAST(:s AS jsonb), "
                    "policy = CAST(:p AS jsonb), updated_at = now() WHERE id = :g"
                ),
                {"g": gid, "s": json.dumps(new_cols[0]), "p": json.dumps(new_cols[1])},
            )


def downgrade() -> None:
    # Data-only revision: nothing structural to revert, and the move is not
    # undone (policy scan_cron is the equivalent, higher-precedence key that
    # the previous revision's code also honours).
    pass
