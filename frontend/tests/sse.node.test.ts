// connectSse (Ruling 8): one SSE helper for scan progress (LibrariesPage) and
// agent retrieves (RetrievePanel). A fake EventSource and fake timers are
// injected through `deps`, like polling.ts, so the lifecycle runs under Node:
// mint → open, abort during the mint, data-bearing `error` = terminal, `done` =
// terminal, bare transport error → capped exponential reconnect with ONE timer
// owner, abort cancels everything.

import assert from "node:assert/strict";
import { test } from "node:test";

import { connectSse, type SseDeps, type SseTerminal } from "../src/lib/sse.ts";

type Listener = (ev: { data?: unknown }) => void;

class FakeEventSource {
  closed = false;
  listeners = new Map<string, Listener[]>();
  url: string;
  constructor(url: string) {
    this.url = url;
  }
  addEventListener(type: string, cb: Listener) {
    const l = this.listeners.get(type) ?? [];
    l.push(cb);
    this.listeners.set(type, l);
  }
  close() {
    this.closed = true;
  }
  /** Fire `type`; `data` undefined = a bare transport error. */
  emit(type: string, data?: unknown) {
    const ev = data === undefined ? {} : { data: typeof data === "string" ? data : JSON.stringify(data) };
    for (const cb of this.listeners.get(type) ?? []) cb(ev);
  }
}

function harness() {
  const sources: FakeEventSource[] = [];
  const timers = new Map<number, { cb: () => void; ms: number }>();
  let nextId = 1;
  const deps: SseDeps = {
    createEventSource: (url) => {
      const es = new FakeEventSource(url);
      sources.push(es);
      return es;
    },
    setTimeout: (cb, ms) => {
      const id = nextId++;
      timers.set(id, { cb, ms });
      return id;
    },
    clearTimeout: (id) => {
      timers.delete(id as number);
    },
  };
  return {
    deps,
    sources,
    last: () => sources[sources.length - 1],
    timers,
    /** Fire every pending timer once (each removes itself first). */
    fireTimers: () => {
      for (const [id, t] of [...timers]) {
        timers.delete(id);
        t.cb();
      }
    },
  };
}

// Let awaited mints (and anything chained on them) settle.
const flush = () => new Promise<void>((r) => setImmediate(r));

function setup(over: Partial<Parameters<typeof connectSse>[0]> = {}) {
  const h = harness();
  const ac = new AbortController();
  const frames: { data: unknown; event: string }[] = [];
  const terminals: SseTerminal[] = [];
  let mints = 0;
  connectSse({
    mint: async () => `tok${++mints}`,
    url: (t) => `/s?stream_token=${t}`,
    onFrame: (data, event) => frames.push({ data, event }),
    onTerminal: (t) => terminals.push(t),
    signal: ac.signal,
    deps: h.deps,
    ...over,
  });
  return { h, ac, frames, terminals, mints: () => mints };
}

test("mints a token, opens the stream with it, and delivers parsed frames", async () => {
  const { h, frames } = setup();
  assert.equal(h.sources.length, 0, "nothing opens before the mint resolves");
  await flush();
  assert.equal(h.sources.length, 1);
  assert.equal(h.last().url, "/s?stream_token=tok1");
  h.last().emit("progress", { seen: 3 });
  assert.deepEqual(frames, [{ data: { seen: 3 }, event: "progress" }]);
});

test("a failed mint falls back to a tokenless attempt", async () => {
  const { h } = setup({
    mint: async () => {
      throw new Error("401");
    },
  });
  await flush();
  assert.equal(h.last().url, "/s?stream_token=");
});

test("aborted during the mint: no EventSource is ever created", async () => {
  const { h, ac } = setup();
  ac.abort();
  await flush();
  assert.equal(h.sources.length, 0);
});

test("an already-aborted signal does nothing at all", async () => {
  const h = harness();
  const ac = new AbortController();
  ac.abort();
  let minted = false;
  connectSse({
    mint: async () => {
      minted = true;
      return "t";
    },
    url: (t) => t,
    onFrame: () => {},
    onTerminal: () => {},
    signal: ac.signal,
    deps: h.deps,
  });
  await flush();
  assert.equal(minted, false);
  assert.equal(h.sources.length, 0);
});

test("a data-bearing error is terminal: detail delivered, closed, no reconnect", async () => {
  const { h, terminals, frames } = setup();
  await flush();
  const es = h.last();
  es.emit("error", { scan_id: "s1", detail: "disk vanished" });
  assert.equal(es.closed, true);
  assert.equal(terminals.length, 1);
  assert.equal(terminals[0].kind, "error");
  assert.equal(terminals[0].kind === "error" && terminals[0].detail, "disk vanished");
  assert.deepEqual(terminals[0].data, { scan_id: "s1", detail: "disk vanished" });
  assert.equal(h.timers.size, 0, "no reconnect timer");
  // The server's trailing `done` (and anything else) is ignored after terminal.
  es.emit("done", { status: "failed" });
  es.emit("progress", { seen: 9 });
  es.emit("error");
  assert.equal(terminals.length, 1);
  assert.equal(frames.length, 0);
  assert.equal(h.timers.size, 0);
  await flush();
  assert.equal(h.sources.length, 1);
});

