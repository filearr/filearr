// cards/highlights.ts: the detail-card registry keyed by backend category
// (ruling 5, A13) and the keys each card renders (A14). cards/keyfacts.ts:
// `_`-prefixed keys stay out of the leftovers, but `_extract_error` is
// surfaced through extractError(). cards/format.ts: audio bitrate units (A32).

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { test } from "node:test";

import {
  BACKEND_CATEGORIES,
  CARD_REGISTRY,
  HIGHLIGHT_KEYS,
  cardFor,
  cardLabel,
  type CardName,
} from "../src/lib/cards/highlights.ts";
import { extractError, orderKeyFacts } from "../src/lib/cards/keyfacts.ts";
import { asStr, fmtAudioBitrate, fmtWhen } from "../src/lib/cards/format.ts";

// ---- registry ---------------------------------------------------------------

test("every CARD_REGISTRY key is a backend category", () => {
  const cats = new Set<string>(BACKEND_CATEGORIES);
  for (const k of Object.keys(CARD_REGISTRY)) assert.ok(cats.has(k), `${k} is not a category`);
  for (const gone of ["model3d", "spreadsheet", "audiobook"]) assert.equal(gone in CARD_REGISTRY, false);
});

test("BACKEND_CATEGORIES matches backend/filearr/file_groups.py", () => {
  assert.deepEqual(
    [...BACKEND_CATEGORIES],
    ["image", "audio", "video", "document", "three-d-cad", "development", "archive", "system", "other"],
  );
});

test("cardFor: category keyed, spreadsheet by group, fallback KeyFactsCard", () => {
  const cases: [string | null | undefined, string | null | undefined, CardName][] = [
    ["three-d-cad", undefined, "Model3dCard"],
    ["document", "spreadsheet", "SpreadsheetCard"],
    ["document", "pdf", "DocumentCard"],
    ["document", undefined, "DocumentCard"],
    ["video", undefined, "VideoCard"],
    ["audio", "audiobook", "AudioCard"],
    ["image", undefined, "ImageCard"],
    ["archive", undefined, "KeyFactsCard"],
    ["development", undefined, "KeyFactsCard"],
    ["system", undefined, "KeyFactsCard"],
    ["other", undefined, "KeyFactsCard"],
    ["spreadsheet", undefined, "KeyFactsCard"],
    ["model3d", undefined, "KeyFactsCard"],
    [null, null, "KeyFactsCard"],
    ["", undefined, "KeyFactsCard"],
    ["toString", undefined, "KeyFactsCard"],
  ];
  for (const [c, g, want] of cases) assert.equal(cardFor(c, g), want, `${c}/${g}`);
});

test("cardLabel: per card, group-specific labels, Details fallback", () => {
  assert.equal(cardLabel("three-d-cad"), "3D model");
  assert.equal(cardLabel("document", "spreadsheet"), "Spreadsheet");
  assert.equal(cardLabel("document"), "Document");
  assert.equal(cardLabel("audio", "audiobook"), "Audiobook");
  assert.equal(cardLabel("audio"), "Audio");
  assert.equal(cardLabel("archive"), "Details");
  assert.equal(cardLabel(undefined), "Details");
});

// ---- highlight keys ---------------------------------------------------------

// The CURATED lists the cards carried before P6 (the keys hidden from "More
// details"). Every one stays covered.
const OLD_CURATED: Record<keyof typeof HIGHLIGHT_KEYS, string[]> = {
  video: [
    "resolution", "video_codec", "audio_codec", "duration", "container",
    "hdr", "hdr_format", "width", "height", "frame_rate", "bitrate",
    "color_primaries", "color_transfer", "audio_tracks", "subtitle_tracks",
    "title", "year", "season", "episode",
  ],
  audio: [
    "artist", "album", "genre", "year", "duration", "bitrate", "samplerate",
    "channels", "title", "chapters", "chapter_count",
  ],
  image: ["width", "height", "format", "mode", "camera", "taken_at"],
  "three-d-cad": [
    "triangles", "vertices", "mesh_count", "bbox", "bbox_volume",
    "watertight", "file_format", "unsupported",
  ],
  document: [
    "pages", "author", "producer", "creator", "subject", "keywords",
    "encrypted", "paragraphs", "revision", "created", "modified",
    "title", "unsupported", "body_text",
  ],
  spreadsheet: [
    "sheets", "sheet_count", "author", "subject", "created", "modified",
    "title", "unsupported",
  ],
};

