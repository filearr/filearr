"""Agent configuration groups: settings schema, tier validation, layered
resolution (W6-D2, rewritten for P13's config-group unification).

The pure, request-free core behind the remote-configuration channel (config
groups, ``docs/ops/agents.md`` § "Configuration groups"):

* :class:`GroupSettings` / :func:`validate_settings` — typed, versioned v1
  validation of a config group's ``settings`` object. Unlike the *policy* half of
  the same group (:mod:`filearr.policy`, which PRESERVES unknown keys for
  forward-compat), ``settings`` REJECTS unknown top-level keys
  (``extra='forbid'`` → 422) so an operator typo never silently no-ops.
* :func:`validate_tiers` — the ≤5-tier percent/delay schedule a phased rollout
  publishes through.
* :func:`agent_bucket` / :func:`rollout_active_percent` — the stable, storage-free
  coverage function that decides which agents a running rollout has reached.
* :func:`resolve_effective_config` — the LAYERED merge: Global plus the agent's
  member groups in ascending ``priority``, later groups overriding only the keys
  they set, composed into the FROZEN wire document an agent already parses.

Path specs (``scan_selections[].paths``) MAY carry env tokens
(``%USERPROFILE%``, ``$HOME``, ``~``) and glob segments (``/home/*/documents``);
central VALIDATES SYNTAX ONLY (non-empty, balanced brackets/braces) and NEVER
resolves a path — final resolution is agent-side per-OS (W6-R1). Regexes are
compiled here as a Python ``re`` sanity gate only; the authoritative match engine
is the Go agent's RE2/``regexp`` (documented divergence: a pattern valid in one
is not guaranteed valid in the other, but the ``re`` gate catches the common
typo class).
"""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from typing import Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
    model_validator,
)
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from filearr.models import (
    AgentConfigGroup,
    AgentConfigGroupMember,
    AgentConfigGroupVersion,
    AgentConfigRollout,
)
from filearr.schedule import InvalidCronError, validate_cron

# --- W6-R1 preset vocabulary (docs/research/agent-inventory-presets.md §5) ---
#: The named folder-selection presets the distributed agent offers. ``custom`` is
#: the empty admin-defined scaffold. These validate ``scan_selections[].preset``;
#: they are NOT the central exclusion bundles (``filearr.presets.PRESET_BUNDLES``
#: — a separate, reused vocabulary the agent applies on top).
SCAN_PRESET_NAMES: frozenset[str] = frozenset(
    {
        "user-documents",
        "user-media",
        "user-profiles-full",
        "downloads",
        "server-data",
        "custom",
    }
)

LOG_LEVELS = ("error", "warn", "info", "verbose", "debug")


# --- Inventory-collector CATALOGUE (2026-08-10) -----------------------------
#: What the console renders as a checkbox list instead of asking an operator to
#: type collector names from memory.
#:
#: This is deliberately a UI catalogue and NOT a validation whitelist. The stored
#: vocabulary stays free-form on purpose (see :class:`InventoryConfig`): a newer
#: agent build may register a collector this central release has never heard of,
#: and refusing it would make central the thing that blocks an agent upgrade.
#: So: describe what we know, keep accepting what we do not, and let the console
#: union these entries with whatever the fleet actually advertises in its
#: ``capabilities.inventory_collectors`` — a collector nobody has described still
#: shows up, just without prose.
#:
#: Names mirror the agent's registry (agent/internal/inventory/collector.go
#: DefaultRegistry -> stat / owner / perms / placeholder). Keep them in step; a
#: name that matches nothing on the agent is silently inert, which is exactly the
#: failure this catalogue exists to prevent.
COLLECTOR_CATALOGUE: tuple[dict[str, Any], ...] = (
    {
        "name": "stat",
        "label": "File stat",
        "description": (
            "Size, timestamps and basic file attributes. The cheapest collector "
            "and the one every other inventory answer is built on."
        ),
        "platforms": ["linux", "darwin", "windows"],
        "cost": "low",
    },
    {
        "name": "owner",
        "label": "Owner / group",
        "description": (
            "The file's owning user and group. On POSIX these are uid/gid "
            "resolved to names; on Windows the owning security principal."
        ),
        "platforms": ["linux", "darwin", "windows"],
        "cost": "low",
    },
    {
        "name": "perms",
        "label": "Permissions",
        "description": (
            "Permission bits / ACL summary for each file. More expensive than "
            "stat because it may need a second syscall per entry, and on Windows "
            "an ACL read. See the Permissions section for the detailed W7 config."
        ),
        "platforms": ["linux", "darwin", "windows"],
        "cost": "medium",
    },
    {
        "name": "permissions",
        "label": "Permissions (full ACL)",
        "description": (
            "The full normalized permission record per entry: owner, group and "
            "every allow/deny ACE with its native mask, inheritance flags and a "
            "fidelity stamp (Linux: mode bits + POSIX ACL xattrs, cifs-without-"
            "cifsacl mounts flagged as synthesized; Windows: owner + full DACL "
            "via the security descriptor, local and UNC). Central stores each "
            "record in permission_snapshots (digest-gated, N per path) and the "
            "Reports page gains 'Permissions: explicit grants by principal' and "
            "'Permissions: broad write access'. Distinct from 'perms' (summary "
            "bits). macOS (agent >= 1.5.3): named ACL entries + BSD flags via "
            "ls -led; a TCC/Full-Disk-Access denial surfaces as a collector "
            "error. Costs one xattr/security-descriptor/ls read per entry."
        ),
        "platforms": ["linux", "darwin", "windows"],
        "cost": "medium",
    },
    {
        "name": "placeholder",
        "label": "Cloud placeholder state",
        "description": (
            "Whether a file is a cloud placeholder (OneDrive / Files On-Demand "
            "and friends) rather than resident on disk. Windows reports this "
            "natively; elsewhere the collector is a no-op that reports nothing."
        ),
        "platforms": ["windows"],
        "cost": "low",
    },
)

