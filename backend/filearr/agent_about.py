"""Per-agent About / dependency report (``GET /agents/{agent_id}/about``).

WHY this module exists (user request, 2026-08-11): "perform version checking on
dependencies on each agent like the central console in an about page",
"include the tools versions also", "pull the tool locations also".

:mod:`filearr.about` answers "which build is running HERE" by interrogating the
live central process — its Python, its Postgres, its Meilisearch, its host
tools. Until this module existed there was no equivalent question anyone could
ask an AGENT. The console knew one string about a remote machine's software,
``agent_version``, and that is a release stamp: it says nothing about the Go
toolchain that compiled the binary, the modules linked into it, the host OS, or
which copy of exiftool the service will actually execute.

**THE GOVERNING DIFFERENCE FROM about.py: nothing here is probed.** about.py
reads live numbers because it is running on the machine it is describing. This
module is describing a machine on the other side of a network that central
cannot — and by design must not — reach out to. So every fact here is an agent
SELF-REPORT, arriving on the 60-second command poll and stored verbatim in
``agents.capabilities``, and the honest framing of every value is "this is what
the agent said, as of ``capabilities_at``". The endpoint is pure assembly: no
network, no subprocess, one row read.

**ONE COMPARATOR, still.** The agent reports facts — version strings, paths —
and central alone judges them, through the same :mod:`filearr.toolversions`
registry and :mod:`filearr.versioncmp` comparator that judge central's own tools
on the About page. Nothing in the Go agent compares a version to a minimum, and
nothing in the frontend does either. A poppler called outdated on one page and
fine on another would discredit both at once, and the fastest way to arrive
there is a second implementation of "is 24.02.0 newer than 22.09.0".

**NEVER BLANK FOR UNKNOWN.** Same rule as about.py and about.ts. An agent that
has never advertised capabilities returns ``reported: False`` with empty
sections — not an error, and emphatically not zeros or seven ``absent`` chips,
which would state as observed fact something never observed. A tool present but
silent about its version is ``version: None`` with verdict ``unknown``, which
the console renders as "installed, version unknown".

**Everything in ``capabilities`` is third-party JSON** from a machine central
does not control, so every field is type-checked on the way out rather than
trusted. A malformed or hostile advertisement degrades to a missing section; it
never raises.
"""

from __future__ import annotations

from typing import Any

from filearr import agentsync, toolversions
from filearr.models import Agent

#: Cap on any single string copied out of an agent's advertisement. These are
#: untrusted strings from a remote machine that end up rendered in a browser and
#: pasted into bug reports; a version banner or a path has no length contract on
#: the wire. Matches the spirit of ``about._MAX_TEXT``.
_MAX_TEXT = 400

#: Cap on the module list central will echo back to the console. The agent
#: already caps itself at 200 (``maxModules`` in
#: ``agent/internal/inventory/modules.go``); this is the belt to that braces,
#: because the cap that matters is the one on the side that does not trust the
#: input.
_MAX_MODULES = 300

#: The fields of the agent's ``build`` block that are rendered, in the order the
#: console shows them. An allowlist rather than a passthrough: this dict comes
#: from a remote machine and a future agent adding a key must not be able to
#: inject an unbounded blob into every console page load.
_BUILD_FIELDS = (
    "go_version",
    "goos",
    "goarch",
    "os_version",
    "vcs_revision",
    "vcs_time",
    "vcs_modified",
    "main_version",
    "num_cpu",
)


def _text(value: Any) -> str | None:
    """A capped, whitespace-collapsed string, or ``None`` for anything that is
    not a usable string. Non-strings become ``None`` rather than ``str(value)``:
    rendering ``"None"`` or ``"{}"`` in a table cell is worse than an honest gap.
    """
    if not isinstance(value, str):
        return None
    flat = " ".join(value.split())
    return flat[:_MAX_TEXT] or None


def _dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def build_section(capabilities: dict | None) -> dict | None:
    """The agent's build provenance, or ``None`` when it reported none.

    ``None`` (rather than a dict of nulls) is what lets the console say "this
    agent's build is not reported — it predates the About channel, or it has not
    polled since" instead of drawing an empty table that looks like a fault.

    ``go_version`` is the toolchain that COMPILED the binary, not a pin in
    go.mod, and the console labels it "built with" for exactly the reason
    about.py separates a pyproject pin from an installed distribution's
    metadata: intent and fact are different questions and they disagree
    precisely when it matters.
    """
    raw = _dict(_dict(capabilities).get("build"))
    if not raw:
        return None
    out: dict[str, Any] = {}
    for key in _BUILD_FIELDS:
        value = raw.get(key)
        if value is None:
            continue
        if isinstance(value, bool):
            out[key] = value
        elif isinstance(value, int):
            out[key] = value
        elif (text := _text(value)) is not None:
            out[key] = text
    # Present the allowlisted keys uniformly: a field the agent did not send is
    # explicitly null, so the console renders "not reported" rather than having
    # to distinguish a missing key from a null one.
    return {key: out.get(key) for key in _BUILD_FIELDS} if out else None


