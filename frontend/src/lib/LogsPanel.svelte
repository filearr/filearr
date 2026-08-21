<script lang="ts">
  // Console Logs panel: tails the unified app+worker log stream (app_logs).
  // Newest first with keyset "load older" paging; level/source/text filters;
  // optional auto-refresh (re-fetches page one — a tail, not a live stream).
  import { untrack } from "svelte";
  import { fetchLogs, friendlyError, type LogRow } from "./api";

  let rows = $state<LogRow[]>([]);
  let enabled = $state(true);
  let nextBeforeId = $state<number | null>(null);
  let loading = $state(false);
  let loaded = $state(false);
  let error = $state("");

  let minLevel = $state<"info" | "warning" | "error">("info");
  let source = $state<"" | "app" | "worker">("");
  let search = $state("");
  let auto = $state(false);
  let expanded = $state<Record<number, boolean>>({});

  let searchTimer: ReturnType<typeof setTimeout> | undefined;
  let autoTimer: ReturnType<typeof setInterval> | undefined;

  async function load(older = false) {
    loading = true;
    try {
      const res = await fetchLogs({
        min_level: minLevel,
        source: source || undefined,
        q: search || undefined,
        limit: 100,
        before_id: older ? (nextBeforeId ?? undefined) : undefined,
      });
      rows = older ? [...rows, ...res.logs] : res.logs;
      enabled = res.enabled;
      nextBeforeId = res.next_before_id;
      error = "";
      loaded = true;
    } catch (e) {
      error = friendlyError(e, "load logs");
    } finally {
      loading = false;
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

  $effect(() => {
    clearInterval(autoTimer);
    if (auto) autoTimer = setInterval(() => void load(), 10_000);
    return () => clearInterval(autoTimer);
  });

  function levelClass(level: string): string {
    if (level === "CRITICAL" || level === "ERROR")
      return "bg-red-100 text-red-700 dark:bg-red-950 dark:text-red-300";
    if (level === "WARNING")
      return "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300";
    return "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300";
  }

  function fmtTs(iso: string): string {
    const d = new Date(iso);
    const today = new Date().toDateString() === d.toDateString();
    return today
      ? d.toLocaleTimeString()
      : `${d.toLocaleDateString()} ${d.toLocaleTimeString()}`;
  }
</script>

<section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
  <div class="mb-3 flex flex-wrap items-center gap-2">
    <h3 class="mr-auto text-sm font-semibold text-slate-700 dark:text-slate-200" title="One stream from both containers: filearr activity (INFO) plus warnings and errors from everything else. Request access lines are never recorded.">
      Logs
    </h3>
    <select bind:value={minLevel} class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700" title="Minimum level">
      <option value="info">info+</option>
      <option value="warning">warning+</option>
      <option value="error">errors</option>
    </select>
    <select bind:value={source} class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700" title="Which process">
      <option value="">app + worker</option>
      <option value="app">app</option>
      <option value="worker">worker</option>
    </select>
    <input
      bind:value={search}
      oninput={onSearchInput}
      placeholder="filter messages…"
      class="w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
    />
    <label class="flex items-center gap-1 text-xs text-slate-500">
      <input type="checkbox" bind:checked={auto} /> auto-refresh
    </label>
    <button
      class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
      onclick={() => void load()}
      disabled={loading}
      title="Refresh">↻</button>
  </div>

  {#if error}
    <p class="text-sm text-red-600 dark:text-red-400">{error}</p>
  {:else if loaded && !enabled}
    <p class="text-sm text-slate-500">
      The log stream is disabled (<code class="text-xs">FILEARR_LOG_DB_ENABLED=false</code>) — nothing is being recorded.
    </p>
  {:else if loaded && rows.length === 0}
    <p class="text-sm text-slate-500">No log records match. The stream fills as the app and worker run{minLevel !== "info" ? " — try the info+ level" : ""}.</p>
  {:else}
    <div class="max-h-96 overflow-y-auto font-mono text-xs">
      <div class="overflow-x-auto"><table class="w-full border-collapse">
        <tbody>
          {#each rows as r (r.id)}
            <tr class="border-b border-slate-100 align-top dark:border-slate-800">
              <td class="whitespace-nowrap py-1 pr-2 tabular-nums text-slate-400" title={new Date(r.ts).toLocaleString()}>{fmtTs(r.ts)}</td>
              <td class="py-1 pr-2">
                <span class="rounded px-1 py-0.5 text-[10px] font-medium {levelClass(r.level)}">{r.level.toLowerCase()}</span>
              </td>
              <td class="whitespace-nowrap py-1 pr-2 text-slate-400">{r.source}</td>
              <td class="hidden whitespace-nowrap py-1 pr-2 text-slate-400 sm:table-cell" title={r.logger}>{r.logger.replace(/^filearr\./, "")}</td>
              <td class="w-full py-1 text-slate-700 dark:text-slate-300">
                <span class="break-all whitespace-pre-wrap">{r.message}</span>
                {#if r.exc}
                  <button
                    class="ml-1 text-sky-600 underline decoration-dotted dark:text-sky-400"
                    onclick={() => (expanded[r.id] = !expanded[r.id])}>
                    {expanded[r.id] ? "hide traceback" : "traceback"}</button>
                  {#if expanded[r.id]}
                    <pre class="mt-1 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-snug text-slate-600 dark:bg-slate-950 dark:text-slate-400">{r.exc}</pre>
                  {/if}
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table></div>
    </div>
    {#if nextBeforeId !== null}
      <button
        class="mt-2 rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
        onclick={() => void load(true)}
        disabled={loading}>
        {loading ? "…" : "Load older"}</button>
    {/if}
  {/if}
</section>