test("a data-bearing error without a JSON detail still terminates (detail null / raw)", async () => {
  const a = setup();
  await flush();
  a.h.last().emit("error", { other: 1 });
  assert.equal(a.terminals[0].kind === "error" && a.terminals[0].detail, null);
  const b = setup();
  await flush();
  b.h.last().emit("error", "plain text");
  assert.equal(b.terminals[0].kind === "error" && b.terminals[0].detail, "plain text");
});

test("done is terminal: data delivered, closed, no reconnect on the trailing error", async () => {
  const { h, terminals } = setup();
  await flush();
  const es = h.last();
  es.emit("done", { status: "finished", seen: 10 });
  assert.equal(es.closed, true);
  assert.deepEqual(terminals, [{ kind: "done", data: { status: "finished", seen: 10 } }]);
  es.emit("error"); // the browser's post-close error
  assert.equal(h.timers.size, 0);
});

test("a bare transport error reconnects with capped exponential backoff and a fresh token", async () => {
  const { h, mints } = setup({ backoff: { initialMs: 1000, maxMs: 5000 } });
  await flush();
  const delays: number[] = [];
  for (let i = 0; i < 5; i++) {
    const es = h.last();
    es.emit("error");
    assert.equal(es.closed, true, "the dropped source is closed (no native replay of the used token)");
    assert.equal(h.timers.size, 1, "exactly one reconnect timer");
    delays.push([...h.timers.values()][0].ms);
    h.fireTimers();
    await flush();
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 5000, 5000]);
  assert.equal(h.sources.length, 6);
  assert.equal(mints(), 6, "every (re)connect mints a new single-use token");
  assert.equal(h.last().url, "/s?stream_token=tok6");
});

test("a healthy frame resets the backoff", async () => {
  const { h } = setup({ backoff: { initialMs: 1000, maxMs: 15000 } });
  await flush();
  h.last().emit("error");
  h.fireTimers();
  await flush();
  h.last().emit("error");
  assert.equal([...h.timers.values()][0].ms, 2000);
  h.fireTimers();
  await flush();
  h.last().emit("progress", { seen: 1 });
  h.last().emit("error");
  assert.equal([...h.timers.values()][0].ms, 1000);
});

test("the default backoff starts at 1 s and caps at 15 s", async () => {
  const { h } = setup();
  await flush();
  const delays: number[] = [];
  for (let i = 0; i < 6; i++) {
    h.last().emit("error");
    delays.push([...h.timers.values()][0].ms);
    h.fireTimers();
    await flush();
  }
  assert.deepEqual(delays, [1000, 2000, 4000, 8000, 15000, 15000]);
});

test("abort during the backoff cancels the timer: nothing reopens", async () => {
  const { h, ac } = setup();
  await flush();
  h.last().emit("error");
  assert.equal(h.timers.size, 1);
  ac.abort();
  assert.equal(h.timers.size, 0);
  await flush();
  assert.equal(h.sources.length, 1);
});

test("abort while the timer's reconnect mint is in flight opens nothing", async () => {
  const { h, ac } = setup();
  await flush();
  h.last().emit("error");
  h.fireTimers(); // reconnect starts minting
  ac.abort();
  await flush();
  assert.equal(h.sources.length, 1);
});

test("abort while open closes the source and ignores later events", async () => {
  const { h, ac, frames, terminals } = setup();
  await flush();
  const es = h.last();
  ac.abort();
  assert.equal(es.closed, true);
  es.emit("progress", { seen: 1 });
  es.emit("done", {});
  es.emit("error");
  assert.equal(frames.length, 0);
  assert.equal(terminals.length, 0);
  assert.equal(h.timers.size, 0);
});

test("events from a superseded (dropped) source are ignored after reconnect", async () => {
  const { h, frames } = setup();
  await flush();
  const old = h.last();
  old.emit("error");
  h.fireTimers();
  await flush();
  old.emit("progress", { stale: true });
  old.emit("error");
  assert.equal(frames.length, 0);
  assert.equal(h.timers.size, 0, "a stale error does not arm a second timer");
  h.last().emit("progress", { fresh: true });
  assert.deepEqual(frames, [{ data: { fresh: true }, event: "progress" }]);
});

test("a malformed frame is skipped without throwing", async () => {
  const { h, frames } = setup();
  await flush();
  h.last().emit("progress", "{not json");
  assert.equal(frames.length, 0);
  h.last().emit("progress", { ok: 1 });
  assert.equal(frames.length, 1);
});

test("custom frame event names are all delivered", async () => {
  const { h, frames } = setup({ frames: ["progress", "offline_timeout"] });
  await flush();
  h.last().emit("offline_timeout", { reason: "offline_timeout" });
  h.last().emit("progress", { bytes: 1 });
  assert.deepEqual(
    frames.map((f) => f.event),
    ["offline_timeout", "progress"],
  );
});
