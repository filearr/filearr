// Per-agent About panel — pure-logic tests for the DOM-free half
// (../src/lib/agentAbout.ts). Runs on Node's built-in test runner with native
// TypeScript type-stripping: `npm test` from frontend/. No bundler, no DOM.
//
// What these defend:
//
//  * the inherited rule from about.ts — an unknown value NEVER renders as a
//    blank or a zero, and each honest answer ("not reported", "version
//    unknown", "not installed", "not reported (payload budget)") survives the
//    round trip into the Markdown dump people paste into bug reports;
//  * that this module makes no version JUDGEMENTS of its own — it renders the
//    verdict central sent and nothing else;
//  * that a resolved path under a user profile is flagged, because that path is
//    the console-visible proof of an agent-side security rule.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  agentAboutMarkdown,
  agentToolCell,
  buildRows,
  isUserProfilePath,
  modulesSummary,
  orUnknown,
  toolPathCell,
  type AgentAbout,
  type AgentAboutTool,
} from "../src/lib/agentAbout.ts";

const TOOL: AgentAboutTool = {
  name: "tesseract",
  purpose: "OCR for scanned documents",
  present: true,
  version: "5.3.4",
  path: "C:\\Program Files\\Tesseract-OCR\\tesseract.exe",
  url: "https://tesseract-ocr.github.io/",
  minimum_version: "5.0.0",
  verdict: "ok",
  impact: "OCR runs on an engine line abandoned in 2021.",
};

function report(over: Partial<AgentAbout> = {}): AgentAbout {
  return {
    agent: {
      id: "11111111-2222-3333-4444-555555555555",
      name: "filer-01",
      hostname: "filer-01",
      platform: "windows",
      status: "active",
      agent_version: "0.9.1",
      config_generation_applied: 7,
      last_seen_at: "2026-08-11T11:59:00Z",
      auth_mode: "mtls",
      capabilities_at: "2026-08-11T12:00:00Z",
    },
    build: {
      go_version: "go1.26.5",
      goos: "windows",
      goarch: "amd64",
      os_version: "Windows 10.0 (build 26100)",
      vcs_revision: "0123456789abcdef0123456789abcdef01234567",
      vcs_time: "2026-08-11T09:00:00Z",
      vcs_modified: false,
      main_version: null,
      num_cpu: 16,
    },
    host_tools: [TOOL],
    modules: [{ path: "golang.org/x/crypto", version: "v0.54.0", url: "https://pkg.go.dev/golang.org/x/crypto" }],
    modules_omitted: false,
    extract: { schema: 1, formats: ["audio", "video"], collectors: ["stat"], inventory_version: 1 },
    reported: true,
    ...over,
  };
}

// --------------------------------------------------------------------------- //
// Never blank                                                                  //
// --------------------------------------------------------------------------- //
test("orUnknown never yields an empty string", () => {
  assert.equal(orUnknown(null), "not reported");
  assert.equal(orUnknown(undefined), "not reported");
  assert.equal(orUnknown("   "), "not reported");
  assert.equal(orUnknown(0), "0"); // a real zero IS a value
  assert.equal(orUnknown("go1.26.5"), "go1.26.5");
  assert.equal(orUnknown(null, "never"), "never");
});

test("a tool row has a meaningful cell in every state", () => {
  assert.equal(agentToolCell({ ...TOOL, present: false, version: null, path: null }).text, "not installed");

  const silent = agentToolCell({ ...TOOL, version: null, verdict: "unknown" });
  assert.equal(silent.text, "installed, version unknown");
  assert.equal(silent.tone, "muted");

  const old = agentToolCell({ ...TOOL, version: "4.1.1", verdict: "outdated" });
  assert.equal(old.tone, "warn");
  assert.match(old.text, /4\.1\.1 — below 5\.0\.0/);
  assert.match(old.hint ?? "", /host-side package upgrade/);

  // Unjudgeable is NEITHER good nor bad news — never amber.
  const git = agentToolCell({ ...TOOL, name: "ffmpeg", version: "N-113579-g1c2d3e4", verdict: "unknown" });
  assert.equal(git.text, "N-113579-g1c2d3e4");
  assert.notEqual(git.tone, "warn");

  const fine = agentToolCell(TOOL);
  assert.equal(fine.text, "5.3.4");
  assert.equal(fine.tone, "ok");
});

test("the cell renders central's verdict and never recomputes it", () => {
  // A version that IS above the minimum, labelled outdated by central: the
  // console must still say outdated. There is one comparator and it is in
  // Python; second-guessing it here is the drift this rule forbids.
  const cell = agentToolCell({ ...TOOL, version: "9.9.9", verdict: "outdated" });
  assert.equal(cell.tone, "warn");
  // ...and the converse: a version BELOW the minimum that central called ok.
  assert.equal(agentToolCell({ ...TOOL, version: "1.0.0", verdict: "ok" }).tone, "ok");
});

// --------------------------------------------------------------------------- //
// Locations                                                                    //
// --------------------------------------------------------------------------- //
test("a location cell distinguishes absent from unreported", () => {
  assert.equal(toolPathCell({ ...TOOL, present: false, path: null }).text, "—");
  assert.equal(toolPathCell({ ...TOOL, path: null }).text, "location not reported");
  assert.equal(toolPathCell(TOOL).text, TOOL.path);
  assert.equal(toolPathCell(TOOL).tone, "ok");
});

