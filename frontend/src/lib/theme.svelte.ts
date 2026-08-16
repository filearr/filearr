// Svelte 5 runes-based theme store: dark/light/system + custom accent.
import { setShareFormat } from "./osFormat.svelte";
import type { FormatPref } from "./osFormat";

export type Mode = "light" | "dark" | "system";
const MODES: readonly Mode[] = ["light", "dark", "system"];
const FORMATS: readonly FormatPref[] = ["auto", "url", "unc"];
const HEX = /^#[0-9a-fA-F]{6}$/;

const rawMode = localStorage.getItem("theme");
const stored: Mode = MODES.includes(rawMode as Mode) ? (rawMode as Mode) : "system";
const rawAccent = localStorage.getItem("accent");

export const theme = $state({ mode: stored, accent: rawAccent && HEX.test(rawAccent) ? rawAccent : "#6366f1" });

export function applyTheme() {
  const prefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const dark = theme.mode === "dark" || (theme.mode === "system" && prefersDark);
  document.documentElement.classList.toggle("dark", dark);
  document.documentElement.style.setProperty("--accent", theme.accent);
  try {
    localStorage.setItem("theme", theme.mode);
    localStorage.setItem("accent", theme.accent);
  } catch {
    /* private-mode / disabled storage — the in-memory value still applies */
  }
}

/** Server-side account preferences → local stores. Called after login (App)
 *  and after the account page saves, so the person's choices follow them
 *  across browsers: server values WIN over localStorage. Malformed or missing
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
    if (typeof accent === "string" && HEX.test(accent)) {
      theme.accent = accent;
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
