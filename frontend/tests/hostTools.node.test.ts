// Pure-logic tests for the Agents page's host-tool chips (src/lib/hostTools.ts).
//
// The chip is a colour, and a colour is a claim. The claims that must never be
// made by accident:
//
//   * amber ("outdated") only ever comes from central's verdict — the console
//     owns no version comparator, precisely so the About page and the Agents
//     page cannot disagree about whether a given poppler is old;
//   * green never appears by default. A missing verdict falls back to
//     unknown/absent, because "we did not hear" must not read as "it is fine";
//   * "unknown" must look like neither good nor bad news. An ffmpeg built from
//     git is usually the NEWEST build in the fleet.
//
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler / DOM.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  minimumsByName,
  toolChip,
  type HostToolMinimum,
  type ToolCapabilities,
} from "../src/lib/hostTools.ts";

const MINIMUMS: HostToolMinimum[] = [
  {
    name: "tesseract",
    minimum_version: "5.0.0",
    impact: "OCR runs on an engine line upstream abandoned in 2021.",
    reason: "4.1.3 was the final 4.x release.",
  },
  {
    name: "ffmpeg",
    minimum_version: "4.3",
    impact: "Older builds cannot decode newer codecs such as AV1.",
    reason: "Paired with ffprobe.",
  },
  {
    name: "mediainfo",
    minimum_version: null,
    impact: null,
    reason: null,
  },
];
const MINS = minimumsByName(MINIMUMS);

const caps = (
  tools: Record<string, boolean>,
  versions: Record<string, string> = {},
): ToolCapabilities => ({ tools, tool_versions: versions });

test("an outdated tool is amber, and its tooltip carries found + minimum + consequence", () => {
  const chip = toolChip(
    "tesseract",
    caps({ tesseract: true }, { tesseract: "4.1.1" }),
    { tesseract: "outdated" },
    MINS,
  );
  assert.equal(chip.verdict, "outdated");
  assert.equal(chip.tone, "warn");
  assert.equal(chip.label, "tesseract 4.1.1");
  assert.match(chip.title, /4\.1\.1/); // what was found
  assert.match(chip.title, /5\.0\.0/); // what is wanted
  assert.match(chip.title, /abandoned in 2021/); // what it costs
});

test("a healthy tool is green and still names the bar it cleared", () => {
  const chip = toolChip(
    "tesseract",
    caps({ tesseract: true }, { tesseract: "5.3.4" }),
    { tesseract: "ok" },
    MINS,
  );
  assert.equal(chip.verdict, "ok");
  assert.equal(chip.tone, "ok");
  assert.equal(chip.label, "tesseract 5.3.4");
  assert.match(chip.title, /minimum of 5\.0\.0/);
});

test("an absent tool is muted and says installing it needs no new agent build", () => {
  const chip = toolChip("exiftool", caps({ exiftool: false }), { exiftool: "absent" }, MINS);
  assert.equal(chip.verdict, "absent");
  assert.equal(chip.tone, "muted");
  assert.equal(chip.label, "exiftool ✕");
  assert.match(chip.title, /NOT on this agent host's PATH/);
});

test("a git-build ffmpeg is unknown — muted, never amber, and says why", () => {
  const chip = toolChip(
    "ffmpeg",
    caps({ ffmpeg: true }, { ffmpeg: "N-113579-g1c2d3e4" }),
    { ffmpeg: "unknown" },
    MINS,
  );
  assert.equal(chip.verdict, "unknown");
  assert.equal(chip.tone, "muted");
  assert.notEqual(chip.tone, "warn");
  assert.equal(chip.label, "ffmpeg N-113579-g1c2d3e4");
  assert.match(chip.title, /usually NEWER than any release/);
});

test("present but silent about its version reads as unjudgeable, not as absent", () => {
  const chip = toolChip("ffmpeg", caps({ ffmpeg: true }), { ffmpeg: "unknown" }, MINS);
  assert.equal(chip.verdict, "unknown");
  assert.equal(chip.label, "ffmpeg ✓");
  assert.match(chip.title, /did not report a version/);
});

test("a tool with no published minimum says so rather than implying it passed", () => {
  const chip = toolChip(
    "mediainfo",
    caps({ mediainfo: true }, { mediainfo: "24.06" }),
    { mediainfo: "unknown" },
    MINS,
  );
  assert.equal(chip.verdict, "unknown");
  assert.match(chip.title, /publishes no\s+minimum version/);
});

test("no verdict from the server never invents one", () => {
  // A never-polled agent, or a response that predates tool_verdicts. Present
  // degrades to unknown and absent to absent — green and amber are claims the
  // console is not entitled to make.
  const present = toolChip("tesseract", caps({ tesseract: true }, { tesseract: "4.1.1" }), {}, MINS);
  assert.equal(present.verdict, "unknown");
  assert.notEqual(present.tone, "warn");

  const missing = toolChip("tesseract", caps({ tesseract: false }), undefined, MINS);
  assert.equal(missing.verdict, "absent");

  const nothing = toolChip("tesseract", null, null, null);
  assert.equal(nothing.verdict, "absent");
  assert.equal(nothing.label, "tesseract ✕");
});

test("an unrecognised verdict from a newer central degrades to unknown", () => {
  const chip = toolChip(
    "tesseract",
    caps({ tesseract: true }, { tesseract: "5.3.4" }),
    { tesseract: "ancient-and-cursed" },
    MINS,
  );
  assert.equal(chip.verdict, "unknown");
  assert.equal(chip.tone, "muted");
});

test("a failed minimums fetch costs the tooltip its numbers, never the verdict", () => {
  const chip = toolChip(
    "tesseract",
    caps({ tesseract: true }, { tesseract: "4.1.1" }),
    { tesseract: "outdated" },
    null,
  );
  assert.equal(chip.verdict, "outdated");
  assert.equal(chip.tone, "warn");
  assert.match(chip.title, /4\.1\.1/);
});

test("minimumsByName indexes the catalogue response", () => {
  assert.equal(minimumsByName(MINIMUMS).ffmpeg.minimum_version, "4.3");
  assert.deepEqual(minimumsByName([]), {});
});
