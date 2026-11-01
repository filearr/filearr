// The api.ts request core (`send`), driven through a stubbed globalThis.fetch.
// api.ts must stay importable on Node (no DOM at module load).

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  ApiError,
  NetworkError,
  SLOW_TIMEOUT_MS,
  anySignal,
  batchPatchItems,
  canWrite,
  changeMyPassword,
  deleteLibrary,
  diagnoseLibrary,
  errorMessage,
  probeDownload,
  transferDownloadUrl,
  errorStatus,
  retryAllExtracts,
  retryExtracts,
  runReport,
  send,
  setUnauthorizedHandler,
  syncTaxonomySeed,
} from "../src/lib/api.ts";

type Call = { url: string; init: RequestInit };
const realFetch = globalThis.fetch;
let calls: Call[] = [];

function stub(respond: (url: string, init: RequestInit) => Response | Promise<Response>): void {
  calls = [];
  globalThis.fetch = (async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const url = String(input);
    calls.push({ url, init });
    return respond(url, init);
  }) as typeof fetch;
}

const realAny = Object.getOwnPropertyDescriptor(AbortSignal, "any");
const realTimeout = AbortSignal.timeout;

afterEach(() => {
  globalThis.fetch = realFetch;
  setUnauthorizedHandler(null);
  if (realAny) Object.defineProperty(AbortSignal, "any", realAny);
  AbortSignal.timeout = realTimeout;
});

/** A fetch that only ever settles by rejecting with its signal's reason. */
const hang = (_url: string, init: RequestInit) =>
  new Promise<Response>((_res, rej) => {
    const s = init.signal!;
    if (s.aborted) rej(s.reason);
    s.addEventListener("abort", () => rej(s.reason));
  });

// AbortSignal.timeout's timer is unref'd; keep the loop alive while waiting.
async function alive<T>(p: Promise<T>): Promise<T> {
  const keepalive = setTimeout(() => {}, 2000);
  try {
    return await p;
  } finally {
    clearTimeout(keepalive);
  }
}

const jsonRes = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { "content-type": "application/json" } });

test("200 JSON resolves the parsed body", async () => {
  stub(() => jsonRes({ ok: 1 }));
  assert.deepEqual(await send("GET", "/x"), { ok: 1 });
  assert.equal(calls[0].url, "/api/v1/x");
  assert.equal(calls[0].init.method, "GET");
});

test("204 resolves undefined", async () => {
  stub(() => new Response(null, { status: 204 }));
  assert.equal(await send("DELETE", "/x"), undefined);
});

test("200 with an empty body resolves undefined", async () => {
  stub(() => new Response(null, { status: 200, headers: { "content-length": "0" } }));
  assert.equal(await send("POST", "/x"), undefined);
  stub(() => new Response("", { status: 200 }));
  assert.equal(await send("POST", "/x"), undefined);
});

test("200 text/html (a proxy page) throws ApiError, not SyntaxError", async () => {
  stub(() => new Response("<html>login</html>", { status: 200, headers: { "content-type": "text/html" } }));
  await assert.rejects(send("GET", "/x"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 200);
    assert.equal(e.body, "<html>login</html>");
    return true;
  });
});

test("404 throws ApiError with the parsed detail", async () => {
  stub(() => jsonRes({ detail: "Item not found" }, 404));
  await assert.rejects(send("GET", "/items/1"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 404);
    assert.equal(e.detail, "Item not found");
    return true;
  });
});

test("JSON body is encoded with a Content-Type; no body, no Content-Type", async () => {
  stub(() => jsonRes({}));
  await send("POST", "/x", { body: { a: 1 } });
  assert.equal(calls[0].init.body, JSON.stringify({ a: 1 }));
  assert.equal((calls[0].init.headers as Record<string, string>)["Content-Type"], "application/json");
  await send("GET", "/x");
  assert.equal((calls[1].init.headers as Record<string, string>)["Content-Type"], undefined);
  assert.equal(calls[1].init.body, undefined);
});

test("FormData and string bodies pass through untouched", async () => {
  stub(() => jsonRes({}));
  const fd = new FormData();
  fd.set("f", "v");
  await send("POST", "/x", { body: fd });
  assert.equal(calls[0].init.body, fd);
  assert.equal((calls[0].init.headers as Record<string, string>)["Content-Type"], undefined);
  await send("POST", "/x", { body: '{"pre":1}' });
  assert.equal(calls[1].init.body, '{"pre":1}');
});

