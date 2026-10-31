<script lang="ts">
  import type { Snippet } from "svelte";
  import { toneClasses, type Tone } from "./tone.ts";

  let {
    tone = "neutral",
    class: className = "",
    children,
  }: { tone?: Tone; class?: string; children?: Snippet } = $props();
</script>

<span
  class="inline-flex items-center rounded-full px-1.5 py-0.5 text-2xs font-medium {toneClasses(tone)} {className}"
>
  {@render children?.()}
</span>
