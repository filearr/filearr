// Pure (DOM-free) logic behind the config-group dialog's INVENTORY COLLECTORS
// picker. Split out of AgentsPage.svelte for the same reason `agentPolicyDoc.ts`
// is split out of the policy editor: the rules here are DATA-LOSS rules, and a
// regression in one silently discards an operator's configuration. They must be
// unit-testable on Node, without a bundler or a DOM.
//
// The three rules, and why each exists:
//
//  1. **The catalogue is a UI aid, never a whitelist.** `inventory.collectors`
//     is stored as free strings on purpose — a newer agent build can implement a
//     collector this central release has never heard of, and central refusing it
//     would make the server the thing that blocks an agent upgrade. So every
//     function here treats an unknown name as VALID, merely unexplained.
//  2. **A stored name absent from the catalogue must survive a round trip.**
//     Loading a group, ticking an unrelated box and saving must not drop it.
//     This mirrors `passthroughFromDoc` in `agentPolicyDoc.ts`.
//  3. **A failed catalogue fetch falls back to free text, not to an empty list.**
//     An empty checkbox list would read as "no collectors exist" and invite the
//     operator to save a group with its collectors silently emptied.

/** One entry of `GET /agents/inventory-collectors` (see
 *  `filearr.api.agent_config_groups.CollectorOut`). The response is the UNION of
 *  the shipped catalogue and every name the enrolled fleet advertises. */
export interface CollectorCatalogueEntry {
  name: string;
  label: string;
  description: string;
  /** Platforms the SHIPPED catalogue documents for this collector. Empty for an
   *  entry discovered only from an agent's advertisement — central cannot know. */
  platforms: string[];
  cost: string;
  /** False = an agent reports this name but this Filearr release has no prose
   *  for it (probably a newer agent build). Offer it; just don't explain it. */
  described: boolean;
  /** How many enrolled agents advertise supporting it, across the WHOLE fleet
   *  (server-computed — not the console's paginated agents table). */
  advertised_by: number;
}

/** One rendered checkbox: a catalogue entry plus this group's checked state,
 *  or a name that exists only because the group already stores it. */
export interface CollectorChoice {
  name: string;
  label: string;
  description: string;
  platforms: string[];
  cost: string;
  described: boolean;
  advertisedBy: number;
  checked: boolean;
  /** True when the name appears in NEITHER the shipped catalogue nor any
   *  agent's advertisement — it came from the group's stored settings or from
   *  the operator's "add another" box. Rendered checked and flagged, never
   *  dropped (rule 2). */
  unrecognised: boolean;
}

/** The collector editor's state for one open dialog.
 *
 *  * `loading` — the catalogue request is in flight. The stored names are held
 *    in the free-text field, so an impatient save still round-trips them.
 *  * `list` — checkboxes.
 *  * `text` — the catalogue could not be fetched; the pre-existing
 *    comma-separated editing stays available and the UI says why (rule 3). */
export type CollectorEditor =
  | { mode: "loading" }
  | { mode: "list"; choices: CollectorChoice[] }
  | { mode: "text"; reason: string };

/** Outcome of the catalogue fetch, as a value so the fallback is testable. */
export type CatalogueFetch =
  | { ok: true; catalogue: CollectorCatalogueEntry[] }
  | { ok: false; error: string };

// Mirrors filearr.agent_config.MAX_COLLECTORS / MAX_COLLECTOR_LEN so a typo
// costs no round trip (same posture as MAX_RETAIN_SNAPSHOTS in api.ts).
export const MAX_COLLECTORS = 64;
export const MAX_COLLECTOR_LEN = 128;

/** Split the free-text field the same way the dialog always has: comma OR
 *  newline separated, trimmed, blanks dropped. Duplicates are collapsed —
 *  `collectors` is a set in every sense that matters and the server counts
 *  entries against MAX_COLLECTORS. */
export function parseCollectorsText(text: string): string[] {
  const out: string[] = [];
  for (const raw of text.split(/[,\n]/)) {
    const name = raw.trim();
    if (name && !out.includes(name)) out.push(name);
  }
  return out;
}

