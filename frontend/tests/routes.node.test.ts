// Shell route metadata: hash → page mapping and per-page document titles.
// Runs on Node's built-in test runner (`npm test` from frontend/); routes.ts is
// DOM-free at import time so it loads without a browser.

import assert from "node:assert/strict";
import { test } from "node:test";

import { routeFromHash, pageTitle, browseHash, type Page } from "../src/lib/routes.ts";

test("routeFromHash maps every plain route hash to its page", () => {
  const pages: Page[] = [
    "search",
    "libraries",
    "admin",
    "jobs",
    "alerts",
    "timeline",
    "reports",
    "agents",
    "taxonomy",
    "filter-builder",
    "help",
    "about",
    "account",
  ];
  for (const p of pages) {
    assert.deepEqual(routeFromHash(`#/${p}`), { page: p, browseLib: "", browsePath: "" }, p);
  }
});

test("routeFromHash falls back to search for empty and unknown hashes", () => {
  const search = { page: "search", browseLib: "", browsePath: "" };
  assert.deepEqual(routeFromHash(""), search);
  assert.deepEqual(routeFromHash("#/nope"), search);
  assert.deepEqual(routeFromHash("#/admin/extra"), search);
});

test("routeFromHash parses a browse hash", () => {
  assert.deepEqual(routeFromHash(browseHash("lib-1", "Movies/2024")), {
    page: "browse",
    browseLib: "lib-1",
    browsePath: "Movies/2024",
  });
  assert.deepEqual(routeFromHash("#/browse/lib-2"), { page: "browse", browseLib: "lib-2", browsePath: "" });
});

test("pageTitle labels the page and suffixes the app name", () => {
  assert.equal(pageTitle("search"), "Search · Filearr");
  assert.equal(pageTitle("filter-builder"), "Filter builder · Filearr");
  assert.equal(pageTitle("account"), "Account · Filearr");
});
