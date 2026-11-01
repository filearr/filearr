<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { HIGHLIGHT_KEYS } from "./highlights";
  import { asStr, fmtWhen } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  // A14: every key here is rendered below AND excluded from "More details".
  const KEYS = HIGHLIGHT_KEYS["image"];

  const highlights = $derived([
    { label: "Dimensions", value: m.width && m.height ? `${asStr(m.width)}×${asStr(m.height)}` : null },
    { label: "Format", value: asStr(m.format) },
    { label: "Colour mode", value: asStr(m.mode) },
    { label: "Camera", value: asStr(m.camera) },
    { label: "Taken at", ...fmtWhen(m.taken_at) },
  ]);
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  <KeyFactsCard {item} exclude={KEYS} heading="More details" />
</div>
