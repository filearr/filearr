/** Shown when copyText() returns false. */
export const COPY_FAILED = "Copy failed — select the text and copy it manually.";

/** Copy text to the clipboard, working on PLAIN-HTTP deployments too.
 *
 * `navigator.clipboard` exists only in secure contexts (https/localhost) —
 * self-hosted Filearr is typically reached over http://<lan-ip>, where the
 * modern API is undefined or rejects ("clipboard blocked", live user report
 * 2026-07-12). Fallback: a temporary off-screen textarea + execCommand("copy"),
 * which still works everywhere for user-gesture-driven copies.
 * Returns true when the text was (apparently) copied; callers must surface a
 * false (COPY_FAILED) rather than claim success. Never throws.
 */
export async function copyText(text: string): Promise<boolean> {
  if (typeof navigator !== "undefined" && navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      return true;
    } catch {
      // fall through to the legacy path (permissions can reject even here)
    }
  }
  if (typeof document === "undefined") return false;
  // The textarea steals focus; hand it back so keyboard users stay in place.
  const prev = document.activeElement as HTMLElement | null;
  let ta: HTMLTextAreaElement | null = null;
  try {
    ta = document.createElement("textarea");
    ta.value = text;
    ta.setAttribute("readonly", "");
    ta.style.position = "fixed";
    ta.style.top = "-1000px";
    ta.style.opacity = "0";
    document.body.appendChild(ta);
    ta.select();
    ta.setSelectionRange(0, text.length);
    return document.execCommand("copy");
  } catch {
    return false;
  } finally {
    ta?.remove();
    if (prev && prev !== document.body && prev.isConnected && typeof prev.focus === "function") prev.focus();
  }
}