def host_tool_rows(capabilities: dict | None) -> list[dict]:
    """One row per host tool the agent knows about, judged by central.

    Row shape mirrors :func:`filearr.about.host_tools` EXACTLY —
    ``{name, purpose, present, version, path, url, minimum_version, verdict,
    impact}`` — so the console's two tables are the same table with a different
    subject, and one component's cell logic (``about.ts`` / ``agentAbout.ts``)
    reads both.

    Order is :data:`filearr.toolversions.HOST_TOOLS`, then any tool the agent
    advertises that this central release has never heard of, sorted. Including
    the unknown ones is deliberate: a newer agent build that grows an eighth
    tool must show up here rather than vanish, and it lands on ``unknown``
    (no published minimum), which is exactly the right verdict for a tool we
    have no opinion about.

    ``path`` is the resolved absolute location on the agent host. It answers
    "which copy", which "present: true" cannot, and it is also the visible proof
    of an agent-side security rule: the agent resolves host tools only from
    machine-wide, admin-writable directories, because it runs as
    LocalSystem/root and executing a binary out of a user-writable path would be
    a local privilege-escalation. A path here under a user profile means that
    rule regressed.
    """
    caps = _dict(capabilities)
    tools = _dict(caps.get("tools"))
    versions = _dict(caps.get("tool_versions"))
    paths = _dict(caps.get("tool_paths"))

    names = list(toolversions.HOST_TOOLS) + sorted(
        n for n in tools if isinstance(n, str) and n not in toolversions.HOST_TOOLS
    )
    rows = []
    for name in names:
        present = tools.get(name) is True
        version = _text(versions.get(name))
        rows.append(
            {
                "name": name,
                "purpose": toolversions.tool_purpose(name),
                "present": present,
                "version": version,
                # Only a present tool can have a location. An agent that sent a
                # path for a tool it also reported absent is contradicting
                # itself; ``present`` is the field the capability matrix already
                # trusts, so it wins.
                "path": _text(paths.get(name)) if present else None,
                "url": toolversions.tool_url(name),
                "minimum_version": toolversions.minimum_version(name),
                "verdict": toolversions.tool_verdict(name, present, version),
                "impact": toolversions.minimum_impact(name),
            }
        )
    return rows


def module_rows(capabilities: dict | None) -> list[dict] | None:
    """The Go modules linked into the agent binary, or ``None`` if not reported.

    ``None`` and ``[]`` mean different things and the console renders them
    differently: ``None`` is "the agent did not send a module list" (an older
    build, or the payload budget — see ``modules_omitted``), while ``[]`` would
    claim the binary has no dependencies. Only the first is ever produced here.

    ``url`` is derived, never looked up: ``https://pkg.go.dev/<path>`` is
    deterministic for every public module, and this page — like the central
    About page — makes no outbound network calls. A private module path simply
    yields a link that 404s at pkg.go.dev, which is a better failure than a
    page that hangs on a box with no route to the internet.
    """
    raw = _dict(capabilities).get("modules")
    if not isinstance(raw, list):
        return None
    rows = []
    for entry in raw[:_MAX_MODULES]:
        if not isinstance(entry, dict):
            continue
        path = _text(entry.get("path"))
        if not path:
            continue
        rows.append(
            {
                "path": path,
                "version": _text(entry.get("version")),
                "url": f"https://pkg.go.dev/{path}",
            }
        )
    return rows or None


def extract_section(capabilities: dict | None) -> dict:
    """What this agent's extraction pass can do — the compact restatement of the
    capability advertisement that the About panel shows next to the tools.

    Kept separate from the chips row on the Agents page rather than replacing
    it: the chips are the at-a-glance answer scanned across a fleet, this is the
    detailed one read about a single machine.
    """
    caps = _dict(capabilities)
    formats = caps.get("formats")
    collectors = caps.get("inventory_collectors")
    schema = caps.get("extract_schema")
    return {
        "schema": schema if isinstance(schema, int) else None,
        "formats": [f for f in formats if isinstance(f, str)] if isinstance(formats, list) else [],
        "collectors": (
            [c for c in collectors if isinstance(c, str)] if isinstance(collectors, list) else []
        ),
        "inventory_version": (
            caps.get("inventory_version")
            if isinstance(caps.get("inventory_version"), int)
            else None
        ),
    }


def _int(value: Any) -> int | None:
    """A non-negative int, or ``None``. ``bool`` is rejected explicitly because
    it is an ``int`` subclass in Python and ``True`` rendering as ``1`` in a
    counter column would be an invented fact. Negatives are rejected for the same
    reason: every value here is a count or a byte size."""
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        return None
    return value


