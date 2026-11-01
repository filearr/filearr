<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { HIGHLIGHT_KEYS } from "./highlights";
  import { asStr, fmtWhen } from "./format";
  import { Alert } from "../ui";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  // A14: every key here is rendered below AND excluded from "More details".
  const KEYS = HIGHLIGHT_KEYS["document"];

  const highlights = $derived([
    { label: "Pages", value: asStr(m.pages) },
    { label: "Author", value: asStr(m.author) },
    { label: "Created", ...fmtWhen(m.created) },
    { label: "Modified", ...fmtWhen(m.modified) },
    { label: "Encrypted", value: m.encrypted == null ? null : m.encrypted ? "yes" : "no" },
    { label: "Paragraphs", value: asStr(m.paragraphs) },
  ]);
  const rows = $derived([
    { label: "Title", value: asStr(m.title) },
    { label: "Subject", value: asStr(m.subject) },
    { label: "Keywords", value: asStr(m.keywords) },
    { label: "Creator", value: asStr(m.creator) },
    { label: "Producer", value: asStr(m.producer) },
    { label: "Revision", value: asStr(m.revision) },
  ]);
  // P4-T10: note whether extracted body text is present (searchable via P3-T5).
  const hasBody = $derived(typeof m.body_text === "string" && (m.body_text as string).trim().length > 0);
</script>

<div class="space-y-4">
  {#if m.unsupported}
    <Alert tone="warn" live={false}>This document format is not supported by the extractor — only file facts are shown.</Alert>
  {/if}
  <Highlights facts={highlights} />
  <p class="text-xs text-muted">
    {#if hasBody}Body text extracted and searchable.{:else}No extractable body text.{/if}
  </p>
  <KeyFactsCard {item} exclude={KEYS} lead={rows} heading="More details" />
</div>
