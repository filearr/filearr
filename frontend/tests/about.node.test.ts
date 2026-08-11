// About page — pure-logic tests for the DOM-free half (../src/lib/about.ts).
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler, no DOM.
//
// What these defend: the page's one hard rule — an unknown value NEVER renders
// as a blank or a zero. "unreachable", "not installed" and "not downloaded" are
// all real answers, and each has to survive the round trip into the Markdown
// dump that people paste into bug reports.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  aboutMarkdown,
  embeddingSummary,
  formatBytes,
  formatWhen,
  packageCell,
  serviceCell,
  shortSha,
  toolCell,
  type About,
  type AboutEmbedding,
  type FrontendStack,
} from "../src/lib/about.ts";

test("formatBytes: binary units, and an unknown size says so", () => {
  assert.equal(formatBytes(0), "0 B");
  assert.equal(formatBytes(512), "512 B");
  assert.equal(formatBytes(1024), "1.0 KiB");
  assert.equal(formatBytes(133_000_000), "127 MiB");
  assert.equal(formatBytes(5 * 1024 ** 3), "5.0 GiB");
  // The states that must never become "0 B".
  assert.equal(formatBytes(null), "unknown");
  assert.equal(formatBytes(undefined), "unknown");
  assert.equal(formatBytes(Number.NaN), "unknown");
  assert.equal(formatBytes(-1), "unknown");
});

test("shortSha truncates but never invents", () => {
  assert.equal(shortSha("9c1f2a3b4d5e6f708192a3b4c5d6e7f809112233"), "9c1f2a3b4d5e");
  assert.equal(shortSha("abc123"), "abc123");
  assert.equal(shortSha(null), null);
  assert.equal(shortSha(""), null);
});

test("formatWhen: absent stays 'unknown', unparseable text is passed through", () => {
  assert.equal(formatWhen(null), "unknown");
  assert.equal(formatWhen(""), "unknown");
  assert.equal(formatWhen("not a date"), "not a date");
  assert.notEqual(formatWhen("2026-07-12T10:11:12+00:00"), "unknown");
});

test("serviceCell: a version when there is one, the reason when there is not", () => {
  assert.deepEqual(
    serviceCell({ name: "Meilisearch", version: "1.49.0", detail: "commit abc", url: "u", error: null }),
    { text: "1.49.0", tone: "ok", hint: "commit abc" },
  );
  const down = serviceCell({
    name: "Meilisearch",
    version: null,
    detail: null,
    url: "u",
    error: "unreachable — ConnectionRefusedError: nothing listening",
  });
  assert.match(down.text, /unreachable/);
  assert.equal(down.tone, "bad");
  // Even a probe that failed without saying why gets a word, not a blank.
  assert.equal(
    serviceCell({ name: "X", version: null, detail: null, url: "u", error: null }).text,
    "unknown",
  );
});

test("toolCell distinguishes absent from present-but-silent", () => {
  assert.equal(
    toolCell({ name: "ffprobe", purpose: "p", present: false, version: null, path: null, url: "u" }).text,
    "not installed",
  );
  const silent = toolCell({
    name: "tesseract",
    purpose: "p",
    present: true,
    version: null,
    path: "/usr/bin/tesseract",
    url: "u",
  });
  assert.equal(silent.text, "installed, version unknown");
  assert.equal(silent.hint, "/usr/bin/tesseract");
  assert.equal(
    toolCell({ name: "ffmpeg", purpose: "p", present: true, version: "6.1.1", path: "/x", url: "u" }).text,
    "6.1.1",
  );
});

test("packageCell flags a declared dependency that is not installed", () => {
  assert.deepEqual(packageCell({ name: "fastapi", version: "0.141.1", url: "u", optional: false }), {
    text: "0.141.1",
    tone: "ok",
  });
  assert.deepEqual(packageCell({ name: "ghost", version: null, url: "u", optional: false }), {
    text: "not installed",
    tone: "bad",
  });
});

const EMBED: AboutEmbedding = {
  enabled: false,
  name: "BAAI/bge-small-en-v1.5",
  repo: "Qdrant/bge-small-en-v1.5-onnx-Q",
  file: "model_optimized.onnx",
  dimensions: 384,
  cache_dir: "/config/models",
  downloaded: false,
  revision: null,
  size: null,
  downloaded_at: null,
  path: null,
  model_url: "https://huggingface.co/Qdrant/bge-small-en-v1.5-onnx-Q",
  revision_url: null,
  license_note: "Runs entirely locally.",
};

