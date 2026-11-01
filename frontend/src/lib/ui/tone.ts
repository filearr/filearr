// Shared colour vocabulary for the UI kit. DOM-free so the Node test runner
// can load it directly (tests/tone.node.test.ts).

export type Tone = "neutral" | "ok" | "warn" | "danger" | "info" | "accent";

// Full literal class strings (never assembled from fragments) so Tailwind's
// source scanner can see every utility and generate it.
const TONE_CLASSES: Record<Tone, string> = {
  neutral: "bg-surface-3 text-muted",
  ok: "bg-ok-bg text-ok",
  warn: "bg-warn-bg text-warn",
  danger: "bg-danger-bg text-danger",
  info: "bg-info-bg text-info",
  accent: "bg-accent/15 text-accent-text",
};

/** Badge/alert colour utilities for a tone, built only from the theme tokens. */
export function toneClasses(tone: Tone): string {
  return TONE_CLASSES[tone];
}

// Exact-word lookup rather than substring matching, so e.g. "disabled" never
// collides with "enabled" and unknown statuses fall through to neutral.
const STATUS_TONES = new Map<string, Tone>();
const groups: [Tone, string[]][] = [
  ["danger", ["critical", "error", "failed", "failure", "fatal", "offline", "revoked", "disabled"]],
  ["warn", ["warning", "warn", "degraded", "stale", "pending", "queued", "held", "stopped", "stopping", "cancelled", "canceled"]],
  ["ok", ["ok", "success", "succeeded", "completed", "done", "healthy", "online", "active", "enabled", "verified", "finished"]],
  ["info", ["running", "info", "in_progress", "scanning", "syncing"]],
];
for (const [tone, words] of groups) for (const w of words) STATUS_TONES.set(w, tone);

/** Map a backend status string to a tone, case-insensitively; unknown → neutral. */
export function toneForStatus(status: string | null | undefined): Tone {
  if (!status) return "neutral";
  return STATUS_TONES.get(status.trim().toLowerCase()) ?? "neutral";
}
