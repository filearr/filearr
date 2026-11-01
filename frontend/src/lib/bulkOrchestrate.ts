// P6 Task 5 — one bulk-edit run, DOM-free with the network injected.
//
// A3: `tags` REPLACES the whole list on the wire, so the per-item arithmetic
// must start from the item's LIVE tags. The search hit is an index snapshot:
// an item edited in the detail pane a moment ago still shows its old tags there
// (the index syncs asynchronously), and computing from it would silently drop
// the new ones. So whenever the ops touch tags, EVERY target is re-read with
// GET /items/{id} (20 at a time); ops that do not touch tags need nothing but
// the hit's identity and make no extra request.
//
// A26: the patches go out in chunks of BATCH_CHUNK. A request-level failure
// on chunk k stops the run and returns what chunks 1..k−1 committed, so the
// page can mirror those rows and say "N updated before the failure; M not
// attempted" instead of hiding a partial write behind one error.
//
// Run token: `signal` aborts the run. Tag resolution stops at once (nothing is
// sent); a chunk already in flight is allowed to finish — it commits server-
// side regardless — and is reported, but no further chunk is sent.

import type { BatchPatchResponse } from "./api.ts";
import {
  buildPatches,
  chunk,
  mergeSummaries,
  summarizeResults,
  type BulkFailure,
  type BulkOps,
  type BulkSummary,
  type BulkTarget,
  type ItemPatchBody,
} from "./bulkEdit.ts";

/** How many GET /items/{id} run at once while resolving live tags. */
export const RESOLVE_CONCURRENCY = 20;

export interface BulkApi {
  // The index signature lets a full ItemRecord satisfy it (no weak-type error).
  getItem(id: string, signal?: AbortSignal): Promise<{ tags?: unknown; [k: string]: unknown }>;
  batchPatch(body: Record<string, ItemPatchBody>): Promise<BatchPatchResponse>;
}

export interface BulkOutcome {
  /** Per-item results of the chunks that COMMITTED (merged). */
  summary: BulkSummary;
  /** Targets whose live tags could not be read — never patched. */
  unresolved: BulkFailure[];
  /** Every patch the run computed (empty = nothing to change). */
  patches: Record<string, ItemPatchBody>;
  /** The patches of the committed chunks — what `applyLocally` may mirror
   *  (filtered further by `summary.ok`). */
  patchesApplied: Record<string, ItemPatchBody>;
  /** Ids in chunks that were never committed (the failed chunk and later, or
   *  the chunks after an abort). */
  notAttempted: string[];
  /** The request-level failure that stopped the run, or null. */
  failed: { chunkIndex: number; error: unknown } | null;
  /** True when `signal` cancelled the run. */
  aborted: boolean;
}

const touchesTags = (ops: BulkOps): boolean => ops.tagsAdd.length > 0 || ops.tagsRemove.length > 0;

const stringTags = (raw: unknown): string[] =>
  Array.isArray(raw) ? raw.filter((t): t is string => typeof t === "string") : [];

export async function runBulk(
  selected: readonly BulkTarget[],
  ops: BulkOps,
  api: BulkApi,
  opts: { signal?: AbortSignal; onChunk?: (done: number, total: number) => void } = {},
): Promise<BulkOutcome> {
  const { signal, onChunk } = opts;
  const out: BulkOutcome = {
    summary: { ok: [], failures: [] },
    unresolved: [],
    patches: {},
    patchesApplied: {},
    notAttempted: [],
    failed: null,
    aborted: false,
  };

  // ---- resolve live tags --------------------------------------------------
  let targets: BulkTarget[] = [...selected];
  if (touchesTags(ops)) {
    targets = [];
    for (const group of chunk(selected, RESOLVE_CONCURRENCY)) {
      if (signal?.aborted) return { ...out, aborted: true, unresolved: [] };
      const recs = await Promise.all(
        group.map((t) =>
          api.getItem(t.id, signal).then(
            (rec) => ({ ok: true as const, rec }),
            () => ({ ok: false as const }),
          ),
        ),
      );
      // A rejection caused by the abort is not an unreadable item.
      if (signal?.aborted) return { ...out, aborted: true, unresolved: [] };
      recs.forEach((r, i) => {
        const t = group[i];
        if (!r.ok) {
          out.unresolved.push({ id: t.id, reason: "could not read this item's current tags" });
          return;
        }
        targets.push({ ...t, tags: stringTags(r.rec.tags) });
      });
    }
  }

  // ---- submit in chunks ---------------------------------------------------
  out.patches = buildPatches(targets, ops);
  const groups = chunk(Object.keys(out.patches));
  const parts: BulkSummary[] = [];
  for (let k = 0; k < groups.length; k++) {
    if (signal?.aborted) {
      out.aborted = true;
      out.notAttempted = groups.slice(k).flat();
      break;
    }
    const body: Record<string, ItemPatchBody> = {};
    for (const id of groups[k]) body[id] = out.patches[id];
    try {
      const res = await api.batchPatch(body);
      parts.push(summarizeResults(res.results));
      Object.assign(out.patchesApplied, body);
      onChunk?.(k + 1, groups.length);
    } catch (error) {
      out.failed = { chunkIndex: k, error };
      out.notAttempted = groups.slice(k).flat();
      break;
    }
  }
  if (!out.aborted && !out.failed && signal?.aborted) out.aborted = true;
  out.summary = mergeSummaries(parts);
  return out;
}
