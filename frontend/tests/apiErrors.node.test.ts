// ApiError + errorMessage: the one place an API failure becomes a sentence a
// person can read. apiErrors.ts is DOM-free so it loads on Node's test runner.

import assert from "node:assert/strict";
import { test } from "node:test";

import { ApiError, NetworkError, errorMessage, errorStatus, friendlyError } from "../src/lib/apiErrors.ts";

const json = (o: unknown) => JSON.stringify(o);
const statusOf409 = "That conflicts with the current state. Reload and try again.";

test("ApiError keeps the legacy '<status>: <body>' message and parses detail", () => {
  const e = new ApiError(409, json({ detail: "already exists" }));
  assert.equal(e.name, "ApiError");
  assert.equal(e.status, 409);
  assert.equal(e.message, `409: ${json({ detail: "already exists" })}`);
  assert.equal(e.body, json({ detail: "already exists" }));
  assert.equal(e.detail, "already exists");
  assert.ok(e instanceof Error);
});

test("ApiError.detail is undefined when the body is not JSON or has no detail", () => {
  assert.equal(new ApiError(502, "<html>Bad Gateway</html>").detail, undefined);
  assert.equal(new ApiError(500, json({ error: "x" })).detail, undefined);
  assert.equal(new ApiError(500, "").detail, undefined);
});

test("detail string wins for non-auth statuses", () => {
  assert.equal(errorMessage(new ApiError(409, json({ detail: "Name already taken" }))), "Name already taken");
  assert.equal(errorMessage(new ApiError(400, json({ detail: "Bad cron" }))), "Bad cron");
});

test("pydantic 422 detail array renders loc tail: msg, joined with '; '", () => {
  const body = json({
    detail: [
      { loc: ["body", "name"], msg: "Field required", type: "missing" },
      { loc: ["query", "limit"], msg: "Input should be a valid integer", type: "int_parsing" },
      { loc: [], msg: "Whole body is wrong", type: "x" },
    ],
  });
  assert.equal(
    errorMessage(new ApiError(422, body)),
    "name: Field required; limit: Input should be a valid integer; Whole body is wrong",
  );
});

test("pydantic loc heads: a bare 'body' loc falls back to msg", () => {
  const body = json({ detail: [{ loc: ["body"], msg: "Invalid JSON" }] });
  assert.equal(errorMessage(new ApiError(422, body)), "Invalid JSON");
});

test("detail.validation array joins msg / message / string items", () => {
  const body = json({
    detail: { validation: [{ msg: "unknown field 'foo'" }, { message: "bad operator" }, "trailing junk"] },
  });
  assert.equal(errorMessage(new ApiError(422, body)), "unknown field 'foo'; bad operator; trailing junk");
});

test("other detail objects fall back to the status sentence, not JSON", () => {
  const body = json({ detail: { code: "locked", until: 5 } });
  assert.equal(errorMessage(new ApiError(423, body)), "Request failed (423).");
  assert.equal(errorMessage(new ApiError(409, json({ detail: { id: "x" } }))), statusOf409);
});

test("pydantic loc ending in an index renders parent[index]", () => {
  const body = json({
    detail: [
      { loc: ["body", "tags", 3], msg: "String should have at most 64 characters" },
      { loc: ["body", "items", 0, "name"], msg: "Field required" },
    ],
  });
  assert.equal(
    errorMessage(new ApiError(422, body)),
    "tags[3]: String should have at most 64 characters; name: Field required",
  );
});

test("NetworkError renders the connection sentence and has no status", () => {
  const e = new NetworkError(new TypeError("Load failed"));
  assert.equal(e.name, "NetworkError");
  assert.ok(e.cause instanceof TypeError);
  assert.equal(errorMessage(e), "Can't reach the server. Check your connection and try again.");
  assert.equal(errorStatus(e), null);
});

