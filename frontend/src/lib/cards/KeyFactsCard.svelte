<script lang="ts">
  // P4-T12 — custom-field-aware key-facts. The generic fallback card AND the
  // "more details" section every per-type card embeds. Orders/labels fields by
  // profile (P4-T1) -> applicable custom fields (P4-T3) -> ad-hoc keys
  // (alphabetical), rendering text-only values (see keyfacts.ts).
  // A44: both definitions are fetched once per page load and shared by every
  // card mount, ItemDetail and BulkEditBar (configCache.ts).
  // P6 (A14): `lead` rows are facts the per-type card renders as plain rows
  // (their keys are in its `exclude`); a failed extraction's `_extract_error`
  // shows as a warning above the facts instead of "No additional details".
  import { cachedCustomFields, cachedProfile } from "../configCache";
  import { Alert } from "../ui";
  import {
    effectiveMeta,
    extractError,
    orderKeyFacts,
    profileFieldLabels,
    customFieldLabels,
    type FieldLabel,
  } from "./keyfacts";

  let {
    item,
    exclude = [],
    heading = "",
    lead = [],
  }: {
    item: Record<string, unknown>;
    exclude?: readonly string[];
    heading?: string;
    lead?: { label: string; value: string | null }[];
  } = $props();

  const mediaType = $derived(typeof item.file_category === "string" ? item.file_category : "");
  const libraryId = $derived(typeof item.library_id === "string" ? item.library_id : "");
  const meta = $derived(effectiveMeta(item));
  const extractFailure = $derived(extractError(meta));
  const leadRows = $derived(lead.filter((r) => r.value != null && r.value !== ""));

  let profileLabels = $state<FieldLabel[]>([]);
  let cfLabels = $state<FieldLabel[]>([]);

  // Profile labels/order (best-effort: an unknown/unseeded type -> no profile,
  // fields then fall back to their raw key names).
  $effect(() => {
    const mt = mediaType;
    if (!mt) {
      profileLabels = [];
      return;
    }
    const ac = new AbortController();
    cachedProfile(mt, ac.signal)
      .then((p) => (profileLabels = p ? profileFieldLabels(p.fields) : []))
      .catch(() => {
        if (!ac.signal.aborted) profileLabels = [];
      });
    return () => ac.abort();
  });

  // Applicable custom fields, narrowed by media_type + library (empty
  // applies_to/library_ids = "all"). Best-effort; a fetch failure -> none.
  $effect(() => {
    const mt = mediaType;
    const lib = libraryId;
    const ac = new AbortController();
    cachedCustomFields(ac.signal)
      .then((defs) => {
        const applicable = defs.filter(
          (d) =>
            (!d.applies_to?.length || d.applies_to.includes(mt)) &&
            (!d.library_ids?.length || d.library_ids.includes(lib)),
        );
        cfLabels = customFieldLabels(applicable);
      })
      .catch(() => {
        if (!ac.signal.aborted) cfLabels = [];
      });
    return () => ac.abort();
  });

  const facts = $derived(orderKeyFacts(meta, profileLabels, cfLabels, { exclude: [...exclude] }));
</script>

{#if extractFailure}
  <Alert tone="warn" live={false} class="mb-3">Extraction failed: {extractFailure}</Alert>
{/if}
{#if facts.length || leadRows.length}
  <div>
    {#if heading}
      <div class="mb-1 text-xs font-semibold text-muted">{heading}</div>
    {/if}
    <dl class="grid grid-cols-[max-content_1fr] gap-x-4 gap-y-1 text-sm">
      {#each leadRows as r (r.label)}
        <dt class="text-muted">{r.label}</dt>
        <dd class="min-w-0 break-words">{r.value}</dd>
      {/each}
      {#each facts as f (f.key)}
        <dt class="text-muted">{f.label}</dt>
        <dd class="min-w-0 break-words">{f.value}</dd>
      {/each}
    </dl>
  </div>
{:else if !heading && !extractFailure}
  <p class="text-sm text-muted">No additional details extracted.</p>
{/if}
