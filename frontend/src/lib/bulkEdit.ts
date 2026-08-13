// IN-T4 — the decision core behind bulk metadata editing, DOM-FREE.
//
// `POST /items/batch` takes a DIFFERENT patch per item (`dict[item_id ->
// ItemPatch]`), and `tags` REPLACES the whole list by design. Those two facts
// together are why "add tag X to 300 files" has to be computed on the CLIENT:
// there is no add/remove verb on the wire, so the UI reads each item's current
// tags, applies the set operation itself, and sends 300 individually-correct
// lists. Get that wrong and a bulk edit silently deletes tags — which is why
// the computation lives here, as pure functions, under test, instead of inline
// in a Svelte component.
//
// Everything in this module is pure and browser-free (no DOM, no fetch, no
// Svelte runes) so `frontend/tests/bulkEdit.node.test.ts` can exercise it on
// Node's built-in runner.

import type { CustomField } from "./api";

/** Server-enforced cap on one `POST /items/batch` request (Agent A rejects >500
 *  keys with 413). The UI chunks to exactly this, so the cap is never a user-
 *  visible error — a 2,000-item selection is four requests. */
export const BATCH_CHUNK = 500;

/** Split a list into fixed-size chunks. Empty input → no chunks (never a single
 *  empty request). */
export function chunk<T>(xs: readonly T[], size = BATCH_CHUNK): T[][] {
  if (size < 1) throw new Error("chunk size must be >= 1");
  const out: T[][] = [];
  for (let i = 0; i < xs.length; i += size) out.push(xs.slice(i, i + size));
  return out;
}

// --------------------------------------------------------------------------- //
// Tag set arithmetic                                                           //
// --------------------------------------------------------------------------- //

// Tag comparison is CASE-INSENSITIVE while the stored spelling is PRESERVED.
// Rationale: the tag type-ahead offers existing values, but a bulk bar also
// accepts free text, and a user who types "HDR" over a catalogue of "hdr" means
// the same tag. Adding would otherwise create a near-duplicate that no facet
// groups, and removing would silently no-op. We never rewrite an item's existing
// spelling — only membership changes.
const norm = (t: string): string => t.trim().toLowerCase();

/** Union: existing tags first (spelling preserved), then the genuinely-new ones
 *  in the order the user typed them. */
export function addTags(current: readonly string[], add: readonly string[]): string[] {
  const seen = new Set(current.map(norm));
  const out = [...current];
  for (const raw of add) {
    const t = raw.trim();
    if (!t || seen.has(norm(t))) continue;
    seen.add(norm(t));
    out.push(t);
  }
  return out;
}

/** Difference: drop every tag matching one of ``remove`` (case-insensitively). */
export function removeTags(current: readonly string[], remove: readonly string[]): string[] {
  const drop = new Set(remove.map(norm).filter(Boolean));
  return current.filter((t) => !drop.has(norm(t)));
}

/** The new full tag list for one item, or ``null`` when the operations are a
 *  no-op for it. Returning null matters: an unchanged item must not have `tags`
 *  in its patch at all, so it records no pointless ItemVersion audit row and
 *  cannot be affected by a concurrent edit racing our stale read. */
export function computeTagPatch(
  current: readonly string[],
  add: readonly string[],
  remove: readonly string[],
): string[] | null {
  if (!add.length && !remove.length) return null;
  // Remove BEFORE add, so "remove x, add x" is a normalise-the-spelling
  // operation rather than a self-cancelling one.
  const next = addTags(removeTags(current, remove), add);
  if (next.length === current.length && next.every((t, i) => t === current[i])) return null;
  return next;
}

// --------------------------------------------------------------------------- //
// Custom-field applicability                                                   //
// --------------------------------------------------------------------------- //

/** The two item facets that decide whether a custom field applies. Deliberately
 *  a structural type: search hits, item records and report rows all satisfy it. */
export interface FieldTarget {
  file_category: string;
  library_id: string;
}

/** Does ``def`` apply to this one item? Empty ``applies_to`` = every category;
 *  empty ``library_ids`` = every library (the definition's own semantics, see
 *  CustomFieldsPanel / KeyFactsCard). */
export function fieldApplies(def: CustomField, target: FieldTarget): boolean {
  const catOk = !def.applies_to?.length || def.applies_to.includes(target.file_category);
  const libOk = !def.library_ids?.length || def.library_ids.includes(target.library_id);
  return catOk && libOk;
}

