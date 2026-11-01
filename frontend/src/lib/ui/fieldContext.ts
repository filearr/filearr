// Field → control wiring. Field publishes its ids and state through Svelte
// context under FIELD_CTX; Input/Select/Textarea/Checkbox/Switch read it and
// merge it with their own props via controlAttrs. DOM-free so the Node test
// runner can load it directly (tests/fieldContext.node.test.ts).

/** Context key Field sets and the kit's controls read. */
export const FIELD_CTX = Symbol("field");

/** What a Field shares with the control inside it. Field implements these as
 *  getters over its reactive state, so reading them always yields the current value. */
export type FieldCtx = {
  readonly id: string;
  readonly describedBy: string | undefined;
  readonly invalid: boolean;
  readonly required: boolean;
};

export type OwnControlProps = {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: string | boolean;
  required?: boolean;
  invalid?: boolean;
};

export type ControlAttrs = {
  id?: string;
  "aria-describedby"?: string;
  "aria-invalid"?: "true" | undefined;
  required?: boolean;
  invalid: boolean;
};

/** Space-separated id lists joined in order, de-duplicated; empty → undefined. */
function mergeIds(...lists: (string | undefined)[]): string | undefined {
  const ids = [...new Set(lists.flatMap((l) => l?.split(/\s+/) ?? []).filter(Boolean))];
  return ids.length ? ids.join(" ") : undefined;
}

/**
 * Attributes for a control: explicit props win, the Field context (if any)
 * fills the gaps. aria-describedby is the exception: own ids come first and
 * the Field's hint/error ids are appended, so a caller's extra description
 * does not silently drop the Field's. `invalid` is the single source of
 * truth for aria-invalid: it is "true" when invalid and otherwise omitted, so
 * a caller's own aria-invalid string (including "false") is deliberately not
 * passed through.
 */
export function controlAttrs(ctx: FieldCtx | undefined, own: OwnControlProps): ControlAttrs {
  const invalid = own.invalid ?? ctx?.invalid ?? false;
  return {
    id: own.id ?? ctx?.id,
    "aria-describedby": mergeIds(own["aria-describedby"], ctx?.describedBy),
    "aria-invalid": invalid ? "true" : undefined,
    required: own.required ?? ctx?.required,
    invalid,
  };
}

/** An own id on a control inside a <Field> wins over the Field's, which leaves
 *  the Field's <label for> pointing at nothing. `enabled` is the caller's
 *  Vite DEV flag; this module reads no Vite env itself so Node can
 *  load it. */
export function warnFieldIdMismatch(ctx: FieldCtx | undefined, ownId: string | undefined, enabled: boolean): void {
  if (enabled && ctx && ownId && ownId !== ctx.id) {
    console.warn("Put the id on <Field> rather than the control; the label points at the Field's id.");
  }
}