test("embeddingSummary: the never-downloaded default reads as normal, not broken", () => {
  // Default install: off and absent. Muted, and it explains itself.
  const off = embeddingSummary(EMBED);
  assert.equal(off.tone, "muted");
  assert.match(off.text, /not downloaded/);
  assert.match(off.text, /semantic search is off/);

  // Just switched on, no embed job has run yet.
  const pending = embeddingSummary({ ...EMBED, enabled: true });
  assert.equal(pending.tone, "muted");
  assert.match(pending.text, /first embedding job/);

  // Cached and live.
  const live = embeddingSummary({ ...EMBED, enabled: true, downloaded: true });
  assert.equal(live.tone, "ok");
  assert.match(live.text, /downloaded and in use/);

  // Cached but the feature was turned back off — still a distinct state.
  assert.match(embeddingSummary({ ...EMBED, downloaded: true }).text, /semantic search is off/);
});

const ABOUT: About = {
  application: {
    app_version: "0.1.0",
    build_stamp: null,
    source_url: "https://github.com/pwsh/filearr",
    license: "AGPL-3.0-or-later",
    license_url: "https://www.gnu.org/licenses/agpl-3.0.html",
    python_version: "3.14.0",
    python_implementation: "CPython",
    platform: "Linux-6.8.12-x86_64-with-glibc2.36",
    system: "Linux",
    machine: "x86_64",
  },
  services: [
    { name: "PostgreSQL", version: "18.4", detail: "PostgreSQL 18.4 (Debian)", url: "https://p", error: null },
    { name: "Meilisearch", version: null, detail: null, url: "https://m", error: "unreachable — ConnectionRefusedError" },
  ],
  python_packages: [
    { name: "fastapi", version: "0.141.1", url: "https://f", optional: false },
    { name: "apprise", version: "1.12.0", url: "https://a", optional: true },
  ],
  host_tools: [
    { name: "ffprobe", purpose: "Video metadata", present: true, version: "7.1.1", path: "/usr/bin/ffprobe", url: "https://ff" },
    { name: "tesseract", purpose: "OCR", present: false, version: null, path: null, url: "https://t" },
  ],
  agents: { total: 3, versions: [{ version: "0.4.1", count: 2 }, { version: null, count: 1 }], error: null },
  embedding: EMBED,
};

const STACK: FrontendStack = {
  node: "24.9.0",
  built_at: "2026-08-10T09:00:00.000Z",
  packages: [{ name: "svelte", version: "5.56.8", kind: "build", url: "https://svelte.dev" }],
};

const FIXED_NOW = new Date("2026-08-10T12:00:00.000Z");

test("aboutMarkdown dumps every section, deterministically", () => {
  const md = aboutMarkdown(ABOUT, STACK, FIXED_NOW);
  for (const heading of [
    "# Filearr 0.1.0 — build stack",
    "## Services",
    "## Backend (Python)",
    "## Frontend (built bundle)",
    "## Extraction tools (this server)",
    "## Agent fleet",
    "## Embedding model",
  ]) {
    assert.ok(md.includes(heading), `missing section: ${heading}`);
  }
  assert.ok(md.includes("2026-08-10T12:00:00.000Z"), "capture time is injectable");
  assert.equal(md, aboutMarkdown(ABOUT, STACK, FIXED_NOW), "same inputs, same output");
});

test("aboutMarkdown never emits an empty version cell", () => {
  const md = aboutMarkdown(ABOUT, STACK, FIXED_NOW);
  // The unknown states appear as words, not gaps.
  assert.ok(md.includes("none (dev checkout)"), "a missing build stamp is named");
  assert.match(md, /unreachable/);
  assert.ok(md.includes("not installed"), "the absent tesseract is named");
  assert.ok(md.includes("unknown"), "the agent that never reported a version is named");
  // And the date is labelled for what it is, in the pasted text too.
  const cached = aboutMarkdown(
    { ...ABOUT, embedding: { ...EMBED, downloaded: true, revision: "a".repeat(40), revision_url: "https://hf/tree/a", size: 133_000_000, downloaded_at: "2026-07-12T10:11:12+00:00" } },
    STACK,
    FIXED_NOW,
  );
  assert.match(cached, /local download time, not the model's release date/);
  assert.ok(cached.includes("127 MiB"));
  assert.ok(cached.includes("https://hf/tree/a"));
  // Never-downloaded says so where the revision would be.
  assert.match(md, /\| Revision \| not downloaded \|/);
});

test("aboutMarkdown survives a build with no injected frontend stack", () => {
  const md = aboutMarkdown(ABOUT, null, FIXED_NOW);
  assert.ok(!md.includes("## Frontend (built bundle)"));
  assert.ok(md.includes("## Services"));
});

test("aboutMarkdown escapes pipes so a path cannot break the table", () => {
  const md = aboutMarkdown(
    {
      ...ABOUT,
      host_tools: [
        { name: "ffprobe", purpose: "p", present: true, version: "1|2", path: "/od|d", url: "https://x" },
      ],
    },
    null,
    FIXED_NOW,
  );
  assert.ok(md.includes("1\\|2"));
  assert.ok(md.includes("/od\\|d"));
});
