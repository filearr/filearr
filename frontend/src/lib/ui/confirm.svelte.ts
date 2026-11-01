// Promise-based replacements for window.confirm() and window.prompt(),
// rendered by ConfirmHost (mounted once in App.svelte). confirm() and
// promptText() share one queue, so only one dialog is ever on screen; a second
// call waits until the first has been answered.

import type { PromptRules } from "./promptRules.ts";

export type ConfirmOptions = {
  title: string;
  message?: string;
  /** Consequences, rendered as a bullet list under the message. */
  details?: string[];
  confirmLabel?: string;
  cancelLabel?: string;
  tone?: "default" | "danger";
  typedName?: string;
};

export type PromptOptions = PromptRules & {
  title: string;
  message?: string;
  /** The field's visible label. */
  label: string;
  initial?: string;
  placeholder?: string;
  confirmLabel?: string;
  cancelLabel?: string;
};

type ConfirmRequest =
  | (ConfirmOptions & { kind: "confirm"; resolve: (ok: boolean) => void })
  | (PromptOptions & { kind: "prompt"; resolve: (value: string | null) => void });

const queue = $state<ConfirmRequest[]>([]);

/** Ask the user to confirm. Resolves `true` only on the confirm action;
 *  Cancel and Escape resolve `false`. */
export function confirm(opts: ConfirmOptions): Promise<boolean> {
  return new Promise((resolve) => {
    queue.push({ ...opts, kind: "confirm", resolve });
  });
}

/** Ask the user for a line of text. Resolves the trimmed value once it passes
 *  `required` (default true) and `validate` (failures show under the field and
 *  keep the dialog open); Cancel and Escape resolve `null`. */
export function promptText(opts: PromptOptions): Promise<string | null> {
  return new Promise((resolve) => {
    queue.push({ ...opts, kind: "prompt", resolve });
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

// Resolves `req` once; returns false if it was already answered. A confirm
// takes a boolean, a prompt its value or null; a mismatched result maps to the
// request's "no" (false / null) rather than leaking the wrong type.
function settle(req: ConfirmRequest, result: boolean | string | null): boolean {
  if (settled.has(req)) return false;
  settled.add(req);
  if (req.kind === "confirm") req.resolve(result === true);
  else req.resolve(typeof result === "string" ? result : null);
  return true;
}

/** Answer `req` (a confirm with true/false, a prompt with its value or null)
 *  and let the next queued request show. Ignores a request that is already
 *  answered or no longer the head, so a stray second answer cannot resolve the
 *  next one. */
export function settleConfirm(req: ConfirmRequest, result: boolean | string | null): void {
  if (queue[0] !== req || !settle(req, result)) return;
  // Shift on a later task: the closing dialog first returns focus to its
  // opener, so the next dialog's focus scope records that opener, not the
  // dying dialog's Cancel button.
  setTimeout(() => {
    if (queue[0] === req) queue.shift();
  }, 0);
}

/** Answer every queued request "no" (a confirm `false`, a prompt `null`) and
 *  empty the queue. App.svelte calls it when the ConfirmHost chunk fails to
 *  load, so callers awaiting confirm()/promptText() get "no" instead of
 *  hanging and later calls are not stuck behind them. */
export function cancelAllConfirms(): void {
  for (const req of queue.splice(0)) settle(req, null);
}
