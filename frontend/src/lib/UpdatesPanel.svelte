<script lang="ts">
  // Jobs page Updates card: compares the running central build + agent bake
  // against the GitHub repository head and shows the recent commit messages
  // (the project's changelog) for review. The page load only reads central's
  // CACHE — GitHub is contacted when the operator clicks "Check now" (or,
  // with the FILEARR_UPDATE_CHECK_AUTO opt-in, on a stale cache).
  import {
    getUpdateCheck,
    runUpdateCheck,
    friendlyError,
    type UpdateCheck,
  } from "./api";

  let data = $state<UpdateCheck | null>(null);
  let checking = $state(false);
  let error = $state("");
  let expanded = $state<Record<string, boolean>>({});

  $effect(() => {
    void (async () => {
      try {
        data = await getUpdateCheck();
      } catch (e) {
        error = friendlyError(e, "load update state");
      }
    })();
  });

  async function checkNow() {
    checking = true;
    try {
      data = await runUpdateCheck();
      error = "";
    } catch (e) {
      error = friendlyError(e, "check for updates");
    } finally {
      checking = false;
    }
  }

  function badge(avail: boolean | null): { text: string; cls: string } {
    if (avail === true)
      return {
        text: "update available",
        cls: "bg-amber-100 text-amber-700 dark:bg-amber-950 dark:text-amber-300",
      };
    if (avail === false)
      return {
        text: "up to date",
        cls: "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300",
      };
    return {
      text: "unknown",
      cls: "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300",
    };
  }
</script>

<section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
  <div class="mb-3 flex items-center gap-2">
    <h3 class="mr-auto text-sm font-semibold text-slate-700 dark:text-slate-200" title="Compares this install (central build + baked agent binaries) against the source repository and lists its recent commit messages — the project's changelog. Checking contacts GitHub; nothing else, and nothing about your catalog is sent.">
      Updates
    </h3>
    {#if data?.checked_at}
      <span class="text-xs text-slate-400" title={new Date(data.checked_at).toLocaleString()}>
        checked {new Date(data.checked_at).toLocaleString()}
      </span>
    {/if}
    <button
      class="rounded-lg border border-slate-300 px-2 py-1 text-xs text-slate-600 disabled:opacity-50 dark:border-slate-700 dark:text-slate-300"
      onclick={checkNow}
      disabled={checking}
      title="Contact GitHub now and compare (admin).">
      {checking ? "checking…" : "Check now"}</button>
  </div>

  {#if error}
    <p class="text-sm text-red-600 dark:text-red-400">{error}</p>
  {:else if data?.error}
    <p class="text-sm text-slate-500">{data.error}</p>
  {:else if !data?.checked_at}
    <p class="text-sm text-slate-500">
      No check has run yet — <b>Check now</b> asks GitHub what's newer than this
      install and pulls the recent changelog. Nothing runs automatically.
    </p>
  {:else}
    <div class="space-y-1">
      {#each data.components as comp (comp.component)}
        {@const b = badge(comp.update_available)}
        <div class="flex flex-wrap items-baseline gap-2 text-xs">
          <span class="w-28 font-medium text-slate-700 dark:text-slate-200">{comp.component}</span>
          <span class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {b.cls}">{b.text}</span>
          <span class="font-mono text-slate-500" title={comp.detail}>{comp.running}</span>
          <span class="text-slate-400">→ {comp.latest}</span>
        </div>
      {/each}
    </div>

    {#if data.changelog.length > 0}
      <h4 class="mt-3 mb-1 text-xs font-semibold text-slate-500">
        Recent changes
        <span class="font-normal text-slate-400">(newest first{data.changelog.some((c) => c.is_new) ? "; • = newer than this build" : ""})</span>
      </h4>
      <div class="max-h-72 overflow-y-auto">
        <ul class="space-y-1 text-xs">
          {#each data.changelog as c (c.sha)}
            <li>
              <div class="flex items-baseline gap-2">
                {#if c.is_new}<span class="text-amber-500" title="Newer than the running build">•</span>{/if}
                <code class="text-slate-400">{c.sha}</code>
                {#if c.date}<span class="whitespace-nowrap tabular-nums text-slate-400">{new Date(c.date).toLocaleDateString()}</span>{/if}
                <button
                  class="text-left text-slate-700 hover:underline dark:text-slate-300 {c.is_new ? 'font-medium' : ''}"
                  onclick={() => (expanded[c.sha] = !expanded[c.sha])}
                  title={c.body ? "Show the full commit message" : ""}>
                  {c.subject}</button>
              </div>
              {#if expanded[c.sha] && c.body}
                <pre class="mt-1 ml-6 max-h-48 overflow-auto rounded bg-slate-50 p-2 text-[11px] leading-snug whitespace-pre-wrap text-slate-600 dark:bg-slate-950 dark:text-slate-400">{c.body}</pre>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</section>
