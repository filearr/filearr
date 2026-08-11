// Pure-logic tests for the config-group dialog's INVENTORY COLLECTORS picker.
//
// The picker replaced a comma-separated text box whose legal values only existed
// in the agent's Go source. Making it a checkbox list introduces exactly one new
// way to lose data — a stored name the checkbox list does not know about — and
// that is the bug these tests exist to prevent. `inventory.collectors` is stored
// FREE-FORM on purpose (a newer agent build can implement a collector this
// central release has never heard of, and central refusing it would make the
// server the thing that blocks an agent upgrade), so:
//
//   * the catalogue endpoint is a UI aid, never a validation whitelist;
//   * a name in the group's settings that the catalogue lacks must survive a
//     load → edit-something-else → save round trip, the same discipline
//     `passthroughFromDoc` enforces for the policy editor's unknown keys;
//   * a failed catalogue fetch must fall back to free-text editing, because an
//     empty checkbox list reads as "no collectors exist" and invites the
//     operator to save a group with its collectors silently emptied.
//
// Runs on Node's built-in test runner with native TypeScript type-stripping:
// `npm test` from frontend/. No bundler / DOM.

import assert from "node:assert/strict";
import { test } from "node:test";

import {
  MAX_COLLECTORS,
  addCollectorName,
  collectorEditorFromFetch,
  collectorStanding,
  collectorsToSave,
  mergeCollectorChoices,
  parseCollectorsText,
  preservedUnknownCollectors,
  toggleCollector,
  type CollectorCatalogueEntry,
  type CollectorChoice,
  type CollectorEditor,
} from "../src/lib/inventoryCollectors.ts";

/** Assert the editor is in checkbox mode and hand back its rows. */
function listChoices(e: CollectorEditor): CollectorChoice[] {
  assert.equal(e.mode, "list");
  if (e.mode !== "list") throw new Error("unreachable");
  return e.choices;
}
const asList = (choices: CollectorChoice[]): CollectorEditor => ({ mode: "list", choices });

/** A stand-in for GET /agents/inventory-collectors on a Linux-only fleet:
 *  `placeholder` is described but Windows-only, so nothing advertises it, and
 *  `zfs-props` is advertised by a newer agent build central cannot describe. */
const CATALOGUE: CollectorCatalogueEntry[] = [
  {
    name: "stat",
    label: "File stat",
    description: "Size, timestamps and basic file attributes.",
    platforms: ["linux", "darwin", "windows"],
    cost: "low",
    described: true,
    advertised_by: 3,
  },
  {
    name: "owner",
    label: "Owner / group",
    description: "The file's owning user and group.",
    platforms: ["linux", "darwin", "windows"],
    cost: "low",
    described: true,
    advertised_by: 3,
  },
  {
    name: "perms",
    label: "Permissions",
    description: "Permission bits / ACL summary for each file.",
    platforms: ["linux", "darwin", "windows"],
    cost: "medium",
    described: true,
    advertised_by: 2,
  },
  {
    name: "placeholder",
    label: "Cloud placeholder state",
    description: "Whether a file is a cloud placeholder.",
    platforms: ["windows"],
    cost: "low",
    described: true,
    advertised_by: 0,
  },
  {
    name: "zfs-props",
    label: "zfs-props",
    description: "Reported by an agent in this fleet but not described…",
    platforms: [],
    cost: "unknown",
    described: false,
    advertised_by: 1,
  },
];

const ok = (catalogue = CATALOGUE) => ({ ok: true as const, catalogue });
const names = (e: CollectorEditor) =>
  e.mode === "list" ? e.choices.map((c) => c.name) : [];
const checked = (e: CollectorEditor) =>
  e.mode === "list" ? e.choices.filter((c) => c.checked).map((c) => c.name) : [];

// --------------------------------------------------------------------------- //
// Merge: catalogue + stored names -> the right checked set                      //
// --------------------------------------------------------------------------- //
test("stored names tick their catalogue rows and nothing else", () => {
  const choices = mergeCollectorChoices(CATALOGUE, ["stat", "perms"]);
  assert.deepEqual(
    choices.filter((c) => c.checked).map((c) => c.name),
    ["stat", "perms"],
  );
  assert.deepEqual(
    choices.map((c) => c.name),
    ["stat", "owner", "perms", "placeholder", "zfs-props"],
    "catalogue order is preserved so the list does not reshuffle between opens",
  );
  assert.equal(choices.every((c) => !c.unrecognised), true);
});