#: Just the names, for callers that only need membership.
KNOWN_COLLECTORS: frozenset[str] = frozenset(c["name"] for c in COLLECTOR_CATALOGUE)

# --- Size / count caps (payload-size guards) --------------------------------
#: Whole-settings byte ceiling (compact JSON). Mirrors the per-agent policy cap.
MAX_SETTINGS_BYTES = 65536
MAX_SCAN_SELECTIONS = 100
MAX_PATHS_PER_SELECTION = 200
MAX_REGEX_PER_SELECTION = 200
MAX_PATH_LEN = 4096
MAX_REGEX_LEN = 4096
MAX_COLLECTORS = 64
MAX_COLLECTOR_LEN = 128
# --- W7 permissions-collector config caps (docs/research/permissions-*.md §4) --
#: ``exclude_principals`` reuses the collectors list's count/length posture (§4).
MAX_EXCLUDE_PRINCIPALS = MAX_COLLECTORS
MAX_EXCLUDE_PRINCIPAL_LEN = MAX_COLLECTOR_LEN
#: ``watch_paths`` mirrors ``ScanSelection.paths`` (count + MAX_PATH_LEN + balance).
MAX_WATCH_PATHS = MAX_PATHS_PER_SELECTION
#: Per-path snapshot-history depth bound (``retain_snapshots``, §5 retention).
MAX_RETAIN_SNAPSHOTS = 1000


#: The settings keys :func:`compose_document` LIFTS to top-level policy keys on
#: delivery (so agent binaries need no change to honour a per-group gate).
#: Single source of truth — the admin effective-config view derives per-key
#: provenance from the same tuple, so the two can never drift.
LIFTED_LOCAL_KEYS: tuple[str, ...] = (
    "web_ui_enabled",
    "local_access_enabled",
    "auth_required",
)


class GroupSettingsValidationError(ValueError):
    """A config group ``settings`` object that fails v1 validation (→ 422)."""


def _check_balanced(spec: str) -> None:
    """Raise ``ValueError`` if glob brackets ``[]`` / braces ``{}`` are unbalanced.

    A minimal syntax gate — central never resolves the path, so this only catches
    the obvious ``/home/[user`` / ``/data/{a,b`` typo class before the spec is
    shipped to the agent (whose per-OS resolver is the authority)."""
    depth_brace = 0
    depth_brack = 0
    for ch in spec:
        if ch == "{":
            depth_brace += 1
        elif ch == "}":
            depth_brace -= 1
        elif ch == "[":
            depth_brack += 1
        elif ch == "]":
            depth_brack -= 1
        if depth_brace < 0 or depth_brack < 0:
            raise ValueError(f"unbalanced glob brackets/braces in path spec: {spec!r}")
    if depth_brace != 0 or depth_brack != 0:
        raise ValueError(f"unbalanced glob brackets/braces in path spec: {spec!r}")


def _validate_regex_list(v: list[str], field: str) -> list[str]:
    if len(v) > MAX_REGEX_PER_SELECTION:
        raise ValueError(f"{field} has {len(v)}; max {MAX_REGEX_PER_SELECTION}")
    for i, pat in enumerate(v):
        if not isinstance(pat, str) or not pat.strip():
            raise ValueError(f"{field}[{i}] must be a non-empty regex string")
        if len(pat) > MAX_REGEX_LEN:
            raise ValueError(f"{field}[{i}] exceeds {MAX_REGEX_LEN} chars")
        try:
            re.compile(pat)  # Python `re` sanity gate; agent uses Go RE2.
        except re.error as err:
            raise ValueError(f"{field}[{i}] is not a valid regex: {err}") from err
    return v


