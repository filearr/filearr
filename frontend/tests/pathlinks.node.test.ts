// safeHref(): the one gate every dynamic href goes through (D9/M26). A value
// that is not an allowed URL scheme or an absolute path/UNC is rendered as text.

import assert from "node:assert/strict";
import { test } from "node:test";

import { buildOpenUrl, classifyPrefix, safeHref } from "../src/lib/pathlinks.ts";

test("safeHref rejects script-capable and unknown schemes", () => {
  for (const v of [
    "javascript:alert(1)",
    "JAVASCRIPT:alert(1)",
    " javascript:alert(1)",
    "\tjavascript:alert(1)",
    "\u0000javascript:alert(1)",
    "java\nscript:alert(1)",
    "javascript://x%0aalert(1)",
    "data:text/html,<script>alert(1)</script>",
    "vbscript:msgbox(1)",
    "blob:https://x/1",
    "mailto:a@b",
    "relative/path",
    "//evil.example/x",
    "",
    "   ",
  ]) {
    assert.equal(safeHref(v), null, JSON.stringify(v));
  }
});

test("safeHref rejects non-strings", () => {
  for (const v of [null, undefined, 42, {}, ["https://x"]]) assert.equal(safeHref(v), null);
});

test("safeHref accepts allowed schemes, absolute paths and UNC", () => {
  for (const v of [
    "https://x",
    "http://host:8080/a?b#c",
    "HTTPS://X",
    "smb://host/share",
    "sftp://h/p",
    "ftp://h/p",
    "ftps://h/p",
    "nfs://h/p",
    "webdav://h/p",
    "webdavs://h/p",
    "file:///mnt/x",
    "/mnt/x",
    "\\\\host\\share",
  ]) {
    assert.equal(safeHref(v), v, v);
  }
});

test("safeHref schemes option narrows the allow-list", () => {
  const web = { schemes: ["http", "https"] };
  assert.equal(safeHref("https://x", web), "https://x");
  assert.equal(safeHref("http://x", web), "http://x");
  assert.equal(safeHref("smb://host/share", web), null);
  assert.equal(safeHref("file:///etc/passwd", web), null);
  assert.equal(safeHref("javascript:alert(1)", web), null);
});

test("safeHref strips tab/CR/LF anywhere before checking", () => {
  assert.equal(safeHref("/\t/evil"), null);
  assert.equal(safeHref("/\n/evil"), null);
  assert.equal(safeHref("java\tscript:alert(1)"), null);
  assert.equal(safeHref("jav\r\nascript:alert(1)"), null);
});

test("safeHref applies the schemes option to UNC (UNC is file)", () => {
  assert.equal(safeHref("\\\\host\\share", { schemes: ["http", "https"] }), null);
  assert.equal(safeHref("\\\\host\\share"), "\\\\host\\share");
  assert.equal(safeHref("\\\\host\\share", { schemes: ["file"] }), "\\\\host\\share");
});

test("classifyPrefix/buildOpenUrl refuse a disallowed scheme", () => {
  assert.equal(classifyPrefix("javascript://x"), "unknown");
  assert.equal(buildOpenUrl("javascript://x"), "");
  assert.equal(buildOpenUrl("javascript://x", "a/b"), "");
  assert.equal(buildOpenUrl("data://x", "a"), "");
});

test("buildOpenUrl still builds the supported shapes", () => {
  assert.equal(buildOpenUrl("smb://nas/media", "a b/c.mkv"), "smb://nas/media/a%20b/c.mkv");
  assert.equal(buildOpenUrl("\\\\nas\\media", "x"), "file://nas/media/x");
  assert.equal(buildOpenUrl("/Volumes/media", "x"), "file:///Volumes/media/x");
});
