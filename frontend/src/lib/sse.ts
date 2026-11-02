// One SSE helper (P7 Ruling 8) for every stream the UI opens: scan progress
// (LibrariesPage) and agent retrieves (RetrievePanel). Modelled on
// RetrievePanel's P6 guards:
//
// * `EventSource` can't set headers, so every (re)connect awaits `mint()` for
//   a single-use stream token. A failed mint tries tokenless (works with auth
//   off; with auth on the 401 lands in the transport-error path below).
// * After the awaited mint, nothing opens if `signal` was aborted meanwhile
//   (component destroyed, stream superseded): the leak in B43.
// * A named server `error` frame carries `data` (failed scan, unknown id): it
//   is TERMINAL — its detail goes to `onTerminal`, the source closes, no
//   reconnect. `done` is terminal too.
// * A bare `error` (no data) is a transport drop. The browser's native retry
//   would replay the already-consumed token and 401, so the source is closed
//   and a reconnect (fresh mint) is scheduled with capped exponential backoff.
//   This helper is the ONE owner of that timer; `signal` cancels it. Callers
//   keep their entry for the stream while it backs off, so a poll can't open
//   a second stream and bypass the backoff.
//
// DOM-free: the EventSource constructor and timers come through `deps`
// (defaults touch the globals only when called), so it runs under Node's test
// runner (tests/sse.node.test.ts).

/** The slice of `EventSource` this helper uses. */
export interface EventSourceLike {
  addEventListener(type: string, listener: (ev: { data?: unknown }) => void): void;
  close(): void;
}

export type SseDeps = {
  createEventSource: (url: string) => EventSourceLike;
  setTimeout: (cb: () => void, ms: number) => unknown;
  clearTimeout: (id: unknown) => void;
};

export type SseBackoff = { initialMs: number; maxMs: number };

export type SseTerminal =
  | { kind: "done"; data: unknown }
  /** `detail`: the frame's `detail` string, else the raw data when it is not
   *  JSON, else null. */
  | { kind: "error"; data: unknown; detail: string | null };

export type SseOptions = {
  /** Resolve a single-use stream token; a rejection means "try tokenless". */
  mint: () => Promise<string>;
  /** The stream URL for a token ("" = tokenless). */
  url: (token: string) => string;
  /** Non-terminal event names delivered to `onFrame` (default `["progress"]`). */
  frames?: readonly string[];
  onFrame: (data: unknown, event: string) => void;
  /** Called at most once; the stream is already closed when it runs. */
  onTerminal: (t: SseTerminal) => void;
  /** Abort = close the source, cancel any pending reconnect, deliver nothing. */
  signal: AbortSignal;
  backoff?: SseBackoff;
  deps?: SseDeps;
};

export const DEFAULT_BACKOFF: SseBackoff = { initialMs: 1000, maxMs: 15000 };

const browserDeps = (): SseDeps => ({
  createEventSource: (url) => new EventSource(url),
  // Called through globalThis: some browsers throw "Illegal invocation" for
  // a detached timer function.
  setTimeout: (cb, ms) => globalThis.setTimeout(cb, ms),
  clearTimeout: (id) => globalThis.clearTimeout(id as ReturnType<typeof setTimeout>),
});

const BAD = Symbol("malformed");

function parse(raw: unknown): unknown {
  if (typeof raw !== "string") return raw ?? null;
  try {
    return JSON.parse(raw);
  } catch {
    return BAD;
  }
}

function detailOf(raw: unknown, data: unknown): string | null {
  if (data === BAD) return typeof raw === "string" && raw ? raw : null;
  if (data && typeof data === "object") {
    const d = (data as { detail?: unknown }).detail;
    if (typeof d === "string" && d) return d;
  }
  return null;
}

export function connectSse(opts: SseOptions): void {
  const { signal, onFrame, onTerminal } = opts;
  const frames = opts.frames ?? ["progress"];
  const { initialMs, maxMs } = opts.backoff ?? DEFAULT_BACKOFF;
  const deps = opts.deps ?? browserDeps();
  if (signal.aborted) return;

  let es: EventSourceLike | null = null;
  let timer: unknown = undefined;
  let attempt = 0;
  let finished = false;

  const teardown = () => {
    finished = true;
    if (timer !== undefined) deps.clearTimeout(timer);
    timer = undefined;
    es?.close();
    es = null;
    signal.removeEventListener("abort", teardown);
  };
  signal.addEventListener("abort", teardown);

  const open = async () => {
    let token = "";
    try {
      token = await opts.mint();
    } catch {
      /* tokenless attempt below */
    }
    // Destroyed / superseded during the mint: open nothing.
    if (finished || signal.aborted) return;
    const src = deps.createEventSource(opts.url(token));
    es = src;
    // Only the current source of a live stream may act: a dropped source's
    // late events (or anything after a terminal) are ignored.
    const current = () => !finished && es === src;

    for (const name of frames) {
      src.addEventListener(name, (ev) => {
        if (!current()) return;
        attempt = 0; // healthy message → reset the backoff
        const data = parse(ev.data);
        if (data !== BAD) onFrame(data, name);
      });
    }
    src.addEventListener("done", (ev) => {
      if (!current()) return;
      const data = parse(ev.data);
      teardown();
      onTerminal({ kind: "done", data: data === BAD ? null : data });
    });
    src.addEventListener("error", (ev) => {
      if (!current()) return;
      if (ev.data) {
        const data = parse(ev.data);
        const detail = detailOf(ev.data, data);
        teardown();
        onTerminal({ kind: "error", data: data === BAD ? ev.data : data, detail });
        return;
      }
      // Transport drop: close (no native replay of the used token) and own
      // the one reconnect timer.
      src.close();
      es = null;
      const delay = Math.min(initialMs * 2 ** attempt, maxMs);
      attempt++;
      timer = deps.setTimeout(() => {
        timer = undefined;
        if (!finished) void open();
      }, delay);
    });
  };

  void open();
}
