<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import {
    timeline,
    listLibraries,
    type TimelineResponse,
    type TimelineBucket,
    type Library,
    errorMessage,
  } from "./api";
  import { encodeSearchHash } from "./searchparams";
  import { Alert, Select, StateBlock } from "./ui";

  // ------------------------------------------------------------------ //
  // P3-T14 — timeline browsing. A date histogram over item mtime (month //
  // or year), rendered as clickable bars (no chart dependency). Clicking //
  // a bar applies an mtime range filter in Search; the "invalid dates"  //
  // bar (future-suspect mtimes, FIX-3) filters mtime beyond +48h.       //
  // ------------------------------------------------------------------ //

  let bucket = $state<"month" | "year">("month");
  let library = $state("");
  let libs = $state<Library[]>([]);
  let data = $state<TimelineResponse | null>(null);
  let error = $state("");
  let loading = $state(false);

  const maxCount = $derived(
    data ? Math.max(1, ...data.buckets.map((b) => b.count), data.invalid_count) : 1,
  );

  // Last-request-wins: flipping bucket/library aborts the older histogram.
  const timelineLoader = latest((signal, b: "month" | "year", lib: string) =>
    timeline(b, lib, signal),
  );
  onDestroy(() => timelineLoader.cancel());

  async function load() {
    error = "";
    loading = true;
    try {
      const r = await timelineLoader.run(bucket, library);
      if (r === undefined) return;
      data = r;
      loading = false;
    } catch (e) {
      error = errorMessage(e);
      loading = false;
    }
  }

  function label(b: TimelineBucket): string {
    const d = new Date(b.start);
    return bucket === "year"
      ? String(d.getUTCFullYear())
      : d.toLocaleString(undefined, { timeZone: "UTC", year: "numeric", month: "short" });
  }

  // Clicking a bucket bar navigates to Search with the bucket's [start, end)
  // mtime window (end is exclusive, so lte = end_epoch - 1 inclusive).
  function openBucket(b: TimelineBucket) {
    const params: Record<string, string> = {
      mtime_gte: String(b.start_epoch),
      mtime_lte: String(b.end_epoch - 1),
    };
    if (library) params.library = library;
    location.hash = encodeSearchHash(params);
  }

  // The "invalid dates" bar: items whose mtime is more than 48h in the future.
  function openInvalid() {
    if (!data) return;
    const params: Record<string, string> = { mtime_gte: String(data.invalid_mtime_gte) };
    if (library) params.library = library;
    location.hash = encodeSearchHash(params);
  }

  function barHeight(count: number): number {
    // Linear scale to a 160px track, with a small floor so a 1-count bar is
    // still visible/clickable.
    return Math.max(4, Math.round((count / maxCount) * 160));
  }

  onMount(async () => {
    try {
      libs = await listLibraries();
    } catch {
      // Non-fatal: the library filter just stays empty (all libraries).
    }
    load();
  });
</script>

<div>
  <div class="flex flex-wrap items-center gap-3">
    <h2 class="text-lg font-semibold">Timeline</h2>
    <span class="grow"></span>
    <!-- Kit controls fill their parent, so the select sits in a sizing wrapper. -->
    <div class="flex items-center gap-1 text-sm text-muted">
      <span aria-hidden="true">Library</span>
      <div>
        <Select size="sm" bind:value={library} onchange={load} aria-label="Filter by library">
          <option value="">All libraries</option>
          {#each libs as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
        </Select>
      </div>
    </div>
    <!-- Segment toggle (bespoke chrome, not an action button). -->
    <div class="flex gap-1" role="group" aria-label="Bucket size">
      {#each ["month", "year"] as b (b)}
        <button
          type="button"
          class="rounded-control px-3 py-1 text-sm {bucket === b
            ? 'bg-accent text-accent-fg'
            : 'border border-border-strong text-muted hover:text-fg'}"
          aria-pressed={bucket === b}
          onclick={() => { bucket = b as "month" | "year"; load(); }}>{b}</button>
      {/each}
    </div>
  </div>

  {#if error && data}
    <!-- A failed refresh keeps the last histogram on screen. -->
    <Alert tone="danger" class="mt-4" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  {#if error && !data}
    <StateBlock class="mt-6" state="error" message={error} onRetry={load} />
  {:else if loading && !data}
    <StateBlock class="mt-6" state="loading" />
  {:else if data && !data.buckets.length && !data.invalid_count}
    <StateBlock
      class="mt-6"
      state="empty"
      message="No dated items to chart yet. Scan a library (or pick another one) to fill the timeline." />
  {:else if data}
    <p class="mt-4 text-sm text-muted">
      Click a bar to filter Search by that period.
    </p>
    <!-- Bars: a horizontally-scrolling row of clickable columns. Each bar's
         height is proportional to its item count; labels sit beneath. A
         refresh keeps the old bars on screen, marked busy. -->
    <div class="mt-3 overflow-x-auto rounded-control border border-border p-4" aria-busy={loading}>
      <div class="flex items-end gap-1" style="min-height: 200px;">
        {#each data.buckets as b (b.start_epoch)}
          <button
            type="button"
            class="group flex shrink-0 flex-col items-center justify-end"
            style="width: 32px;"
            title={`${label(b)}: ${b.count} item${b.count === 1 ? "" : "s"}`}
            onclick={() => openBucket(b)}>
            <span class="mb-1 text-[10px] tabular-nums text-muted group-hover:text-accent-text"
              >{b.count}</span>
            <div
              class="w-5 rounded-t bg-accent/60 group-hover:bg-accent"
              style={`height: ${barHeight(b.count)}px;`}></div>
            <span class="mt-1 w-8 -rotate-45 origin-top-left text-[10px] whitespace-nowrap text-muted"
              >{label(b)}</span>
          </button>
        {/each}
        {#if data.invalid_count}
          <!-- Future-suspect (invalid) mtimes, surfaced as a separate danger bar. -->
          <button
            type="button"
            class="group ml-4 flex shrink-0 flex-col items-center justify-end"
            style="width: 40px;"
            title={`${data.invalid_count} item(s) with a suspect future date`}
            onclick={openInvalid}>
            <span class="mb-1 text-[10px] tabular-nums text-danger">{data.invalid_count}</span>
            <div
              class="w-6 rounded-t bg-danger/50 group-hover:bg-danger"
              style={`height: ${barHeight(data.invalid_count)}px;`}></div>
            <span class="mt-1 text-[10px] text-danger">invalid</span>
          </button>
        {/if}
      </div>
    </div>
  {/if}
</div>
