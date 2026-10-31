// Shared look for the kit's form controls (Input, Select, Textarea), kept in
// one place so the three wrappers cannot drift. DOM-free so the Node test
// runner can load it directly (tests/control.node.test.ts).

export type ControlSize = "sm" | "md";

// Full literal class strings (never assembled from fragments) so Tailwind's
// source scanner can see every utility and generate it. No focus ring here:
// an accent ring fails 3:1 on dark surfaces, so the global :focus-visible
// outline in app.css (lightened under .dark) is the single indicator.
const BASE = "w-full rounded-control border bg-surface text-fg placeholder:text-muted disabled:opacity-50";

const SIZES: Record<ControlSize, string> = {
  md: "px-3 py-1.5 text-sm",
  sm: "px-2 py-1 text-xs",
};

/** Class string for a form control at `size`, with the danger border when `invalid`. */
export function controlClass(size: ControlSize, invalid: boolean): string {
  return `${BASE} ${SIZES[size]} ${invalid ? "border-danger" : "border-border-strong"}`;
}
