// The own-write token between SearchPage's reflectHash() and its hashchange
// listener (A5, ruling 2). Pure state machine: no DOM, no Svelte.
//
// reflect() records the hash the page is about to write; incoming() ignores a
// hashchange for exactly that hash ONCE and clears the token, so a later Back or
// Forward to the same hash applies. Any other hash applies and clears it too.
//
// A replace (history.replaceState) fires no hashchange, so its token simply
// waits for the next reflect() or incoming() to clear it. That is safe: a
// hashchange needs the URL to differ from the replaced hash, so the lingering
// token can only meet a DIFFERENT hash first, which clears it.

export type HashWrite = "push" | "replace" | "none";

export interface HashSync {
  /** The page wants the URL to say `encoded` (it currently says `current`).
   *  Returns how to write it: `mode`, or `none` when it already says that. */
  reflect(current: string, encoded: string, mode: "push" | "replace"): { write: HashWrite };
  /** A hashchange arrived for `hash`: `ignore` when it is our own last write. */
  incoming(hash: string): "apply" | "ignore";
}

export function createHashSync(): HashSync {
  let token: string | null = null;
  return {
    reflect(current, encoded, mode) {
      if (encoded === current) {
        token = null;
        return { write: "none" };
      }
      token = encoded;
      return { write: mode };
    },
    incoming(hash) {
      const own = token !== null && hash === token;
      token = null;
      return own ? "ignore" : "apply";
    },
  };
}