class ScanSelection(BaseModel):
    """One path selection an agent walks (W6-D2). A ``preset`` names a W6-R1
    folder set the agent resolves per-OS; ``paths`` are explicit path specs (env
    tokens + glob segments, syntax-validated only); ``include_regex`` /
    ``exclude_regex`` refine matches. ``enabled`` gates the whole selection.

    Either ``preset`` or ``paths`` (or both) is expected in practice, but an
    all-empty selection is NOT rejected — an operator may stage a disabled
    scaffold (``enabled: false``) before filling it in."""

    model_config = ConfigDict(extra="forbid")

    preset: str | None = None
    paths: list[str] = Field(default_factory=list)
    include_regex: list[str] = Field(default_factory=list)
    exclude_regex: list[str] = Field(default_factory=list)
    enabled: bool = True

    @field_validator("preset")
    @classmethod
    def _known_preset(cls, v: str | None) -> str | None:
        if v is not None and v not in SCAN_PRESET_NAMES:
            raise ValueError(
                f"unknown preset {v!r}; one of {sorted(SCAN_PRESET_NAMES)}"
            )
        return v

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_PATHS_PER_SELECTION:
            raise ValueError(f"paths has {len(v)}; max {MAX_PATHS_PER_SELECTION}")
        for i, spec in enumerate(v):
            if not isinstance(spec, str) or not spec.strip():
                raise ValueError(f"paths[{i}] must be a non-empty path spec")
            if len(spec) > MAX_PATH_LEN:
                raise ValueError(f"paths[{i}] exceeds {MAX_PATH_LEN} chars")
            _check_balanced(spec)
        return v

    @field_validator("include_regex")
    @classmethod
    def _valid_include(cls, v: list[str]) -> list[str]:
        return _validate_regex_list(v, "include_regex")

    @field_validator("exclude_regex")
    @classmethod
    def _valid_exclude(cls, v: list[str]) -> list[str]:
        return _validate_regex_list(v, "exclude_regex")


class AuditConfig(BaseModel):
    """Change-audit knobs for the ``permissions`` collector (W7, brief §5).

    Gates the (agent-side, scaffold) snapshot-diff + alert routing. Central
    VALIDATES + STORES this ahead of the collector; nothing consumes it yet (the
    collector + diff ingestion are agent-side/W7-T6+ scaffold). Default OFF."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    retain_snapshots: int = 10  # per-path snapshot-history depth, bounded
    alert_on_change: bool = False
    watch_paths: list[str] = Field(default_factory=list)  # path specs, syntax-only

    @field_validator("retain_snapshots")
    @classmethod
    def _valid_retain(cls, v: int) -> int:
        if v < 1 or v > MAX_RETAIN_SNAPSHOTS:
            raise ValueError(f"retain_snapshots must be 1..{MAX_RETAIN_SNAPSHOTS}")
        return v

    @field_validator("watch_paths")
    @classmethod
    def _valid_watch_paths(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_WATCH_PATHS:
            raise ValueError(f"watch_paths has {len(v)}; max {MAX_WATCH_PATHS}")
        for i, spec in enumerate(v):
            if not isinstance(spec, str) or not spec.strip():
                raise ValueError(f"watch_paths[{i}] must be a non-empty path spec")
            if len(spec) > MAX_PATH_LEN:
                raise ValueError(f"watch_paths[{i}] exceeds {MAX_PATH_LEN} chars")
            _check_balanced(spec)
        return v


class PermissionsConfig(BaseModel):
    """Detailed knobs for the ``permissions`` collector (W7, brief §4).

    Sibling to :attr:`InventoryConfig.collectors`: only takes effect when
    ``"permissions"`` is ALSO named in ``inventory.collectors`` (defense in
    depth — an admin must both name the collector and configure it). This block
    is ADDITIVE + validated so an operator can author config ahead of the
    collector, but nothing runs yet — the consuming collector is agent-side
    scaffold. Exclusion defaults (``exclude_well_known=True`` /
    ``include_inherited=False``) make a first-run report highlight only explicit,
    non-baseline grants; ``include_effective_access`` is reserved for v2 (§3.5)
    and is a no-op until shipped. Default OFF (opt-in)."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    resolve_names: bool = True  # best-effort SID/uid -> display name
    include_inherited: bool = False  # explicit/non-inherited ACEs only by default
    include_effective_access: bool = False  # reserved v2; agent no-ops until shipped
    exclude_well_known: bool = True  # SYSTEM/Administrators/root/Everyone/CREATOR OWNER
    exclude_principals: list[str] = Field(default_factory=list)  # canonical_id strings
    collect_share_acls: bool = False  # optional Windows-only share-level ACL (§2.1)
    audit: AuditConfig | None = None

    @field_validator("exclude_principals")
    @classmethod
    def _valid_exclude_principals(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_EXCLUDE_PRINCIPALS:
            raise ValueError(
                f"exclude_principals has {len(v)}; max {MAX_EXCLUDE_PRINCIPALS}"
            )
        for i, name in enumerate(v):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"exclude_principals[{i}] must be a non-empty string")
            if len(name) > MAX_EXCLUDE_PRINCIPAL_LEN:
                raise ValueError(
                    f"exclude_principals[{i}] exceeds {MAX_EXCLUDE_PRINCIPAL_LEN} chars"
                )
        return v


