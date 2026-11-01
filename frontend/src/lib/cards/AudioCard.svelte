<script lang="ts">
  // The whole `audio` category; audiobooks (a file group) add chapters.
  import Highlights from "./Highlights.svelte";
  import TrackTable from "./TrackTable.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { HIGHLIGHT_KEYS } from "./highlights";
  import { asStr, asRows, fmtAudioBitrate, fmtDuration } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  // A14: every key here is rendered below AND excluded from "More details".
  const KEYS = HIGHLIGHT_KEYS["audio"];

  const highlights = $derived([
    { label: "Artist", value: asStr(m.artist) },
    { label: "Album", value: asStr(m.album) },
    { label: "Genre", value: asStr(m.genre) },
    { label: "Year", value: asStr(m.year) },
    { label: "Duration", value: fmtDuration(m.duration) },
    // A32: tinytag reports kbit/s, the mutagen fallback and ffprobe bit/s;
    // fmtAudioBitrate normalises both to kbps.
    { label: "Bitrate", value: fmtAudioBitrate(m.bitrate) },
    { label: "Sample rate", value: m.samplerate ? `${asStr(m.samplerate)} Hz` : null },
    { label: "Channels", value: asStr(m.channels) },
  ]);
  const rows = $derived([
    { label: "Title", value: asStr(m.title) },
    { label: "Chapters", value: asStr(m.chapter_count) },
  ]);
  const chapters = $derived(asRows(m.chapters));
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  {#if chapters}<TrackTable title="Chapters" rows={chapters} />{/if}
  <KeyFactsCard {item} exclude={KEYS} lead={rows} heading="More details" />
</div>
