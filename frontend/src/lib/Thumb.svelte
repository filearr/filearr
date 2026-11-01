<script lang="ts">
  // A small thumbnail in a fixed box (S12/P12 slice 1, P6 A22 / ruling 6).
  // The box always renders a placeholder (the category's short name), so a row
  // or tile is the same size with or without a picture. Only items the server
  // can render (thumbPolicy.thumbable, mirroring backend thumbs.py) request an
  // image, layered over the placeholder. A failed load retries once after
  // RETRY_DELAY_MS — a video poster that is still queued is a 404 at first —
  // and a second failure leaves the placeholder.
  import { onDestroy } from "svelte";
  import { thumbUrl } from "./api";
  import { RETRY_DELAY_MS, placeholderText, retryUrl, thumbable } from "./thumbPolicy";

  interface Props {
    id: string;
    tier?: "grid" | "preview";
    size?: string; // tailwind size classes for the box, e.g. "h-10 w-10"
    rounded?: string;
    /** backend category; decides whether a thumbnail is requested at all. */
    category?: string | null;
    /** the file name; a `document` is thumbnailed only when it is a .pdf. */
    filename?: string | null;
    /** "contain" for previews that must show the whole picture. */
    fit?: "cover" | "contain";
  }
  let {
    id,
    tier = "grid",
    size = "h-10 w-10",
    rounded = "rounded",
    category = null,
    filename = null,
    fit = "cover",
  }: Props = $props();

  const wanted = $derived(thumbable(category, filename));
  const base = $derived(thumbUrl(id, tier));

  // Per-URL load state. "waiting": the first load failed, the <img> is gone
  // until the retry; "retrying": the retry URL is loading; "failed": final.
  // Keyed by URL so an instance reused for another item starts fresh.
  type Phase = "first" | "waiting" | "retrying" | "failed";
  let load = $state<{ key: string; phase: Phase }>({ key: "", phase: "first" });
  const phase = $derived(load.key === base ? load.phase : "first");
  const src = $derived(phase === "retrying" ? retryUrl(base) : base);

  // Loaded: the placeholder text goes, so it never shows through a contained
  // (letterboxed) or transparent picture.
  let loadedSrc = $state("");
  const loaded = $derived(wanted && loadedSrc === src && (phase === "first" || phase === "retrying"));

  let timer: ReturnType<typeof setTimeout> | undefined;
  function onError() {
    const key = base;
    if (phase === "first") {
      load = { key, phase: "waiting" };
      clearTimeout(timer);
      timer = setTimeout(() => {
        if (base === key) load = { key, phase: "retrying" };
      }, RETRY_DELAY_MS);
    } else {
      load = { key, phase: "failed" };
    }
  }
  onDestroy(() => clearTimeout(timer));
</script>

<span class="relative flex shrink-0 items-center justify-center overflow-hidden bg-surface-3 {size} {rounded}">
  {#if !loaded}
    <span aria-hidden="true" class="text-2xs uppercase text-muted">{placeholderText(category)}</span>
  {/if}
  {#if wanted && (phase === "first" || phase === "retrying")}
    <img
      {src}
      alt=""
      loading="lazy"
      decoding="async"
      class="absolute inset-0 h-full w-full {fit === 'contain' ? 'object-contain' : 'object-cover'} {rounded}"
      onload={() => (loadedSrc = src)}
      onerror={onError}
    />
  {/if}
</span>