// A14: excluded from "More details" but rendered nowhere before.
const PREVIOUSLY_MISSING: Partial<Record<keyof typeof HIGHLIGHT_KEYS, string[]>> = {
  video: ["bitrate", "title", "year", "season", "episode", "color_primaries", "color_transfer"],
  audio: ["title", "chapter_count"],
  document: ["producer", "creator", "subject", "keywords", "revision", "title"],
  spreadsheet: ["subject", "title"],
};

test("HIGHLIGHT_KEYS covers every old CURATED key and the previously missing ones", () => {
  for (const [card, keys] of Object.entries(OLD_CURATED)) {
    const have = new Set(HIGHLIGHT_KEYS[card as keyof typeof HIGHLIGHT_KEYS]);
    for (const k of keys) assert.ok(have.has(k), `${card}: ${k}`);
  }
  for (const [card, keys] of Object.entries(PREVIOUSLY_MISSING)) {
    const have = new Set(HIGHLIGHT_KEYS[card as keyof typeof HIGHLIGHT_KEYS]);
    for (const k of keys!) assert.ok(have.has(k), `${card}: ${k}`);
  }
});

// The key list is both what a card renders and its `exclude`: check that each
// card's source reads every key it names (`m.key`) and passes the list on.
const CARD_FILES: Record<keyof typeof HIGHLIGHT_KEYS, string> = {
  video: "VideoCard",
  audio: "AudioCard",
  image: "ImageCard",
  document: "DocumentCard",
  spreadsheet: "SpreadsheetCard",
  "three-d-cad": "Model3dCard",
};

test("each card reads every key of its HIGHLIGHT_KEYS list and excludes the same list", () => {
  for (const [card, file] of Object.entries(CARD_FILES)) {
    const src = readFileSync(new URL(`../src/lib/cards/${file}.svelte`, import.meta.url), "utf8");
    assert.match(src, /HIGHLIGHT_KEYS\[/, `${file} imports its list`);
    assert.match(src, /exclude=\{KEYS\}/, `${file} excludes its list`);
    for (const k of HIGHLIGHT_KEYS[card as keyof typeof HIGHLIGHT_KEYS]) {
      assert.match(src, new RegExp(`\\bm\\.${k}\\b`), `${file} renders ${k}`);
    }
  }
});

// ---- keyfacts: _extract_error -----------------------------------------------

test("orderKeyFacts keeps _extract_error out of leftovers; extractError returns it", () => {
  const meta = { _extract_error: "ffprobe exited 1", _extract_error_kind: "error", codec: "h264" };
  const facts = orderKeyFacts(meta, [], []);
  assert.deepEqual(facts.map((f) => f.key), ["codec"]);
  assert.equal(extractError(meta), "ffprobe exited 1");
});

test("extractError: absent, blank, and structured values", () => {
  assert.equal(extractError({}), null);
  assert.equal(extractError({ _extract_error: "  " }), null);
  assert.equal(extractError({ _extract_error: null }), null);
  assert.equal(extractError({ _extract_error: { msg: "x" } }), '{"msg":"x"}');
});

// ---- format -----------------------------------------------------------------

test("asStr: objects become JSON, never [object Object]", () => {
  assert.equal(asStr({ a: 1 }), '{"a":1}');
  assert.equal(asStr([1, 2]), "[1,2]");
});

test("fmtAudioBitrate: bit/s above 10000, kbit/s otherwise", () => {
  assert.equal(fmtAudioBitrate(1411200), "1411 kbps"); // mutagen / ffprobe bit/s
  assert.equal(fmtAudioBitrate(320), "320 kbps"); // tinytag kbit/s
  assert.equal(fmtAudioBitrate(128.4), "128 kbps");
  assert.equal(fmtAudioBitrate("256000"), "256 kbps");
  assert.equal(fmtAudioBitrate(0), null);
  assert.equal(fmtAudioBitrate(null), null);
  assert.equal(fmtAudioBitrate("x"), null);
});

test("fmtWhen: EXIF and ISO dates render local with a UTC title; absent drops; unparseable stays raw", () => {
  process.env.TZ = "UTC";
  assert.deepEqual(fmtWhen("2024:05:06 07:08:09"), { value: "2024-05-06 07:08", title: "2024-05-06T07:08:09Z" });
  assert.deepEqual(fmtWhen("2024-05-06T07:08:09Z"), { value: "2024-05-06 07:08", title: "2024-05-06T07:08:09Z" });
  assert.deepEqual(fmtWhen(null), { value: null });
  assert.deepEqual(fmtWhen(""), { value: null });
  assert.deepEqual(fmtWhen("D:20240506"), { value: "D:20240506" }, "an unparseable value is not hidden");
});
