// About page — the DOM-free half.
//
// Everything here is pure: the row shapes the API returns, the "how do I render
// an unknown value honestly" decisions, and the Markdown dump behind the Copy
// button. Kept out of the .svelte component so it can be unit-tested on Node
// without a bundler or a DOM (see frontend/tests/about.node.test.ts).
//
// THE ONE RULE THIS MODULE ENFORCES: an unknown value never renders as a blank
// or a zero. "unreachable", "not installed", "not downloaded" and "unknown" are
// all legitimate, meaningful answers, and collapsing any of them to an empty
// cell turns "we could not determine this" into "there is nothing here" — the
// exact misreading that costs an operator twenty minutes mid-incident.

export type AboutApplication = {
  app_version: string;
  build_stamp: string | null;
  source_url: string;
  license: string;
  license_url: string;
  python_version: string;
  python_implementation: string;
  platform: string;
  system: string;
  machine: string;
};

export type AboutService = {
  name: string;
  version: string | null;
  detail: string | null;
  url: string;
  error: string | null;
};

export type AboutPackage = {
  name: string;
  version: string | null;
  url: string;
  optional: boolean;
};

export type AboutTool = {
  name: string;
  purpose: string;
  present: boolean;
  version: string | null;
  path: string | null;
  url: string;
  /** The version Filearr recommends at minimum, or null when it publishes no
   *  opinion about this tool. */
  minimum_version: string | null;
  /** Central's verdict, from the SAME function that judges an agent's tools
   *  (backend/filearr/toolversions.py). `ok` | `outdated` | `unknown` |
   *  `absent`; see ./hostTools for what each one means. The two surfaces share
   *  the comparator on purpose — a poppler called old on the Agents page and
   *  fine here would discredit both pages at once. */
  verdict: "ok" | "outdated" | "unknown" | "absent";
  /** One sentence on what degrades below the minimum. Null when there is no
   *  minimum. */
  impact: string | null;
};

export type AboutAgents = {
  total: number;
  versions: { version: string | null; count: number }[];
  error: string | null;
};

export type AboutEmbedding = {
  enabled: boolean;
  name: string;
  repo: string;
  file: string;
  dimensions: number;
  cache_dir: string;
  downloaded: boolean;
  revision: string | null;
  size: number | null;
  /** Local download time (blob mtime) — NOT the model's publication date. */
  downloaded_at: string | null;
  path: string | null;
  model_url: string;
  revision_url: string | null;
  license_note: string;
};

export type About = {
  application: AboutApplication;
  services: AboutService[];
  python_packages: AboutPackage[];
  host_tools: AboutTool[];
  agents: AboutAgents | null;
  embedding: AboutEmbedding;
};

/** The build-time-injected frontend stack (vite.config.ts `define`). */
export type FrontendStack = {
  node: string;
  built_at: string;
  packages: { name: string; version: string; kind: "runtime" | "build"; url: string }[];
};

/** Human byte size. Binary units, because that is what `ls -lh` and every disk
 *  figure elsewhere in this console uses. */
export function formatBytes(n: number | null | undefined): string {
  if (n == null || !Number.isFinite(n) || n < 0) return "unknown";
  if (n < 1024) return `${n} B`;
  const units = ["KiB", "MiB", "GiB", "TiB"];
  let v = n / 1024;
  let i = 0;
  while (v >= 1024 && i < units.length - 1) {
    v /= 1024;
    i += 1;
  }
  return `${v < 10 ? v.toFixed(1) : Math.round(v)} ${units[i]}`;
}

/** First 12 characters of a commit sha — enough to be unambiguous, short enough
 *  to sit in a table cell. Returns null for a missing sha so callers must make
 *  an explicit decision about the empty case. */
export function shortSha(sha: string | null | undefined): string | null {
  if (!sha) return null;
  return sha.length > 12 ? sha.slice(0, 12) : sha;
}

/** ISO timestamp -> local, readable. Invalid/absent input stays honest. */
export function formatWhen(iso: string | null | undefined): string {
  if (!iso) return "unknown";
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return iso;
  return d.toLocaleString();
}