/** The INTERSECTION: fields applicable to EVERY selected item.
 *
 *  Intersection, not union, on purpose. A bulk write is one value applied to the
 *  whole selection; offering a field that only some items accept would produce a
 *  batch where an unpredictable subset 422s — the exact partial-failure noise
 *  this feature is supposed to make rare. An empty selection offers nothing. */
export function applicableFields(
  defs: readonly CustomField[],
  targets: readonly FieldTarget[],
): CustomField[] {
  if (!targets.length) return [];
  return defs.filter((d) => targets.every((t) => fieldApplies(d, t)));
}

// --------------------------------------------------------------------------- //
// Typed value coercion                                                         //
// --------------------------------------------------------------------------- //

export type CoerceResult =
  | { ok: true; value: string | number | boolean }
  | { ok: false; error: string };

/** Turn a form control's raw value into the JSON type the custom field declares.
 *
 *  The backend type-checks these too (P4-T4, structured 422) — this is the
 *  friendly first pass, not the enforcement. The ONE case where the client is
 *  the only guard is `select`: option membership is documented as not
 *  server-enforced, so constraining the UI to the defined options is what keeps
 *  a typo out of user_metadata. */
export function coerceCustomValue(def: CustomField, raw: string | boolean): CoerceResult {
  switch (def.data_type) {
    case "boolean":
      return { ok: true, value: typeof raw === "boolean" ? raw : raw === "true" };
    case "integer": {
      const s = String(raw).trim();
      const n = Number(s);
      if (!s || !Number.isInteger(n)) return { ok: false, error: `${def.label} must be a whole number` };
      return { ok: true, value: n };
    }
    case "float": {
      const s = String(raw).trim();
      const n = Number(s);
      if (!s || !Number.isFinite(n)) return { ok: false, error: `${def.label} must be a number` };
      return { ok: true, value: n };
    }
    case "select": {
      const s = String(raw);
      const opts = def.select_options ?? [];
      if (!opts.includes(s)) return { ok: false, error: `${def.label}: pick one of the defined options` };
      return { ok: true, value: s };
    }
    case "date": {
      const s = String(raw).trim();
      // <input type="date"> already yields YYYY-MM-DD; a typed value might not.
      if (!/^\d{4}-\d{2}-\d{2}$/.test(s)) return { ok: false, error: `${def.label} must be a date (YYYY-MM-DD)` };
      return { ok: true, value: s };
    }
    case "url": {
      const s = String(raw).trim();
      if (!s) return { ok: false, error: `${def.label} must not be empty` };
      return { ok: true, value: s };
    }
    default: {
      const s = String(raw);
      if (!s.trim()) return { ok: false, error: `${def.label} must not be empty` };
      return { ok: true, value: s };
    }
  }
}

// --------------------------------------------------------------------------- //
// Patch construction                                                           //
// --------------------------------------------------------------------------- //

/** The wire shape of one item's patch (mirrors the backend `ItemPatch`).
 *  ABSENT key = untouched; explicit ``null`` = clear. Note the deliberate
 *  omission of `title`: the design contract keeps title single-item only,
 *  because one title on N files is nearly always a mistake. */
export interface ItemPatchBody {
  title?: string | null;
  year?: number | null;
  tags?: string[];
  user_metadata?: Record<string, unknown>;
}

/** One selected item as the patch builder needs it: identity + the current tag
 *  list (already resolved from the search hit, or re-fetched when the hit did
 *  not carry one). */
export interface BulkTarget extends FieldTarget {
  id: string;
  tags: string[];
}

/** What the bulk bar asks for. ``"none"`` means the operator did not touch that
 *  control at all — the distinction from ``"clear"`` is the whole point (absent
 *  vs explicit null on the wire). */
export interface BulkOps {
  tagsAdd: string[];
  tagsRemove: string[];
  yearMode: "none" | "set" | "clear";
  year: number | null;
  fieldName: string;
  fieldMode: "none" | "set" | "clear";
  fieldValue: unknown;
}

export const EMPTY_OPS: BulkOps = {
  tagsAdd: [],
  tagsRemove: [],
  yearMode: "none",
  year: null,
  fieldName: "",
  fieldMode: "none",
  fieldValue: null,
};

/** True when ``ops`` would change nothing anywhere — drives the Apply button's
 *  disabled state so a no-op never costs a round trip. */
