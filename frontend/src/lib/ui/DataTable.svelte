<!--
  Sortable, captioned table with a card-list fallback below `md`.

    <DataTable
      caption="Files" captionHidden
      columns={[{ key: "name", label: "Name", sortable: true }, { key: "size", label: "Size", align: "right", cell: size }]}
      rows={files} rowKey={(f) => f.id} bind:sort
    />
    {#snippet size(f: FileRow)}{formatBytes(f.size)}{/snippet}

  Cell snippets render twice (the table and the card list both stay in the
  DOM), so a cell must not contain a static `id`.
-->
<script lang="ts" generics="T">
  import type { Snippet } from "svelte";
  import Button from "./Button.svelte";
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

  const controls = "a,button,input,select,textarea,label,summary,[role=button]";

  // One guard for click and keyboard: a click or key that starts on a control
  // inside a cell (a link, a cell's own button) belongs to that control, not
  // to the row. Returns whether the row was activated.
  function activate(e: Event, row: T): boolean {
    const hit = (e.target as Element).closest(controls);
    if (hit !== null && hit !== e.currentTarget) return false;
    onRowClick?.(row);
    return true;
  }

  function onRowKey(e: KeyboardEvent, row: T) {
    // preventDefault only once the row took the key, so Space still types in a
    // cell's input and does not scroll the page from the row.
    if ((e.key === "Enter" || e.key === " ") && activate(e, row)) e.preventDefault();
  }

  // Distinguishes each card's Open button for screen readers by the first column.
  const openLabel = (row: T) =>
    `Open ${columns.length ? String(accessor(row, columns[0].key) ?? "") : ""}`.trim();
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
                <!-- Buttons don't inherit text-transform, so the thead's case
                     and tracking are repeated here. -->
                <button
                  type="button"
                  class="inline-flex items-center gap-1 font-medium uppercase tracking-wide hover:text-fg"
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
          <!-- Rows stay <tr> (role="button" would hide the cells from table
               navigation). tabindex and handlers are conditional so a table
               without onRowClick has no stray tab stops, and so svelte-check
               accepts the interactive <tr>. -->
          <tr
            class="border-t border-border {onRowClick ? 'cursor-pointer hover:bg-surface-3' : ''}"
            tabindex={onRowClick ? 0 : undefined}
            onclick={onRowClick ? (e) => activate(e, row) : undefined}
            onkeydown={onRowClick ? (e) => onRowKey(e, row) : undefined}
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
          <!-- The card stays a plain container so its <dl> and any controls in
               the cells keep their own semantics; the row action gets its own button. -->
          <li>
            <div class="rounded-control border border-border bg-surface-2 p-3 text-sm">
              {@render fields(row)}
              {#if onRowClick}
                <div class="mt-2 flex justify-end">
                  <Button variant="secondary" size="sm" aria-label={openLabel(row)} onclick={() => onRowClick(row)}>
                    Open
                  </Button>
                </div>
              {/if}
            </div>
          </li>
        {/each}
      </ul>
    {/if}
  </div>
</div>