test("a group with no collectors ticks nothing but still offers everything", () => {
  const choices = mergeCollectorChoices(CATALOGUE, []);
  assert.deepEqual(choices.filter((c) => c.checked), []);
  assert.equal(choices.length, CATALOGUE.length);
});

test("a fleet-discovered collector central cannot describe is offered, not hidden", () => {
  // `described: false` = a newer agent build's collector. The console must let
  // an operator select it even though it has no prose for it.
  const choices = mergeCollectorChoices(CATALOGUE, ["zfs-props"]);
  const zfs = choices.find((c) => c.name === "zfs-props");
  assert.ok(zfs);
  assert.equal(zfs.checked, true);
  assert.equal(zfs.described, false);
  assert.equal(zfs.unrecognised, false, "the fleet advertises it — it is not unknown");
});

test("an unknown stored name is appended as a TICKED, flagged row", () => {
  const choices = mergeCollectorChoices(CATALOGUE, ["stat", "quota-usage"]);
  const extra = choices.at(-1);
  assert.ok(extra);
  assert.equal(extra.name, "quota-usage");
  assert.equal(extra.checked, true, "an operator only stores a name in order to use it");
  assert.equal(extra.unrecognised, true);
});

test("duplicate stored names collapse to one row", () => {
  const choices = mergeCollectorChoices(CATALOGUE, ["quota-usage", "quota-usage"]);
  assert.equal(choices.filter((c) => c.name === "quota-usage").length, 1);
});

// --------------------------------------------------------------------------- //
// THE data-loss test: unknown stored name survives load -> edit -> save         //
// --------------------------------------------------------------------------- //
test("an unknown stored name survives a load → toggle-something-else → save", () => {
  // The regression this whole file exists for. Before the checkbox list the text
  // box round-tripped anything typed into it; a list built only from the
  // catalogue would silently drop `quota-usage` the first time anyone edited the
  // group for an unrelated reason.
  const stored = ["stat", "quota-usage"];
  const loaded = collectorEditorFromFetch(stored, ok());
  assert.deepEqual(checked(loaded), ["stat", "quota-usage"]);

  // The operator ticks an unrelated collector and saves.
  const editor = asList(toggleCollector(listChoices(loaded), "owner", true));

  const saved = collectorsToSave(editor, "");
  assert.deepEqual(saved, ["stat", "owner", "quota-usage"]);
  assert.ok(saved.includes("quota-usage"), "DATA LOSS: the unknown name was dropped");
});

test("the preserved-unknown set is reported so the UI can say it out loud", () => {
  const editor = collectorEditorFromFetch(["stat", "quota-usage", "zfs-props"], ok());
  assert.deepEqual(preservedUnknownCollectors(editor), ["quota-usage"]);
});

test("unticking an unknown name is the ONE way it leaves — and it works", () => {
  // Preservation must not become a trap: an operator who deliberately removes a
  // stale name has to be able to.
  const loaded = collectorEditorFromFetch(["stat", "quota-usage"], ok());
  const editor = asList(toggleCollector(listChoices(loaded), "quota-usage", false));
  assert.deepEqual(collectorsToSave(editor, ""), ["stat"]);
});

// --------------------------------------------------------------------------- //
// Save payload                                                                  //
// --------------------------------------------------------------------------- //
test("unticking everything yields an empty list, not a dropped key", () => {
  // `collectors: []` ("inventory on, nothing collected") is a real document and
  // differs from omitting the key. The caller writes whatever this returns into
  // settings.inventory.collectors verbatim.
  let choices = listChoices(collectorEditorFromFetch(["stat", "owner"], ok()));
  for (const n of ["stat", "owner"]) choices = toggleCollector(choices, n, false);
  const saved = collectorsToSave(asList(choices), "stat, owner");
  assert.deepEqual(saved, []);
  assert.equal(Array.isArray(saved), true, "an array, never undefined");
});

test("the payload follows display order, not stored order", () => {
  // Stable ordering keeps the saved document from churning on every edit.
  const editor = collectorEditorFromFetch(["perms", "stat"], ok());
  assert.deepEqual(collectorsToSave(editor, ""), ["stat", "perms"]);
});

test("saving before the catalogue arrives round-trips the loaded text", () => {
  // An impatient operator can hit Save while the fetch is in flight. The dialog
  // still holds the stored names as text; losing them would be the same bug.
  const editor: CollectorEditor = { mode: "loading" };
  assert.deepEqual(collectorsToSave(editor, "stat, quota-usage"), ["stat", "quota-usage"]);
});

