"""Agent policy-document validation (Phase 5, P5-T6; trimmed by P13).

The pure core behind the ``policy`` half of a configuration group's document:

* :class:`PolicyModel` / :func:`validate_policy` — additive v1 validation of the
  known policy keys (unknown keys are PRESERVED verbatim — an older central must
  never strip a newer agent's keys; §6.3).
* :func:`flatten_path_grants` — RBAC grants → the flattened ``path_scope``
  predicate list the agent applies locally.

HISTORY (P13, 2026-08-11): this module also owned the scope grammar
(``global`` | ``group:<name>`` | ``agent:<uuid>``) and ``resolve_effective_policy``,
a most-specific-wins resolution where the winning row supplied the WHOLE
document. Both are gone. There is exactly one grouping concept now — the
configuration group — and resolution is a per-key layered merge across an
agent's groups in priority order (:func:`filearr.agent_config.resolve_effective_config`).
What survives here is validation, which the group's ``policy`` section still
runs unchanged.
"""

from __future__ import annotations

import json
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from filearr import rbac
from filearr.presets import validate_preset_names

#: Offline-grace default for the local query surface (P7-T4 / research §5.2, R4).
#: This REUSES Phase-5's 24h reconciliation threshold — the same 24h value the Go
#: agent carries as ``config.DefaultOfflineGrace`` (== ``defaultReconcileInterval``
#: in ``agent/cmd/filearr-agent/config.go``). It is deliberately NOT a second
#: constant: past this window with no fresh policy, the agent web UI fails closed
#: while the CLI same-user path keeps answering. The operator may override it per
#: policy via ``offline_grace_seconds``.
DEFAULT_OFFLINE_GRACE_SECONDS = 86400

#: Upper bound on the number of flattened path-scope predicates a policy may carry
#: (payload-size guard; the whole policy is additionally 64KiB-capped at the API).
MAX_PATH_SCOPE_PREDICATES = 1000


# --------------------------------------------------------------------------- #
# Policy JSON validation (additive; unknown keys preserved)                    #
# --------------------------------------------------------------------------- #
class PolicyValidationError(ValueError):
    """A policy body that fails v1 validation (maps onto a 422 at the API)."""


