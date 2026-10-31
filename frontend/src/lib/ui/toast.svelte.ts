// Transient notifications, rendered by ToastHost (mounted once in App.svelte).
// Speech goes through the app's single live region via announce(); the visual
// stack is aria-hidden so a screen reader hears each message once.
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

/** Show a toast. Errors (`danger`) persist until dismissed; the rest
 *  auto-dismiss after `durationMs` (default 5 s). */
export function toast(
  message: string,
  opts: { tone?: ToastTone; durationMs?: number; actionLabel?: string; onAction?: () => void } = {},
): void {
  announce(message);
  const tone = opts.tone ?? "info";
  entries.push({
    id: ++nextId,
    message,
    tone,
    // An error the user never saw must not vanish on its own.
    durationMs: tone === "danger" ? null : (opts.durationMs ?? DEFAULT_DURATION_MS),
    actionLabel: opts.actionLabel,
    onAction: opts.onAction,
  });
  // Oldest drop first, so the newest message is always on screen.
  if (entries.length > MAX_VISIBLE) entries.splice(0, entries.length - MAX_VISIBLE);
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
