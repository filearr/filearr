// thumbPolicy.ts: which items request a thumbnail at all (ruling 6, mirrors
// backend/filearr/tasks/thumbs.py) and the one-retry URL (A22).

import assert from "node:assert/strict";
import { test } from "node:test";

import { RETRY_DELAY_MS, placeholderText, retryUrl, shouldRetry, thumbable } from "../src/lib/thumbPolicy.ts";

test("thumbable table", () => {
  const cases: [string | null | undefined, string | null | undefined, boolean][] = [
    ["image", "a.jpg", true],
    ["video", "a.mkv", true],
    ["audio", "a.flac", true],
    ["three-d-cad", "a.stl", true],
    ["document", "a.pdf", true],
    ["document", "dir/A.PDF", true],
    ["document", "a.docx", false],
    ["document", "a.txt", false],
    ["document", null, false],
    ["document", undefined, false],
    ["archive", "a.zip", false],
    ["development", "a.py", false],
    ["system", "a.exe", false],
    ["other", "a", false],
    [null, "a.jpg", false],
    [undefined, undefined, false],
    ["", "a.jpg", false],
  ];
  for (const [c, f, want] of cases) assert.equal(thumbable(c, f), want, `${c}/${f}`);
});

test("retryUrl appends r=1 with the right separator", () => {
  assert.equal(retryUrl("/api/v1/items/x/thumb?tier=grid"), "/api/v1/items/x/thumb?tier=grid&r=1");
  assert.equal(retryUrl("/api/v1/items/x/thumb"), "/api/v1/items/x/thumb?r=1");
  assert.equal(RETRY_DELAY_MS, 2000);
});

test("placeholderText: first three letters of the category", () => {
  assert.equal(placeholderText("document"), "doc");
  assert.equal(placeholderText("three-d-cad"), "3d");
  assert.equal(placeholderText(null), "");
  assert.equal(placeholderText("archive"), "arc");
});

test("shouldRetry: only a video miss means 'queued; retry shortly' (item 13)", () => {
  assert.equal(shouldRetry("video"), true);
  for (const c of ["image", "audio", "three-d-cad", "document", "archive", "other", "", null, undefined]) {
    assert.equal(shouldRetry(c), false, String(c));
  }
});
