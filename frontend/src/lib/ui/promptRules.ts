// The rule promptText() applies before it resolves. DOM-free so
// tests/promptRules.node.test.ts runs it on Node's test runner.

export type PromptRules = {
  /** Default true: an empty (or whitespace-only) value is rejected. */
  required?: boolean;
  /** Return error copy to reject the trimmed value, undefined to accept it. */
  validate?: (value: string) => string | undefined;
};

/** Trims `raw` and checks it: the error, if any, is shown under the field
 *  instead of resolving. */
export function checkPromptValue(raw: string, opts: PromptRules): { value: string; error?: string } {
  const value = raw.trim();
  if (value === "" && (opts.required ?? true)) return { value, error: "Enter a value." };
  const error = opts.validate?.(value);
  return error === undefined ? { value } : { value, error };
}