/** Merge the catalogue with this group's STORED names into the checkbox list.
 *
 *  Order is stable and meaningful: catalogue order first (the shipped, described
 *  collectors, then any fleet-discovered ones the server appended), followed by
 *  stored names the catalogue does not know, in stored order. */
export function mergeCollectorChoices(
  catalogue: CollectorCatalogueEntry[],
  stored: string[],
): CollectorChoice[] {
  const selected = new Set(stored);
  const seen = new Set<string>();
  const choices: CollectorChoice[] = [];
  for (const entry of catalogue) {
    if (seen.has(entry.name)) continue; // defensive: server should not repeat
    seen.add(entry.name);
    choices.push({
      name: entry.name,
      label: entry.label || entry.name,
      description: entry.description,
      platforms: entry.platforms ?? [],
      cost: entry.cost,
      described: entry.described,
      advertisedBy: entry.advertised_by ?? 0,
      checked: selected.has(entry.name),
      unrecognised: false,
    });
  }
  for (const name of stored) {
    if (seen.has(name)) continue;
    seen.add(name);
    choices.push(unrecognisedChoice(name));
  }
  return choices;
}

/** A name central cannot explain: stored by this group, or typed into the
 *  "add another" box. Always checked — an operator only names one to use it. */
function unrecognisedChoice(name: string): CollectorChoice {
  return {
    name,
    label: name,
    description:
      "Not in this Filearr release's catalogue and not advertised by any " +
      "enrolled agent. It is kept exactly as stored and delivered unchanged — " +
      "an agent that implements it will run it, and one that does not ignores it.",
    platforms: [],
    cost: "unknown",
    described: false,
    advertisedBy: 0,
    checked: true,
    unrecognised: true,
  };
}

/** Build the editor state for a dialog that has just resolved its catalogue
 *  request. A failure keeps free-text editing rather than showing an empty
 *  checkbox list (rule 3). */
export function collectorEditorFromFetch(
  stored: string[],
  outcome: CatalogueFetch,
): CollectorEditor {
  if (!outcome.ok) return { mode: "text", reason: outcome.error };
  return { mode: "list", choices: mergeCollectorChoices(outcome.catalogue, stored) };
}

/** Flip one checkbox, returning a NEW array (the caller assigns it back into
 *  `$state`, so Svelte sees the change without deep proxying). */
export function toggleCollector(
  choices: CollectorChoice[],
  name: string,
  checked: boolean,
): CollectorChoice[] {
  return choices.map((c) => (c.name === name ? { ...c, checked } : c));
}

/** Result of the "add another" escape hatch: either the extended list, or a
 *  reason the console declined (never a silent no-op). */
export interface AddCollectorResult {
  choices: CollectorChoice[];
  /** Empty on success. */
  error: string;
  /** Set when the name already existed — the caller can clear the input and
   *  point at the row instead of complaining. */
  existing: boolean;
}

/** Add a collector name the catalogue and the fleet both lack. This is the
 *  escape hatch that keeps the checkbox list from being a cage: an operator
 *  pre-configuring a group for an agent build that has not rolled out yet has
 *  no other way to name its collector. */
export function addCollectorName(
  choices: CollectorChoice[],
  raw: string,
): AddCollectorResult {
  const name = raw.trim();
  if (!name) return { choices, error: "Enter a collector name.", existing: false };
  if (name.length > MAX_COLLECTOR_LEN) {
    return {
      choices,
      error: `Collector names are at most ${MAX_COLLECTOR_LEN} characters.`,
      existing: false,
    };
  }
  const found = choices.find((c) => c.name === name);
  if (found) {
    // Already offered (possibly unticked) — tick it rather than duplicating.
    return {
      choices: found.checked ? choices : toggleCollector(choices, name, true),
      error: "",
      existing: true,
    };
  }
  if (choices.filter((c) => c.checked).length >= MAX_COLLECTORS) {
    return {
      choices,
      error: `At most ${MAX_COLLECTORS} collectors can be selected.`,
      existing: false,
    };
  }
  return { choices: [...choices, unrecognisedChoice(name)], error: "", existing: false };
}

