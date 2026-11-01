// The api.ts request core (`send`), driven through a stubbed globalThis.fetch.
// api.ts must stay importable on Node (no DOM at module load).

import assert from "node:assert/strict";
import { afterEach, test } from "node:test";

import {
  ApiError,
  changeMyPassword,
  deleteLibrary,
  errorMessage,
  send,
  setUnauthorizedHandler,
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

afterEach(() => {
  globalThis.fetch = realFetch;
  setUnauthorizedHandler(null);
});

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

test("401 invokes the unauthorized handler exactly once and still throws", async () => {
  const seen: string[] = [];
  setUnauthorizedHandler((p) => seen.push(p));
  stub(() => jsonRes({ detail: "Missing bearer token" }, 401));
  await assert.rejects(send("GET", "/libraries"), ApiError);
  assert.deepEqual(seen, ["/libraries"]);
});

test("/auth/me 401 does not invoke the handler", async () => {
  let n = 0;
  setUnauthorizedHandler(() => n++);
  stub(() => jsonRes({ detail: "no session" }, 401));
  await assert.rejects(send("GET", "/auth/me"), ApiError);
  assert.equal(n, 0);
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
