// Host-tool chips: turning "we found tesseract 4.1.1" into something an
// operator can act on. The DOM-free half of the Agents page's capability
// matrix, split out for the same reason `inventoryCollectors.ts` and
// `agentPolicyDoc.ts` are — the rules are subtle, the failure is silent, and
// they must be unit-testable on Node without a bundler (see
// frontend/tests/hostTools.node.test.ts).
//
// THE RULE THIS MODULE ENFORCES: the console never decides whether a version is
// old. It renders a verdict central computed (`AgentOut.tool_verdicts`, from
// backend/filearr/toolversions.py). There is exactly one comparator in this
// codebase and it is in Python, because the About page's server-probed tools and
// an agent's advertised tools MUST be judged by the same rule — two
// implementations of "is 24.02.0 newer than 22.09.0" would eventually disagree,
// and the moment the two pages contradict each other neither is believed again.
//
// So when the server sends no verdict for a tool, this module falls back to
// `unknown`/`absent` and NEVER to `ok` or `outdated`: those two are claims, and
// the console is not in a position to make them.

/** Central's judgement of one host tool.
 *
 *  * `ok`       — installed, and its version meets the published minimum.
 *  * `outdated` — installed and demonstrably below it. The only warning state.
 *  * `unknown`  — installed but unjudgeable: no version reported, a version
 *    string with no comparable number in it (an ffmpeg git build), or no
 *    published minimum. Explicitly NOT a problem, and must not look like one.
 *  * `absent`   — not installed.
 */
export type ToolVerdict = "ok" | "outdated" | "unknown" | "absent";

const VERDICTS: readonly string[] = ["ok", "outdated", "unknown", "absent"];

/** One row of `GET /agents/host-tool-minimums` (`HostToolMinimumOut`): the
 *  number, the one-sentence consequence of being below it, and the
 *  justification for the number itself. */
export interface HostToolMinimum {
  name: string;
  /** `null` = Filearr publishes no minimum for this tool. Unjudged, not fine. */
  minimum_version: string | null;
  impact: string | null;
  reason: string | null;
}

/** The capability fields this module reads. Structurally compatible with
 *  `AgentCapabilities` (agentPolicyDoc.ts) without importing it, so the chip
 *  logic can be tested against a two-field literal. */
export interface ToolCapabilities {
  tools?: Record<string, boolean>;
  tool_versions?: Record<string, string>;
}

/** A rendered chip: what it says, how it should look, and why. */
export interface ToolChip {
  tool: string;
  verdict: ToolVerdict;
  /** Chip text, e.g. "tesseract 4.1.1" / "exiftool ✕" / "ffmpeg ✓". */
  label: string;
  /** Colour intent, mapped to classes by the component.
   *  `warn` is reserved for `outdated` — nothing else may be amber, or amber
   *  stops meaning "act on this". */
  tone: "ok" | "warn" | "muted";
  /** Full tooltip. Always answers: what was found, what is wanted, what it costs. */
  title: string;
}

/** Index the minimums response by tool name for O(1) chip lookups. */
export function minimumsByName(rows: readonly HostToolMinimum[]): Record<string, HostToolMinimum> {
  const out: Record<string, HostToolMinimum> = {};
  for (const r of rows) out[r.name] = r;
  return out;
}

/** Narrow an untrusted verdict string from the API. An unrecognised value (a
 *  newer central release inventing a fifth state) degrades to `unknown` rather
 *  than being rendered raw or crashing the row. */
function asVerdict(raw: unknown): ToolVerdict | null {
  return typeof raw === "string" && VERDICTS.includes(raw) ? (raw as ToolVerdict) : null;
}

const TONES: Record<ToolVerdict, ToolChip["tone"]> = {
  ok: "ok",
  outdated: "warn",
  unknown: "muted",
  absent: "muted",
};

/** How one host-tool chip reads for one agent.
 *
 *  `verdicts` is the agent row's `tool_verdicts`; `minimums` is the (static,
 *  fleet-wide) catalogue, indexed by {@link minimumsByName}. Both may be
 *  missing — a never-polled agent has no verdicts, and the minimums request is
 *  a separate fetch that can fail — and neither absence may change a chip from
 *  green to amber or back. A failed minimums fetch costs the tooltip its
 *  numbers, nothing more. */
export function toolChip(
  tool: string,
  caps: ToolCapabilities | null | undefined,
  verdicts: Record<string, string> | null | undefined,
  minimums: Record<string, HostToolMinimum> | null | undefined,
): ToolChip {
  const present = caps?.tools?.[tool] === true;
  const version = caps?.tool_versions?.[tool] || null;
  const min = minimums?.[tool] ?? null;
  const wanted = min?.minimum_version ?? null;

  // Central's verdict wins. Without one, the honest fallback: present-but-
  // unjudged, or absent. Never `ok`, never `outdated` — see the module note.
  const verdict: ToolVerdict = asVerdict(verdicts?.[tool]) ?? (present ? "unknown" : "absent");

  const label = `${tool} ${present ? (version ?? "✓") : "✕"}`;
  return {
    tool,
    verdict,
    label,
    tone: TONES[verdict],
    title: chipTitle(tool, verdict, version, wanted, min?.impact ?? null),
  };
}

function chipTitle(
  tool: string,
  verdict: ToolVerdict,
  version: string | null,
  wanted: string | null,
  impact: string | null,
): string {
  if (verdict === "absent")
    return (
      `${tool} is NOT on this agent host's PATH — capabilities that need it are ` +
      `unavailable here. Install it on the machine; no new agent build is required.`
    );

  // `wanted` is absent whenever the (separate, best-effort) minimums fetch
  // failed. The verdict still stands — central computed it — so the tooltip
  // drops the number rather than the warning, and never renders "recommends
  // null or newer".
  if (verdict === "outdated")
    return (
      `${tool} ${version} was found on this agent host, but Filearr recommends ` +
      (wanted ? `${wanted} or newer.` : `a newer version.`) +
      (impact ? ` ${impact}` : "") +
      ` Upgrading is a host-side package upgrade — the agent installer can do it, ` +
      `and no new agent build is required.`
    );

  if (verdict === "ok")
    return (
      `${tool} ${version} was found on this agent host` +
      (wanted ? ` — at or above the recommended minimum of ${wanted}.` : `.`)
    );

  // `unknown`, which has three distinct causes worth telling apart: the operator
  // reads a different next step from each, and lumping them into one sentence
  // ("we don't know") is what makes a tooltip useless.
  if (!version)
    return (
      `${tool} was found on this agent host's PATH, but did not report a version ` +
      `(an older build, or a wrapper script), so it cannot be checked against a ` +
      `recommended minimum.`
    );
  if (!wanted)
    return (
      `${tool} ${version} was found on this agent host. Filearr publishes no ` +
      `minimum version for this tool, so it is not judged either way.`
    );
  return (
    `${tool} ${version} was found on this agent host. That version cannot be ` +
    `compared numerically with the recommended minimum of ${wanted} — typically a ` +
    `build straight from git, which is usually NEWER than any release — so it is ` +
    `reported as unknown rather than guessed at.`
  );
}
