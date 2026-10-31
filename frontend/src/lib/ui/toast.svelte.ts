// Transient notifications, rendered by ToastHost (mounted once in App.svelte).
// Speech goes through the app's single live region via announce(); the visual
// stack is a labelled region with no live role, so a screen reader hears each
// message once while the toasts' buttons stay reachable.
import { announce } from "../announce.svelte.ts";
import type { Tone } from "./tone.ts";

export type ToastTone = Exclude<Tone, "neutral" | "accent">;

export type ToastEntry = {
  id: number;
  message: string;
  tone: ToastTone;
  /** null: stays until dismissed. */
  durationMs: number | null;
  actionLabel?: string;
  onAction?: () => void;
};

const MAX_VISIBLE = 3;
const DEFAULT_DURATION_MS = 5000;

const entries = $state<ToastEntry[]>([]);
let nextId = 0;

/** Show a toast. Errors (`danger`) and toasts with an action persist until
 *  dismissed unless `durationMs` is passed; the rest auto-dismiss after
 *  `durationMs` (default 5 s). Errors persist even if `durationMs` is passed. */
export function toast(
  message: string,
  opts: { tone?: ToastTone; durationMs?: number; actionLabel?: string; onAction?: () => void } = {},
): void {
  announce(message);
  const tone = opts.tone ?? "info";
  // An error the user never saw must not vanish on its own, and an action a
  // keyboard user is still tabbing towards must not vanish before they get there.
  const persistent = tone === "danger" || (opts.actionLabel !== undefined && opts.durationMs === undefined);
  entries.push({
    id: ++nextId,
    message,
    tone,
    durationMs: persistent ? null : (opts.durationMs ?? DEFAULT_DURATION_MS),
    actionLabel: opts.actionLabel,
    onAction: opts.onAction,
  });
  // Over the cap, the oldest routine toast (no error, no action) goes first so persistent
  // ones (errors, actions) are not lost to routine messages; only when all are
  // persistent does the oldest go. The newest message is always on screen.
  while (entries.length > MAX_VISIBLE) {
    const i = entries.findIndex((t) => t.tone !== "danger" && t.actionLabel === undefined);
    entries.splice(i === -1 ? 0 : i, 1);
  }
}

// Host-only plumbing below; index.ts does not re-export it.

/** The toasts currently on screen, oldest first. */
export function visibleToasts(): readonly ToastEntry[] {
  return entries;
}

export function dismissToast(id: number): void {
  const i = entries.findIndex((t) => t.id === id);
  if (i !== -1) entries.splice(i, 1);
}
