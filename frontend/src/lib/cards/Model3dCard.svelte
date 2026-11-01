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
  const KEYS = HIGHLIGHT_KEYS["three-d-cad"];

  const highlights = $derived([
    { label: "Triangles", value: asStr(m.triangles) },
    { label: "Vertices", value: asStr(m.vertices) },
    { label: "Meshes", value: asStr(m.mesh_count) },
    { label: "Format", value: asStr(m.file_format) },
    { label: "Watertight", value: m.watertight == null ? null : m.watertight ? "yes" : "no" },
    { label: "BBox volume", value: asStr(m.bbox_volume) },
    { label: "Bounding box", value: Array.isArray(m.bbox) ? (m.bbox as unknown[]).join(" × ") : null },
  ]);
</script>

<div class="space-y-4">
  {#if m.unsupported}
    <Alert tone="warn" live={false}>Unsupported / non-mesh geometry — stats may be incomplete.</Alert>
  {/if}
  <Highlights facts={highlights} />
  <KeyFactsCard {item} exclude={KEYS} heading="More details" />
</div>
