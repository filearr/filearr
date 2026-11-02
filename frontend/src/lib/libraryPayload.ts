// The library form's draft → API body, and a failed save → field errors.
// DOM-free so it runs on Node's test runner (tests/libraryPayload.node.test.ts).
//
// * Create sends the full body; edit sends ONLY the fields whose normalised
//   value differs from the library as loaded (B31). A full-form PATCH would
//   write back every field the form was seeded with, reverting anything that
//   changed server-side meanwhile (the scheduler flipping `enabled`, another
//   admin's edit). Nothing changed → `{}`, and the caller skips the PATCH.
// * Invalid input is a field error, never silently dropped or saved as blank
//   (B4 required fields, B20 extraction overrides).
// * Numbers are typed: drafts hold `number | null` from <input type="number">
//   (or a string from seeded state); `numOrNull`/`intOrNull` normalise both.

import type { HashPolicy, Library } from "./api";
import { ApiError, errorMessage } from "./apiErrors.ts";
import { intOrNull, numOrNull } from "./numInput.ts";

export interface OverrideField {
  key: string;
  label: string;
  unit: string;
  /** Display unit → stored unit. > 1 = a byte field edited in MiB (rounded to
   *  whole bytes on save); 1 = seconds / ratio (fractions kept). */
  scale: number;
  help: string;
}

const MiB = 1048576;

/** Mirrors schemas.LIBRARY_DESCRIPTION_MAX: characters after trimming. */
export const LIBRARY_DESCRIPTION_MAX = 2000;

/** Per-library extraction-limit overrides, stored server-side in raw units
 *  (bytes / seconds / ratio); byte fields are edited in MiB. Blank = fall back
 *  to the global FILEARR_* setting. Keys match config.EXTRACT_OVERRIDE_KEYS. */
export const OVERRIDE_FIELDS: OverrideField[] = [
  { key: "extract_timeout_seconds", label: "Extractor timeout", unit: "s", scale: 1,
    help: "Wall-clock budget per file before the parser is abandoned (FILEARR_EXTRACT_TIMEOUT_SECONDS). Raise for huge 3MF/PDF files on slow storage." },
  { key: "ffprobe_timeout_s", label: "ffprobe timeout", unit: "s", scale: 1,
    help: "Per-file ffprobe budget for video metadata (FILEARR_FFPROBE_TIMEOUT_S)." },
  { key: "document_max_bytes", label: "Document ceiling", unit: "MiB", scale: MiB,
    help: "Documents/spreadsheets larger than this are guard-skipped (FILEARR_DOCUMENT_MAX_BYTES)." },
  { key: "doc_decompressed_max", label: "Decompressed ceiling", unit: "MiB", scale: MiB,
    help: "Zip/docx whose DECLARED uncompressed total exceeds this are guard-skipped as decompression bombs (FILEARR_DOC_DECOMPRESSED_MAX). Raise for libraries of legitimately huge archives (e.g. 3D-print zips)." },
  { key: "doc_decompression_ratio", label: "Decompression ratio", unit: ":1", scale: 1,
    help: "Uncompressed:compressed ratio above which an archive is treated as a bomb (FILEARR_DOC_DECOMPRESSION_RATIO)." },
  { key: "doc_decompression_ratio_min_bytes", label: "Ratio guard floor", unit: "MiB", scale: MiB,
    help: "The ratio guard only applies to payloads larger than this (FILEARR_DOC_DECOMPRESSION_RATIO_MIN_BYTES)." },
  { key: "model3d_max_bytes", label: "3D model ceiling", unit: "MiB", scale: MiB,
    help: "3D models larger than this skip geometry extraction (FILEARR_MODEL3D_MAX_BYTES)." },
  { key: "email_max_bytes", label: "E-mail ceiling", unit: "MiB", scale: MiB,
    help: "E-mail files larger than this are guard-skipped (FILEARR_EMAIL_MAX_BYTES)." },
];

/** The form's working state, keyed by API field name. Text fields stay raw
 *  (trimmed on build); globs are newline-joined text; numbers are what
 *  bind:value wrote (null when emptied). */
