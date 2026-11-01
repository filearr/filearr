// Field → control wiring: own props win, the Field context fills the gaps.
// fieldContext.ts is DOM-free so it runs on Node's built-in test runner.

import assert from "node:assert/strict";
import { test } from "node:test";

import { controlAttrs, warnFieldIdMismatch, type FieldCtx } from "../src/lib/ui/fieldContext.ts";

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

test("controlAttrs merges own aria-describedby ahead of the context's", () => {
  assert.equal(controlAttrs(ctx, { "aria-describedby": "other" })["aria-describedby"], "other f1-hint f1-error");
});

test("controlAttrs de-duplicates and normalises the merged aria-describedby", () => {
  // The Field snippet form hands the context's ids to the control explicitly.
  assert.equal(controlAttrs(ctx, { "aria-describedby": "f1-hint f1-error" })["aria-describedby"], "f1-hint f1-error");
  assert.equal(controlAttrs(ctx, { "aria-describedby": "  a  f1-error a " })["aria-describedby"], "a f1-error f1-hint");
  assert.equal(controlAttrs(undefined, { "aria-describedby": "  " })["aria-describedby"], undefined);
  assert.equal(controlAttrs({ ...ctx, describedBy: undefined }, { "aria-describedby": "d" })["aria-describedby"], "d");
});

test("warnFieldIdMismatch warns only for an own id that differs from the Field's", (t) => {
  const warn = t.mock.method(console, "warn", () => {});
  warnFieldIdMismatch(ctx, "mine", true);
  assert.equal(warn.mock.callCount(), 1);
  assert.match(String(warn.mock.calls[0].arguments[0]), /Put the id on <Field> rather than the control/);
  warnFieldIdMismatch(ctx, "f1", true); // same id: fine
  warnFieldIdMismatch(ctx, undefined, true); // no own id
  warnFieldIdMismatch(undefined, "mine", true); // no Field
  warnFieldIdMismatch(ctx, "mine", false); // production
  assert.equal(warn.mock.callCount(), 1);
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