export type Cell = {
  /** What to show. Never empty. */
  text: string;
  /** `ok` = a real observed value, `bad` = a probe that failed, `warn` = a real
   *  value that needs attention (a host tool below its recommended minimum),
   *  `muted` = a legitimate "nothing here" (a tool that simply is not
   *  installed). Drives colour only; the text alone is always sufficient. */
  tone: "ok" | "bad" | "warn" | "muted";
  /** Longer explanation for a title tooltip, when there is one. */
  hint?: string;
};

/** How a service row's version cell reads. A failed probe is INFORMATION —
 *  "Meilisearch: unreachable" is the answer someone needs — so it renders as a
 *  first-class value with its reason attached, not as an empty cell. */
export function serviceCell(s: AboutService): Cell {
  if (s.version) return { text: s.version, tone: "ok", hint: s.detail ?? undefined };
  return { text: s.error ?? "unknown", tone: "bad", hint: s.error ?? undefined };
}

/** How a host-tool row reads. Four distinct states, and the ones in the middle
 *  are the easy ones to get wrong:
 *
 *  * a tool can be present and still refuse to state a version, which is
 *    "installed, version unknown" — not "absent", not blank;
 *  * a version BELOW the recommended minimum is still a working install, so it
 *    reads as the version it is, amber, with the reason in the tooltip — not as
 *    a failure and never as plain green.
 *
 *  The verdict itself is central's (`t.verdict`), never recomputed here: the
 *  Agents page and this page must call the same poppler old or neither is
 *  trusted. This function only decides how it READS. */
export function toolCell(t: AboutTool): Cell {
  if (!t.present) return { text: "not installed", tone: "muted" };
  if (!t.version)
    return {
      text: "installed, version unknown",
      tone: "muted",
      hint: t.path ?? undefined,
    };
  if (t.verdict === "outdated")
    return {
      text: `${t.version} — below ${t.minimum_version ?? "the recommended minimum"}`,
      tone: "warn",
      hint:
        `Filearr recommends ${t.minimum_version ?? "a newer version"} or newer.` +
        (t.impact ? ` ${t.impact}` : "") +
        (t.path ? ` (${t.path})` : ""),
    };
  if (t.verdict === "unknown")
    // Installed, states a version, and we still decline to judge it: either the
    // string carries no comparable number (a build from git, usually NEWER than
    // any release) or Filearr publishes no minimum. Neutral on purpose.
    return {
      text: t.version,
      tone: "ok",
      hint: hint(
        t.minimum_version
          ? `This version cannot be compared with the recommended minimum of ${t.minimum_version}, so it is not judged either way.`
          : "Filearr publishes no minimum version for this tool.",
        t.path,
      ),
    };
  return {
    text: t.version,
    tone: "ok",
    hint: hint(t.minimum_version ? `Minimum recommended: ${t.minimum_version}.` : null, t.path),
  };
}

/** Join a tooltip's sentence and the resolved path, dropping whichever is
 *  missing. Returns undefined for "no tooltip" so the caller renders none. */
function hint(sentence: string | null, path: string | null): string | undefined {
  const parts = [sentence, path].filter(Boolean);
  return parts.length ? parts.join(" ") : undefined;
}

/** A dependency's version cell. A declared dependency that is not importable is
 *  a genuinely broken install and says so rather than showing nothing. */
export function packageCell(p: AboutPackage): Cell {
  if (p.version) return { text: p.version, tone: "ok" };
  return { text: "not installed", tone: "bad" };
}

/** One-line summary of the embedding model's real state on this machine.
 *  Semantic search being off, or the model never having been fetched, is the
 *  COMMON case on a default install — it reads as normal, not as a fault. */
export function embeddingSummary(e: AboutEmbedding): Cell {
  if (e.downloaded)
    return {
      text: e.enabled ? "downloaded and in use" : "downloaded (semantic search is off)",
      tone: e.enabled ? "ok" : "muted",
    };
  if (e.enabled)
    return {
      text: "not downloaded yet — fetched on the first embedding job",
      tone: "muted",
    };
  return { text: "not downloaded (semantic search is off)", tone: "muted" };
}

function mdTable(headers: string[], rows: string[][]): string {
  const out = [
    `| ${headers.join(" | ")} |`,
    `| ${headers.map(() => "---").join(" | ")} |`,
    ...rows.map((r) => `| ${r.join(" | ")} |`),
  ];
  return out.join("\n");
}