export interface LibraryDraft {
  name: string;
  description: string;
  root_path: string;
  native_prefix: string;
  share_prefix: string;
  enabled: boolean;
  enabled_categories: string[];
  enabled_groups: string[];
  include_globs: string;
  exclude_globs: string;
  scan_cron: string;
  watch_mode: boolean;
  hash_policy: HashPolicy;
  hash_full_max_bytes: number | null;
  enabled_presets: string[];
  ocr_enabled: boolean;
  chunking_enabled: boolean;
  expose_gps: boolean;
  count_pruned_files: boolean;
  /** Display units (see OVERRIDE_FIELDS); null = blank. */
  extract_overrides: Record<string, number | null>;
  /** Number inputs whose DOM `validity.badInput` is set: an unparseable entry
   *  ("1e") binds null, which would otherwise read as blank. */
  bad_input?: string[];
}

export interface LibraryPayload {
  name?: string;
  description?: string | null;
  root_path?: string;
  native_prefix?: string | null;
  share_prefix?: string | null;
  enabled?: boolean;
  enabled_categories?: string[];
  enabled_groups?: string[];
  include_globs?: string[];
  exclude_globs?: string[];
  enabled_presets?: string[];
  scan_cron?: string | null;
  watch_mode?: boolean;
  hash_policy?: HashPolicy;
  hash_full_max_bytes?: number | null;
  extract_overrides?: Record<string, number> | null;
  ocr_enabled?: boolean;
  chunking_enabled?: boolean;
  expose_gps?: boolean;
  count_pruned_files?: boolean;
}

export type FieldErrors = Record<string, string>;

export type LibraryPayloadResult =
  | { ok: true; payload: LibraryPayload }
  | { ok: false; errors: FieldErrors };

/** The draft a library (or a new one, `null`) opens with. Arrays are copied. */
export function draftFromLibrary(library: Library | null): LibraryDraft {
  const ov = library?.extract_overrides ?? {};
  const extract_overrides = Object.fromEntries(
    OVERRIDE_FIELDS.map((f) => {
      const v = ov[f.key];
      return [f.key, v != null ? v / f.scale : null];
    }),
  );
  if (library === null) {
    return {
      name: "",
      description: "",
      root_path: "/data/media/",
      native_prefix: "",
      share_prefix: "",
      enabled: true,
      enabled_categories: [],
      enabled_groups: [],
      include_globs: "",
      exclude_globs: "",
      scan_cron: "",
      watch_mode: false,
      hash_policy: "auto",
      hash_full_max_bytes: null,
      enabled_presets: [],
      ocr_enabled: false,
      chunking_enabled: false,
      expose_gps: false,
      count_pruned_files: false,
      extract_overrides,
      bad_input: [],
    };
  }
  return {
    name: library.name,
    description: library.description ?? "",
    root_path: library.root_path,
    native_prefix: library.native_prefix ?? "",
    share_prefix: library.share_prefix ?? "",
    enabled: library.enabled,
    enabled_categories: [...(library.enabled_categories ?? [])],
    enabled_groups: [...(library.enabled_groups ?? [])],
    include_globs: (library.include_globs ?? []).join("\n"),
    exclude_globs: (library.exclude_globs ?? []).join("\n"),
    scan_cron: library.scan_cron ?? "",
    watch_mode: library.watch_mode,
    hash_policy: library.hash_policy,
    hash_full_max_bytes: library.hash_full_max_bytes ?? null,
    enabled_presets: [...(library.enabled_presets ?? [])],
    ocr_enabled: library.ocr_enabled,
    chunking_enabled: library.chunking_enabled,
    expose_gps: library.expose_gps,
    count_pruned_files: library.count_pruned_files,
    extract_overrides,
    bad_input: [],
  };
}

/** A glob textarea → its lines. Split on `\n`, strip only `\r` (CRLF
 *  paste), drop whitespace-only lines; every other line is kept VERBATIM —
 *  an escaped trailing space (`foo\ `) and a leading space are both
 *  meaningful in gitignore syntax, so trimming would change the pattern. */
export function linesToArray(s: string): string[] {
  return s
    .split("\n")
    .map((l) => l.replace(/\r/g, ""))
    .filter((l) => l.trim() !== "");
}

