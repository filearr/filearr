// Field → control wiring: own props win, the Field context fills the gaps.
// fieldContext.ts is DOM-free so it runs on Node's built-in test runner.

import assert from "node:assert/strict";
import { test } from "node:test";

import { controlAttrs, type FieldCtx } from "../src/lib/ui/fieldContext.ts";

const ctx: FieldCtx = { id: "f1", describedBy: "f1-hint f1-error", invalid: true, required: true };

test("controlAttrs without a context leaves everything unset and valid", () => {
  assert.deepEqual(controlAttrs(undefined, {}), {
    id: undefined,
    "aria-describedby": undefined,
    "aria-invalid": undefined,
    required: undefined,
    invalid: false,
  });
});

test("controlAttrs without a context passes own props through", () => {
  const got = controlAttrs(undefined, { id: "x", "aria-describedby": "d", required: true, invalid: true });
  assert.deepEqual(got, { id: "x", "aria-describedby": "d", "aria-invalid": "true", required: true, invalid: true });
});

test("controlAttrs takes every field from the context when own props are absent", () => {
  assert.deepEqual(controlAttrs(ctx, {}), {
    id: "f1",
    "aria-describedby": "f1-hint f1-error",
    "aria-invalid": "true",
    required: true,
    invalid: true,
  });
});

test("controlAttrs own id overrides the context", () => {
  assert.equal(controlAttrs(ctx, { id: "mine" }).id, "mine");
});

test("controlAttrs own aria-describedby overrides the context", () => {
  assert.equal(controlAttrs(ctx, { "aria-describedby": "other" })["aria-describedby"], "other");
});

test("controlAttrs own required overrides the context, including false", () => {
  assert.equal(controlAttrs(ctx, { required: false }).required, false);
  assert.equal(controlAttrs({ ...ctx, required: false }, { required: true }).required, true);
});

test("controlAttrs own invalid overrides the context, including false", () => {
  const off = controlAttrs(ctx, { invalid: false });
  assert.equal(off.invalid, false);
  assert.equal(off["aria-invalid"], undefined);
  const on = controlAttrs({ ...ctx, invalid: false }, { invalid: true });
  assert.equal(on.invalid, true);
  assert.equal(on["aria-invalid"], "true");
});

test("controlAttrs sets aria-invalid only when invalid", () => {
  assert.equal(controlAttrs({ ...ctx, invalid: false }, {})["aria-invalid"], undefined);
  assert.equal(controlAttrs(ctx, {})["aria-invalid"], "true");
  // A caller's aria-invalid string alone does not mark the control invalid:
  // `invalid` is the single source, so "false"/"true" strings are dropped.
  assert.equal(controlAttrs(undefined, { "aria-invalid": "true" })["aria-invalid"], undefined);
  assert.equal(controlAttrs(undefined, { "aria-invalid": "false" })["aria-invalid"], undefined);
});
