<script lang="ts">
  // Console Logs panel: tails the unified app+worker log stream (app_logs).
  // Newest first with keyset "load older" paging; level/source/text filters;
  // optional auto-refresh (re-fetches page one — a tail, not a live stream).
  import { onDestroy, untrack } from "svelte";
  import { fetchLogs, friendlyError, type LogRow } from "./api";
  import { usePolling } from "./polling";
  import { latest } from "./latest";
  import { Alert, Badge, Button, Checkbox, DataTable, Input, ScrollRegion, Select, StateBlock, type Tone } from "./ui";

  let rows = $state<LogRow[]>([]);
  let enabled = $state(true);
  let nextBeforeId = $state<number | null>(null);
  let loading = $state(false);
  // Only "Load older" dims the table (aria-busy): the 10 s auto-refresh and
  // filter reloads swap rows in place without flashing it every tick. A newer
  // request supersedes an older one, so each load() sets this afresh.
  let loadingOlder = $state(false);
  let loaded = $state(false);
  let error = $state("");

  let minLevel = $state<"info" | "warning" | "error">("info");
  let source = $state<"" | "app" | "worker">("");
  let search = $state("");
  let auto = $state(false);
  let expanded = $state<Record<number, boolean>>({});

  let searchTimer: ReturnType<typeof setTimeout> | undefined;

  // Last-request-wins: a filter flip, a debounced keystroke and the auto
  // poll all supersede the older fetch, so stale rows never land (and a
  // superseded "load older" never appends to a newer page one).
  const logsLoader = latest((signal, opts: Parameters<typeof fetchLogs>[0]) =>
    fetchLogs(opts, signal),
  );
  onDestroy(() => {
    logsLoader.cancel();
    clearTimeout(searchTimer);
  });

  async function load(older = false) {
    loading = true;
    loadingOlder = older;
    try {
      const res = await logsLoader.run({
        min_level: minLevel,
        source: source || undefined,
        q: search || undefined,
        limit: 100,
        before_id: older ? (nextBeforeId ?? undefined) : undefined,
      });
      if (res === undefined) return;
      rows = older ? [...rows, ...res.logs] : res.logs;
      enabled = res.enabled;
      nextBeforeId = res.next_before_id;
      error = "";
      loaded = true;
      loading = false;
      loadingOlder = false;
    } catch (e) {
      error = friendlyError(e, "load logs");
      loading = false;
      loadingOlder = false;
    }
  }

  $effect(() => {
    // Re-fetch page one whenever a filter changes. Deps are ONLY the two
    // selects: untrack keeps load()'s own state reads (search, cursor) from
    // re-triggering this effect — search is debounced separately below.
    void minLevel;
    void source;
    untrack(() => void load());
  });

  function onSearchInput() {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void load(), 350);
  }

  // The effect owns the poller: toggling `auto` (or unmounting) runs the
  // cleanup, so no separate onDestroy is needed here.
  $effect(() => {
    if (!auto) return;
    // Returning the promise lets the poller skip ticks while a fetch is slow.
    const poller = usePolling(() => load(), 10_000);
    return () => poller.stop();
  });

  // Badge tone per record level (CRITICAL/ERROR/WARNING map through the kit's
  // status vocabulary; INFO and DEBUG stay neutral).
  function levelTone(level: string): Tone {
    if (level === "CRITICAL" || level === "ERROR") return "danger";
    if (level === "WARNING") return "warn";
    return "neutral";
  }

  function fmtTs(iso: string): string {
    const d = new Date(iso);
    const today = new Date().toDateString() === d.toDateString();
    return today
      ? d.toLocaleTimeString()
      : `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
  }
</script>

{#snippet tsCell(r: LogRow)}
  <span class="whitespace-nowrap tabular-nums text-muted" title={new Date(r.ts).toLocaleString()}>{fmtTs(r.ts)}</span>
{/snippet}
{#snippet levelCell(r: LogRow)}<Badge tone={levelTone(r.level)}>{r.level.toLowerCase()}</Badge>{/snippet}
{#snippet sourceCell(r: LogRow)}<span class="whitespace-nowrap text-muted">{r.source}</span>{/snippet}
{#snippet loggerCell(r: LogRow)}
  <span class="whitespace-nowrap text-muted" title={r.logger}>{r.logger.replace(/^filearr\./, "")}</span>
{/snippet}
{#snippet messageCell(r: LogRow)}
  <span class="break-all whitespace-pre-wrap">{r.message}</span>
  {#if r.exc}
    <button
      type="button"
      class="ml-1 text-accent-text underline decoration-dotted"
      aria-expanded={!!expanded[r.id]}
      onclick={() => (expanded[r.id] = !expanded[r.id])}>
      {expanded[r.id] ? "hide traceback" : "traceback"}</button>
    {#if expanded[r.id]}
      <pre class="mt-1 max-h-48 overflow-auto rounded-control bg-surface-3 p-2 text-[11px] leading-snug text-muted">{r.exc}</pre>
    {/if}
  {/if}
{/snippet}

<section class="rounded-2xl border border-border bg-surface p-4">
  <div class="mb-3 flex flex-wrap items-center gap-2">
    <h3 class="mr-auto text-sm font-semibold" title="One stream from both containers: filearr activity (INFO) plus warnings and errors from everything else. Request access lines are never recorded.">
      Logs
    </h3>
    <!-- Kit controls fill their parent, so each sits in a sizing wrapper. -->
    <div>
      <Select size="sm" bind:value={minLevel} aria-label="Minimum level" title="Minimum level">
        <option value="info">info+</option>
        <option value="warning">warning+</option>
        <option value="error">errors</option>
      </Select>
    </div>
    <div>
      <Select size="sm" bind:value={source} aria-label="Which process" title="Which process">
        <option value="">app + worker</option>
        <option value="app">app</option>
        <option value="worker">worker</option>
      </Select>
    </div>
    <div class="w-40">
      <Input
        size="sm"
        type="search"
        bind:value={search}
        oninput={onSearchInput}
        aria-label="Filter messages"
        placeholder="filter messages…"
      />
    </div>
    <Checkbox label="auto-refresh" bind:checked={auto} />
    <Button
      size="sm"
      variant="ghost"
      onclick={() => void load()}
      disabled={loading}
      aria-label="Refresh"
      title="Refresh"><span aria-hidden="true">↻</span></Button>
  </div>

  {#if error && !loaded}
    <StateBlock state="error" message={error} onRetry={() => void load()} />
  {:else if !loaded}
    <StateBlock state="loading" message="Loading logs…" />
  {:else}
    {#if error}
      <!-- A failed refresh keeps the records already shown. -->
      <Alert tone="danger" class="mb-2" onDismiss={() => (error = "")}>{error}</Alert>
    {/if}
    {#if !enabled}
      <p class="text-sm text-muted">
        The log stream is disabled (<code class="text-xs">FILEARR_LOG_DB_ENABLED=false</code>) — nothing is being recorded.
      </p>
    {:else if rows.length === 0}
      <StateBlock
        state="empty"
        message="No log records match. The stream fills as the app and worker run{minLevel !== 'info' ? ' — try the info+ level' : ''}." />
    {:else}
      <!-- Scrollable region: focusable so keyboard users can scroll it. -->
      <ScrollRegion label="Log records" class="max-h-96 font-mono text-xs">
        <DataTable
          caption="Log records, newest first"
          captionHidden
          columns={[
            { key: "ts", label: "Time", cell: tsCell },
            { key: "level", label: "Level", cell: levelCell },
            { key: "source", label: "Source", cell: sourceCell },
            { key: "logger", label: "Logger", cell: loggerCell },
            { key: "message", label: "Message", width: "100%", cell: messageCell },
          ]}
          {rows}
          rowKey={(r) => r.id}
          loading={loadingOlder}
        />
      </ScrollRegion>
      {#if nextBeforeId !== null}
        <Button size="sm" class="mt-2" onclick={() => void load(true)} disabled={loading}>
          {loading ? "…" : "Load older"}</Button>
      {/if}
    {/if}
  {/if}
</section>
