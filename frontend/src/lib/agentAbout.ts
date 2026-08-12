// Per-agent About panel — the DOM-free half.
//
// Sibling of ./about.ts, and deliberately written in its voice: the row shapes
// the API returns, the "how do I render an unknown value honestly" decisions,
// and the Markdown dump behind the Copy button. Kept out of the .svelte
// component so it can be unit-tested on Node without a bundler or a DOM (see
// frontend/tests/agentAbout.node.test.ts).
//
// THE ONE RULE, inherited from ./about.ts: an unknown value never renders as a
// blank or a zero. "not reported", "version unknown", "not installed" are all
// legitimate, meaningful answers, and collapsing any of them to an empty cell
// turns "we could not determine this" into "there is nothing here".
//
// THE SECOND RULE, inherited from ./hostTools.ts: this module does not judge
// versions. Central computed every `verdict` (backend/filearr/toolversions.py),
// and the status chips in the panel come from `toolChip` in ./hostTools — there
// is exactly one comparator in this codebase and it is in Python, because the
// About page's server-probed tools and an agent's advertised tools must be
// judged by the same rule or neither page is believed.
//
// THE THIRD RULE, specific to this surface: every fact here is a REMOTE
// SELF-REPORT, not a live probe. Central never reaches out to an agent — agents
// poll — so the whole panel is "what this agent last said, at
// `capabilities_at`". On an offline agent that can be days old and still look
// perfectly current, which is why the timestamp is rendered at the top rather
// than tucked into a tooltip.

// Explicit `.ts` extension: this is a VALUE import (the two formatters are
// reused rather than reimplemented — a second `shortSha` that truncated to a
// different length would make the two pages disagree about the same commit),
// and the Node test runner resolves it at runtime with no bundler to guess an
// extension for it. Every other cross-module import in src/lib/ is type-only
// and therefore erased before it reaches Node.
import { formatWhen, shortSha, type Cell } from "./about.ts";

export type AgentAboutIdentity = {
  id: string;
  name: string;
  hostname: string;
  platform: string;
  status: string;
  agent_version: string | null;
  config_generation_applied: number | null;
  last_seen_at: string | null;
  /** Central's OBSERVATION of the transport ('bearer' | 'mtls'), not a claim
   *  the agent made about itself. */
  auth_mode: string | null;
  /** When the advertisement this whole panel is derived from arrived. */
  capabilities_at: string | null;
};

export type AgentAboutBuild = {
  /** The Go toolchain that COMPILED the binary — not a pin in go.mod. Labelled
   *  "built with" in the UI for that reason. */
  go_version: string | null;
  goos: string | null;
  goarch: string | null;
  os_version: string | null;
  vcs_revision: string | null;
  vcs_time: string | null;
  /** Working tree was dirty at build time: the commit id alone does not
   *  identify this binary. */
  vcs_modified: boolean | null;
  main_version: string | null;
  num_cpu: number | null;
};

/** One host tool on the AGENT's machine. Structurally the same row as
 *  `AboutTool` (./about.ts) — same keys, same meanings — because the two tables
 *  answer the same question about different machines. */
export type AgentAboutTool = {
  name: string;
  purpose: string | null;
  present: boolean;
  version: string | null;
  /** Resolved absolute path ON THE AGENT HOST. */
  path: string | null;
  url: string | null;
  minimum_version: string | null;
  verdict: "ok" | "outdated" | "unknown" | "absent";
  impact: string | null;
};

export type AgentAboutModule = {
  path: string;
  version: string | null;
  url: string;
};

export type AgentAboutExtract = {
  schema: number | null;
  formats: string[];
  collectors: string[];
  inventory_version: number | null;
};

export type AgentAbout = {
  agent: AgentAboutIdentity;
  build: AgentAboutBuild | null;
  host_tools: AgentAboutTool[];
  /** Null = no module list was reported. Never `[]`, which would claim the
   *  binary has no dependencies. */
  modules: AgentAboutModule[] | null;
  /** True when the AGENT deliberately left its module list out of the poll to
   *  stay inside its payload budget. */
  modules_omitted: boolean;
  extract: AgentAboutExtract;
  /** False = this agent has never advertised capabilities at all. */
  reported: boolean;
};

/** Render any value that might be missing. The fallback is a SENTENCE, never an
 *  empty string — see the module note. */
export function orUnknown(v: string | number | null | undefined, fallback = "not reported"): string {
  if (v === null || v === undefined) return fallback;
  const s = String(v).trim();
  return s === "" ? fallback : s;
}

/** How a host-tool row reads on this panel.
 *
 *  Identical decisions to `toolCell` in ./about.ts — the states and their
 *  wording must match, or the same poppler reads differently depending on which
 *  page you are on — with one addition this surface needs: the resolved PATH is
 *  a first-class column here rather than tooltip-only, because "which copy of
 *  exiftool will the service run" is a question you can only ask about a remote
 *  machine you are not sitting at.
 *
 *  The verdict is central's (`t.verdict`), never recomputed. */
