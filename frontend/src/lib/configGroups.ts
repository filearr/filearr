// Configuration-group layering: the DOM-free core behind the Agents console's
// config-group surface (P13, 2026-08-11).
//
// Everything here is a CLIENT-SIDE MIRROR of backend behaviour, kept in one
// unit-testable module for the same reason `agentPolicyDoc.ts` exists: the rules
// below are the ones an operator is silently misled by when they drift.
//
//  1. **Merge order is the whole model.** Groups apply in ascending `priority`
//     and a later group overrides only the keys it sets. Get the order wrong and
//     the dialog's preview tells someone their new group wins when it loses.
//  2. **Ties are deterministic, not arbitrary.** Equal priorities tie-break by
//     (name, id) on BOTH ends — the console must not invent its own order, or
//     the preview disagrees with what the fleet actually receives.
//  3. **Merging is shallow at each section's top level.** A nested object
//     (`inventory`, `scan_selections`) REPLACES wholesale rather than deep
//     merging. This surprises people, so it is pinned by a test rather than
//     left to a comment.
//  4. **Tier validation mirrors the server's** so a typo costs no round trip —
//     and, more importantly, so the "Save & phased rollout" button can be
//     disabled with a specific reason instead of surfacing a raw 422.
//
// Nothing here touches the DOM or the network: `npm test` runs it on Node.

import type { AgentPolicyDoc, GroupSettings, RolloutTier } from "./api";

// --------------------------------------------------------------------------- //
// Merge order                                                                   //
// --------------------------------------------------------------------------- //

/** The identity + rank of one layer. Structural on purpose (not `ConfigGroupOut`)
 *  so the merge can also be run over a DRAFT the operator has not saved yet. */
export interface LayerRef {
  id: string;
  name: string;
  priority: number;
}

/** A layer as merged: its rank plus the two document sections it contributes. */
export interface ConfigLayer extends LayerRef {
  settings?: GroupSettings | null;
  policy?: AgentPolicyDoc | null;
}

/** Backend merge order: ascending `priority`, then `name`, then `id`.
 *
 *  The (name, id) tail is not decoration — priorities are NOT unique (no
 *  constraint enforces it), so two groups at 100 are a normal state and the
 *  fleet still has to get one deterministic answer. Sorts a COPY. */
export function sortLayers<T extends LayerRef>(layers: readonly T[]): T[] {
  return [...layers].sort(
    (a, b) =>
      a.priority - b.priority ||
      (a.name < b.name ? -1 : a.name > b.name ? 1 : 0) ||
      (a.id < b.id ? -1 : a.id > b.id ? 1 : 0),
  );
}

/** Where one merged key came from — the console's own provenance, computed for
 *  a preview before anything is saved. Mirrors the server's `provenance` entry
 *  shape minus `version` (a draft has no version yet). */
export interface MergeSource {
  group_id: string;
  group_name: string;
}

export interface MergedDocument {
  settings: Record<string, unknown>;
  policy: Record<string, unknown>;
  /** Keyed `"settings.<key>"` / `"policy.<key>"`, exactly like the server's. */
  provenance: Record<string, MergeSource>;
  /** The layers in the order they were applied (first = lowest priority). */
  order: LayerRef[];
}

/** Per-section, per-key merge in priority order — later layers win.
 *
 *  Shallow at the top level of each section by design (see the header): the
 *  operator mental model for `inventory` or `scan_selections` is "this group's
 *  list", not "this group's list unioned with something below it".
 *
 *  A key present with a value of `null`/`undefined` is treated as ABSENT, i.e.
 *  "inherit", never as an override to null — the same rule the policy form's
 *  tri-state encodes and the backend's optional-field semantics apply. */
export function mergeDocuments(layers: readonly ConfigLayer[]): MergedDocument {
  const order = sortLayers(layers);
  const settings: Record<string, unknown> = {};
  const policy: Record<string, unknown> = {};
  const provenance: Record<string, MergeSource> = {};

  for (const layer of order) {
    const src: MergeSource = { group_id: layer.id, group_name: layer.name };
    const apply = (
      section: "settings" | "policy",
      into: Record<string, unknown>,
      from: Record<string, unknown> | null | undefined,
    ) => {
      if (!from) return;
      for (const [key, value] of Object.entries(from)) {
        if (value === null || value === undefined) continue;
        into[key] = value;
        provenance[`${section}.${key}`] = src;
      }
    };
    apply("settings", settings, layer.settings as Record<string, unknown> | null);
    apply("policy", policy, layer.policy as Record<string, unknown> | null);
  }

  return {
    settings,
    policy,
    provenance,
    order: order.map((l) => ({ id: l.id, name: l.name, priority: l.priority })),
  };
}

/** The layers a merged key is NOT taking its value from, i.e. the ones a higher
 *  group is shadowing. Used by the dialog to answer "why is my value not
 *  applying" without making the operator diff two JSON blobs by eye. */