class PolicyModel(BaseModel):
    """Validation gate for the KNOWN v1 policy keys — all optional. Unknown keys
    are allowed (``extra='allow'``) and PRESERVED (the row stores the ORIGINAL
    dict verbatim; this model only validates). Bounds mirror the frozen contract:
    presets against ``PRESET_BUNDLES``; ``content_hash_max_bytes >= 0``;
    ``reconcile_interval_seconds >= 300``; ``poll_interval_seconds`` 60..86400.

    P7-T4 adds the local-query-surface keys the Go agent consumes
    (``agent/internal/config``, research §5):

    * ``local_access_enabled`` (bool) — gates the CLI/local-API listener. Absent =
      agent default ON (a never-contacted agent keeps the CLI enabled). An explicit
      ``false`` persists through offline periods (it is cached).
    * ``web_ui_enabled`` (bool) — gates the local web UI (P7-T5). Absent = agent
      default OFF (a never-contacted agent starts web-UI-disabled). Fails closed
      when the cached policy is older than ``offline_grace_seconds``.
    * ``auth_required`` (bool) — whether the web UI demands the bootstrap token;
      absent = agent default ON. Never affects the CLI peer-credential check.
    * ``read_only`` (bool) — ALWAYS ``true``. The local surface is read-only by
      invariant; a ``false`` is REJECTED here (fail-closed, security > all).
      This is about the CATALOG and is unaffected by the ``local_*_control``
      permissions below, which delegate agent SELF-ADMINISTRATION only.
    * ``path_scope`` (list[str]) — the FLATTENED allow-list of ``rel_path`` GLOB
      predicates the agent applies as ``WHERE rel_path GLOB ?`` (OR-combined) to
      every local result set (R2: the agent consumes flattened predicates only, it
      never grows a rule evaluator). Operator-authored, or produced by
      :func:`flatten_path_grants` from RBAC grants. Empty/absent = unrestricted.
    * ``offline_grace_seconds`` (int) — the web-UI fail-closed grace window; absent
      = :data:`DEFAULT_OFFLINE_GRACE_SECONDS` (24h, R4).
    """

    model_config = ConfigDict(extra="allow")

    presets: list[str] | None = None
    include_globs: list[str] | None = None
    exclude_globs: list[str] | None = None
    content_hash_max_bytes: int | None = Field(default=None, ge=0)
    watch_mode: bool | None = None
    reconcile_interval_seconds: int | None = Field(default=None, ge=300)
    poll_interval_seconds: int | None = Field(default=None, ge=60, le=86400)

    # --- P7-T4 local query surface -----------------------------------------
    local_access_enabled: bool | None = None
    web_ui_enabled: bool | None = None
    auth_required: bool | None = None
    read_only: bool | None = None
    path_scope: list[str] | None = None
    offline_grace_seconds: int | None = Field(default=None, ge=0)

    # --- In-daemon scan scheduler (2026-08-03) ------------------------------
    # A lone `filearr-agent run` service self-schedules scans from these keys
    # (no external cron/Task Scheduler — a Windows re-install losing its
    # scheduled task silently froze a fleet member's catalog for nine days).
    # ``scan_cron`` (5-field, agent-local time) wins over
    # ``scan_interval_seconds`` when both are set; ``scan_on_start`` fires one
    # scan ~30s after daemon start. All absent = scheduler off (containers
    # keep their entrypoint loop; nothing double-scans).
    scan_cron: str | None = None
    scan_interval_seconds: int | None = Field(default=None, ge=300)
    scan_on_start: bool | None = None

    # --- P10-T4 agent staging-upload rate cap ------------------------------
    # ``upload_rate_bytes_per_sec`` (int >= 0) — the per-agent token-bucket
    # ceiling the Go agent applies to a ``stage_upload`` (research §2.4). 0 or
    # absent = UNLIMITED. The agent reads the cached value at upload START; a
    # mid-upload policy change takes effect on the NEXT upload (documented). This
    # is additive — the P7-T4 keys and their tests are untouched.
    upload_rate_bytes_per_sec: int | None = Field(default=None, ge=0)

    # --- agent self-update gate (2026-08-05) -------------------------------
    # ``auto_update`` — whether central OFFERS updates on this agent's periodic
    # update-manifest poll. Enforced SERVER-side (the poll answers 204 when
    # disabled), so it gates every agent build uniformly. Absent = TRUE
    # (preserves the historic always-offer behaviour). Staged binary rollout:
    # set it false in Global and true in a higher-priority group — the P13
    # layered merge overrides per KEY, so that narrower group carries this one
    # key and nothing else. An operator-triggered self_update command bypasses
    # the gate entirely (the click IS the authorization).
    auto_update: bool | None = None
    #: WHEN central offers updates (2026-08-18), layered on ``auto_update``:
    #: ``update_window`` = "<days> HH:MM-HH:MM [IANA zone]" (only inside the
    #: window; zone absent = central local); ``update_not_before`` = ISO-8601
    #: datetime (nothing offered before it; "release now" = unset). Both are
    #: enforced server-side on the manifest poll and bypassed by the operator's
    #: per-agent update action. Pure evaluation lives in filearr.update_gate.
    update_window: str | None = None
    update_not_before: str | None = None

    # --- agent-side content extraction (2026-08-09 parity pass) -------------
    # archive/docs/agent-parity-design.md §"Policy keys". The agent runs the
    # extraction pass itself and ships the result on its replication events
    # (``AgentEvent.extracted``); central never opens a remote file. Capability is
    # a HOST property, not a build property — an agent whose host lacks the tool
    # (tesseract for OCR, ffprobe for the media probe) logs the ignored key once
    # and the console flags it against the agent's advertised ``capabilities``.
    #: Run the agent-side extraction pass at all. Absent = agent default OFF, so
    #: enabling extraction is always a deliberate operator act (events grow).
    extract_enabled: bool | None = None
    #: Include document BODY TEXT in the extraction payload. Absent = OFF. This is
    #: the key that makes agent items chunkable/embeddable on content rather than
    #: filename alone, and also the one that makes events materially larger.
    extract_body_text: bool | None = None
    #: OCR images / scanned PDFs (``ocr_text`` in the payload). Absent = OFF.
    #: Requires ``tesseract`` on the agent host; an agent without it ignores this.
    extract_ocr: bool | None = None
    #: Deep EXIF for images (``exif.*`` keys). Absent = OFF, and deliberately so:
    #: central runs its own EXIF pass unconditionally, but on an agent the same
    #: pass costs one exiftool subprocess per image INSIDE the scan walk and sends
    #: GPS coordinates off the machine. Requires ``exiftool`` on the agent host.
    extract_exif: bool | None = None
    #: Skip files larger than this many bytes during the extraction pass (the
    #: identity half of the event is unaffected). Absent = the agent's built-in
    #: cap (32 MiB). 0 = extract nothing by size.
    extract_max_bytes: int | None = Field(default=None, ge=0)

    # --- local self-administration permissions (2026-08-10) ----------------
    # These delegate a slice of AGENT ADMINISTRATION to the operator sitting at
    # the machine, through the agent's own local web UI. They are deliberately
    # NOT about the catalog: ``read_only`` above keeps its exact meaning and the
    # local surface never mutates items or metadata, whatever these say.
    #
    # All three are absent = FALSE (matching the ``extract_*`` family), so an
    # existing fleet delegates nothing until an operator opts in.
    #
    # Precedence, and the reason it is not negotiable: a key CENTRAL EXPLICITLY
    # SET is locked in the local UI. Central re-applies its document on every
    # policy poll, so a local edit to the same key would silently revert within a
    # poll interval; the agent refuses the edit and names the owning scope
    # instead. Local editing may only fill in keys central left unset, which
    # yields the chain ``central policy > local override > FILEARR_AGENT_* env >
    # sidecar > built-in default``.
    #: Let the local UI pause/resume this agent's scanning and trigger a scan
    #: now. The local pause is a scan-only flag SEPARATE from the ``suspend``
    #: command (which also stops replication fleet-wide); both gate the
    #: scheduler, and a local resume can never lift a central suspend.
    local_scan_control: bool | None = None
    #: Let the local UI edit ``scan_cron`` / ``scan_interval_seconds`` /
    #: ``scan_on_start`` — but only the ones this policy does not itself set.
    local_schedule_control: bool | None = None
    #: Let the local UI add/remove this agent's scan roots (its ``scan.json``).
    #: Still refused when a config group's ``scan_selections`` derives the roots,
    #: since central would recompute a local edit away.
    local_roots_control: bool | None = None

    @field_validator("scan_cron")
    @classmethod
    def _valid_scan_cron(cls, v: str | None) -> str | None:
        if v is not None:
            from filearr.schedule import InvalidCronError, validate_cron

            try:
                validate_cron(v)
            except InvalidCronError as exc:
                raise ValueError(f"invalid scan_cron: {exc}") from exc
        return v

    @field_validator("update_window")
    @classmethod
    def _valid_update_window(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            from filearr.update_gate import parse_update_window

            try:
                parse_update_window(v)
            except ValueError as exc:
                raise ValueError(f"invalid update_window: {exc}") from exc
            return v.strip()
        return None if v is not None and not v.strip() else v

    @field_validator("update_not_before")
    @classmethod
    def _valid_update_not_before(cls, v: str | None) -> str | None:
        if v is not None and v.strip():
            from filearr.update_gate import parse_not_before

            try:
                parse_not_before(v)
            except ValueError as exc:
                raise ValueError(f"invalid update_not_before: {exc}") from exc
            return v.strip()
        return None if v is not None and not v.strip() else v

    @field_validator("presets")
    @classmethod
    def _known_presets(cls, v: list[str] | None) -> list[str] | None:
        if v is not None:
            unknown = validate_preset_names(v)
            if unknown:
                raise ValueError(f"unknown preset(s): {', '.join(sorted(unknown))}")
        return v

    @field_validator("read_only")
    @classmethod
    def _read_only_is_true(cls, v: bool | None) -> bool | None:
        # The local surface is read-only by invariant (research §3.4). Reject any
        # attempt to disable it rather than silently normalize — an operator asking
        # for a writable local surface is a policy error, not a preference.
        if v is False:
            raise ValueError(
                "read_only cannot be disabled — the local query surface is always read-only"
            )
        return v

    @field_validator("path_scope")
    @classmethod
    def _valid_path_scope(cls, v: list[str] | None) -> list[str] | None:
        if v is None:
            return v
        if len(v) > MAX_PATH_SCOPE_PREDICATES:
            raise ValueError(
                f"path_scope has {len(v)} predicates; max {MAX_PATH_SCOPE_PREDICATES}"
            )
        for i, pred in enumerate(v):
            if not isinstance(pred, str) or not pred.strip():
                raise ValueError(f"path_scope[{i}] must be a non-empty glob string")
        return v


def validate_policy(policy: Any) -> None:
    """Validate the KNOWN v1 keys of ``policy`` (unknown keys pass through).

    Raises :class:`PolicyValidationError` when ``policy`` is not a JSON object or
    a known key violates its bound. The caller stores the ORIGINAL ``policy``
    verbatim — this is a gate, not a transform."""
    if not isinstance(policy, dict):
        raise PolicyValidationError("policy must be a JSON object")
    try:
        PolicyModel(**policy)
    except ValidationError as err:
        raise PolicyValidationError(_summarise(err)) from err


def _summarise(err: ValidationError) -> str:
    parts = []
    for e in err.errors():
        loc = ".".join(str(x) for x in e.get("loc", ())) or "policy"
        parts.append(f"{loc}: {e.get('msg', 'invalid')}")
    return "; ".join(parts) or "invalid policy"


def policy_json_len(policy: Any) -> int:
    """Compact-JSON byte length of a policy (the oversize gate's measure)."""
    return len(
        json.dumps(policy, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    )


# --------------------------------------------------------------------------- #
# RBAC grant → flattened path-scope predicate list (R2, best-effort)           #
# --------------------------------------------------------------------------- #
class PathScopeFlattenError(ValueError):
    """A grant set that cannot be safely flattened into an OR-combined GLOB
    allow-list (a deny grant, or an irreversible hashed ltree label). Raised
    FAIL-CLOSED: central must never emit an over-broad scope in these cases —
    the operator should author the ``path_scope`` list explicitly instead."""


# A leading ``lib_<uuid.hex>`` ltree label (see :func:`rbac.library_label`).
_LIB_LABEL_RE = re.compile(r"^lib_[0-9a-f]{32}$")


def flatten_path_grants(
    grants: list[rbac.PathGrant],
    *,
    read_action: str = "search_metadata",
    strip_library_prefix: bool = True,
) -> list[str]:
    """Flatten a principal's RBAC path grants into the ``path_scope`` predicate
    list the agent applies locally (R2 — the agent never evaluates rules).

    This is a **minimal, best-effort** helper for the clean subset that maps onto
    an OR-combined ``rel_path`` GLOB allow-list. The local surface is read-only, so
    only grants for ``read_action`` (default ``search_metadata``) are relevant. For
    each such ALLOW grant the ltree-encoded ``PathGrant.path`` is decoded back to a
    ``rel_path`` and emitted as two globs — the exact path and its subtree
    ``<rel_path>/**`` — so both a file grant and a directory grant are covered.
    A library-root grant (only the ``lib_<uuid>`` label) becomes ``**`` (the whole
    library subtree).

    **Documented gaps** (grants do NOT map cleanly in these cases — the function
    fail-closes rather than emit a wrong scope; author ``path_scope`` by hand):

    * **Deny grants.** An explicit deny (``allow=False``) for ``read_action``
      cannot be expressed in a pure OR-allow list — it would require a local rule
      evaluator, which R2 forbids. Raises :class:`PathScopeFlattenError`.
    * **The library dimension is dropped.** The ``lib_<uuid>`` prefix is stripped
      (``strip_library_prefix``): the agent index is per-machine with its own roots
      and no central library_id, so multi-library grants collapse onto one
      rel_path space. A per-machine agent typically maps to one library's roots;
      push the right scope from the right config group.
    * **Hashed (over-long) ltree labels** are one-way (:data:`rbac.HASHED_LABEL`)
      and cannot be turned back into a glob. Raises :class:`PathScopeFlattenError`.
    * **GLOB metacharacters in a literal directory name** (a real dir literally
      named with ``*``/``?``/``[``) are not escaped — rare; author by hand if hit.
    """
    read_grants = [g for g in grants if g.action == read_action]
    predicates: set[str] = set()
    for g in read_grants:
        if not g.allow:
            raise PathScopeFlattenError(
                "cannot flatten a deny grant into an OR-allow path_scope list "
                f"(path={g.path!r}); author path_scope explicitly"
            )
        rel = _ltree_to_rel_path(g.path, strip_library_prefix=strip_library_prefix)
        if rel == "":
            predicates.add("**")  # whole (library) subtree
        else:
            predicates.add(rel)
            predicates.add(f"{rel}/**")
    return sorted(predicates)


def _ltree_to_rel_path(path: str, *, strip_library_prefix: bool) -> str:
    """Decode an ltree grant path back to a ``rel_path`` (empty = library root)."""
    labels = path.split(".")
    if strip_library_prefix and labels and _LIB_LABEL_RE.match(labels[0]):
        labels = labels[1:]
    segments: list[str] = []
    for label in labels:
        seg = rbac.decode_path_label(label)
        if seg == rbac.HASHED_LABEL:
            raise PathScopeFlattenError(
                f"grant path {path!r} contains a one-way hashed ltree label; "
                "author path_scope explicitly"
            )
        segments.append(seg)
    return "/".join(segments)
