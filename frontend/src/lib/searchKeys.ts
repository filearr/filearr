// Search page keyboard map, as a pure function so the target gate is testable.
// The page listens on window, so every key typed anywhere on it lands here:
// list keys must only fire from the results listbox (or the bare page), never
// from the facet inputs, selects or buttons (A1). The query box is the
// combobox for the results: plain ArrowDown/ArrowUp there move the results
// cursor, every other key (Enter, Space, letters) stays with the text box.

export type KeyAction = "focusSearch" | "move:-1" | "move:1" | "open" | "copy" | "toggle";

const CONTROLS = new Set(["INPUT", "SELECT", "TEXTAREA", "BUTTON", "A"]);

export function listKeyAction(
  e: { key: string; metaKey: boolean; ctrlKey: boolean; shiftKey: boolean },
  target: { tag: string; editable: boolean; inListbox: boolean; combobox: boolean },
  state: { modalOpen: boolean; view: string; hasHits: boolean },
): KeyAction | null {
  const mod = e.metaKey || e.ctrlKey;
  // Cmd/Ctrl+K works from anywhere; '/' only when not typing.
  if (mod && (e.key === "k" || e.key === "K")) return "focusSearch";
  if (e.key === "/" && !target.editable) return "focusSearch";

  // The ItemDetail modal owns keys while open; on the map, arrows pan.
  if (state.modalOpen || state.view === "map" || !state.hasHits) return null;
  if (target.combobox && !mod && !e.shiftKey) {
    if (e.key === "ArrowDown") return "move:1";
    if (e.key === "ArrowUp") return "move:-1";
  }
  if (target.editable) return null;
  if (!target.inListbox && CONTROLS.has(target.tag)) return null;

  const bare = target.tag === "BODY" || target.tag === "MAIN";
  if (e.key === "ArrowDown") return target.inListbox || bare ? "move:1" : null;
  if (e.key === "ArrowUp") return target.inListbox || bare ? "move:-1" : null;
  if (!target.inListbox) return null;
  if (e.key === "Enter") return mod ? "copy" : "open";
  if (e.key === " ") return "toggle";
  return null;
}