class InventoryConfig(BaseModel):
    """Per-group inventory-collector toggle (W6-D2). ``collectors`` are FREE
    strings (W6-D3 defines the vocabulary; central does NOT hard-code it) — only
    length/count-capped so a hostile/buggy payload cannot bloat the row.

    ``permissions`` (W7) is the optional detailed config for the ``permissions``
    collector, a typed sibling to the free-string ``collectors`` list (which
    carries no config payload) — mirroring how ``scan_selections`` is its own
    typed field, not shoehorned into a string list. Optional/None = absent."""

    model_config = ConfigDict(extra="forbid")

    enabled: bool = False
    collectors: list[str] = Field(default_factory=list)
    permissions: PermissionsConfig | None = None
    # 2026-08-20: central-side scheduling. When ``schedule_cron`` is set (and
    # ``enabled``), the minutely worker tick enqueues an inventory COMMAND for
    # every member agent on the cron's occurrences — the same command an
    # operator fires by hand, so the whole existing pipeline (poll, run,
    # results, permission ingest, drift alert) is reused. ``paths`` /
    # ``preset`` say what to walk (at least one is required with a schedule;
    # path specs use the agent's ``pathspec`` grammar, e.g. ``D:\\`` or
    # ``home_glob:*``).
    schedule_cron: str | None = None
    # 2026-08-23: which CLOCK ``schedule_cron`` is read against. None/absent =
    # UTC (the original contract, so stored documents keep their meaning), an
    # IANA zone name (e.g. "America/Chicago") = that zone's wall clock,
    # DST-aware, or the sentinel "agent" = each member agent's own local time
    # (from the ``utc_offset_minutes`` it self-reports on every command poll;
    # an agent build that does not report one runs the schedule on UTC).
    schedule_tz: str | None = None
    paths: list[str] = Field(default_factory=list)
    preset: str | None = None

    @field_validator("schedule_cron")
    @classmethod
    def _valid_schedule_cron(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from filearr.schedule import InvalidCronError, validate_cron

        if not isinstance(v, str) or not v.strip():
            raise ValueError("inventory.schedule_cron must be a non-empty cron expression")
        try:
            validate_cron(v.strip())
        except InvalidCronError as err:
            raise ValueError(f"invalid inventory.schedule_cron: {err}") from err
        return v.strip()

    @field_validator("schedule_tz")
    @classmethod
    def _valid_schedule_tz(cls, v: str | None) -> str | None:
        if v is None:
            return None
        from filearr.schedule import validate_schedule_tz

        if not isinstance(v, str) or not v.strip():
            raise ValueError(
                "inventory.schedule_tz must be 'agent' or an IANA timezone (omit for UTC)"
            )
        try:
            validate_schedule_tz(v.strip())
        except ValueError as err:
            raise ValueError(f"invalid inventory.schedule_tz: {err}") from err
        return v.strip()

    @field_validator("paths")
    @classmethod
    def _valid_paths(cls, v: list[str]) -> list[str]:
        if len(v) > 200:
            raise ValueError(f"inventory.paths has {len(v)}; max 200")
        for i, p in enumerate(v):
            if not isinstance(p, str) or not p.strip():
                raise ValueError(f"inventory.paths[{i}] must be a non-empty string")
            if len(p) > 1024:
                raise ValueError(f"inventory.paths[{i}] exceeds 1024 chars")
        return v

    @model_validator(mode="after")
    def _schedule_needs_roots(self) -> InventoryConfig:
        # 2026-08-25: a schedule with neither paths nor a preset is allowed --
        # the run walks the agent's own scan roots (its libraries' root paths,
        # which central knows). See ``inventory_roots_for_agent``.
        if self.schedule_tz and not self.schedule_cron:
            raise ValueError(
                "inventory.schedule_tz has no effect without inventory.schedule_cron"
            )
        return self

    @field_validator("collectors")
    @classmethod
    def _valid_collectors(cls, v: list[str]) -> list[str]:
        if len(v) > MAX_COLLECTORS:
            raise ValueError(f"collectors has {len(v)}; max {MAX_COLLECTORS}")
        for i, name in enumerate(v):
            if not isinstance(name, str) or not name.strip():
                raise ValueError(f"collectors[{i}] must be a non-empty string")
            if len(name) > MAX_COLLECTOR_LEN:
                raise ValueError(f"collectors[{i}] exceeds {MAX_COLLECTOR_LEN} chars")
        return v


class GroupSettings(BaseModel):
    """The typed, versioned v1 config-group ``settings`` object. Unknown top-level
    keys are REJECTED (``extra='forbid'`` → 422) so an operator typo never
    silently no-ops (contrast the per-agent policy body, which preserves unknown
    keys for forward-compat)."""

    model_config = ConfigDict(extra="forbid")

    log_level: Literal["error", "warn", "info", "verbose", "debug"] | None = None
    scan_selections: list[ScanSelection] | None = None
    inventory: InventoryConfig | None = None
    scan_schedule_cron: str | None = None
    # Local-surface gates per CONFIG GROUP (user request 2026-07-27). None =
    # inherit (let a lower-priority group, or the agent default, supply this).
    # Delivery lifts these to the TOP-LEVEL policy keys the agent's P7-T4 gate
    # already reads — see compose_document for the settings-wins tie-break — so
    # agent binaries need no change.
    web_ui_enabled: bool | None = None
    local_access_enabled: bool | None = None
    auth_required: bool | None = None

    @field_validator("scan_selections")
    @classmethod
    def _cap_selections(
        cls, v: list[ScanSelection] | None
    ) -> list[ScanSelection] | None:
        if v is not None and len(v) > MAX_SCAN_SELECTIONS:
            raise ValueError(
                f"scan_selections has {len(v)}; max {MAX_SCAN_SELECTIONS}"
            )
        return v

    @field_validator("scan_schedule_cron")
    @classmethod
    def _valid_cron(cls, v: str | None) -> str | None:
        if v is None:
            return v
        if not v.strip():
            raise ValueError("scan_schedule_cron must be a non-empty cron expression")
        try:
            validate_cron(v)
        except InvalidCronError as err:
            raise ValueError(f"invalid scan_schedule_cron: {err}") from err
        return v


async def inventory_roots_for_agent(session: Any, agent_id: Any) -> list[str]:
    """The agent's scan roots as central knows them: the ``root_path`` of every
    library the agent replicates (agent-local absolute paths such as ``D:\\``
    or ``/srv/share`` -- valid inventory path specs as-is). The default walk
    for an inventory run whose group names no ``paths``/``preset`` (2026-08-25:
    the first press of the console's inventory button 422'd on a group that
    had collectors but no paths, and the agent's roots were sitting right
    there in the libraries table)."""
    from sqlalchemy import select

    from filearr.models import Library

    rows = (
        await session.execute(
            select(Library.root_path)
            .where(Library.source_agent_id == agent_id)
            .order_by(Library.root_path)
        )
    ).all()
    return [r[0] for r in rows if r[0]]


def inventory_command_payload(inv: dict[str, Any], *, scheduled: bool) -> dict[str, Any]:
    """The ``inventory`` command payload for a group's merged ``inventory``
    block -- ONE builder for the schedule tick and the run-it-now endpoint so
    a manual run is byte-for-byte the run the schedule would have fired.
    ``scheduled`` is the once-per-occurrence cursor marker the tick keys on
    (it must be False for a manual run or the next tick would treat it as a
    consumed occurrence)."""
    payload: dict[str, Any] = {
        "scheduled": scheduled,
        "collectors": list(inv.get("collectors") or []),
        "paths": list(inv.get("paths") or []),
    }
    if inv.get("preset"):
        payload["preset"] = inv["preset"]
    return payload


def settings_json_len(settings: Any) -> int:
    """Compact-JSON byte length of a settings object (the oversize gate's measure)."""
    return len(
        json.dumps(settings, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


def validate_settings(settings: Any) -> None:
    """Validate a config group ``settings`` object (W6-D2).

    Raises :class:`GroupSettingsValidationError` when ``settings`` is not a JSON
    object, exceeds :data:`MAX_SETTINGS_BYTES`, carries an unknown top-level key,
    or a known key violates its bound (bad preset / regex / cron / oversize). The
    caller stores the ORIGINAL ``settings`` verbatim — this is a gate, not a
    transform."""
    if not isinstance(settings, dict):
        raise GroupSettingsValidationError("settings must be a JSON object")
    if settings_json_len(settings) > MAX_SETTINGS_BYTES:
        raise GroupSettingsValidationError(
            f"settings exceeds {MAX_SETTINGS_BYTES} bytes"
        )
    try:
        GroupSettings(**settings)
    except ValidationError as err:
        raise GroupSettingsValidationError(_summarise(err)) from err


def _summarise(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ())) or "settings"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid settings"


# --------------------------------------------------------------------------- #
# Phased-rollout tiers                                                          #
# --------------------------------------------------------------------------- #
#: Hard ceiling on tiers in one rollout. Five is the design's number and it is a
#: UI decision as much as a storage one: a schedule an operator cannot hold in
#: their head is a schedule they will not supervise.
MAX_ROLLOUT_TIERS = 5


class RolloutValidationError(ValueError):
    """A malformed tier schedule (→ 422 at the API)."""


def validate_tiers(tiers: Any) -> list[dict[str, int]]:
    """Validate + normalise a phased-rollout tier list.

    Shape: 1..:data:`MAX_ROLLOUT_TIERS` entries of
    ``{"percent": 1..100, "delay_minutes": >= 0}``, percents STRICTLY ASCENDING,
    and the LAST percent exactly 100.

    The last-must-be-100 rule is deliberate, and the alternative was considered:
    a rollout that stops at 60% would leave the group permanently split between
    two documents with nothing in the schema recording that this was intentional.
    "Publish to a subset and stop" is an edit to a narrower group, not a rollout.

    ``delay_minutes`` of tier N is the wait after tier N-1 ACTIVATED; tier 0's
    delay counts from the rollout's start (so ``delay_minutes: 0`` on tier 0
    means "go live at the next tick"). Returns the normalised list (ints, only
    the two known keys) — the caller stores THAT, not the raw input."""
    if not isinstance(tiers, list) or not tiers:
        raise RolloutValidationError("tiers must be a non-empty list")
    if len(tiers) > MAX_ROLLOUT_TIERS:
        raise RolloutValidationError(
            f"tiers has {len(tiers)} entries; max {MAX_ROLLOUT_TIERS}"
        )
    out: list[dict[str, int]] = []
    previous = 0
    for i, tier in enumerate(tiers):
        if not isinstance(tier, dict):
            raise RolloutValidationError(f"tiers[{i}] must be an object")
        percent = tier.get("percent")
        delay = tier.get("delay_minutes", 0)
        # bool is an int subclass; a JSON `true` here is a typo, not a percent.
        if not isinstance(percent, int) or isinstance(percent, bool):
            raise RolloutValidationError(f"tiers[{i}].percent must be an integer")
        if not 1 <= percent <= 100:
            raise RolloutValidationError(f"tiers[{i}].percent must be 1..100")
        if percent <= previous:
            raise RolloutValidationError(
                f"tiers[{i}].percent ({percent}) must be greater than the previous "
                f"tier's ({previous}) — tiers only ever widen coverage"
            )
        if not isinstance(delay, int) or isinstance(delay, bool) or delay < 0:
            raise RolloutValidationError(
                f"tiers[{i}].delay_minutes must be an integer >= 0"
            )
        previous = percent
        out.append({"percent": percent, "delay_minutes": delay})
    if out[-1]["percent"] != 100:
        raise RolloutValidationError(
            "the last tier must be 100 — a rollout always finishes fleet-wide; "
            "to configure a subset permanently, edit a narrower group instead"
        )
    return out


def agent_bucket(agent_id: uuid.UUID) -> int:
    """The agent's stable rollout bucket, 0..99.

    ``sha256(agent.id.bytes)[:4]`` big-endian, modulo 100. Deterministic (the
    same agent is always in the same bucket, across restarts and re-deploys),
    uniform (a UUIDv7's time prefix would NOT be — consecutively-enrolled agents
    would land in the same slice and a 10% tier would mean "the ten machines
    installed on the same afternoon"), and free of any extra storage: nothing
    records which agents a tier covers, so a fleet that grows mid-rollout keeps
    a uniform slice with no backfill."""
    return int.from_bytes(hashlib.sha256(agent_id.bytes).digest()[:4], "big") % 100


def rollout_covers(rollout: Any, agent_id: uuid.UUID) -> bool:
    """Whether a (config OR release) rollout currently covers ``agent_id``:
    running, a tier active, and the agent's bucket below its percent."""
    percent = rollout_active_percent(rollout)
    return percent is not None and agent_bucket(agent_id) < percent


def rollout_active_percent(rollout: Any) -> int | None:
    """The percentage a rollout currently covers, or ``None`` when it covers
    nobody (no rollout, not running, or running with no tier activated yet).

    Only a ``running`` rollout with ``current_tier >= 0`` covers anything. A
    ``scheduled`` rollout whose ``starts_at`` has passed covers nobody until the
    minute tick promotes it — at most a 60 s lag, and the alternative (deriving
    activation from wall-clock at read time) would mean the API and the worker
    each compute their own notion of "now" and disagree during the gap."""
    if rollout is None or rollout.status != "running" or rollout.current_tier < 0:
        return None
    tiers = rollout.tiers or []
    if rollout.current_tier >= len(tiers):
        return None
    percent = tiers[rollout.current_tier].get("percent")
    return percent if isinstance(percent, int) else None


# --------------------------------------------------------------------------- #
# Layered resolution (Global + member groups, ascending priority, last wins)    #
# --------------------------------------------------------------------------- #
@dataclass(frozen=True)
class GroupContribution:
    """One group's contribution to an agent's effective configuration."""

    group_id: uuid.UUID
    name: str
    priority: int
    is_system: bool
    #: The per-group version this agent actually received (``current_version``,
    #: or a rollout's ``target_version`` when this agent's bucket is covered).
    version_used: int
    #: The snapshot's global generation counter (``agent_config_group_versions.seq``).
    seq: int
    #: True when ``version_used`` came from a running rollout rather than
    #: ``current_version`` — i.e. this agent is an early adopter right now.
    via_rollout: bool
    settings: dict[str, Any]
    policy: dict[str, Any]


@dataclass(frozen=True)
class EffectiveConfig:
    """The resolved answer for one agent: the wire document plus everything the
    console needs to explain it."""

    #: The FROZEN wire document (merged policy keys at top level, ``group`` =
    #: merged settings, the lifted local-surface keys). The server-injected
    #: ``taxonomy_version`` is NOT part of it — see :func:`compose_document`.
    document: dict[str, Any]
    settings: dict[str, Any]  # the merged settings section on its own
    policy: dict[str, Any]  # the merged policy section on its own
    #: ``max(seq)`` over the contributing snapshots. Monotonic fleet-wide, so it
    #: is comparable between two agents in entirely different groups.
    generation: int
    #: ``sha256`` (12 hex chars) of the canonical document — the ETag's content
    #: half, so a rollback to a byte-identical document still re-validates.
    hash: str
    contributors: list[GroupContribution]
    #: ``"<section>.<key>" -> {group_id, group_name, version}`` for every key any
    #: group set. Sections are ``settings`` and ``policy``.
    provenance: dict[str, dict[str, Any]]


def compose_document(settings: dict[str, Any], policy: dict[str, Any]) -> dict[str, Any]:
    """Compose the FROZEN wire document from the two merged sections.

    The shape is exactly what agents have parsed since W6-D2, and it is frozen:
    the Go ``config.Policy`` struct must need zero changes.

    * merged ``policy`` keys sit at the TOP level (including unknown
      forward-compat keys, which pass through verbatim);
    * merged ``settings`` ride under a top-level ``group`` section;
    * the three :data:`LIFTED_LOCAL_KEYS` are additionally lifted OUT of settings
      to top-level keys the agent's P7-T4 gate already reads, so a group can gate
      its members' local web UI with no agent change.

    **Settings win the lift.** Pre-P13 the rule was a precedence dance between
    the winning policy SCOPE and the config group; with one layered document
    there are no scopes left to rank, so the tie-break is between the two
    sections of the same merged document. Settings win because ``settings`` is
    the typed, validated, console-rendered half — the half an operator edits with
    checkboxes — while the same key in ``policy`` is a raw JSON escape hatch. A
    ``None`` settings value means "inherit", so it never overrides anything.

    A ``group`` key authored inside the POLICY section is overwritten by the
    settings section: ``group`` IS the settings section now, so an operator
    "setting" it in raw policy JSON is authoring a shadow of the real thing."""
    doc: dict[str, Any] = {**policy, "group": settings}
    for key in LIFTED_LOCAL_KEYS:
        value = settings.get(key)
        if value is not None:
            doc[key] = value
    return doc


def canonical_hash(document: dict[str, Any]) -> str:
    """The document's content fingerprint (first 12 hex chars of sha256 over its
    canonical JSON). Sorted keys + compact separators so a semantically identical
    document always hashes the same regardless of dict insertion order."""
    payload = json.dumps(
        document, sort_keys=True, separators=(",", ":"), ensure_ascii=False
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


async def load_agent_groups(
    session: AsyncSession, agent_id: uuid.UUID
) -> list[AgentConfigGroup]:
    """Global + the agent's explicit member groups, in MERGE ORDER.

    Order is ``(priority, name, id)``: ascending priority with a deterministic
    tie-break, because equal priorities are legal (forcing uniqueness would make
    "insert a group between these two" a renumbering exercise) and two groups
    that both set a key must still resolve the same way on every poll."""
    q = (
        select(AgentConfigGroup)
        .outerjoin(
            AgentConfigGroupMember,
            (AgentConfigGroupMember.group_id == AgentConfigGroup.id)
            & (AgentConfigGroupMember.agent_id == agent_id),
        )
        .where(
            or_(
                AgentConfigGroup.is_system.is_(True),
                AgentConfigGroupMember.agent_id.is_not(None),
            )
        )
        .order_by(
            AgentConfigGroup.priority,
            AgentConfigGroup.name,
            AgentConfigGroup.id,
        )
    )
    return list((await session.execute(q)).scalars())


async def _live_rollouts(
    session: AsyncSession, group_ids: list[uuid.UUID]
) -> dict[uuid.UUID, AgentConfigRollout]:
    """The single live (scheduled/running) rollout per group, if any. One query
    for the whole group set — an agent in eight groups must not cost eight."""
    if not group_ids:
        return {}
    rows = (
        await session.execute(
            select(AgentConfigRollout).where(
                AgentConfigRollout.group_id.in_(group_ids),
                AgentConfigRollout.status.in_(("scheduled", "running")),
            )
        )
    ).scalars()
    return {r.group_id: r for r in rows}


async def resolve_effective_config(
    session: AsyncSession, agent: Any
) -> EffectiveConfig:
    """Resolve ``agent``'s effective configuration by LAYERED per-key merge.

    1. Load Global + the agent's member groups in ``(priority, name, id)`` order.
    2. For each group pick the version THIS agent gets: a running rollout whose
       active tier covers the agent's :func:`agent_bucket` hands over
       ``target_version``; everyone else gets ``current_version``.
    3. Merge each section (``settings``, ``policy``) key by key in that order —
       a later group overrides only the keys it sets. The merge is SHALLOW at the
       top level of each section: a nested object (``inventory``,
       ``scan_selections``) REPLACES wholesale rather than deep-merging, which
       matches how an operator reads "this group sets the inventory config" and
       avoids the un-explainable half-merged list.
    4. Compose the frozen wire document, the generation and the content hash.

    Always returns a complete answer — an agent with no groups at all still gets
    Global, and a fleet with no Global row (only possible mid-migration) resolves
    to an empty document rather than an error. An agent must never fail to get a
    configuration."""
    groups = await load_agent_groups(session, agent.id)
    rollouts = await _live_rollouts(session, [g.id for g in groups])
    bucket = agent_bucket(agent.id)

    # Pick each group's active version first, then fetch every snapshot in ONE
    # query (the alternative is a round-trip per group on every agent poll).
    wanted: list[tuple[AgentConfigGroup, int, bool]] = []
    for group in groups:
        version = group.current_version
        via_rollout = False
        percent = rollout_active_percent(rollouts.get(group.id))
        if percent is not None and bucket < percent:
            rollout = rollouts[group.id]
            # Only ever FORWARD: a rollout targeting an older version than the
            # published one would silently downgrade covered agents.
            if rollout.target_version > version:
                version = rollout.target_version
                via_rollout = True
        wanted.append((group, version, via_rollout))

    snapshots: dict[tuple[uuid.UUID, int], AgentConfigGroupVersion] = {}
    if wanted:
        rows = (
            await session.execute(
                select(AgentConfigGroupVersion).where(
                    AgentConfigGroupVersion.group_id.in_([g.id for g, _, _ in wanted])
                )
            )
        ).scalars()
        snapshots = {(r.group_id, r.version): r for r in rows}

    merged_settings: dict[str, Any] = {}
    merged_policy: dict[str, Any] = {}
    provenance: dict[str, dict[str, Any]] = {}
    contributors: list[GroupContribution] = []
    generation = 0

    for group, version, via_rollout in wanted:
        snap = snapshots.get((group.id, version))
        if snap is None:
            # Defensive: a group whose snapshot row is missing (only reachable
            # via direct DB surgery) falls back to its live authoring columns
            # rather than dropping out of the merge — a missing history row must
            # not silently un-configure a fleet.
            settings = dict(group.settings or {})
            policy = dict(group.policy or {})
            seq = 0
        else:
            settings = dict(snap.settings or {})
            policy = dict(snap.policy or {})
            seq = snap.seq
        generation = max(generation, seq)
        for section, source, target in (
            ("settings", settings, merged_settings),
            ("policy", policy, merged_policy),
        ):
            for key, value in source.items():
                target[key] = value
                provenance[f"{section}.{key}"] = {
                    "group_id": str(group.id),
                    "group_name": group.name,
                    "version": version,
                }
        contributors.append(
            GroupContribution(
                group_id=group.id,
                name=group.name,
                priority=group.priority,
                is_system=group.is_system,
                version_used=version,
                seq=seq,
                via_rollout=via_rollout,
                settings=settings,
                policy=policy,
            )
        )

    document = compose_document(merged_settings, merged_policy)
    return EffectiveConfig(
        document=document,
        settings=merged_settings,
        policy=merged_policy,
        generation=generation,
        hash=canonical_hash(document),
        contributors=contributors,
        provenance=provenance,
    )


def config_etag(generation: int, doc_hash: str, taxonomy_version: int) -> str:
    """The strong validator served with the policy document.

    ``"groups/<generation>/h:<hash>/t:<taxonomy_version>"``. Three independent
    components because three independent things invalidate a cache: ANY
    contributing group publishing (generation), the merged content changing
    (hash — which also catches a membership or priority change that moves no
    version number), and a taxonomy edit (the W8-E version gate). The Go client
    re-applies on any ETag change, so all three are equally effective."""
    return f'"groups/{generation}/h:{doc_hash}/t:{taxonomy_version}"'
