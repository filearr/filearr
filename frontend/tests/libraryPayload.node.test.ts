// buildLibraryPayload: the library form's draft → API body, with field errors
// instead of silent drops (B4, B20) and, on edit, only the changed fields
// (B31: a full-form PATCH would revert a concurrent flip such as `enabled`).
// libraryServerErrors: a failed save → field errors (409 name, 422 loc) or a
// banner sentence.

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError } from "../src/lib/apiErrors.ts";
import type { Library } from "../src/lib/api";
import {
  LIBRARY_DESCRIPTION_MAX,
  OVERRIDE_FIELDS,
  buildLibraryPayload,
  draftFromLibrary,
  libraryServerErrors,
  linesToArray,
  type LibraryDraft,
} from "../src/lib/libraryPayload.ts";

const MiB = 1048576;

function lib(over: Partial<Library> = {}): Library {
  return {
    id: "L1",
    name: "Movies",
    description: null,
    root_path: "/data/media/movies",
    native_prefix: null,
    share_prefix: null,
    enabled_categories: ["video"],
    enabled_groups: [],
    include_globs: ["keep.me"],
    exclude_globs: ["**/*.partial"],
    enabled_presets: [],
    scan_cron: "0 3 * * *",
    watch_mode: false,
    hash_policy: "auto",
    hash_full_max_bytes: null,
    extract_overrides: { document_max_bytes: 2 * MiB, extract_timeout_seconds: 30 },
    ocr_enabled: false,
    chunking_enabled: false,
    expose_gps: false,
    count_pruned_files: false,
    enabled: true,
    last_scan: null,
    last_scoped_scan: null,
    share_prefix_effective: null,
    share_prefix_source: "none",
    share_unc_effective: null,
    source_agent_id: null,
    agent_name: null,
    agent_status: null,
    agent_last_seen_at: null,
    agent_last_reconcile_at: null,
    ...over,
  };
}

function newDraft(over: Partial<LibraryDraft> = {}): LibraryDraft {
  return { ...draftFromLibrary(null), name: "Movies", root_path: "/data/media/movies", ...over };
}

function okPayload(r: ReturnType<typeof buildLibraryPayload>) {
  assert.ok(r.ok, `expected ok, got ${JSON.stringify(r)}`);
  return r.payload;
}
function errs(r: ReturnType<typeof buildLibraryPayload>) {
  assert.ok(!r.ok, `expected errors, got ${JSON.stringify(r)}`);
  return r.errors;
}

// ---- draft seeding ----

test("draftFromLibrary(null): create defaults", () => {
  const d = draftFromLibrary(null);
  assert.equal(d.name, "");
  assert.equal(d.root_path, "/data/media/");
  assert.equal(d.enabled, true);
  assert.equal(d.include_globs, "");
  assert.equal(d.hash_policy, "auto");
  assert.equal(d.hash_full_max_bytes, null);
  for (const f of OVERRIDE_FIELDS) assert.equal(d.extract_overrides[f.key], null);
});

test("draftFromLibrary(lib): globs joined, overrides in display units", () => {
  const d = draftFromLibrary(lib());
  assert.equal(d.include_globs, "keep.me");
  assert.equal(d.exclude_globs, "**/*.partial");
  assert.equal(d.scan_cron, "0 3 * * *");
  assert.equal(d.extract_overrides.document_max_bytes, 2);
  assert.equal(d.extract_overrides.extract_timeout_seconds, 30);
  assert.equal(d.extract_overrides.email_max_bytes, null);
});

test("draftFromLibrary copies arrays (no aliasing of the prop)", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.enabled_categories.push("audio");
  assert.deepEqual(l.enabled_categories, ["video"]);
});

// ---- create ----

test("create: trims name/root_path and blank optionals become null", () => {
  const p = okPayload(buildLibraryPayload(newDraft({
    name: "  Movies ", root_path: " /data/media/movies ",
    native_prefix: "  ", share_prefix: " smb://tower/media ", scan_cron: "  ",
  })));
  assert.equal(p.name, "Movies");
  assert.equal(p.root_path, "/data/media/movies");
  assert.equal(p.native_prefix, null);
  assert.equal(p.share_prefix, "smb://tower/media");
  assert.equal(p.scan_cron, null);
});

test("create: blank name / root_path are field errors (B4)", () => {
  const e = errs(buildLibraryPayload(newDraft({ name: "   ", root_path: "" })));
  assert.ok(e.name);
  assert.ok(e.root_path);
});

test("create: full body; empty taxonomy omitted; enabled not sent", () => {
  const p = okPayload(buildLibraryPayload(newDraft()));
  assert.equal("enabled_categories" in p, false);
  assert.equal("enabled_groups" in p, false);
  assert.equal("enabled" in p, false);
  assert.deepEqual(p.include_globs, []);
  assert.deepEqual(p.exclude_globs, []);
  assert.equal(p.hash_policy, "auto");
  assert.equal(p.hash_full_max_bytes, null);
  assert.equal(p.extract_overrides, null);
  assert.equal(p.watch_mode, false);
});