// --------------------------------------------------------------------------- //
// "add another" — the escape hatch                                              //
// --------------------------------------------------------------------------- //
test("add-another appends a ticked, unrecognised collector and it saves", () => {
  // Pre-configuring a group for an agent build that has not rolled out yet.
  const res = addCollectorName(
    listChoices(collectorEditorFromFetch(["stat"], ok())),
    "  smart-attrs  ",
  );
  assert.equal(res.error, "");
  assert.equal(res.existing, false);
  const editor = asList(res.choices);
  assert.deepEqual(collectorsToSave(editor, ""), ["stat", "smart-attrs"]);
  assert.deepEqual(preservedUnknownCollectors(editor), ["smart-attrs"]);
});

test("add-another for a name already listed ticks that row instead of duplicating", () => {
  const editor = collectorEditorFromFetch([], ok());
  const res = addCollectorName(listChoices(editor), "owner");
  assert.equal(res.error, "");
  assert.equal(res.existing, true);
  assert.deepEqual(names(asList(res.choices)), names(editor));
  assert.deepEqual(checked(asList(res.choices)), ["owner"]);
});

test("add-another rejects blank and over-long names with a reason, not silence", () => {
  const choices = listChoices(collectorEditorFromFetch([], ok()));
  const blank = addCollectorName(choices, "   ");
  assert.match(blank.error, /Enter a collector name/);
  assert.deepEqual(blank.choices, choices);
  const long = addCollectorName(choices, "x".repeat(129));
  assert.match(long.error, /128 characters/);
  assert.deepEqual(long.choices, choices);
});

test("add-another refuses to exceed the server's 64-collector cap", () => {
  const choices = listChoices(
    collectorEditorFromFetch(
      Array.from({ length: MAX_COLLECTORS }, (_, i) => `c${i}`),
      { ok: true, catalogue: [] },
    ),
  );
  const res = addCollectorName(choices, "one-too-many");
  assert.match(res.error, /At most 64/);
  assert.equal(res.choices.length, choices.length);
});

// --------------------------------------------------------------------------- //
// Fetch failure -> free-text fallback                                           //
// --------------------------------------------------------------------------- //
test("a failed catalogue fetch falls back to text, never to an empty list", () => {
  const editor = collectorEditorFromFetch(["stat", "quota-usage"], {
    ok: false,
    error: "403: admin scope required",
  });
  assert.equal(editor.mode, "text");
  assert.match(editor.mode === "text" ? editor.reason : "", /admin scope required/);
  // Crucially: the stored names are NOT lost — the text field still drives save.
  assert.deepEqual(
    collectorsToSave(editor, "stat, quota-usage"),
    ["stat", "quota-usage"],
  );
  assert.deepEqual(preservedUnknownCollectors(editor), []);
});

test("free-text parsing accepts commas, newlines and stray whitespace", () => {
  assert.deepEqual(parseCollectorsText(" stat, owner\nperms ,, \n"), [
    "stat",
    "owner",
    "perms",
  ]);
  assert.deepEqual(parseCollectorsText("stat, stat"), ["stat"], "deduped");
  assert.deepEqual(parseCollectorsText("   "), []);
});

// --------------------------------------------------------------------------- //
// Standing — how a row reads                                                    //
// --------------------------------------------------------------------------- //
test("standing distinguishes 'will do something' from 'nothing reports it'", () => {
  const choices = mergeCollectorChoices(CATALOGUE, []);
  const by = (n: string) => collectorStanding(choices.find((c) => c.name === n)!);

  assert.equal(by("stat").kind, "active");
  assert.equal(by("stat").chip, "", "an active collector needs no warning chip");

  // Windows-only collector, Linux-only fleet: advertised_by 0. Distinguishable,
  // but still selectable — the Windows host may enroll tomorrow.
  const ph = by("placeholder");
  assert.equal(ph.kind, "unmatched");
  assert.match(ph.note, /No enrolled agent advertises/);
  assert.match(ph.note, /windows/, "the platform explains the zero");

  // Advertised but undescribed: a newer agent build.
  assert.equal(by("zfs-props").kind, "unknown");
  assert.match(by("zfs-props").note, /newer agent build/);
});

test("an unrecognised stored name reads as preserved, not as broken", () => {
  const choices = mergeCollectorChoices(CATALOGUE, ["quota-usage"]);
  const st = collectorStanding(choices.find((c) => c.name === "quota-usage")!);
  assert.equal(st.kind, "unknown");
  assert.equal(st.chip, "unrecognised");
  assert.match(st.note, /preserved and delivered verbatim/);
});
