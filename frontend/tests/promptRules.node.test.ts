// checkPromptValue: the rule promptText() applies before it resolves. DOM-free,
// so it runs on Node's built-in test runner (`npm test` from frontend/).

import assert from "node:assert/strict";
import { test } from "node:test";

import { checkPromptValue } from "../src/lib/ui/promptRules.ts";

test("checkPromptValue rejects an empty value by default", () => {
  assert.deepEqual(checkPromptValue("", {}), { value: "", error: "Enter a value." });
});

test("checkPromptValue treats whitespace-only as empty", () => {
  assert.deepEqual(checkPromptValue("   ", {}), { value: "", error: "Enter a value." });
});

test("checkPromptValue accepts an empty value when not required", () => {
  assert.deepEqual(checkPromptValue("", { required: false }), { value: "" });
});

test("checkPromptValue trims the value", () => {
  assert.deepEqual(checkPromptValue("  Movies ", {}), { value: "Movies" });
});

test("checkPromptValue returns the validate message as the error", () => {
  assert.deepEqual(checkPromptValue("Movies", { validate: () => "Taken" }), { value: "Movies", error: "Taken" });
});

test("checkPromptValue passes the trimmed value to validate", () => {
  let seen: string | undefined;
  checkPromptValue("  Movies ", {
    validate: (v) => {
      seen = v;
      return undefined;
    },
  });
  assert.equal(seen, "Movies");
});

test("checkPromptValue has no error when validate returns undefined", () => {
  assert.deepEqual(checkPromptValue("Movies", { validate: () => undefined }), { value: "Movies" });
});
