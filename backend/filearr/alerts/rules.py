"""Alert-rule matching + grouping (Phase 8, roadmap §6 / brief §2.6, §3).

This module is **inert scaffolding** for Phase 8 (see
``docs/tasks/phase-8-alerting-tasks.md``). It ships the pure, testable core of
the alert engine — the ``FileEvent`` input shape, the ``AlertRule`` dataclass
mirroring the intended ``alert_rules`` DDL (brief §8.1), ``match_rule`` (the
per-event predicate) and ``group_key`` (the dedup/grouping tuple). Anything that
touches Postgres, the scan walk, Procrastinate, or the network lives in a typed
stub elsewhere in this package (``dispatch.py``), tagged with the task that
implements it.

No runtime module imports this package yet — only its tests do. Wiring
``match_rule`` into ``scan.py``'s per-item classification is **P8-T5**.

Glob engine (brief §3.4): matching delegates to the **same**
``pathspec.GitIgnoreSpec`` (MPL-2.0) oracle used for library include/exclude in
``filearr.presets`` — one glob dialect across the product, no second engine.

group_by (Architect ruling **R1**): the base vocabulary ``{event_type,
library_id, rule_id}`` is always present. Since 2026-08-19 (roadmap §6 polish) a
rule may ADD keys from :data:`GROUP_BY_EXTRAS` — ``folder`` (the top-level
directory of ``rel_path``), ``extension`` or ``file`` (per item) — to split
one library-wide group into finer notifications ("one email per folder that
changed" instead of one for the whole library). ``AlertRule.__post_init__``
enforces base ⊆ group_by ⊆ base ∪ extras so a drifting caller fails loudly.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pathspec import GitIgnoreSpec

# The four file-transition kinds a scan/agent diff can classify (brief §2.6).
# Deliberately the same four-way vocabulary server-side ``scan.py`` +
# ``move.py`` already produce (brief §3.5 — unify the transition vocabulary, do
# NOT unify the outbox/alert tables).
EVENT_TYPES: frozenset[str] = frozenset({"created", "modified", "deleted", "moved"})

# Fixed v1 group_by vocabulary (R1). Order is canonical + load-bearing:
# ``group_key`` returns values in exactly this order.
GROUP_BY: tuple[str, ...] = ("event_type", "library_id", "rule_id")
#: Optional extra grouping keys a rule may add (roadmap §6 polish, 2026-08-19).
GROUP_BY_EXTRAS: frozenset[str] = frozenset({"folder", "extension", "file"})

# Digest window cadences (brief §4.1). ``None`` = fire per group_wait window.
DIGEST_WINDOWS: frozenset[str] = frozenset({"hourly", "daily"})


@dataclass(frozen=True)
class FileEvent:
    """One classified file transition — the pure input to rule matching.

    Mirrors the data ``scan.py`` already has in hand at classification time
    (brief §8.2): ``rel_path`` + item identity, plus (for ``modified``) the old
    and new content-hash so the hash-change gate can be evaluated without a DB
    round-trip. ``old_hash``/``new_hash`` are ``None`` when the hash policy did
    not compute one (``quick_only`` degradation, per T7) — the hash-change gate
    treats an unknown hash as "cannot prove a change", so it does not fire.
    """

    event_type: str  # one of EVENT_TYPES
    library_id: str
    rel_path: str
    old_hash: str | None = None
    new_hash: str | None = None

    def __post_init__(self) -> None:
        if self.event_type not in EVENT_TYPES:
            raise ValueError(f"unknown event_type {self.event_type!r}")


@dataclass(frozen=True)
class AlertRule:
    """A file-watch alert rule — mirrors the intended ``alert_rules`` DDL (§8.1).

    ``library_id=None`` scopes the rule to **all** libraries (DDL: nullable FK).
    ``path_glob=None``/``""`` matches every path. ``event_types`` is one or more
    of :data:`EVENT_TYPES`. ``hash_change_only`` is meaningful only for
    ``modified`` events (brief §2.6). The throttle fields
    (``group_wait_s``/``digest_window``/``repeat_interval_s``) are carried here
    for completeness but consumed by :mod:`filearr.alerts.windows`, not by
    ``match_rule``. ``threshold_count``/``threshold_window_s`` are populated only
    for ``is_system`` operational rules (brief §6.2/§6.4).
    """

    id: str
    name: str
    event_types: tuple[str, ...]
    enabled: bool = True
    is_system: bool = False
    library_id: str | None = None
    path_glob: str | None = None
    hash_change_only: bool = False
    group_by: tuple[str, ...] = GROUP_BY
    group_wait_s: int = 30
    digest_window: str | None = None
    repeat_interval_s: int | None = None
    threshold_count: int | None = None
    threshold_window_s: int | None = None

    def __post_init__(self) -> None:
        bad = set(self.event_types) - EVENT_TYPES
        if bad:
            raise ValueError(f"unknown event_types {sorted(bad)}")
        if not self.event_types:
            raise ValueError("event_types must be non-empty")
        # R1 base set always present; only the documented extras may be added.
        gb = set(self.group_by)
        if not set(GROUP_BY) <= gb or not gb <= set(GROUP_BY) | GROUP_BY_EXTRAS:
            raise ValueError(
                f"group_by must contain {GROUP_BY} plus only {sorted(GROUP_BY_EXTRAS)}; "
                f"got {self.group_by}"
            )
        if self.digest_window is not None and self.digest_window not in DIGEST_WINDOWS:
            raise ValueError(f"unknown digest_window {self.digest_window!r}")


@lru_cache(maxsize=1024)
def _compile_glob(pattern: str) -> GitIgnoreSpec:
    """Compile (and cache) a single gitignore-syntax glob into a spec.

    Cached because ``match_rule`` is called per file per rule during a scan; the
    implementing task (P8-T5) loads the enabled rule set once per scan run and
    reuses these compiled specs across every file.
    """
    return GitIgnoreSpec.from_lines([pattern])


def _path_matches(path_glob: str | None, rel_path: str) -> bool:
    """True if ``rel_path`` matches ``path_glob`` (None/empty = match all)."""
    if not path_glob:
        return True
    return _compile_glob(path_glob).match_file(rel_path)


def match_rule(rule: AlertRule, event: FileEvent) -> bool:
    """Pure predicate: does ``event`` satisfy ``rule``? (brief §2.6, §8.2)

    Gates, in cheap-to-expensive order:

    1. **enabled** — a disabled rule never matches.
    2. **event type** — ``event.event_type`` must be in ``rule.event_types``.
    3. **library scope** — ``rule.library_id is None`` (all libraries) or equals
       ``event.library_id``.
    4. **hash-change gate** — when ``hash_change_only`` and the event is
       ``modified``, both hashes must be known **and** differ. For non-modified
       events the gate is a no-op (it is only meaningful for ``modified``).
    5. **path glob** — the ``pathspec`` match (evaluated last; it is the
       priciest check).

    No side effects, no I/O — a scan can call this in its hot loop.
    """
    if not rule.enabled:
        return False
    if event.event_type not in rule.event_types:
        return False
    if rule.library_id is not None and rule.library_id != event.library_id:
        return False
    if rule.hash_change_only and event.event_type == "modified":
        if event.old_hash is None or event.new_hash is None:
            return False
        if event.old_hash == event.new_hash:
            return False
    return _path_matches(rule.path_glob, event.rel_path)


def group_key(rule: AlertRule, event: FileEvent) -> tuple[str, str, str]:
    """The grouping/dedup tuple for ``(rule, event)`` per the fixed R1 vocabulary.

    Returns values in canonical :data:`GROUP_BY` order:
    ``(event_type, library_id, rule_id)``. The dispatch layer hashes this into
    ``alert_events.dedup_key`` (brief §4.2) so throttle/digest windowing
    operates per-group — e.g. "all ``modified`` events in library X under rule
    Y this window" is one key, not one per file. Extra keys (``folder`` /
    ``extension`` / ``file``) are computed by :func:`group_extras` and appended
    to the dedup key by the pipeline.
    """
    return (event.event_type, event.library_id, rule.id)


def group_extras(rule: AlertRule, event: FileEvent) -> dict[str, str]:
    """The rule's extra grouping values for this event (empty for an R1-only
    rule). ``folder`` = the first path segment of ``rel_path`` ("" at the root);
    ``extension`` = lower-cased extension without the dot; ``file`` = rel_path."""
    out: dict[str, str] = {}
    rel = event.rel_path or ""
    if "folder" in rule.group_by:
        norm = rel.replace("\\", "/")
        out["folder"] = norm.split("/", 1)[0] if "/" in norm else ""
    if "extension" in rule.group_by:
        name = rel.rsplit("/", 1)[-1]
        out["extension"] = name.rsplit(".", 1)[-1].lower() if "." in name else ""
    if "file" in rule.group_by:
        out["file"] = rel
    return out