test("expect blob / text / none", async () => {
  stub(() => new Response("abc", { status: 200, headers: { "content-type": "text/csv" } }));
  const b = await send<Blob>("GET", "/x", { expect: "blob" });
  assert.equal(await b.text(), "abc");
  assert.equal(await send<string>("GET", "/x", { expect: "text" }), "abc");
  assert.equal(await send("GET", "/x", { expect: "none" }), undefined);
});

test("expect response: 200 JSON resolves status, headers and the parsed body", async () => {
  stub(
    () =>
      new Response(JSON.stringify({ ok: 1 }), {
        status: 200,
        headers: { "content-type": "application/json", "x-total-count": "42" },
      }),
  );
  const r = await send<{ ok: number }>("GET", "/x", { expect: "response" });
  assert.equal(r.status, 200);
  assert.deepEqual(r.body, { ok: 1 });
  assert.equal(r.headers.get("x-total-count"), "42");
});

test("expect response: 204 resolves status 204 with body undefined", async () => {
  stub(() => new Response(null, { status: 204 }));
  const r = await send("DELETE", "/x", { expect: "response" });
  assert.equal(r.status, 204);
  assert.equal(r.body, undefined);
  assert.ok(r.headers instanceof Headers);
});

test("expect response: a non-JSON or empty body resolves body undefined", async () => {
  stub(() => new Response("<html/>", { status: 202, headers: { "content-type": "text/html" } }));
  const r = await send("POST", "/x", { expect: "response" });
  assert.equal(r.status, 202);
  assert.equal(r.body, undefined);
  stub(() => new Response("", { status: 200, headers: { "content-type": "application/json" } }));
  assert.equal((await send("POST", "/x", { expect: "response" })).body, undefined);
});

test("expect response: a non-2xx still throws ApiError", async () => {
  stub(() => jsonRes({ detail: "nope" }, 409));
  await assert.rejects(send("POST", "/x", { expect: "response" }), (e: unknown) => e instanceof ApiError && e.status === 409);
});

test("401 invokes the unauthorized handler exactly once and still throws", async () => {
  const seen: string[] = [];
  setUnauthorizedHandler((p) => seen.push(p));
  stub(() => jsonRes({ detail: "Missing bearer token" }, 401));
  await assert.rejects(send("GET", "/libraries"), ApiError);
  assert.deepEqual(seen, ["/libraries"]);
});

test("GET /auth/me 401 does not invoke the handler", async () => {
  let n = 0;
  setUnauthorizedHandler(() => n++);
  stub(() => jsonRes({ detail: "no session" }, 401));
  await assert.rejects(send("GET", "/auth/me"), ApiError);
  assert.equal(n, 0);
});

test("hook skip is method-aware: only the probe, login and bootstrap calls skip it", async () => {
  const seen: string[] = [];
  setUnauthorizedHandler((p) => seen.push(p));
  stub(() => jsonRes({ detail: "x" }, 401));
  for (const [m, p] of [
    ["GET", "/auth/status"],
    ["GET", "/auth/me"],
    ["POST", "/auth/login"],
    ["POST", "/auth/bootstrap"],
    ["PATCH", "/auth/me"],
    ["POST", "/auth/password"],
    ["POST", "/auth/logout"],
    ["POST", "/auth/status"],
  ]) {
    await assert.rejects(send(m, p), ApiError);
  }
  assert.deepEqual(seen, ["/auth/me", "/auth/password", "/auth/logout", "/auth/status"]);
});

test("a fetch rejecting with Safari's 'Load failed' becomes NetworkError", async () => {
  stub(() => Promise.reject(new TypeError("Load failed")));
  await assert.rejects(send("GET", "/x"), (e: unknown) => {
    assert.ok(e instanceof NetworkError);
    assert.ok((e as NetworkError).cause instanceof TypeError);
    assert.equal(errorMessage(e), "Can't reach the server. Check your connection and try again.");
    assert.equal(errorStatus(e), null);
    return true;
  });
});

test("a fetch rejecting with AbortError is re-thrown unchanged", async () => {
  const abort = new DOMException("stopped", "AbortError");
  stub(() => Promise.reject(abort));
  await assert.rejects(send("GET", "/x"), (e: unknown) => {
    assert.equal(e, abort);
    assert.equal(errorMessage(e), "The request was cancelled.");
    return true;
  });
  const err = Object.assign(new Error("timed out"), { name: "TimeoutError" });
  stub(() => Promise.reject(err));
  await assert.rejects(send("GET", "/x"), (e: unknown) => e === err);
});

