// Last-request-wins sequencing for loaders a user can re-trigger faster than
// the API answers (paging, route changes, filter edits). Each run aborts the
// one before it, and a superseded run resolves `undefined` instead of
// throwing, so a stale response never lands and a cancelled one never
// surfaces as "The request was cancelled.". DOM-free, so it is Node-tested.

export type Latest<A extends unknown[], R> = {
  /** Resolves `fn`'s result, or `undefined` when a newer run or `cancel()`
   *  superseded this one. Errors from the current run propagate. */
  run(...args: A): Promise<R | undefined>;
  /** Abort the in-flight run (e.g. on destroy); it resolves `undefined`. */
  cancel(): void;
};

export function latest<A extends unknown[], R>(
  fn: (signal: AbortSignal, ...args: A) => Promise<R>,
): Latest<A, R> {
  let seq = 0;
  let controller: AbortController | null = null;
  const cancel = () => {
    seq++;
    controller?.abort();
    controller = null;
  };
  return {
    async run(...args: A) {
      cancel();
      const id = seq;
      const ac = new AbortController();
      controller = ac;
      try {
        const r = await fn(ac.signal, ...args);
        return id === seq ? r : undefined;
      } catch (e) {
        // Superseded: whatever it failed with (usually the abort) is moot.
        if (id !== seq) return undefined;
        throw e;
      } finally {
        if (controller === ac) controller = null;
      }
    },
    cancel,
  };
}
