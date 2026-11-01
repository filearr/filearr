// The own-write token between SearchPage's reflectHash() and its hashchange
// listener (A5, ruling 2), plus the editing-session flag (final review item 1).
// Pure state machine: no DOM, no Svelte.
//
// reflect() records the hash the page is about to write; incoming() ignores a
// hashchange for exactly that hash ONCE and clears the token, so a later Back or
// Forward to the same hash applies. Any other hash applies and clears it too.
//
// A replace (history.replaceState) fires no hashchange, so its token simply
// waits for the next reflect() or incoming() to clear it. That is safe: a
// hashchange needs the URL to differ from the replaced hash, so the lingering
// token can only meet a DIFFERENT hash first, which clears it.
//
// Editing sessions: typing and slider drags ask for "replace", but replacing
// the entry a chip / saved search / Enter just committed (or the entry Back just
// landed on) would erase it from history. So the FIRST real replace after a
// push or an applied incoming hash is upgraded to a push and opens a session;
// later replaces stay replaces (one entry per session); any push, and any
// applied incoming hash, ends it. "canonical" is the page re-encoding a hash it
// was just handed (deep link, Back/Forward): it always replaces in place and
// leaves the session closed — upgrading it would push a copy of the entry Back
// just landed on and trap the user there.

export type HashWrite = "push" | "replace" | "none";
export type HashMode = "push" | "replace" | "canonical";

export interface HashSync {
  /** The page wants the URL to say `encoded` (it currently says `current`).
   *  Returns how to write it, or `none` when it already says that. */
  reflect(current: string, encoded: string, mode: HashMode): { write: HashWrite };
  /** A hashchange arrived for `hash`: `ignore` when it is our own last write. */
  incoming(hash: string): "apply" | "ignore";
}

export function createHashSync(): HashSync {
  let token: string | null = null;
  let editing = false;
  return {
    reflect(current, encoded, mode) {
      if (mode === "push") editing = false;
      if (encoded === current) {
        token = null;
        return { write: "none" };
      }
      token = encoded;
      if (mode === "canonical") return { write: "replace" };
      if (mode === "replace" && !editing) {
        editing = true;
        return { write: "push" };
      }
      return { write: mode };
    },
    incoming(hash) {
      const own = token !== null && hash === token;
      token = null;
      if (!own) editing = false;
      return own ? "ignore" : "apply";
    },
  };
}