test("status sentences when there is no usable detail", () => {
  const cases: [number, string][] = [
    [400, "The request was invalid."],
    [401, "Your session has expired. Sign in again."],
    [403, "You don't have permission to do that."],
    [404, "Not found. It may be outside your access or removed."],
    [409, "That conflicts with the current state. Reload and try again."],
    [413, "That is too large."],
    [422, "Some of the input is invalid."],
    [429, "Too many requests. Try again shortly."],
    [500, "The server returned an error (500)."],
    [503, "The server returned an error (503)."],
    [418, "Request failed (418)."],
  ];
  for (const [status, want] of cases) {
    assert.equal(errorMessage(new ApiError(status, "")), want, `status ${status}`);
  }
});

test("a 502 HTML body becomes the server-error sentence, not the markup", () => {
  const msg = errorMessage(new ApiError(502, "<html><body><h1>502 Bad Gateway</h1></body></html>"));
  assert.equal(msg, "The server returned an error (502).");
});

test("403 uses the verb", () => {
  assert.equal(
    errorMessage(new ApiError(403, ""), { verb: "delete this preset" }),
    "You don't have permission to delete this preset.",
  );
});

test("401/403/404 status sentences win even over a detail string", () => {
  assert.equal(
    errorMessage(new ApiError(401, json({ detail: "Missing bearer token" }))),
    "Your session has expired. Sign in again.",
  );
  assert.equal(
    errorMessage(new ApiError(403, json({ detail: "scope admin required" })), { verb: "manage users" }),
    "You don't have permission to manage users.",
  );
  assert.equal(
    errorMessage(new ApiError(404, json({ detail: "Item not found" }))),
    "Not found. It may be outside your access or removed.",
  );
});

test("a network TypeError reads as unreachable", () => {
  const want = "Can't reach the server. Check your connection and try again.";
  assert.equal(errorMessage(new TypeError("Failed to fetch")), want);
  assert.equal(errorMessage(new TypeError("NetworkError when attempting to fetch resource.")), want);
  assert.equal(errorMessage(new TypeError("network connection was lost")), want);
});

test("a non-network TypeError keeps its message", () => {
  assert.equal(errorMessage(new TypeError("x is not a function")), "x is not a function");
});

test("TimeoutError reads as a timeout", () => {
  assert.equal(errorMessage(new DOMException("signal timed out", "TimeoutError")), "The request timed out.");
  const e = new Error("x");
  e.name = "TimeoutError";
  assert.equal(errorMessage(e), "The request timed out.");
});

test("AbortError reads as a cancellation, not a timeout", () => {
  assert.equal(errorMessage(new DOMException("aborted", "AbortError")), "The request was cancelled.");
  const e = new Error("x");
  e.name = "AbortError";
  assert.equal(errorMessage(e), "The request was cancelled.");
});

test("a 2xx ApiError with no detail is an unexpected response", () => {
  assert.equal(errorMessage(new ApiError(200, "<html>login</html>")), "The server returned an unexpected response.");
  assert.equal(errorMessage(new ApiError(200, "{not json")), "The server returned an unexpected response.");
});

test("other values: Error.message, else String()", () => {
  assert.equal(errorMessage(new Error("boom")), "boom");
  assert.equal(errorMessage("plain"), "plain");
  assert.equal(errorMessage(42), "42");
  assert.equal(errorMessage(null), "null");
});

test("never returns a string starting with 'ApiError:'", () => {
  for (const s of [0, 200, 301, 400, 401, 403, 404, 418, 500, 599]) {
    for (const body of ["", "x", json({ detail: "" }), json({ detail: [] }), json({ detail: null })]) {
      const msg = errorMessage(new ApiError(s, body));
      assert.ok(!msg.startsWith("ApiError:"), `${s} ${body} -> ${msg}`);
      assert.ok(msg.length > 0, `${s} ${body} empty`);
    }
  }
});

test("errorStatus reads the status of an ApiError only", () => {
  assert.equal(errorStatus(new ApiError(404, "")), 404);
  assert.equal(errorStatus(new Error("404: nope")), null);
  assert.equal(errorStatus(undefined), null);
});

test("friendlyError is errorMessage with a default 'view' verb", () => {
  assert.equal(friendlyError(new ApiError(403, "")), "You don't have permission to view.");
  assert.equal(friendlyError(new ApiError(403, ""), "save"), "You don't have permission to save.");
  assert.equal(friendlyError(new ApiError(409, json({ detail: "dup" }))), "dup");
});
