// Promise-based replacement for window.confirm(), rendered by ConfirmHost
// (mounted once in App.svelte). Requests queue so only one dialog is ever on
// screen; a second call waits until the first has been answered.

export type ConfirmOptions = {
  title: string;
  message?: string;
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  typedName?: string;
};

type ConfirmRequest = ConfirmOptions & { resolve: (ok: boolean) => void };

const queue = $state<ConfirmRequest[]>([]);

/** Ask the user to confirm. Resolves `true` only on the confirm action;
 *  Cancel and Escape resolve `false`. */
export function confirm(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    queue.push({ ...opts, resolve });
  });
}

// Host-only plumbing below; index.ts does not re-export it.

/** The request currently on screen (the head of the queue). */
export function activeConfirm(): ConfirmRequest | undefined {
  return queue[0];
}

/** True while any request is waiting or on screen. App.svelte uses it to
 *  load ConfirmHost (and bits-ui) only when a dialog is actually needed. */
export function hasPendingConfirm(): boolean {
  return queue.length > 0;
}

const settled = new WeakSet<ConfirmRequest>();

/** Answer `req` and let the next queued request show. Ignores a request that
 *  is already answered or no longer the head, so a stray second answer cannot
 *  resolve the next one. */
export function settleConfirm(req: ConfirmRequest, ok: boolean): void {
  if (queue[0] !== req || settled.has(req)) return;
  settled.add(req);
  req.resolve(ok);
  // Shift on a later task: the closing dialog first returns focus to its
  // opener, so the next dialog's focus scope records that opener, not the
  // dying dialog's Cancel button.
  setTimeout(() => {
    if (queue[0] === req) queue.shift();
  }, 0);
}
