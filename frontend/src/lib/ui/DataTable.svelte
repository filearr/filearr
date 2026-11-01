<!--
  Sortable, captioned table with a card-list fallback below `md`.

    <DataTable
      caption="Files" captionHidden
      columns={[{ key: "name", label: "Name", sortable: true }, { key: "size", label: "Size", align: "right", cell: size }]}
      rows={files} rowKey={(f) => f.id} bind:sort
    />
    {#snippet size(f: FileRow)}{formatBytes(f.size)}{/snippet}

  States, in precedence order: `error` (StateBlock with `onRetry`) > `loading`
  > empty (`emptyMessage`) > rows. Each state fills one full-width row in the
  table and one block in the card list. With `keepRowsWhileLoading` (default)
  a `loading` table that already has rows keeps them, dimmed and aria-busy, so
  a refresh does not blank it; the loading block shows only when there are no
  rows yet (or the prop is false).

  Below `md`, when any column is sortable, a "Sort by" select plus a direction
  button sit above the cards; both drive the same bindable `sort` as the
  header buttons.

  `Column.key` autocompletes T's keys but accepts any string (computed columns
  read through `accessor` or `cell`), so it does not catch typos by itself.

  Cell snippets render twice (the table and the card list both stay in the
  DOM), so a cell must not contain a static `id`.
-->
<script lang="ts" generics="T">
  import type { Snippet } from "svelte";
  import Button from "./Button.svelte";
  import Select from "./Select.svelte";
  import StateBlock from "./StateBlock.svelte";
  import { nextSort, sortRows, toggleDir, type SortState } from "./sort.ts";

  type Column = {
    // `string & {}` keeps literal autocomplete for T's keys while still
    // allowing computed column keys.
    key: (keyof T & string) | (string & {});
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
    loading = false,
    error = null,
    onRetry,
    keepRowsWhileLoading = true,
    class: className = "",
    onRowClick,
  }: {
    columns: Column[];
    rows: readonly T[];
    rowKey: (row: T) => string | number;
    caption: string;
    captionHidden?: boolean;
    sort?: SortState;
    accessor?: (row: T, key: string) => unknown;
    emptyMessage?: string;
    loading?: boolean;
    error?: string | null;
    onRetry?: () => void;
    keepRowsWhileLoading?: boolean;
    class?: string;
    onRowClick?: (row: T) => void;
  } = $props();

  // Computed once so the table and the card list always show the same order.
  const sorted = $derived(sortRows(rows, sort, accessor));

  // One decision for both views: error > loading > empty > rows. A loading
  // table with rows (and keepRowsWhileLoading) stays on "rows", dimmed.
  const view = $derived<"error" | "loading" | "empty" | "rows">(
    error
      ? "error"
      : loading && (!keepRowsWhileLoading || rows.length === 0)
        ? "loading"
        : rows.length === 0
          ? "empty"
          : "rows",
  );

  const sortable = $derived(columns.filter((c) => c.sortable));
  const sortId = $props.id();

  // Select gets a plain value (not bind:) so `sort` stays the only state; ""
  // is the "Default order" option.
  function onSortSelect(e: Event & { currentTarget: HTMLSelectElement }) {
    const key = e.currentTarget.value;
    sort = key === "" ? null : { key, dir: "asc" };
  }

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

{#snippet stateBlock()}
  {#if view === "error"}
    <StateBlock state="error" message={error ?? undefined} {onRetry} compact />
  {:else if view === "loading"}
    <StateBlock state="loading" compact />
  {:else}
    <StateBlock state="empty" message={emptyMessage} compact />
  {/if}
{/snippet}

<!-- aria-busy only while stale rows are shown: a busy subtree mutes live
     regions, which would silence the loading/error StateBlock. -->
<div class={className} aria-busy={loading && view === "rows"}>
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
      <tbody class:opacity-60={loading && view === "rows"}>
        {#if view !== "rows"}
          <tr class="border-t border-border">
            <td colspan={columns.length}>{@render stateBlock()}</td>
          </tr>
        {:else}
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
          {/each}
        {/if}
      </tbody>
    </table>
  </div>

  <!-- Below md the table would scroll sideways; each row becomes a card. The
       caption is hidden with the table, so it labels the list instead. -->
  <div class="md:hidden">
    <!-- The header buttons are hidden with the table, so sorting needs its own
         control here. -->
    {#if sortable.length > 0}
      <div class="mb-2 flex items-center gap-2">
        <label for={sortId} class="sr-only">Sort by</label>
        <Select id={sortId} size="sm" value={sort?.key ?? ""} onchange={onSortSelect}>
          <option value="">Default order</option>
          {#each sortable as col (col.key)}
            <option value={col.key}>{col.label}</option>
          {/each}
        </Select>
        <Button
          variant="ghost"
          size="sm"
          aria-label="Toggle sort direction"
          disabled={!sort}
          onclick={() => (sort = toggleDir(sort))}
        >
          <span aria-hidden="true">{sort?.dir === "desc" ? "▼" : "▲"}</span>
        </Button>
      </div>
    {/if}
    {#if view !== "rows"}
      {@render stateBlock()}
    {:else}
      <ul aria-label={caption} class="flex flex-col gap-2" class:opacity-60={loading}>
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
