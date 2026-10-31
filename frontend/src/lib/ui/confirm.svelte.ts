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

/** Answer `req` and let the next queued request show. Ignores a request that
 *  is no longer the head, so a stray second answer cannot resolve the next one. */
export function settleConfirm(req: ConfirmRequest, ok: boolean): void {
  if (queue[0] !== req) return;
  queue.shift();
  req.resolve(ok);
}