/** Escape the two characters that break a Markdown table cell. Versions and
 *  paths are third-party strings and a Windows path is full of backslashes. */
function md(value: string | null | undefined, fallback = "unknown"): string {
  const v = (value ?? "").trim();
  if (!v) return fallback;
  return v.replace(/\|/g, "\\|").replace(/\n/g, " ");
}

function link(text: string, url: string | null | undefined): string {
  return url ? `[${md(text)}](${url})` : md(text);
}

/**
 * The whole stack as Markdown, for pasting into a bug report — which is the
 * realistic reason anyone opens this page and then reaches for the mouse.
 *
 * `now` is injectable so the output is deterministic under test.
 */
export function aboutMarkdown(a: About, fe: FrontendStack | null, now = new Date()): string {
  const app = a.application;
  const parts: string[] = [];

  parts.push(`# Filearr ${app.app_version} — build stack`);
  parts.push(`_Captured ${now.toISOString()} from the running instance._`);

  parts.push(
    mdTable(
      ["Component", "Value"],
      [
        ["Application", `Filearr ${md(app.app_version)}`],
        ["Build stamp", md(app.build_stamp, "none (dev checkout)")],
        ["License", link(app.license, app.license_url)],
        ["Source", md(app.source_url)],
        ["Python", `${md(app.python_implementation)} ${md(app.python_version)}`],
        ["Platform", md(app.platform)],
        ["Architecture", md(app.machine)],
      ],
    ),
  );

  parts.push("## Services");
  parts.push(
    mdTable(
      ["Service", "Version", "Notes"],
      a.services.map((s) => [
        link(s.name, s.url),
        md(serviceCell(s).text),
        md(s.detail, ""),
      ]),
    ),
  );

  parts.push("## Backend (Python)");
  parts.push(
    mdTable(
      ["Package", "Version", "Link"],
      a.python_packages.map((p) => [
        md(p.name) + (p.optional ? " _(optional)_" : ""),
        md(packageCell(p).text),
        md(p.url, ""),
      ]),
    ),
  );

  if (fe) {
    parts.push("## Frontend (built bundle)");
    parts.push(`Built with Node ${md(fe.node)} at ${md(fe.built_at)}.`);
    parts.push(
      mdTable(
        ["Package", "Version", "Link"],
        fe.packages.map((p) => [md(p.name), md(p.version), md(p.url, "")]),
      ),
    );
  }

  parts.push("## Extraction tools (this server)");
  parts.push(
    mdTable(
      ["Tool", "Status", "Path"],
      a.host_tools.map((t) => [link(t.name, t.url), md(toolCell(t).text), md(t.path, "—")]),
    ),
  );

  if (a.agents) {
    parts.push("## Agent fleet");
    parts.push(
      a.agents.versions.length === 0
        ? "No agents enrolled."
        : mdTable(
            ["Agent version", "Agents"],
            a.agents.versions.map((v) => [md(v.version, "unknown"), String(v.count)]),
          ),
    );
  }

  const e = a.embedding;
  parts.push("## Embedding model");
  parts.push(
    mdTable(
      ["Field", "Value"],
      [
        ["Model", md(e.name)],
        ["Repository", link(e.repo, e.model_url)],
        ["File", md(e.file)],
        ["Dimensions", String(e.dimensions)],
        ["Semantic search", e.enabled ? "enabled" : "disabled"],
        ["State", md(embeddingSummary(e).text)],
        ["Revision", e.revision ? link(e.revision, e.revision_url) : "not downloaded"],
        ["Cached size", e.downloaded ? formatBytes(e.size) : "—"],
        // Spelled out in the dump too: the reader of a pasted bug report has
        // no tooltip to hover, and "2026-07-12" next to a model name looks
        // exactly like a release date unless you say otherwise.
        [
          "Downloaded to this machine",
          e.downloaded_at ? `${md(e.downloaded_at)} (local download time, not the model's release date)` : "never",
        ],
        ["Cache directory", md(e.cache_dir)],
      ],
    ),
  );

  return parts.join("\n\n") + "\n";
}
