<script lang="ts">
  import Highlights from "./Highlights.svelte";
  import TrackTable from "./TrackTable.svelte";
  import KeyFactsCard from "./KeyFactsCard.svelte";
  import { effectiveMeta } from "./keyfacts";
  import { HIGHLIGHT_KEYS } from "./highlights";
  import { asStr, asRows, fmtDuration, fmtVideoBitrate } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();
  const m = $derived(effectiveMeta(item));

  // A14: every key here is rendered below AND excluded from "More details".
  const KEYS = HIGHLIGHT_KEYS["video"];

  const highlights = $derived([
    { label: "Resolution", value: asStr(m.resolution) ?? (m.width && m.height ? `${m.width}×${m.height}` : null) },
    { label: "Video codec", value: asStr(m.video_codec) },
    { label: "Audio codec", value: asStr(m.audio_codec) },
    { label: "Duration", value: fmtDuration(m.duration) },
    { label: "Frame rate", value: m.frame_rate ? `${asStr(m.frame_rate)} fps` : null },
    { label: "HDR", value: m.hdr ? (asStr(m.hdr_format) ?? "yes") : null },
    { label: "Container", value: asStr(m.container) },
  ]);
  const rows = $derived([
    { label: "Title", value: asStr(m.title) },
    { label: "Year", value: asStr(m.year) },
    { label: "Season", value: asStr(m.season) },
    { label: "Episode", value: asStr(m.episode) },
    { label: "Bitrate", value: fmtVideoBitrate(m.bitrate) },
    { label: "Colour primaries", value: asStr(m.color_primaries) },
    { label: "Colour transfer", value: asStr(m.color_transfer) },
  ]);
  const audioTracks = $derived(asRows(m.audio_tracks));
  const subtitleTracks = $derived(asRows(m.subtitle_tracks));
</script>

<div class="space-y-4">
  <Highlights facts={highlights} />
  {#if audioTracks}<TrackTable title="Audio tracks" rows={audioTracks} />{/if}
  {#if subtitleTracks}<TrackTable title="Subtitle tracks" rows={subtitleTracks} />{/if}
  <KeyFactsCard {item} exclude={KEYS} lead={rows} heading="More details" />
</div>