test("a user-profile path is flagged loudly", () => {
  // The agent must never resolve a host tool out of a user-writable directory —
  // it runs as LocalSystem/root. Displaying the path is what makes that rule
  // auditable, and this is the alarm if it regresses.
  const bad = toolPathCell({ ...TOOL, path: "C:\\Users\\someone\\AppData\\Local\\Programs\\ExifTool\\exiftool.exe" });
  assert.equal(bad.tone, "bad");
  assert.match(bad.hint ?? "", /privilege-escalation/);

  assert.equal(isUserProfilePath("C:\\Program Files\\ffmpeg\\bin\\ffmpeg.exe"), false);
  assert.equal(isUserProfilePath("/usr/bin/ffmpeg"), false);
  assert.equal(isUserProfilePath("/home/someone/bin/ffmpeg"), true);
  assert.equal(isUserProfilePath("c:/users/someone/scoop/shims/exiftool.exe"), true);
});

// --------------------------------------------------------------------------- //
// Build stack                                                                  //
// --------------------------------------------------------------------------- //
test("build rows fill in every label even with no build block", () => {
  const rows = buildRows(report({ build: null, agent: { ...report().agent, agent_version: null } }));
  const byLabel = Object.fromEntries(rows.map((r) => [r.label, r.value]));
  assert.equal(byLabel["Agent version"], "not reported");
  assert.equal(byLabel["Go toolchain"], "not reported");
  assert.equal(byLabel["Source commit"], "not reported");
  for (const r of rows) assert.notEqual(r.value.trim(), "");
});

test("the Go toolchain is labelled as the compiler, and a dirty tree is marked", () => {
  const rows = buildRows(report());
  const go = rows.find((r) => r.label === "Go toolchain")!;
  assert.equal(go.value, "go1.26.5");
  assert.match(go.hint ?? "", /COMPILED/);

  const dirty = buildRows(report({ build: { ...report().build!, vcs_modified: true } }));
  const commit = dirty.find((r) => r.label === "Source commit")!;
  assert.match(commit.value, /\(modified\)$/);
  assert.match(commit.hint ?? "", /DIRTY/);
  // Short sha, not the full 40 characters.
  assert.match(commit.value, /^0123456789ab/);

  // "Built for" collapses the platform pair into one readable cell.
  assert.equal(rows.find((r) => r.label === "Built for")!.value, "windows/amd64");

  // The as-of stamp is a first-class row, never a footnote.
  assert.ok(rows.some((r) => r.label === "Reported"));
  assert.equal(
    buildRows(report({ agent: { ...report().agent, capabilities_at: null } })).find(
      (r) => r.label === "Reported",
    )!.value,
    "never",
  );
});

// --------------------------------------------------------------------------- //
// Modules                                                                      //
// --------------------------------------------------------------------------- //
test("modules summary tells apart 'trimmed', 'not reported' and a real list", () => {
  assert.match(modulesSummary(report()).text, /^1 module\(s\)/);

  const trimmed = modulesSummary(report({ modules: null, modules_omitted: true }));
  assert.equal(trimmed.text, "not reported (payload budget)");
  assert.match(trimmed.hint ?? "", /local web UI/);

  const silent = modulesSummary(report({ modules: null, modules_omitted: false }));
  assert.equal(silent.text, "not reported");
  assert.notEqual(silent.text, trimmed.text);
});

// --------------------------------------------------------------------------- //
// Markdown dump                                                                //
// --------------------------------------------------------------------------- //
const NOW = new Date("2026-08-11T13:00:00Z");

test("the Markdown dump is complete, dated and paste-safe", () => {
  const md = agentAboutMarkdown(report(), NOW);
  assert.match(md, /# Agent filer-01 — build stack/);
  // The as-of stamp is what makes a pasted dump interpretable at all.
  assert.match(md, /last reported at 2026-08-11T12:00:00Z/);
  assert.match(md, /go1\.26\.5/);
  assert.match(md, /Windows 10\.0 \(build 26100\)/);
  assert.match(md, /## Host tools \(on the agent machine\)/);
  assert.match(md, /\[tesseract\]\(https:\/\/tesseract-ocr\.github\.io\/\)/);
  assert.match(md, /## Go modules/);
  assert.match(md, /golang\.org\/x\/crypto \| v0\.54\.0/);
  // Windows paths survive: a pipe would break the table, a backslash must not
  // be mangled.
  assert.ok(md.includes("C:\\Program Files\\Tesseract-OCR\\tesseract.exe"));
});

test("the dump of a never-polled agent says so instead of looking empty", () => {
  const md = agentAboutMarkdown(
    report({
      reported: false,
      build: null,
      host_tools: [],
      modules: null,
      extract: { schema: null, formats: [], collectors: [], inventory_version: null },
      agent: { ...report().agent, capabilities_at: null, agent_version: null },
    }),
    NOW,
  );
  assert.match(md, /never sent a capability advertisement/);
  assert.match(md, /last reported at never/);
  assert.match(md, /Not reported\./); // the host-tools section
  assert.match(md, /not reported/); // the modules section
  // No blank table cells anywhere — the rule this module exists to enforce.
  // Checked per line: `\s` matches a newline, so testing the joined block would
  // match the "|" that ends one row against the "|" that starts the next.
  for (const line of md.split("\n")) {
    if (!line.startsWith("| ") || line.startsWith("| ---")) continue;
    assert.ok(!/\|\s*\|/.test(line), `blank cell in: ${line}`);
  }
});

test("a pipe in an agent-supplied string cannot break the table", () => {
  const md = agentAboutMarkdown(
    report({ host_tools: [{ ...TOOL, version: "5.3|4", verdict: "unknown", minimum_version: null }] }),
    NOW,
  );
  assert.ok(md.includes("5.3\\|4"));
});
