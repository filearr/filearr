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

/**
 * Attributes for a control: explicit props win, the Field context (if any)
 * fills the gaps. `invalid` is the single source of truth for aria-invalid:
 * it is "true" when invalid and otherwise omitted, so a caller's own
 * aria-invalid string (including "false") is deliberately not passed through.
 */
export function controlAttrs(ctx: FieldCtx | undefined, own: OwnControlProps): ControlAttrs {
  const invalid = own.invalid ?? ctx?.invalid ?? false;
  return {
    id: own.id ?? ctx?.id,
    "aria-describedby": own["aria-describedby"] ?? ctx?.describedBy,
    "aria-invalid": invalid ? "true" : undefined,
    required: own.required ?? ctx?.required,
    invalid,
  };
}