test("synchronous bulk/admin endpoints opt in to longer timeouts", async () => {
  const seen: number[] = [];
  AbortSignal.timeout = (ms: number) => {
    seen.push(ms);
    return realTimeout.call(AbortSignal, ms);
  };
  stub(() => jsonRes({}));
  const expect = async (call: () => Promise<unknown>, ms: number, label: string) => {
    seen.length = 0;
    await call();
    assert.deepEqual(seen, [ms], label);
  };
  await expect(() => deleteLibrary("lib1", "name"), 10 * 60_000, "deleteLibrary");
  await expect(() => runReport("largest_files"), SLOW_TIMEOUT_MS, "runReport");
  await expect(() => retryExtracts("lib1"), SLOW_TIMEOUT_MS, "retryExtracts");
  await expect(() => retryAllExtracts(), SLOW_TIMEOUT_MS, "retryAllExtracts");
  await expect(() => diagnoseLibrary("lib1"), SLOW_TIMEOUT_MS, "diagnoseLibrary");
  await expect(() => batchPatchItems({}), SLOW_TIMEOUT_MS, "batchPatchItems");
  await expect(() => syncTaxonomySeed(), SLOW_TIMEOUT_MS, "syncTaxonomySeed");
  await expect(() => send("GET", "/x"), 30_000, "default");
});

test("a timeout aborts and renders as 'The request timed out.'", async () => {
  // AbortSignal.timeout's timer is unref'd; keep the loop alive for the test.
  const keepalive = setTimeout(() => {}, 1000);
  stub(
    (_url, init) =>
      new Promise<Response>((_res, rej) => {
        const s = init.signal!;
        s.addEventListener("abort", () => rej(s.reason));
      }),
  );
  try {
    await assert.rejects(send("GET", "/slow", { timeoutMs: 10 }), (e: unknown) => {
      assert.equal(errorMessage(e), "The request timed out.");
      return true;
    });
  } finally {
    clearTimeout(keepalive);
  }
});

test("a caller signal is honoured alongside the timeout", async () => {
  const keepalive = setTimeout(() => {}, 1000);
  stub(
    (_url, init) =>
      new Promise<Response>((_res, rej) => {
        const s = init.signal!;
        s.addEventListener("abort", () => rej(s.reason));
      }),
  );
  const ac = new AbortController();
  const p = send("GET", "/slow", { signal: ac.signal });
  ac.abort();
  try {
    await assert.rejects(p, (e: unknown) => (e as Error).name === "AbortError");
  } finally {
    clearTimeout(keepalive);
  }
});

test("changeMyPassword resolves on a 204", async () => {
  stub(() => new Response(null, { status: 204 }));
  assert.equal(await changeMyPassword("old", "new-password"), undefined);
  assert.equal(calls[0].url, "/api/v1/auth/password");
  assert.equal(calls[0].init.method, "POST");
});

test("a folded delete helper throws ApiError (not plain Error) on failure", async () => {
  stub(() => jsonRes({ detail: "scan running" }, 409));
  await assert.rejects(deleteLibrary("lib1", "name"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.message, `409: ${JSON.stringify({ detail: "scan running" })}`);
    return true;
  });
  stub(() => new Response(null, { status: 204 }));
  assert.equal(await deleteLibrary("lib1", "name"), undefined);
});

test("200 content-length 0 with expect blob resolves an empty Blob", async () => {
  stub(() => new Response(null, { status: 200, headers: { "content-length": "0" } }));
  const b = await send<Blob>("GET", "/x", { expect: "blob" });
  assert.ok(b instanceof Blob);
  assert.equal(b.size, 0);
  assert.equal(await send<string>("GET", "/x", { expect: "text" }), "");
});

test("malformed JSON throws ApiError rendered as an unexpected response", async () => {
  stub(() => new Response("{oops", { status: 200, headers: { "content-type": "application/json" } }));
  await assert.rejects(send("GET", "/x"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 200);
    assert.equal(errorMessage(e), "The server returned an unexpected response.");
    return true;
  });
});

