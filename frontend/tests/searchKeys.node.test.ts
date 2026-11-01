// listKeyAction(): the Search page's window keydown → action table. Rows are
// written out by hand (not derived from the rules) so the table IS the spec.

import assert from "node:assert/strict";
import { test } from "node:test";

import { listKeyAction, type KeyAction } from "../src/lib/searchKeys.ts";

type Target = { tag: string; editable: boolean; inListbox: boolean; combobox: boolean };
type Key = { key: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean };

const key = (k: string, mods: Partial<Omit<Key, "key">> = {}): Key => ({
  key: k,
  metaKey: false,
  ctrlKey: false,
  shiftKey: false,
  ...mods,
});

const KEYS: Record<string, Key> = {
  "/": key("/"),
  "Ctrl+K": key("k", { ctrlKey: true }),
  "Cmd+K": key("K", { metaKey: true }),
  ArrowDown: key("ArrowDown"),
  ArrowUp: key("ArrowUp"),
  Enter: key("Enter"),
  "Ctrl+Enter": key("Enter", { ctrlKey: true }),
  "Cmd+Enter": key("Enter", { metaKey: true }),
  Space: key(" "),
  "Shift+Space": key(" ", { shiftKey: true }),
  a: key("a"),
};

const TARGETS: Record<string, Target> = {
  BODY: { tag: "BODY", editable: false, inListbox: false, combobox: false },
  MAIN: { tag: "MAIN", editable: false, inListbox: false, combobox: false },
  "INPUT (facet text box)": { tag: "INPUT", editable: true, inListbox: false, combobox: false },
  TEXTAREA: { tag: "TEXTAREA", editable: true, inListbox: false, combobox: false },
  contentEditable: { tag: "DIV", editable: true, inListbox: false, combobox: false },
  SELECT: { tag: "SELECT", editable: false, inListbox: false, combobox: false },
  BUTTON: { tag: "BUTTON", editable: false, inListbox: false, combobox: false },
  A: { tag: "A", editable: false, inListbox: false, combobox: false },
  "DIV (outside listbox)": { tag: "DIV", editable: false, inListbox: false, combobox: false },
  "listbox DIV": { tag: "DIV", editable: false, inListbox: true, combobox: false },
  "BUTTON in listbox": { tag: "BUTTON", editable: false, inListbox: true, combobox: false },
  "checkbox in listbox": { tag: "INPUT", editable: true, inListbox: true, combobox: false },
  // The query box is the combobox for the results (data-results-combobox).
  "INPUT (results combobox)": { tag: "INPUT", editable: true, inListbox: false, combobox: true },
};

const N = null;
const F = "focusSearch";
// Columns, in order: / Ctrl+K Cmd+K ArrowDown ArrowUp Enter Ctrl+Enter Cmd+Enter Space Shift+Space a
const NORMAL: Record<string, (KeyAction | null)[]> = {
  BODY:                    [F, F, F, "move:1", "move:-1", N, N, N, N, N, N],
  MAIN:                    [F, F, F, "move:1", "move:-1", N, N, N, N, N, N],
  "INPUT (facet text box)":     [N, F, F, N, N, N, N, N, N, N, N],
  TEXTAREA:                [N, F, F, N, N, N, N, N, N, N, N],
  contentEditable:         [N, F, F, N, N, N, N, N, N, N, N],
  SELECT:                  [F, F, F, N, N, N, N, N, N, N, N],
  BUTTON:                  [F, F, F, N, N, N, N, N, N, N, N],
  A:                       [F, F, F, N, N, N, N, N, N, N, N],
  "DIV (outside listbox)": [F, F, F, N, N, N, N, N, N, N, N],
  "listbox DIV":           [F, F, F, "move:1", "move:-1", "open", "copy", "copy", "toggle", "toggle", N],
  "BUTTON in listbox":     [F, F, F, "move:1", "move:-1", "open", "copy", "copy", "toggle", "toggle", N],
  "checkbox in listbox":   [N, F, F, N, N, N, N, N, N, N, N],
  // Arrows move the results cursor; Enter/Space/letters stay with the text box.
  "INPUT (results combobox)": [N, F, F, "move:1", "move:-1", N, N, N, N, N, N],
};

const keyNames = Object.keys(KEYS);
const live = { modalOpen: false, view: "list", hasHits: true };

for (const [tname, target] of Object.entries(TARGETS)) {
  test(`listKeyAction: ${tname} with hits, list view`, () => {
    const row = NORMAL[tname];
    assert.equal(row.length, keyNames.length, "table row width");
    keyNames.forEach((kname, i) => {
      assert.equal(listKeyAction(KEYS[kname], target, live), row[i], `${tname} × ${kname}`);
    });
    // grid view behaves like list view
    keyNames.forEach((kname, i) => {
      assert.equal(listKeyAction(KEYS[kname], target, { ...live, view: "grid" }), row[i], `grid: ${tname} × ${kname}`);
    });
  });
}

const BLOCKED: Record<string, typeof live> = {
  "modal open": { ...live, modalOpen: true },
  "map view": { ...live, view: "map" },
  "no hits": { ...live, hasHits: false },
};

for (const [sname, state] of Object.entries(BLOCKED)) {
  test(`listKeyAction: ${sname} — only the focus-search shortcuts survive`, () => {
    for (const [tname, target] of Object.entries(TARGETS)) {
      for (const kname of keyNames) {
        const want: KeyAction | null =
          kname === "Ctrl+K" || kname === "Cmd+K" ? F : kname === "/" && !target.editable ? F : N;
        assert.equal(listKeyAction(KEYS[kname], target, state), want, `${sname}: ${tname} × ${kname}`);
      }
    }
  });
}

test("listKeyAction: modified arrows in the results combobox stay with the text box", () => {
  const combo = TARGETS["INPUT (results combobox)"];
  for (const k of [
    key("ArrowDown", { shiftKey: true }),
    key("ArrowUp", { shiftKey: true }),
    key("ArrowDown", { metaKey: true }),
    key("ArrowUp", { ctrlKey: true }),
  ]) {
    assert.equal(listKeyAction(k, combo, live), null, JSON.stringify(k));
  }
});
