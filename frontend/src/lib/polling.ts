// Shared visibility-aware poller. Every page that refreshes on a timer goes
// through here so a backgrounded tab stops hitting the API, and a tab brought
// back to the foreground catches up at once instead of waiting a full interval.
//
// The core (`createPoller`) takes its clock and visibility as deps so it runs
// under Node's test runner; only `usePolling` touches `document`, and only
// inside its body, so importing this module never needs a DOM.

export type PollerDeps = {
  setInterval: typeof globalThis.setInterval;
  clearInterval: typeof globalThis.clearInterval;
  isVisible: () => boolean;
  onVisibility: (cb: () => void) => () => void; // subscribe; returns unsubscribe
  now: () => number; // ms clock for the return-refresh rate limit
};

export type Poller = { stop(): void };

export type PollerOpts = {
  /** Call `fn` once at creation (default false: sites do their own first load). */
  immediate?: boolean;
  /** Call `fn` once when the tab becomes visible again (default true), unless
   *  it already ran within the last `intervalMs`. */
  refreshOnReturn?: boolean;
};

export function createPoller(
  fn: () => void,
  intervalMs: number,
  deps: PollerDeps,
  opts: PollerOpts = {},
): Poller {
  const { immediate = false, refreshOnReturn = true } = opts;
  let lastCall = -Infinity;
  const call = () => {
    lastCall = deps.now();
    fn();
  };
  // The interval keeps running while hidden and just skips the call: cheaper
  // and simpler than tearing it down and re-arming on every visibility flip.
  const id = deps.setInterval(() => {
    if (deps.isVisible()) call();
  }, intervalMs);
  // Rate-limited so rapid tab switching cannot fire `fn` on every return;
  // the data is never staler than one interval anyway. Ticks are not limited.
  const unsubscribe = deps.onVisibility(() => {
    if (refreshOnReturn && deps.isVisible() && deps.now() - lastCall >= intervalMs) call();
  });
  if (immediate) call();
  return {
    stop() {
      deps.clearInterval(id);
      unsubscribe();
    },
  };
}

/** Browser wrapper: `createPoller` wired to `document.visibilityState`. */
export function usePolling(
  fn: () => void,
  intervalMs: number,
  opts?: PollerOpts,
): Poller {
  return createPoller(
    fn,
    intervalMs,
    {
      // Bound to globalThis: some browsers throw "Illegal invocation" when
      // setInterval is called detached from window.
      setInterval: globalThis.setInterval.bind(globalThis),
      clearInterval: globalThis.clearInterval.bind(globalThis),
      isVisible: () => document.visibilityState === "visible",
      onVisibility: (cb) => {
        document.addEventListener("visibilitychange", cb);
        return () => document.removeEventListener("visibilitychange", cb);
      },
      now: Date.now,
    },
    opts,
  );
}