/** Stored glob array → the array the form would produce from it untouched
 *  (join + the same line rule), so comparison is like for like. */
const normGlobs = (a: string[] | null | undefined): string[] => linesToArray((a ?? []).join("\n"));

const blankToNull = (s: string): string | null => (s.trim() ? s.trim() : null);

/** Overrides in stored units, or null when none are set (the server also
 *  normalises `{}` to null). */
function buildOverrides(draft: LibraryDraft, errors: FieldErrors): Record<string, number> | null {
  const bad = new Set(draft.bad_input ?? []);
  const out: Record<string, number> = {};
  for (const f of OVERRIDE_FIELDS) {
    if (bad.has(f.key)) {
      errors[f.key] = "Enter a number, or leave blank.";
      continue;
    }
    const raw = draft.extract_overrides[f.key];
    if (raw === null || raw === undefined || (typeof raw === "string" && !(raw as string).trim())) continue;
    const n = numOrNull(raw);
    if (n === null || n <= 0) {
      errors[f.key] = "Enter a number greater than 0, or leave blank.";
      continue;
    }
    // MiB fields become whole bytes; scale-1 fields (seconds, ratio) keep
    // their fractions (B20).
    const v = f.scale > 1 ? Math.round(n * f.scale) : n;
    if (v <= 0) {
      errors[f.key] = "Too small: this rounds to 0 bytes.";
      continue;
    }
    out[f.key] = v;
  }
  return Object.keys(out).length ? out : null;
}

function buildHashCeiling(draft: LibraryDraft, errors: FieldErrors): number | null {
  if ((draft.bad_input ?? []).includes("hash_full_max_bytes")) {
    errors.hash_full_max_bytes = "Enter a whole number of bytes.";
    return null;
  }
  const raw = draft.hash_full_max_bytes;
  if (raw === null || raw === undefined || (typeof raw === "string" && !(raw as string).trim())) return null;
  const n = intOrNull(raw, { min: 1 });
  if (n === null) errors.hash_full_max_bytes = "Enter a whole number of bytes (1 or more), or leave blank.";
  return n;
}

/** Sorted-key JSON, so value-equal objects and arrays compare equal. */
function canon(v: unknown): string {
  if (Array.isArray(v)) return `[${v.map(canon).join(",")}]`;
  if (v && typeof v === "object") {
    const o = v as Record<string, unknown>;
    return `{${Object.keys(o).sort().map((k) => `${JSON.stringify(k)}:${canon(o[k])}`).join(",")}}`;
  }
  return JSON.stringify(v ?? null);
}

/** The library's stored values in the same normalised shape as a full body. */
function normaliseOriginal(l: Library): Required<LibraryPayload> {
  const ov = l.extract_overrides;
  return {
    name: l.name.trim(),
    description: blankToNull(l.description ?? ""),
    root_path: l.root_path.trim(),
    native_prefix: blankToNull(l.native_prefix ?? ""),
    share_prefix: blankToNull(l.share_prefix ?? ""),
    enabled: l.enabled,
    enabled_categories: l.enabled_categories ?? [],
    enabled_groups: l.enabled_groups ?? [],
    include_globs: normGlobs(l.include_globs),
    exclude_globs: normGlobs(l.exclude_globs),
    enabled_presets: l.enabled_presets ?? [],
    scan_cron: blankToNull(l.scan_cron ?? ""),
    watch_mode: l.watch_mode,
    hash_policy: l.hash_policy,
    hash_full_max_bytes: l.hash_full_max_bytes ?? null,
    extract_overrides: ov && Object.keys(ov).length ? ov : null,
    ocr_enabled: l.ocr_enabled,
    chunking_enabled: l.chunking_enabled,
    expose_gps: l.expose_gps,
    count_pruned_files: l.count_pruned_files,
  };
}

/** Draft → request body. Create (no `original`): the full body. Edit: only
 *  the fields that differ from `original` (possibly `{}`). */