test("a non-JSON 2xx renders as an unexpected response", async () => {
  stub(() => new Response("<html>sso</html>", { status: 200, headers: { "content-type": "text/html" } }));
  await assert.rejects(send("GET", "/x"), (e: unknown) => {
    assert.equal(errorMessage(e), "The server returned an unexpected response.");
    return true;
  });
});

test("hook skip list is exact: /auth/users and /auth-config/x invoke it", async () => {
  const seen: string[] = [];
  setUnauthorizedHandler((p) => seen.push(p));
  stub(() => jsonRes({ detail: "x" }, 401));
  await assert.rejects(send("GET", "/auth/users"), ApiError);
  await assert.rejects(send("POST", "/auth-config/ldap/test"), ApiError);
  await assert.rejects(send("GET", "/auth/me"), ApiError);
  await assert.rejects(send("GET", "/auth/status?probe=1"), ApiError);
  assert.deepEqual(seen, ["/auth/users", "/auth-config/ldap/test"]);
});

test("a throwing hook still yields the ApiError", async () => {
  setUnauthorizedHandler(() => {
    throw new Error("hook broke");
  });
  stub(() => jsonRes({ detail: "x" }, 401));
  await assert.rejects(send("GET", "/libraries"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 401);
    return true;
  });
});

test("without AbortSignal.any, a caller signal still aborts the request", async () => {
  delete (AbortSignal as { any?: unknown }).any;
  assert.equal(typeof AbortSignal.any, "undefined");
  stub(hang);
  const ac = new AbortController();
  const p = send("GET", "/slow", { signal: ac.signal });
  ac.abort();
  await alive(assert.rejects(p, (e: unknown) => errorMessage(e) === "The request was cancelled."));
});

test("without AbortSignal.any, the timeout still fires", async () => {
  delete (AbortSignal as { any?: unknown }).any;
  stub(hang);
  await alive(
    assert.rejects(send("GET", "/slow", { timeoutMs: 10 }), (e: unknown) => errorMessage(e) === "The request timed out."),
  );
});

test("anySignal fallback honours an already-aborted input", () => {
  delete (AbortSignal as { any?: unknown }).any;
  const ac = new AbortController();
  ac.abort(new DOMException("gone", "AbortError"));
  const s = anySignal([new AbortController().signal, ac.signal]);
  assert.equal(s.aborted, true);
  assert.equal((s.reason as DOMException).name, "AbortError");
});

// A29 / ruling 8 (option A): the download probe is a one-byte ranged GET.
test("probeDownload: 206 resolves; sends GET with Range bytes=0-0 and the cookie", async () => {
  stub(() => new Response("h", { status: 206, headers: { "content-range": "bytes 0-0/5" } }));
  await probeDownload("t1");
  assert.equal(calls.length, 1);
  assert.equal(calls[0].url, "/api/v1/transfers/t1/download");
  assert.equal(calls[0].init.method, "GET");
  assert.equal((calls[0].init.headers as Record<string, string>).Range, "bytes=0-0");
  assert.equal(calls[0].init.credentials, "same-origin");
  assert.equal(transferDownloadUrl("t1"), "/api/v1/transfers/t1/download");
});

test("probeDownload: 409 rejects with ApiError carrying the detail", async () => {
  stub(() => new Response(JSON.stringify({ detail: "Transfer has expired" }), {
    status: 409, headers: { "content-type": "application/json" },
  }));
  await assert.rejects(probeDownload("t1"), (e: unknown) => {
    assert.ok(e instanceof ApiError);
    assert.equal(e.status, 409);
    assert.equal(errorMessage(e, { verb: "download" }), "Transfer has expired");
    return true;
  });
});

// The targeted-scan route is require_scope("write"); write is held by the
// write and admin roles alike.
test("canWrite follows the write scope, not the admin scope", () => {
  const base = { id: "1", username: "u", email: null, kind: "user", disabled: false };
  assert.equal(canWrite(null), false);
  assert.equal(canWrite(undefined), false);
  assert.equal(canWrite({ ...base, global_role: "editor", scopes: ["read", "write"] }), true);
  assert.equal(canWrite({ ...base, global_role: "admin", scopes: ["admin", "read", "write"] }), true);
  assert.equal(canWrite({ ...base, global_role: "viewer", scopes: ["read"] }), false);
  // older payloads without scopes fall back to the admin role check
  assert.equal(canWrite({ ...base, global_role: "admin" }), true);
  assert.equal(canWrite({ ...base, global_role: "user" }), false);
});
