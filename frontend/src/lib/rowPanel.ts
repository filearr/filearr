// Focus moves for an editor or expander that a DataTable row button opens
// below the table. DataTable renders every cell twice (table ≥md, card list
// <md, one hidden by CSS), so the row button carries a `data-*-for="<rowId>"`
// attribute (never an id) and the visible copy is the one focused. The panel
// has an id and tabindex="-1"; the row button points at it with aria-controls.

import { tick } from "svelte";

/** After the panel renders: scroll it into view (nearest) and focus it. */
export async function focusPanel(id: string): Promise<void> {
  await tick();
  const el = document.getElementById(id);
  el?.scrollIntoView({ block: "nearest" });
  el?.focus({ preventScroll: true });
}

/** After the panel closes: focus the visible row button whose `attr` is `key`. */
export async function focusRowButton(attr: string, key: string): Promise<void> {
  await tick();
  const buttons = document.querySelectorAll<HTMLElement>(`[${attr}="${CSS.escape(key)}"]`);
  Array.from(buttons).find((b) => b.offsetParent !== null)?.focus();
}