export function opsAreEmpty(ops: BulkOps): boolean {
  return (
    !ops.tagsAdd.length &&
    !ops.tagsRemove.length &&
    ops.yearMode === "none" &&
    (ops.fieldMode === "none" || !ops.fieldName)
  );
}

/**
 * Build the per-item patch map for `POST /items/batch`.
 *
 * Items whose patch would be empty are OMITTED entirely (see computeTagPatch):
 * sending them would burn request budget, write audit rows and re-index
 * documents for a no-change edit.
 */
export function buildPatches(
  targets: readonly BulkTarget[],
  ops: BulkOps,
): Record<string, ItemPatchBody> {
  const out: Record<string, ItemPatchBody> = {};
  for (const t of targets) {
    const patch: ItemPatchBody = {};
    const tags = computeTagPatch(t.tags ?? [], ops.tagsAdd, ops.tagsRemove);
    if (tags) patch.tags = tags;
    if (ops.yearMode === "set" && ops.year != null) patch.year = ops.year;
    else if (ops.yearMode === "clear") patch.year = null; // explicit null = clear
    if (ops.fieldName && ops.fieldMode !== "none") {
      // user_metadata merges key-by-key server-side; an explicit null POPS the
      // key (single-PATCH semantics, which Agent A is bringing to batch too).
      patch.user_metadata = {
        [ops.fieldName]: ops.fieldMode === "clear" ? null : ops.fieldValue,
      };
    }
    if (Object.keys(patch).length) out[t.id] = patch;
  }
  return out;
}

// --------------------------------------------------------------------------- //
// Per-item result reading                                                      //
// --------------------------------------------------------------------------- //

/** One item that the server refused, with a human-readable reason. */
export interface BulkFailure {
  id: string;
  reason: string;
}

export interface BulkSummary {
  ok: string[];
  failures: BulkFailure[];
}

/** Read `{"results": {id: "ok" | "error: …" | {error:"validation", detail:[…]}}}`.
 *
 *  Every non-"ok" value is surfaced, never swallowed: per-item RBAC denials
 *  arrive here as plain strings ("error: 403 …"), and a bulk edit that silently
 *  dropped a third of a selection because a path grant denied it would be worse
 *  than one that failed outright. Failures are DATA in this feature. */
export function summarizeResults(results: Record<string, unknown>): BulkSummary {
  const ok: string[] = [];
  const failures: BulkFailure[] = [];
  for (const [id, v] of Object.entries(results ?? {})) {
    if (v === "ok") {
      ok.push(id);
      continue;
    }
    failures.push({ id, reason: describeResult(v) });
  }
  return { ok, failures };
}

/** Flatten one per-item result value into a sentence. Handles the two documented
 *  failure shapes plus anything unexpected (never renders "[object Object]"). */
export function describeResult(v: unknown): string {
  if (typeof v === "string") return v.replace(/^error:\s*/, "");
  if (v && typeof v === "object") {
    const o = v as { error?: unknown; detail?: unknown };
    if (Array.isArray(o.detail)) {
      const parts = o.detail.map((d) => {
        if (typeof d === "string") return d;
        const rec = d as { loc?: unknown; msg?: unknown; field?: unknown };
        const where = Array.isArray(rec.loc) ? rec.loc.join(".") : String(rec.field ?? "");
        const msg = typeof rec.msg === "string" ? rec.msg : JSON.stringify(d);
        return where ? `${where}: ${msg}` : msg;
      });
      return parts.join("; ") || String(o.error ?? "validation error");
    }
    if (typeof o.error === "string") return o.error;
  }
  return JSON.stringify(v);
}

/** Merge several chunk responses into one summary (the UI submits in chunks of
 *  BATCH_CHUNK but reports once). */
export function mergeSummaries(parts: readonly BulkSummary[]): BulkSummary {
  return {
    ok: parts.flatMap((p) => p.ok),
    failures: parts.flatMap((p) => p.failures),
  };
}

// --------------------------------------------------------------------------- //
// Selection range (shift-click)                                                //
// --------------------------------------------------------------------------- //

/** The inclusive index range a shift-click spans, normalised for either
 *  direction. Pure so the SearchPage's list and grid views can share it (and so
 *  the off-by-one is tested rather than eyeballed twice). */
export function rangeIndices(anchor: number, target: number): number[] {
  const lo = Math.min(anchor, target);
  const hi = Math.max(anchor, target);
  const out: number[] = [];
  for (let i = lo; i <= hi; i++) out.push(i);
  return out;
}
