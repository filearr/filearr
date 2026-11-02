<script lang="ts">
  // P3-T13 — archive contents section (shown in ItemDetail whenever the item's
  // extracted metadata carries an ``archive`` fact). Read-only: the member list
  // is index-only (names + declared sizes), never an unpack. Member NAMES are
  // untrusted strings rendered as plain text (Svelte auto-escapes; never {@html}).
  import { effectiveMeta } from "./cards/keyfacts";
  import { fmtBytes } from "./format";

  let { item }: { item: Record<string, unknown> } = $props();

  type Member = { name?: unknown; size?: unknown };
  type Archive = {
    member_count?: unknown;
    total_uncompressed?: unknown;
    members?: unknown;
    truncated?: unknown;
    format?: unknown;
  };

  const meta = $derived(effectiveMeta(item));
  const archive = $derived(
    (meta.archive && typeof meta.archive === "object" ? meta.archive : null) as Archive | null,
  );
  const memberCount = $derived(
    typeof archive?.member_count === "number" ? archive.member_count : null,
  );
  // Sizes come from untrusted metadata; an absent/zero size is simply omitted
  // (an unknown member size is stored as 0, which is not "0 B").
  const size = (v: unknown): string => (typeof v === "number" && v > 0 ? fmtBytes(v) : "");
  const totalBytes = $derived(size(archive?.total_uncompressed));
  const format = $derived(typeof archive?.format === "string" ? archive.format : "");
  const truncated = $derived(archive?.truncated === true);
  const members = $derived(
    Array.isArray(archive?.members) ? (archive!.members as Member[]) : [],
  );

  // B50: the stored list is capped server-side (`max_stored`), so it can be
  // shorter than member_count. Say so, and base "Show N more" on what is stored.
  const listCapped = $derived(memberCount != null && members.length < memberCount);

  // Collapsed by default; a large archive shows the first N with an expander.
  const PREVIEW = 20;
  let expanded = $state(false);
  const shown = $derived(expanded ? members : members.slice(0, PREVIEW));
  const hiddenCount = $derived(Math.max(0, members.length - PREVIEW));
</script>

{#if memberCount != null}
  <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
    <h3 class="mb-2 text-sm font-semibold">
      Archive contents
      <span class="ml-1 font-normal text-slate-500">
        {memberCount}
        {memberCount === 1 ? "file" : "files"}{#if format} · {format}{/if}{#if totalBytes}
          · {totalBytes} uncompressed{/if}
      </span>
    </h3>

    {#if listCapped && memberCount != null}
      <p class="mb-2 text-xs text-amber-600 dark:text-amber-400">
        Showing {members.length.toLocaleString()} of {memberCount.toLocaleString()} members (list capped).
      </p>
    {:else if truncated}
      <p class="mb-2 text-xs text-amber-600 dark:text-amber-400">
        Listing stopped at a size/count guard — the archive may hold more members than counted.
      </p>
    {/if}

    {#if members.length}
      <ul class="flex flex-col gap-0.5 font-mono text-xs">
        {#each shown as m, i (i)}
          <li class="flex items-center gap-2">
            <span class="min-w-0 flex-1 truncate" title={String(m.name ?? "")}
              >{String(m.name ?? "")}</span>
            {#if size(m.size)}
              <span class="shrink-0 text-slate-500">{size(m.size)}</span>
            {/if}
          </li>
        {/each}
      </ul>
      {#if members.length > PREVIEW}
        <button
          type="button"
          class="mt-2 text-xs text-accent-text"
          onclick={() => (expanded = !expanded)}
        >
          {expanded ? "Show fewer" : `Show ${hiddenCount.toLocaleString()} more`}
        </button>
      {/if}
    {/if}
  </div>
{/if}
