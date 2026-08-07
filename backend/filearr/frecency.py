"""Central frecency (frequency + recency) personal ranking (roadmap §5 P3).

The agent already ships a local zoxide-style frecency store for query history
(``agent/internal/history``); this is its central, per-principal, per-ITEM
counterpart: every time a user opens an item's detail view the UI fires a
cheap ``POST /items/{id}/touch``, and search results get a **bounded, page-
local re-rank** that lifts the caller's habitually-used items a few positions.

Design constraints:

* **Same scoring shape as the agent** (deliberately): ``score = rank ×
  recency_weight`` with the weight bucketed — <1h ×4, <1d ×2, <1w ×0.5,
  older ×0.25 — plus the same opportunistic maintenance (retention prune at
  90 days, rank-halving once an owner's total rank passes a cap). Two rankers,
  one behaviour.
* **Personal, never global.** Rows are keyed by ``owner`` — the principal id,
  or ``'anonymous'`` when auth is off (a single-user deploy still gets
  personalization; multi-user deploys are isolated per principal). One user's
  habits never move another user's results.
* **Bounded influence.** The re-rank only reorders the ALREADY-RETURNED page:
  relevance (Meili's ranking) still decides what is on the page; frecency
  lifts an item at most ``MAX_LIFT`` positions within it. It never fires when
  the caller asked for an explicit sort.
* **Fail-open.** Recording and boosting are best-effort: any error degrades
  to unpersonalized behaviour, never to a failed search.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete, func, select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.models import ItemFrecency

log = logging.getLogger("filearr.frecency")

#: Recency multipliers — MUST stay in lockstep with the agent's
#: ``history.recencyWeight`` (agent/internal/history/history.go).
_HOUR = 3600.0
_DAY = 86400.0
_WEEK = 604800.0
MULT_HOUR = 4.0
MULT_DAY = 2.0
MULT_WEEK = 0.5
MULT_OLDER = 0.25

#: Retention + decay bounds (mirroring the agent's maintain()).
RETENTION_DAYS = 90
MAX_TOTAL_RANK = 10_000.0
PRUNE_EPSILON = 1.0

#: A boosted item rises at most this many positions within the returned page.
MAX_LIFT = 10

ANONYMOUS_OWNER = "anonymous"


def recency_weight(age_seconds: float) -> float:
    if age_seconds < _HOUR:
        return MULT_HOUR
    if age_seconds < _DAY:
        return MULT_DAY
    if age_seconds < _WEEK:
        return MULT_WEEK
    return MULT_OLDER


def score(rank: float, last_used: datetime, now: datetime | None = None) -> float:
    now = now or datetime.now(UTC)
    age = max(0.0, (now - last_used).total_seconds())
    return rank * recency_weight(age)


def owner_from_actor(actor: str | None) -> str:
    """Derive the frecency owner key from ``request.state.actor``.

    ``principal:<uuid>`` -> the uuid; an API-key prefix or missing actor ->
    ``'anonymous'`` (auth-off single-user deploys, and key-driven automation,
    share one profile — personalization is still useful there, and multi-user
    session deploys get proper per-principal isolation)."""
    if actor and actor.startswith("principal:"):
        return actor.split(":", 1)[1]
    return ANONYMOUS_OWNER


async def record_touch(session: AsyncSession, owner: str, item_id) -> None:
    """Upsert one use for (owner, item) and run opportunistic maintenance.

    Commit is the caller's job (the endpoint wraps this in its session)."""
    now = datetime.now(UTC)
    stmt = pg_insert(ItemFrecency).values(
        owner=owner, item_id=item_id, rank=1.0, last_used=now
    )
    stmt = stmt.on_conflict_do_update(
        index_elements=[ItemFrecency.owner, ItemFrecency.item_id],
        set_={"rank": ItemFrecency.rank + 1.0, "last_used": now},
    )
    await session.execute(stmt)

    # Opportunistic maintenance, same order as the agent: retention prune,
    # then rank-halving once the owner's total passes the cap (keeps scores
    # comparable over years of use without a scheduled job).
    cutoff = now - timedelta(days=RETENTION_DAYS)
    await session.execute(
        delete(ItemFrecency).where(
            ItemFrecency.owner == owner, ItemFrecency.last_used < cutoff
        )
    )
    total = (
        await session.execute(
            select(func.coalesce(func.sum(ItemFrecency.rank), 0.0)).where(
                ItemFrecency.owner == owner
            )
        )
    ).scalar_one()
    if float(total) > MAX_TOTAL_RANK:
        await session.execute(
            update(ItemFrecency)
            .where(ItemFrecency.owner == owner)
            .values(rank=ItemFrecency.rank / 2.0)
        )
        await session.execute(
            delete(ItemFrecency).where(
                ItemFrecency.owner == owner, ItemFrecency.rank < PRUNE_EPSILON
            )
        )


async def scores_for(
    session: AsyncSession, owner: str, item_ids: list[str]
) -> dict[str, float]:
    """Frecency scores for the given item ids (missing rows simply absent)."""
    if not item_ids:
        return {}
    rows = (
        await session.execute(
            select(ItemFrecency.item_id, ItemFrecency.rank, ItemFrecency.last_used)
            .where(ItemFrecency.owner == owner)
            .where(ItemFrecency.item_id.in_(item_ids))
        )
    ).all()
    now = datetime.now(UTC)
    return {str(r.item_id): score(r.rank, r.last_used, now) for r in rows}


def lift(item_score: float) -> int:
    """Positions an item may rise within the page: bounded, sub-linear.

    score 4 (one fresh use) -> 2; ~28 (a weekly habit) -> 4; caps at MAX_LIFT
    so even an extreme habit cannot pin an irrelevant hit to the top."""
    if item_score <= 0:
        return 0
    n = 1
    threshold = 2.0
    while item_score >= threshold and n < MAX_LIFT:
        n += 1
        threshold *= 2
    return n


def rerank(hits: list[dict], scores: dict[str, float]) -> list[dict]:
    """Stable page-local re-rank: each hit's effective position is its original
    index minus its bounded lift. A boosted hit wins position ties against an
    unboosted one (the extra half-step), so "lifted N positions" is literal;
    among equally-adjusted hits the original relevance order is preserved."""
    if not scores:
        return hits

    def effective(pair: tuple[int, dict]) -> float:
        idx, hit = pair
        n = lift(scores.get(str(hit.get("id", "")), 0.0))
        return idx - n - (0.5 if n else 0.0)

    return [h for _, h in sorted(enumerate(hits), key=effective)]