export function shadowedBy(
  layers: readonly ConfigLayer[],
  section: "settings" | "policy",
  key: string,
): LayerRef[] {
  const order = sortLayers(layers);
  const setters = order.filter((l) => {
    const doc = (section === "settings" ? l.settings : l.policy) as
      | Record<string, unknown>
      | null
      | undefined;
    return !!doc && doc[key] !== null && doc[key] !== undefined;
  });
  // Everything but the LAST setter loses; the last one is the winner.
  return setters
    .slice(0, -1)
    .map((l) => ({ id: l.id, name: l.name, priority: l.priority }));
}

// --------------------------------------------------------------------------- //
// Phased-rollout tiers                                                          //
// --------------------------------------------------------------------------- //

/** Mirrors filearr.agent_config.MAX_ROLLOUT_TIERS. */
export const MAX_ROLLOUT_TIERS = 5;

/** Server-mirroring tier validation. Returns null when the tier list would be
 *  accepted, otherwise ONE message naming the offending row (1-based, as the
 *  editor numbers them).
 *
 *  The last-tier-must-be-100 rule is the one that needs explaining rather than
 *  merely reporting: a rollout is a scheduled path to the WHOLE fleet, so a
 *  permanent subset is a narrower group, not a stalled rollout. */
export function validateTiers(tiers: readonly RolloutTier[]): string | null {
  if (!tiers.length) return "Add at least one tier.";
  if (tiers.length > MAX_ROLLOUT_TIERS)
    return `${tiers.length} tiers; at most ${MAX_ROLLOUT_TIERS} are allowed.`;
  let previous = 0;
  for (let i = 0; i < tiers.length; i++) {
    const t = tiers[i];
    const n = i + 1;
    if (!Number.isInteger(t.percent))
      return `Tier ${n}: percent must be a whole number.`;
    if (t.percent < 1 || t.percent > 100)
      return `Tier ${n}: percent must be between 1 and 100.`;
    if (t.percent <= previous)
      return `Tier ${n}: ${t.percent}% must be greater than the previous tier's ${previous}% — tiers only ever widen coverage.`;
    if (!Number.isInteger(t.delay_minutes) || t.delay_minutes < 0)
      return `Tier ${n}: delay must be a whole number of minutes, 0 or more.`;
    previous = t.percent;
  }
  if (tiers[tiers.length - 1].percent !== 100)
    return "The last tier must be 100% — a rollout always finishes fleet-wide. To hold a subset permanently, put those agents in a narrower group instead.";
  return null;
}

/** Cumulative minutes from the rollout's start until tier `index` activates.
 *  Each tier's delay counts from the PREVIOUS tier's activation, so an ETA is a
 *  running sum rather than the tier's own number. */
export function tierEtaMinutes(tiers: readonly RolloutTier[], index: number): number {
  let total = 0;
  for (let i = 0; i <= index && i < tiers.length; i++)
    total += Math.max(0, tiers[i].delay_minutes || 0);
  return total;
}

/** "tier 2 of 3 · 50% covered" — the one-line rollout status used by both the
 *  rollouts card and the groups-table chip. `current_tier` is -1 before the
 *  first tier activates, which reads as "scheduled", not "tier 0". */
export function describeRollout(r: {
  status: string;
  current_tier: number;
  tiers: readonly RolloutTier[];
  covered_percent?: number;
}): string {
  const total = r.tiers.length;
  if (r.status === "scheduled" || r.current_tier < 0)
    return `scheduled · ${total} tier${total === 1 ? "" : "s"}`;
  if (r.status === "completed") return "completed · 100%";
  if (r.status === "cancelled") return "cancelled";
  const pct = r.covered_percent ?? r.tiers[r.current_tier]?.percent ?? 0;
  return `tier ${r.current_tier + 1} of ${total} · ${pct}% covered`;
}

// --------------------------------------------------------------------------- //
// Provenance formatting                                                         //
// --------------------------------------------------------------------------- //

/** The source badge for one key of an agent's effective config: `"<group> v<n>"`,
 *  plus a "via rollout" marker when that group's version reached this agent
 *  through a rollout tier rather than through `current_version`.
 *
 *  Why the marker matters: two agents in the same group can legitimately be on
 *  DIFFERENT versions mid-rollout, and without saying so the console looks like
 *  it is reporting stale data. */
export function formatProvenance(
  entry: { group_name: string; version: number } | undefined,
  opts: { viaRollout?: boolean } = {},
): string {
  if (!entry) return "built-in default";
  return `${entry.group_name} v${entry.version}${opts.viaRollout ? " · via rollout" : ""}`;
}

/** Look up the provenance of one key in an effective-config response and format
 *  it, resolving `via_rollout` from the contributing-group list. Keeps the
 *  section/key string concatenation in ONE place — a mismatch between
 *  `"policy.foo"` here and the server's key silently degrades every badge to
 *  "built-in default", which is a lie that looks like data. */
export function provenanceFor(
  provenance: Record<string, { group_id: string; group_name: string; version: number }>,
  groups: readonly { id: string; via_rollout: boolean }[],
  section: "settings" | "policy",
  key: string,
): string {
  const entry = provenance[`${section}.${key}`];
  if (!entry) return "built-in default";
  const viaRollout = groups.find((g) => g.id === entry.group_id)?.via_rollout ?? false;
  return formatProvenance(entry, { viaRollout });
}