export function agentToolCell(t: AgentAboutTool): Cell {
  if (!t.present) return { text: "not installed", tone: "muted" };
  if (!t.version)
    return {
      text: "installed, version unknown",
      tone: "muted",
      hint:
        "The binary resolved but would not state a version (an older build, or a " +
        "wrapper script), so it cannot be checked against a recommended minimum.",
    };
  if (t.verdict === "outdated")
    return {
      text: `${t.version} — below ${t.minimum_version ?? "the recommended minimum"}`,
      tone: "warn",
      hint:
        `Filearr recommends ${t.minimum_version ?? "a newer version"} or newer.` +
        (t.impact ? ` ${t.impact}` : "") +
        " Fixing it is a host-side package upgrade on the agent machine — no new agent build.",
    };
  if (t.verdict === "unknown")
    // Installed, states a version, and we still decline to judge: either the
    // string carries no comparable number (a git build, usually NEWER than any
    // release) or Filearr publishes no minimum. Neutral on purpose.
    return {
      text: t.version,
      tone: "ok",
      hint: t.minimum_version
        ? `This version cannot be compared with the recommended minimum of ${t.minimum_version}, so it is not judged either way.`
        : "Filearr publishes no minimum version for this tool.",
    };
  return {
    text: t.version,
    tone: "ok",
    hint: t.minimum_version ? `Minimum recommended: ${t.minimum_version}.` : undefined,
  };
}

/** How the LOCATION cell reads.
 *
 *  Absent tools have no location and say so. A present tool with no path is a
 *  contradiction the agent should never send, but if it does the honest answer
 *  is that we were not told — not a blank cell that reads as "installed at
 *  nowhere".
 *
 *  `bad` tone is reserved for a path under a user profile. That must never
 *  happen: the agent resolves host tools only from machine-wide,
 *  admin-writable directories, because it runs as LocalSystem/root and
 *  executing a binary out of a user-writable directory would let any local user
 *  escalate. Displaying the path is what makes that rule auditable from the
 *  console, and this is the alarm if it ever regresses. */
export function toolPathCell(t: AgentAboutTool): Cell {
  if (!t.present) return { text: "—", tone: "muted" };
  if (!t.path) return { text: "location not reported", tone: "muted" };
  if (isUserProfilePath(t.path))
    return {
      text: t.path,
      tone: "bad",
      hint:
        "This tool resolved from a USER-WRITABLE directory. The agent is supposed to " +
        "refuse those: it runs as LocalSystem/root, so executing a binary out of a " +
        "user profile is a local privilege-escalation path. Report this — it means " +
        "the agent's resolver regressed.",
    };
  return { text: t.path, tone: "ok" };
}

/** Does a path live under a user profile? Deliberately conservative and
 *  case-insensitive — it only ever adds a warning, never removes one. */
