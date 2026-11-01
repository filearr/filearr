// Svelte 5 runes-based theme store: dark/light/system + custom accent.
import { setShareFormat } from "./osFormat.svelte";
import type { FormatPref } from "./osFormat";
import { DEFAULT_ACCENT, accentTextColor, normalizeAccent, onAccentColor } from "./color";
import * as safeStorage from "./safeStorage";

export type Mode = "light" | "dark" | "system";
const MODES: readonly Mode[] = ["light", "dark", "system"];
const FORMATS: readonly FormatPref[] = ["auto", "url", "unc"];

const rawMode = safeStorage.get("theme");
const stored: Mode = MODES.includes(rawMode as Mode) ? (rawMode as Mode) : "system";
// Defined in color.ts (DOM-free, so normalizeAccent is testable under Node);
// re-exported because the account page imports it from here.
export { DEFAULT_ACCENT };

export const theme = $state({ mode: stored, accent: normalizeAccent(safeStorage.get("accent")) ?? DEFAULT_ACCENT });

export function applyTheme() {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme.mode === "dark" || (theme.mode === "system" && prefersDark);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.setProperty("--accent", theme.accent);
  // Text on the accent flips to dark when the chosen accent is too pale for white.
  document.documentElement.style.setProperty("--accent-fg", onAccentColor(theme.accent));
  // Accent-as-text, nudged just far enough for 4.5:1 per theme. Dark targets
  // surface-2 (#0f172a), the lighter — so stricter — of the two dark surfaces.
  document.documentElement.style.setProperty("--accent-text-light", accentTextColor(theme.accent, "#ffffff"));
  document.documentElement.style.setProperty("--accent-text-dark", accentTextColor(theme.accent, "#0f172a"));
  safeStorage.set("theme", theme.mode);
  safeStorage.set("accent", theme.accent);
}

/** Server-side account preferences → local stores. Called after login (App)
 *  and after the account page saves, so the person's choices follow them
 *  across browsers: server values WIN over local storage. Malformed or missing
 *  values are ignored field-by-field. Returns true when anything was applied. */
export function applyServerPreferences(prefs: unknown): boolean {
  if (!prefs || typeof prefs !== "object") return false;
  const p = prefs as Record<string, unknown>;
  let touched = false;
  const t = p.theme;
  if (t && typeof t === "object") {
    const { mode, accent } = t as Record<string, unknown>;
    if (typeof mode === "string" && MODES.includes(mode as Mode)) {
      theme.mode = mode as Mode;
      touched = true;
    }
    const a = normalizeAccent(accent);
    if (a) {
      theme.accent = a;
      touched = true;
    }
  }
  if (touched) applyTheme();
  const sf = p.share_format;
  if (typeof sf === "string" && FORMATS.includes(sf as FormatPref)) {
    setShareFormat(sf as FormatPref);
    touched = true;
  }
  return touched;
}