def rehash_section(health: dict | None) -> dict | None:
    """The agent's quick_hash migration state (QH-T6), or ``None`` when it has
    never run a sweep.

    **Why this reads HEALTH and not CAPABILITIES**, unlike everything else in
    this module: capabilities is a slow-moving statement about what an agent CAN
    do, while this changes minute by minute during a multi-hour sweep, and health
    is the block the agent re-evaluates on every poll.

    **Why the console needs it at all.** This is the ONLY place the migration's
    state is visible, and central cannot derive it from the catalogue. There is
    no hash-provenance column for agent-owned rows — ``agentsync.apply_batch``
    never writes ``policy_version`` — so no query central can write distinguishes
    a stale agent quick_hash from a correct one. The agent's own cursor is the
    entire completion signal, and this is the wire it arrives on.

    ``None`` (rather than a dict of zeros) is what lets the console say "not run"
    instead of drawing a converged-looking report of a sweep that never happened.
    Same rule as :func:`build_section`, and the same reason.

    Everything here is third-party JSON from a machine central does not control,
    so every field is type-checked on the way out; a malformed block degrades to
    ``None`` rather than raising.
    """
    raw = _dict(_dict(health).get("rehash"))
    started = _text(raw.get("started"))
    if not started:
        # The agent omits the whole block until a sweep has run, and stamps
        # started_at before its first batch — so no start time means no sweep,
        # whatever else the block happens to contain.
        return None
    return {
        # "h<scheme>-<min>-<max>", deliberately human-readable on the Go side so
        # an operator can compare two agents' migration state by eye.
        "fp": _text(raw.get("fp")),
        "started": started,
        "finished": _text(raw.get("finished")),
        # The agent's own flag rather than ``finished is not None``: an empty
        # finished_at is the durable "partway through" state the next command
        # resumes from, and it is the agent's to declare.
        "complete": raw.get("complete") is True,
        "seen": _int(raw.get("seen")),
        # changed vs verified stay SEPARATE all the way to the panel. A sweep
        # reporting 40,000 seen / 0 changed / 40,000 verified is confirming an
        # already-converged agent; the same numbers with the two added together
        # are indistinguishable from a sweep that did nothing.
        "changed": _int(raw.get("changed")),
        "verified": _int(raw.get("verified")),
        "skipped": _int(raw.get("skipped")),
        "failed": _int(raw.get("failed")),
        "cursor": _int(raw.get("cursor")),
        "min_size": _int(raw.get("min_size")),
        "max_size": _int(raw.get("max_size")),
    }


def agent_about(agent: Agent) -> dict[str, Any]:
    """Assemble the whole per-agent About report from one already-loaded row.

    Pure and synchronous — no session, no I/O, no network — so it is trivially
    testable against a hand-built ``capabilities`` dict, which is the point:
    every interesting case here is a shape a remote machine sent us, and the
    only way to have confidence in the degradation paths is to be able to write
    them down.

    ``reported`` is the flag the console keys its empty state on. False means
    this agent has never polled with a capability advertisement — a pending
    enrollment, an agent that has not started yet, or a build older than the
    capability channel. That is a NORMAL state, not an error, and the endpoint
    returns 200 with empty sections rather than a 404 or a page of zeros.
    """
    caps = agent.capabilities if isinstance(agent.capabilities, dict) else None
    return {
        "agent": {
            "id": str(agent.id),
            "name": agent.name,
            "hostname": agent.hostname,
            "platform": agent.platform,
            "status": agentsync.agent_status(agent),
            # The release stamp the agent binary carries. Deliberately shown
            # next to, never instead of, the build block: this is what the
            # pipeline named the release, while `build.vcs_revision` is what the
            # binary was actually made from, and a hand-built or re-tagged
            # binary is exactly the case where they disagree.
            "agent_version": agent.agent_version,
            "config_generation_applied": agent.config_generation_applied,
            "last_seen_at": agent.last_seen_at,
            # Central's OBSERVATION of the transport ('bearer' | 'mtls'), not
            # the agent's claim about it.
            "auth_mode": agent.last_auth_mode,
            # When the advertisement below arrived. Everything in this report is
            # as-of this instant, and on an offline agent that can be days ago —
            # which is why it is a first-class field rather than a footnote.
            "capabilities_at": agent.capabilities_at,
        },
        "build": build_section(caps),
        "host_tools": host_tool_rows(caps) if caps else [],
        "modules": module_rows(caps),
        # The agent sets this when its own dependency list would have pushed the
        # poll payload past its self-imposed budget. Distinguishing "did not
        # send" from "deliberately left out, and here is why" is the difference
        # between a console that says "not reported (payload budget)" and one
        # that shows a confident empty table.
        "modules_omitted": _dict(caps).get("modules_omitted") is True,
        "extract": extract_section(caps),
        # QH-T6. Sourced from HEALTH, not capabilities (see rehash_section), and
        # so it is present even on an agent that has never sent a capability
        # advertisement — hence its own null rather than a field of `extract`.
        "rehash": rehash_section(
            agent.health if isinstance(agent.health, dict) else None
        ),
        "reported": caps is not None,
    }
