// One refresh at a time, with a guaranteed refresh after an action (B26).
// A plain call while a run is in flight joins it (a poll tick never stacks a
// second request), so the poller's own in-flight skip keeps working. A forced
// call queues exactly ONE trailing run that starts once the current one
// settles, however many forced calls arrive meanwhile, and resolves when that
// trailing run finishes: an action's `await refresh({ force: true })` returns
// with data fetched after the action. DOM-free, so it is Node-tested.

export type RefreshOpts = { force?: boolean };

export function refreshGate(fn: () => Promise<void>): (opts?: RefreshOpts) => Promise<void> {
  let current: Promise<void> | null = null;
  let trailing: Promise<void> | null = null;
  const noop = () => {};
  const start = (): Promise<void> => {
    const p: Promise<void> = Promise.resolve()
      .then(fn)
      .finally(() => {
        if (current === p) current = null;
      });
    current = p;
    return p;
  };
  return ({ force = false } = {}) => {
    if (!current) return start();
    if (!force) return current;
    if (!trailing) {
      trailing = current.then(noop, noop).then(() => {
        trailing = null;
        return start();
      });
    }
    return trailing;
  };
}