test("create: description trimmed; blank → null (B: library description)", () => {
  assert.equal(okPayload(buildLibraryPayload(newDraft({ description: "  Films\nfrom the NAS  " }))).description,
    "Films\nfrom the NAS");
  assert.equal(okPayload(buildLibraryPayload(newDraft({ description: "  \n " }))).description, null);
  assert.equal(okPayload(buildLibraryPayload(newDraft())).description, null);
});

test("description: the cap counts trimmed characters; one over is a field error", () => {
  const at = "x".repeat(LIBRARY_DESCRIPTION_MAX);
  assert.equal(okPayload(buildLibraryPayload(newDraft({ description: ` ${at} ` }))).description, at);
  const e = errs(buildLibraryPayload(newDraft({ description: `${at}x` })));
  assert.match(e.description, /2,?000/);
});

test("glob textareas → lines verbatim; only \\r stripped, blank lines dropped", () => {
  assert.deepEqual(linesToArray("a/**\r\n\n  \r\nb.txt\n"), ["a/**", "b.txt"]);
  // Leading/trailing spaces are kept: an escaped trailing space is legal
  // gitignore (`foo\ ` matches "foo "), and a leading space is part of the name.
  assert.deepEqual(linesToArray("foo\\ \n bar\n\t\n"), ["foo\\ ", " bar"]);
  const p = okPayload(buildLibraryPayload(newDraft({ exclude_globs: "x\r\n\n \ny\n" })));
  assert.deepEqual(p.exclude_globs, ["x", "y"]);
});

// ---- numbers ----

test("hash ceiling: integer ≥1 passes as a number (B1)", () => {
  const p = okPayload(buildLibraryPayload(newDraft({ hash_full_max_bytes: 4096 })));
  assert.equal(p.hash_full_max_bytes, 4096);
});

test("hash ceiling: string from seeded state still typed (B1)", () => {
  const p = okPayload(buildLibraryPayload(newDraft({ hash_full_max_bytes: "4096" as unknown as number })));
  assert.strictEqual(p.hash_full_max_bytes, 4096);
});

test("hash ceiling: 0, negative, fractional → field error", () => {
  for (const v of [0, -5, 1.5]) {
    const e = errs(buildLibraryPayload(newDraft({ hash_full_max_bytes: v })));
    assert.ok(e.hash_full_max_bytes, `value ${v}`);
  }
});

test("badInput (unparseable entry that binds null) → field error, not blank", () => {
  const e = errs(buildLibraryPayload(newDraft({
    hash_full_max_bytes: null, bad_input: ["hash_full_max_bytes", "document_max_bytes"],
  })));
  assert.ok(e.hash_full_max_bytes);
  assert.ok(e.document_max_bytes);
});

test("overrides: only byte fields are rounded; seconds/ratio keep fractions (B20)", () => {
  const p = okPayload(buildLibraryPayload(newDraft({
    extract_overrides: {
      ...draftFromLibrary(null).extract_overrides,
      document_max_bytes: 1.5,
      extract_timeout_seconds: 2.5,
      doc_decompression_ratio: 12.25,
      email_max_bytes: 0.3333333,
    },
  })));
  assert.deepEqual(p.extract_overrides, {
    document_max_bytes: 1.5 * MiB,
    extract_timeout_seconds: 2.5,
    doc_decompression_ratio: 12.25,
    email_max_bytes: Math.round(0.3333333 * MiB),
  });
});

test("overrides: ≤0 or a byte value rounding to 0 is a field error, not dropped (B20)", () => {
  const base = draftFromLibrary(null).extract_overrides;
  for (const [key, v] of [
    ["extract_timeout_seconds", 0],
    ["ffprobe_timeout_s", -1],
    ["document_max_bytes", 1e-9],
  ] as const) {
    const e = errs(buildLibraryPayload(newDraft({ extract_overrides: { ...base, [key]: v } })));
    assert.ok(e[key], `${key}=${v}`);
  }
});

test("overrides: all blank → null", () => {
  const p = okPayload(buildLibraryPayload(newDraft()));
  assert.equal(p.extract_overrides, null);
});

// ---- edit: changed fields only (B31) ----

test("edit: untouched draft → empty payload", () => {
  const l = lib();
  assert.deepEqual(okPayload(buildLibraryPayload(draftFromLibrary(l), l)), {});
});

test("edit: untouched draft with every override set → empty payload (round-trip exact)", () => {
  const l = lib({
    extract_overrides: {
      extract_timeout_seconds: 12.5, ffprobe_timeout_s: 7, document_max_bytes: 1000000,
      doc_decompressed_max: 3 * MiB, doc_decompression_ratio: 99.5,
      doc_decompression_ratio_min_bytes: 123457, model3d_max_bytes: 5 * MiB, email_max_bytes: 1,
    },
    hash_full_max_bytes: 65536,
  });
  assert.deepEqual(okPayload(buildLibraryPayload(draftFromLibrary(l), l)), {});
});

