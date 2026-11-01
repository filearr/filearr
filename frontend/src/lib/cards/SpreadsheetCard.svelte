<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { HIGHLIGHT_KEYS } from "./highlights";
  import { asStr } from "./format";
  import { Alert } from "../ui";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  // A14: every key here is rendered below AND excluded from "More details".
  const KEYS = HIGHLIGHT_KEYS["spreadsheet"];

  const highlights = $derived([
    { label: "Sheets", value: asStr(m.sheet_count) },
    { label: "Author", value: asStr(m.author) },
    { label: "Created", value: asStr(m.created) },
    { label: "Modified", value: asStr(m.modified) },
  ]);
  const rows = $derived([
    { label: "Title", value: asStr(m.title) },
    { label: "Subject", value: asStr(m.subject) },
  ]);
  const sheets = $derived(Array.isArray(m.sheets) ? (m.sheets as unknown[]).map(String) : []);
</script>

<div class="space-y-4">
  {#if m.unsupported}
    <Alert tone="warn" live={false}>This spreadsheet format is not supported by the extractor — only file facts are shown.</Alert>
  {/if}
  <Highlights facts={highlights} />
  {#if sheets.length}
    <div>
      <div class="mb-1 text-xs font-semibold text-muted">Sheet names</div>
      <div class="flex flex-wrap gap-1">
        {#each sheets as s, i (i)}
          <span class="rounded-control bg-surface-3 px-2 py-0.5 text-xs">{s}</span>
        {/each}
      </div>
    </div>
  {/if}
  <KeyFactsCard {item} exclude={KEYS} lead={rows} heading="More details" />
</div>
