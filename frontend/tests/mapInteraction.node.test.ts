// mapInteraction.ts: which pointer presses on the map svg start a drag (A27).
// A press on a marker (or a cluster glyph) must never start a select-drag:
// the drag's click branch would clear the active area, and the svg's pointer
// capture would swallow the marker's own click.

import assert from "node:assert/strict";
import { test } from "node:test";

import { shouldStartDrag } from "../src/lib/mapInteraction.ts";

test("a primary press on the map background starts a drag", () => {
  assert.equal(shouldStartDrag({ inMarker: false, button: 0 }), true);
});

test("a press inside a marker never starts a drag", () => {
  assert.equal(shouldStartDrag({ inMarker: true, button: 0 }), false);
});

test("non-primary buttons never start a drag", () => {
  for (const button of [1, 2, 3, 4, -1]) {
    assert.equal(shouldStartDrag({ inMarker: false, button }), false, `button ${button}`);
    assert.equal(shouldStartDrag({ inMarker: true, button }), false, `marker button ${button}`);
  }
});