export function isUserProfilePath(path: string): boolean {
  const p = path.replace(/\//g, "\\").toLowerCase();
  return (
    p.includes("\\users\\") ||
    p.startsWith("c:\\users") ||
    /^\\home\\[^\\]+\\/.test(p) ||
    /^\\users\\[^\\]+\\/.test(p)
  );
}

/** The build stack as label/value rows, in the order the panel renders them.
 *
 *  `go_version` is labelled "Go toolchain (built with)" on purpose: it is the
 *  compiler, not the `go` directive in go.mod, and the two disagree routinely.
 *  Central's About page draws the same distinction between a pyproject pin
 *  (intent) and installed metadata (fact). */
export function buildRows(a: AgentAbout): { label: string; value: string; hint?: string }[] {
  const b = a.build;
  const rows: { label: string; value: string; hint?: string }[] = [
    {
      label: "Agent version",
      value: orUnknown(a.agent.agent_version),
      hint: "The release stamp the agent binary carries — what the pipeline named it.",
    },
  ];
  rows.push({
    label: "Go toolchain",
    value: orUnknown(b?.go_version),
    hint: "The Go toolchain that COMPILED this binary — not the `go` directive in go.mod.",
  });
  rows.push({
    label: "Built for",
    value:
      b?.goos && b?.goarch ? `${b.goos}/${b.goarch}` : orUnknown(b?.goos ?? b?.goarch ?? null),
  });
  rows.push({ label: "Host OS", value: orUnknown(b?.os_version) });
  rows.push({
    label: "CPUs",
    value: orUnknown(b?.num_cpu),
    hint: "Logical CPUs visible to the agent process.",
  });
  rows.push({
    label: "Source commit",
    value: vcsText(b),
    hint: b?.vcs_modified
      ? "The working tree was DIRTY at build time, so this commit does not fully identify the binary."
      : b?.vcs_time
        ? `Committed ${formatWhen(b.vcs_time)}.`
        : undefined,
  });
  rows.push({
    label: "Reported",
    value: a.agent.capabilities_at ? formatWhen(a.agent.capabilities_at) : "never",
    hint:
      "When this agent last sent its capability advertisement. Everything on this " +
      "panel is as of that moment — central never queries an agent, agents poll.",
  });
  return rows;
}

function vcsText(b: AgentAboutBuild | null | undefined): string {
  const sha = shortSha(b?.vcs_revision);
  if (!sha) return "not reported";
  return b?.vcs_modified ? `${sha} (modified)` : sha;
}

/** What the modules section should say. Three states, and conflating the last
 *  two is the mistake this function exists to prevent: "the agent did not tell
 *  us" and "there are none" look identical as an empty table. */
export function modulesSummary(a: AgentAbout): Cell {
  if (a.modules && a.modules.length)
    return { text: `${a.modules.length} module(s) linked into the agent binary`, tone: "ok" };
  if (a.modules_omitted)
    return {
      text: "not reported (payload budget)",
      tone: "muted",
      hint:
        "The agent left its dependency list out of the poll to stay inside the " +
        "capability payload budget. The agent's own local web UI shows it in full — " +
        "nothing crosses a network to reach that page.",
    };
  return {
    text: "not reported",
    tone: "muted",
    hint: "This agent build does not report its Go module list.",
  };
}

function mdTable(headers: string[], rows: string[][]): string {
  return [
    `| ${headers.join(" | ")} |`,
    `| ${headers.map(() => "---").join(" | ")} |`,
    ...rows.map((r) => `| ${r.join(" | ")} |`),
  ].join("\n");
}

/** Escape the two characters that break a Markdown table cell. Versions and
 *  paths are third-party strings and a Windows path is full of backslashes. */
function md(value: string | number | null | undefined, fallback = "unknown"): string {
  const v = String(value ?? "").trim();
  if (!v) return fallback;
  return v.replace(/\|/g, "\\|").replace(/\n/g, " ");
}

function link(text: string, url: string | null | undefined): string {
  return url ? `[${md(text)}](${url})` : md(text);
}

/**
 * The whole agent report as Markdown, for pasting into a bug report — the
 * realistic reason anyone opens this panel and then reaches for the mouse.
 *
 * The "as of" line is not decoration: a pasted report of a machine's tool
 * versions is worthless without the timestamp, and an agent that has been
 * offline for a week produces a perfectly confident-looking dump.
 *
 * `now` is injectable so the output is deterministic under test.
 */
export function agentAboutMarkdown(a: AgentAbout, now = new Date()): string {
  const parts: string[] = [];
  const id = a.agent;

  parts.push(`# Agent ${md(id.name)} — build stack`);
  parts.push(
    `_Captured ${now.toISOString()} from central. The agent last reported at ` +
      `${id.capabilities_at ? md(id.capabilities_at) : "never"} — every value below is as of then._`,
  );

  if (!a.reported) {
    parts.push(
      "**This agent has never sent a capability advertisement.** It has not polled " +
        "yet (a fresh enrollment), or it is running a build older than the capability " +
        "channel. Nothing below is missing — there is nothing yet to report.",
    );
  }

  parts.push(
    mdTable(
      ["Field", "Value"],
      [
        ["Agent", md(id.name)],
        ["Hostname", md(id.hostname)],
        ["Platform", md(id.platform)],
        ["Status", md(id.status)],
        ["Transport", md(id.auth_mode, "not observed")],
        ["Last seen", md(id.last_seen_at, "never")],
        ...buildRows(a).map((r) => [md(r.label), md(r.value)]),
      ],
    ),
  );

  parts.push("## Host tools (on the agent machine)");
  parts.push(
    a.host_tools.length === 0
      ? "Not reported."
      : mdTable(
          ["Tool", "Status", "Path"],
          a.host_tools.map((t) => [
            link(t.name, t.url),
            md(agentToolCell(t).text),
            md(t.path, "—"),
          ]),
        ),
  );

  parts.push("## Extraction");
  parts.push(
    mdTable(
      ["Field", "Value"],
      [
        ["Schema", md(a.extract.schema)],
        ["Formats", md(a.extract.formats.join(", "), "none")],
        ["Inventory collectors", md(a.extract.collectors.join(", "), "none")],
        ["Inventory version", md(a.extract.inventory_version)],
      ],
    ),
  );

  parts.push("## Go modules");
  parts.push(
    a.modules && a.modules.length
      ? mdTable(
          ["Module", "Version"],
          a.modules.map((m) => [md(m.path), md(m.version, "unknown")]),
        )
      : md(modulesSummary(a).text),
  );

  return parts.join("\n\n") + "\n";
}
