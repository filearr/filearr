<!--
  Sortable, captioned table with a card-list fallback below `md`.

    <DataTable
      caption="Files" captionHidden
      columns={[{ key: "name", label: "Name", sortable: true }, { key: "size", label: "Size", align: "right", cell: size }]}
      rows={files} rowKey={(f) => f.id} bind:sort
    />
    {#snippet size(f: FileRow)}{formatBytes(f.size)}{/snippet}
-->
<script lang="ts" generics="T">
  import type { Snippet } from "svelte";
  import StateBlock from "./StateBlock.svelte";
  import { nextSort, sortRows, type SortState } from "./sort.ts";

  type Column = {
    key: string;
    label: string;
    align?: "left" | "right";
    sortable?: boolean;
    width?: string;
    cell?: Snippet<[T]>;
  };

  let {
    columns,
    rows,
    rowKey,
    caption,
    captionHidden = false,
    sort = $bindable<SortState>(null),
    accessor = (r: T, k: string) => (r as Record<string, unknown>)[k],
    emptyMessage,
    class: className = "",
    onRowClick,
  }: {
    columns: Column[];
    rows: readonly T[];
    rowKey: (row: T) => string;
    caption: string;
    captionHidden?: boolean;
    sort?: SortState;
    accessor?: (row: T, key: string) => unknown;
    emptyMessage?: string;
    class?: string;
    onRowClick?: (row: T) => void;
  } = $props();

  // Computed once so the table and the card list always show the same order.
  const sorted = $derived(sortRows(rows, sort, accessor));

  const alignClass = (col: Column) => (col.align === "right" ? "text-right" : "text-left");

  function ariaSort(col: Column): "ascending" | "descending" | "none" | undefined {
    if (!col.sortable) return undefined;
    if (sort?.key !== col.key) return "none";
    return sort.dir === "asc" ? "ascending" : "descending";
  }

  // Rows stay <tr> (a role="button" row would hide its cells from table
  // navigation), so clickability is a tab stop plus Enter/Space.
  function onKey(e: KeyboardEvent, row: T) {
    // Ignore keys bubbling from a control inside the row (a cell's own button).
    if (e.target !== e.currentTarget) return;
    if (e.key === "Enter" || e.key === " ") {
      e.preventDefault();
      onRowClick?.(row);
    }
  }
</script>

{#snippet value(col: Column, row: T)}
  {#if col.cell}{@render col.cell(row)}{:else}{String(accessor(row, col.key) ?? "")}{/if}
{/snippet}

{#snippet fields(row: T)}
  <dl class="flex flex-col gap-1.5">
    {#each columns as col (col.key)}
      <div>
        <dt class="text-2xs text-muted">{col.label}</dt>
        <dd>{@render value(col, row)}</dd>
      </div>
    {/each}
  </dl>
{/snippet}

<div class={className}>
  <div class="hidden overflow-x-auto rounded-control border border-border md:block">
    <table class="w-full text-sm">
      <caption class={captionHidden ? "sr-only" : "px-3 py-2 text-left font-medium"}>{caption}</caption>
      <thead class="bg-surface-2 text-2xs uppercase tracking-wide text-muted">
        <tr>
          {#each columns as col (col.key)}
            <th
              scope="col"
              aria-sort={ariaSort(col)}
              style={col.width ? `width: ${col.width}` : undefined}
              class="px-3 py-2 {alignClass(col)}"
            >
              {#if col.sortable}
                <button
                  type="button"
                  class="inline-flex items-center gap-1 font-medium hover:text-fg"
                  onclick={() => (sort = nextSort(sort, col.key))}
                >
                  {col.label}
                  {#if sort?.key === col.key}
                    <span aria-hidden="true">{sort.dir === "asc" ? "▲" : "▼"}</span>
                  {/if}
                </button>
              {:else}
                {col.label}
              {/if}
            </th>
          {/each}
        </tr>
      </thead>
      <tbody>
        {#each sorted as row (rowKey(row))}
          <tr
            class="border-t border-border {onRowClick ? 'cursor-pointer hover:bg-surface-3' : ''}"
            tabindex={onRowClick ? 0 : undefined}
            onclick={onRowClick ? () => onRowClick(row) : undefined}
            onkeydown={onRowClick ? (e) => onKey(e, row) : undefined}
          >
            {#each columns as col (col.key)}
              <td class="px-3 py-2 {alignClass(col)}">{@render value(col, row)}</td>
            {/each}
          </tr>
        {:else}
          <tr class="border-t border-border">
            <td colspan={columns.length}><StateBlock state="empty" message={emptyMessage} compact /></td>
          </tr>
        {/each}
      </tbody>
    </table>
  </div>

  <!-- Below md the table would scroll sideways; each row becomes a card. The
       caption is hidden with the table, so it labels the list instead. -->
  <div class="md:hidden">
    {#if sorted.length === 0}
      <StateBlock state="empty" message={emptyMessage} compact />
    {:else}
      <ul aria-label={caption} class="flex flex-col gap-2">
        {#each sorted as row (rowKey(row))}
          {@const card = "block rounded-control border border-border bg-surface-2 p-3 text-sm"}
          <li>
            {#if onRowClick}
              <!-- A card is not a table row, so it can take the button role a
                   clickable <tr> must not; a <button> element itself cannot hold a <dl>. -->
              <div
                role="button"
                tabindex="0"
                class="{card} cursor-pointer hover:bg-surface-3"
                onclick={() => onRowClick(row)}
                onkeydown={(e) => onKey(e, row)}
              >
                {@render fields(row)}
              </div>
            {:else}
              <div class={card}>{@render fields(row)}</div>
            {/if}
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>