/** The `settings.inventory.collectors` payload for a save.
 *
 *  In `list` mode this is every ticked box in display order — INCLUDING the
 *  unrecognised ones, which is rule 2. In `text` mode (fetch failed) and in
 *  `loading` mode (saved before the catalogue arrived) it is the free-text field,
 *  so a dialog that never rendered a checkbox still round-trips what it loaded.
 *
 *  Unticking everything yields `[]` — an explicit "no collectors", which is a
 *  different (and honest) document from omitting the key. */
export function collectorsToSave(editor: CollectorEditor, text: string): string[] {
  if (editor.mode === "list") {
    return editor.choices.filter((c) => c.checked).map((c) => c.name);
  }
  return parseCollectorsText(text);
}

/** The ticked names central cannot explain — surfaced as one line under the
 *  list so an operator sees the preservation happening rather than wondering
 *  why an unfamiliar row is ticked. */
export function preservedUnknownCollectors(editor: CollectorEditor): string[] {
  if (editor.mode !== "list") return [];
  return editor.choices.filter((c) => c.checked && c.unrecognised).map((c) => c.name);
}

/** How a collector should READ in the list. The three states are the whole
 *  point of surfacing `platforms` / `advertised_by`:
 *
 *  * `active`    — at least one enrolled agent advertises it. Ticking it does
 *                  something today.
 *  * `unmatched` — described by this release, but NOTHING in the fleet reports
 *                  it. Usually a platform mismatch (`placeholder` is Windows-only
 *                  on a Linux-only fleet), occasionally an agent too old to
 *                  advertise. Still tickable — pre-configuring a group before the
 *                  matching host enrolls is legitimate.
 *  * `unknown`   — no prose for it here: either a newer agent's collector
 *                  (`described: false`) or a name only this group stores.
 *
 *  We key "will this do anything" off `advertised_by` — a whole-fleet count the
 *  SERVER computed — rather than intersecting `platforms` with the platforms of
 *  the agents table, which is paginated (50 rows) and would mislabel a collector
 *  on page 2 of a large fleet. `platforms` is then used only to EXPLAIN a zero. */
export type CollectorStandingKind = "active" | "unmatched" | "unknown";

export interface CollectorStanding {
  kind: CollectorStandingKind;
  /** Short chip text, or "" when the collector needs no chip (`active`). */
  chip: string;
  /** Sentence for the chip's tooltip / the row's hint line. */
  note: string;
}

export function collectorStanding(c: CollectorChoice): CollectorStanding {
  if (c.unrecognised) {
    return {
      kind: "unknown",
      chip: "unrecognised",
      note:
        `"${c.name}" is stored on this group but is neither in this Filearr ` +
        "release's catalogue nor advertised by any enrolled agent. It is preserved " +
        "and delivered verbatim; unticking it is the only way to remove it.",
    };
  }
  if (!c.described) {
    return {
      kind: "unknown",
      chip: "newer agent",
      note:
        `${c.advertisedBy} enrolled agent(s) advertise "${c.name}", but this ` +
        "Filearr release has no description for it — it comes from a newer agent " +
        "build. It can be enabled; consult that agent's documentation.",
    };
  }
  if (c.advertisedBy === 0) {
    const where = c.platforms.length
      ? `Documented for ${c.platforms.join(", ")}.`
      : "No platform recorded.";
    return {
      kind: "unmatched",
      chip: "no agent reports it",
      note:
        `No enrolled agent advertises "${c.name}". ${where} Most often that is a ` +
        "platform mismatch, sometimes an agent too old to advertise its collectors. " +
        "You can still select it — it takes effect on the first host that supports it.",
    };
  }
  return {
    kind: "active",
    chip: "",
    note:
      `${c.advertisedBy} enrolled agent(s) advertise "${c.name}"` +
      (c.platforms.length ? ` (${c.platforms.join(", ")})` : "") +
      `. Cost: ${c.cost}.`,
  };
}