test("edit: only the changed field is sent (a concurrent `enabled` flip survives)", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.watch_mode = true;
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { watch_mode: true });
});

test("edit: arrays compared by value, not identity", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.enabled_categories = ["video"]; // new array, same contents
  d.exclude_globs = "**/*.partial\r\n\n  \n"; // same lines once \\r and blanks go
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
  d.exclude_globs = "**/*.partial "; // a trailing space IS a change now
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { exclude_globs: ["**/*.partial "] });
  d.exclude_globs = "**/*.partial";
  d.enabled_categories = [];
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { enabled_categories: [] });
});

test("edit: stored globs go through the same line rule (untouched form = no change)", () => {
  // A stored array the form would re-split differently (a blank entry, a
  // `\\r`, an escaped trailing space) still compares equal when untouched.
  const l = lib({ exclude_globs: ["foo\\ ", "", "bar\r"], include_globs: [" keep.me"] });
  const d = draftFromLibrary(l);
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
});

test("edit: whitespace-only name change is not a change; trimmed rename is", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.name = " Movies ";
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
  d.name = " Films ";
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { name: "Films" });
});

test("edit: blank name is a field error (B4)", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.name = "  ";
  assert.ok(errs(buildLibraryPayload(d, l)).name);
});

test("edit: clearing cron sends null; clearing all overrides sends null", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.scan_cron = "";
  for (const f of OVERRIDE_FIELDS) d.extract_overrides[f.key] = null;
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { scan_cron: null, extract_overrides: null });
});

test("edit: changing one override sends the whole overrides object", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.extract_overrides.email_max_bytes = 4;
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {
    extract_overrides: { document_max_bytes: 2 * MiB, extract_timeout_seconds: 30, email_max_bytes: 4 * MiB },
  });
});

test("edit: description set, whitespace-only change ignored, cleared → null", () => {
  const l = lib({ description: "Films" });
  const d = draftFromLibrary(l);
  assert.equal(d.description, "Films");
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
  d.description = " Films ";
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
  d.description = "Feature films";
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { description: "Feature films" });
  d.description = "   ";
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { description: null });
});

test("edit: a library with no description seeds a blank draft and sends nothing", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  assert.equal(d.description, "");
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), {});
});

test("edit: enabled is sent when toggled", () => {
  const l = lib();
  const d = draftFromLibrary(l);
  d.enabled = false;
  assert.deepEqual(okPayload(buildLibraryPayload(d, l)), { enabled: false });
});

// ---- server errors ----

test("409 duplicate name → Name field error", () => {
  const e = new ApiError(409, JSON.stringify({ detail: 'a library named "Movies" already exists' }));
  const r = libraryServerErrors(e);
  assert.equal(r.fields.name, 'a library named "Movies" already exists');
  assert.equal(r.message, null);
});

test("422 pydantic loc → field by loc[-1], 'Value error, ' prefix dropped", () => {
  const e = new ApiError(422, JSON.stringify({
    detail: [
      { loc: ["body", "include_globs"], msg: "Value error, line 2 'foo\\\\': not a valid gitignore pattern (x)", type: "value_error" },
      { loc: ["body", "name"], msg: "String should have at least 1 character", type: "string_too_short" },
    ],
  }));
  const r = libraryServerErrors(e);
  assert.equal(r.fields.include_globs, "line 2 'foo\\\\': not a valid gitignore pattern (x)");
  assert.equal(r.fields.name, "String should have at least 1 character");
  assert.equal(r.message, null);
});

test("422 on description → Description field error", () => {
  const e = new ApiError(422, JSON.stringify({
    detail: [{ loc: ["body", "description"], msg: "Value error, description must be at most 2000 characters (got 2001)" }],
  }));
  const r = libraryServerErrors(e);
  assert.equal(r.fields.description, "description must be at most 2000 characters (got 2001)");
  assert.equal(r.message, null);
});

test("422 nested override key loc → that override's field", () => {
  const e = new ApiError(422, JSON.stringify({
    detail: [{ loc: ["body", "extract_overrides", "email_max_bytes"], msg: "Input should be a valid number", type: "x" }],
  }));
  assert.equal(libraryServerErrors(e).fields.email_max_bytes, "Input should be a valid number");
});

test("422 with an unknown loc or a string detail → banner", () => {
  const e1 = new ApiError(422, JSON.stringify({ detail: "invalid scan_cron: bad field" }));
  const r1 = libraryServerErrors(e1);
  assert.deepEqual(r1.fields, {});
  assert.equal(r1.message, "invalid scan_cron: bad field");
  const e2 = new ApiError(422, JSON.stringify({ detail: [{ loc: ["body", "mystery"], msg: "nope" }] }));
  const r2 = libraryServerErrors(e2);
  assert.deepEqual(r2.fields, {});
  assert.ok(r2.message);
});

test("other errors → banner via errorMessage", () => {
  const r = libraryServerErrors(new ApiError(500, "boom"));
  assert.deepEqual(r.fields, {});
  assert.equal(r.message, "The server returned an error (500).");
});
