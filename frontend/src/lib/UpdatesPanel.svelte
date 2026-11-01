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
  import { Alert, Badge, Button, StateBlock, type Tone } from "./ui";

  let data = $state<UpdateCheck | null>(null);
  let checking = $state(false);
  // Errors by origin: reading the cached state (replaces the card body) and
  // the Check now action (an Alert under its button).
  let loadError = $state("");
  let checkError = $state("");
  let loaded = $state(false);
  let expanded = $state<Record<string, boolean>>({});

  async function load() {
    loadError = "";
    try {
      data = await getUpdateCheck();
      loaded = true;
    } catch (e) {
      loadError = friendlyError(e, "load update state");
    }
  }

  $effect(() => {
    void load();
  });

  async function checkNow() {
    checking = true;
    checkError = "";
    try {
      data = await runUpdateCheck();
      loaded = true;
      loadError = "";
    } catch (e) {
      checkError = friendlyError(e, "check for updates");
    } finally {
      checking = false;
    }
  }

  function badge(avail: boolean | null): { text: string; tone: Tone } {
    if (avail === true) return { text: "update available", tone: "warn" };
    if (avail === false) return { text: "up to date", tone: "ok" };
    return { text: "unknown", tone: "neutral" };
  }
</script>

<section class="rounded-2xl border border-border bg-surface p-4">
  <div class="mb-3 flex items-center gap-2">
    <h3 class="mr-auto text-sm font-semibold" title="Compares this install (central build + baked agent binaries) against the source repository and lists its recent commit messages — the project's changelog. Checking contacts GitHub; nothing else, and nothing about your catalog is sent.">
      Updates
    </h3>
    {#if data?.checked_at}
      <span class="text-xs text-muted" title={new Date(data.checked_at).toLocaleString()}>
        checked {new Date(data.checked_at).toLocaleString()}
      </span>
    {/if}
    <Button
      size="sm"
      onclick={checkNow}
      disabled={checking}
      title="Contact GitHub now and compare (admin).">
      {checking ? "checking…" : "Check now"}</Button>
  </div>
  {#if checkError}
    <Alert tone="danger" class="mb-3" onDismiss={() => (checkError = "")}>{checkError}</Alert>
  {/if}

  {#if loadError && !loaded}
    <StateBlock state="error" compact message={loadError} onRetry={load} />
  {:else if !loaded}
    <StateBlock state="loading" compact message="Loading update state…" />
  {:else if data?.error}
    <!-- A standing state of the last check, not news: no live role. -->
    <Alert tone="warn" live={false}>{data.error}</Alert>
  {:else if !data?.checked_at}
    <StateBlock
      state="empty"
      compact
      message="No check has run yet — Check now asks GitHub what's newer than this install and pulls the recent changelog. Nothing runs automatically." />
  {:else}
    <div class="space-y-1">
      {#each data.components as comp (comp.component)}
        {@const b = badge(comp.update_available)}
        <div class="flex flex-wrap items-baseline gap-2 text-xs">
          <span class="w-28 font-medium">{comp.component}</span>
          <Badge tone={b.tone}>{b.text}</Badge>
          <span class="font-mono text-muted" title={comp.detail}>{comp.running}</span>
          <span class="text-muted">→ {comp.latest}</span>
        </div>
      {/each}
    </div>

    {#if data.changelog.length > 0}
      <h4 class="mt-3 mb-1 text-xs font-semibold text-muted">
        Recent changes
        <span class="font-normal">(newest first{data.changelog.some((c) => c.is_new) ? "; • = newer than this build" : ""})</span>
      </h4>
      <div class="max-h-72 overflow-y-auto">
        <ul class="space-y-1 text-xs">
          {#each data.changelog as c (c.sha)}
            <li>
              <div class="flex items-baseline gap-2">
                {#if c.is_new}<span class="text-warn" title="Newer than the running build">•</span>{/if}
                <code class="text-muted">{c.sha}</code>
                {#if c.date}<span class="whitespace-nowrap tabular-nums text-muted">{new Date(c.date).toLocaleDateString()}</span>{/if}
                <!-- Bespoke disclosure (commit subject → full message); stays a raw button. -->
                <button
                  type="button"
                  class="text-left hover:underline {c.is_new ? 'font-medium' : ''}"
                  aria-expanded={c.body ? !!expanded[c.sha] : undefined}
                  onclick={() => (expanded[c.sha] = !expanded[c.sha])}
                  title={c.body ? "Show the full commit message" : ""}>
                  {c.subject}</button>
              </div>
              {#if expanded[c.sha] && c.body}
                <pre class="mt-1 ml-6 max-h-48 overflow-auto rounded-control bg-surface-3 p-2 text-[11px] leading-snug whitespace-pre-wrap text-muted">{c.body}</pre>
              {/if}
            </li>
          {/each}
        </ul>
      </div>
    {/if}
  {/if}
</section>
