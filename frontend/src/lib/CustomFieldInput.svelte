<script lang="ts">
  // IN-T4 — ONE typed control for a custom-field VALUE, shared by the bulk
  // action bar and the single-item editor in ItemDetail.
  //
  // The per-data_type mapping mirrors CustomFieldsPanel's create/edit form (the
  // admin surface for the DEFINITIONS): string/url → text, integer/float →
  // number, boolean → checkbox, date → date, select → a <select> of the defined
  // options. Sharing one component is what keeps "what the admin declared" and
  // "what the editor offers" from drifting apart.
  //
  // The bound value is always the RAW control value (string, or boolean for a
  // checkbox). Turning it into the JSON type the field declares — and rejecting
  // a select value that is not one of the options — is `coerceCustomValue()` in
  // ./bulkEdit.ts, called at submit time. Keeping coercion out of the component
  // means it is pure, testable, and identical on both call sites.
  import type { CustomField } from "./api";

  let {
    def,
    value = $bindable(),
    id = undefined,
    disabled = false,
  }: {
    def: CustomField;
    value: string | boolean;
    id?: string;
    disabled?: boolean;
  } = $props();

  const BOX =
    "rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm outline-none " +
    "focus:border-[var(--accent)] disabled:opacity-50 dark:border-slate-700";

  // Text-ish controls are driven value/oninput rather than with `bind:value` on
  // purpose: `bind:value` on <input type="number"> coerces to `number`, which
  // would force this component's one bindable to change type per branch. Keeping
  // the binding a RAW string (or boolean for the checkbox) means one type, one
  // coercion site, and no "0 vs empty" ambiguity when a field is left blank.
  const asText = (v: string | boolean): string => (typeof v === "boolean" ? String(v) : v ?? "");
  const checked = $derived(value === true || value === "true");
</script>

{#if def.data_type === "boolean"}
  <label class="inline-flex items-center gap-1.5 text-sm">
    <input
      {id}
      type="checkbox"
      {disabled}
      {checked}
      onchange={(e) => (value = e.currentTarget.checked)} />
    <span class="text-slate-500">{checked ? "true" : "false"}</span>
  </label>
{:else if def.data_type === "select"}
  <!-- Option membership is NOT enforced server-side (documented deferred), so a
       closed <select> over the declared options is the only thing keeping a typo
       out of user_metadata. Never a free-text box for this type. -->
  <select
    {id}
    class="{BOX} w-48"
    {disabled}
    value={asText(value)}
    onchange={(e) => (value = e.currentTarget.value)}>
    <option value="">— pick a value —</option>
    {#each def.select_options ?? [] as opt (opt)}
      <option value={opt}>{opt}</option>
    {/each}
  </select>
{:else if def.data_type === "integer" || def.data_type === "float"}
  <input
    {id}
    type="number"
    class="{BOX} w-32"
    step={def.data_type === "integer" ? "1" : "any"}
    {disabled}
    placeholder={def.data_type}
    value={asText(value)}
    oninput={(e) => (value = e.currentTarget.value)} />
{:else if def.data_type === "date"}
  <input
    {id}
    type="date"
    class="{BOX} w-40"
    {disabled}
    value={asText(value)}
    oninput={(e) => (value = e.currentTarget.value)} />
{:else if def.data_type === "url"}
  <input
    {id}
    type="url"
    class="{BOX} w-64"
    {disabled}
    placeholder="https://…"
    value={asText(value)}
    oninput={(e) => (value = e.currentTarget.value)} />
{:else}
  <input
    {id}
    type="text"
    class="{BOX} w-64"
    {disabled}
    placeholder={def.label}
    value={asText(value)}
    oninput={(e) => (value = e.currentTarget.value)} />
{/if}