export function buildLibraryPayload(draft: LibraryDraft, original?: Library | null): LibraryPayloadResult {
  const errors: FieldErrors = {};
  const name = draft.name.trim();
  const root_path = draft.root_path.trim();
  if (!name) errors.name = "Name is required.";
  if (!root_path) errors.root_path = "Root path is required.";
  const description = blankToNull(draft.description);
  if (description !== null && description.length > LIBRARY_DESCRIPTION_MAX) {
    errors.description =
      `Keep it to ${LIBRARY_DESCRIPTION_MAX.toLocaleString("en-US")} characters (currently ${description.length.toLocaleString("en-US")}).`;
  }
  const hash_full_max_bytes = buildHashCeiling(draft, errors);
  const extract_overrides = buildOverrides(draft, errors);
  if (Object.keys(errors).length) return { ok: false, errors };

  const full: Required<LibraryPayload> = {
    name,
    description,
    root_path,
    native_prefix: blankToNull(draft.native_prefix),
    share_prefix: blankToNull(draft.share_prefix),
    enabled: draft.enabled,
    enabled_categories: [...draft.enabled_categories],
    enabled_groups: [...draft.enabled_groups],
    include_globs: linesToArray(draft.include_globs),
    exclude_globs: linesToArray(draft.exclude_globs),
    enabled_presets: [...draft.enabled_presets],
    scan_cron: blankToNull(draft.scan_cron),
    watch_mode: draft.watch_mode,
    hash_policy: draft.hash_policy,
    hash_full_max_bytes,
    extract_overrides,
    ocr_enabled: draft.ocr_enabled,
    chunking_enabled: draft.chunking_enabled,
    expose_gps: draft.expose_gps,
    count_pruned_files: draft.count_pruned_files,
  };

  if (!original) {
    // Create: `enabled` is not on the Add form (new libraries start enabled);
    // empty taxonomy lists are omitted (server default = all types).
    const { enabled: _enabled, enabled_categories, enabled_groups, ...rest } = full;
    const payload: LibraryPayload = { ...rest };
    if (enabled_categories.length) payload.enabled_categories = enabled_categories;
    if (enabled_groups.length) payload.enabled_groups = enabled_groups;
    return { ok: true, payload };
  }

  const before = normaliseOriginal(original);
  const payload: Record<string, unknown> = {};
  for (const key of Object.keys(full) as (keyof LibraryPayload)[]) {
    if (canon(full[key]) !== canon(before[key])) payload[key] = full[key];
  }
  return { ok: true, payload: payload as LibraryPayload };
}

/** Fields the form renders an error on. A server error for any other field
 *  (scan_cron, presets, …) goes to the banner beside Save instead, so it is
 *  never attached to something invisible. */
export const ERROR_FIELDS: ReadonlySet<string> = new Set([
  "name", "description", "root_path", "native_prefix", "share_prefix",
  "include_globs", "exclude_globs", "hash_policy", "hash_full_max_bytes",
  "extract_overrides", ...OVERRIDE_FIELDS.map((f) => f.key),
]);

/** A failed create/update → field errors plus, when anything is left over, a
 *  banner sentence. 409 is the duplicate-name refusal (the only unique
 *  constraint a client-settable field can trip) → Name. A 422 pydantic list
 *  maps each item by the deepest `loc` element the form renders. */
export function libraryServerErrors(e: unknown): { fields: FieldErrors; message: string | null } {
  const fields: FieldErrors = {};
  if (e instanceof ApiError && e.status === 409 && typeof e.detail === "string" && e.detail.trim()) {
    fields.name = e.detail.trim();
    return { fields, message: null };
  }
  if (e instanceof ApiError && e.status === 422 && Array.isArray(e.detail)) {
    let unmapped = 0;
    for (const item of e.detail as unknown[]) {
      const { loc, msg } = (item && typeof item === "object" ? item : {}) as { loc?: unknown; msg?: unknown };
      const parts = Array.isArray(loc) ? loc.map(String) : [];
      const field = [...parts].reverse().find((p) => ERROR_FIELDS.has(p));
      const text = typeof msg === "string" ? msg.replace(/^Value error, /, "") : "";
      if (field && text) {
        fields[field] = fields[field] ? `${fields[field]}; ${text}` : text;
      } else {
        unmapped++;
      }
    }
    return { fields, message: unmapped ? errorMessage(e, { verb: "save this library" }) : null };
  }
  return { fields, message: errorMessage(e, { verb: "save this library" }) };
}
