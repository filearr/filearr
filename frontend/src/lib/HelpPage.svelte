<script lang="ts">
  // FIX-8 (UI docs links): the standalone Help page (#/help). Top-down:
  // resource links (bundled manual, API reference, source), keyboard
  // shortcuts, the shared query-syntax reference (dslHelp.ts — same data the
  // query-box "Query syntax" popover uses, so the two never drift), then the
  // field-help copy the inline "?" popovers use (lib/help.ts), grouped by
  // topic with a client-side filter. Any HELP key not assigned to a topic is
  // collected under "Other" so a new HELP entry is never silently dropped.
  import { DSL_SECTIONS } from "./dslHelp";
  import { HELP, HELP_TOPICS, type HelpTopic } from "./help";

  let { sourceUrl = "https://github.com/filearr/filearr" }: { sourceUrl?: string } =
    $props();

  // The bundled mkdocs manual is served by the backend at /docs/; the Vite dev
  // server has no such mount, so dev falls back to the public docs site.
  const DOCS_URL = import.meta.env.DEV
    ? "https://filearr.com/"
    : "/docs/";

  // $derived so the Source entry tracks the runtime /version source_url
  // override (the prop can update after mount).
  const RESOURCES: { href: string; title: string; desc: string }[] = $derived([
    {
      href: DOCS_URL,
      title: "Documentation",
      desc: "The full manual — setup, deployment, agents, security, operations & recovery. Served by this instance, works offline.",
    },
    {
      href: "/api/docs",
      title: "API reference",
      desc: "Interactive Swagger UI for the REST API — try requests against this instance directly.",
    },
    {
      href: sourceUrl,
      title: "Source code",
      desc: "This instance's Corresponding Source (AGPL-3.0-or-later, §13).",
    },
  ]);

  // Advertised search shortcuts (handlers live in SearchPage.svelte + the
  // detail/dialog components).
  const SHORTCUTS: { keys: string[]; action: string }[] = [
    { keys: ["/", "Ctrl/⌘ K"], action: "Focus the search box (from anywhere on the Search tab)" },
    { keys: ["↑", "↓"], action: "Move the result selection" },
    { keys: ["Enter"], action: "Open the selected item's details" },
    { keys: ["Ctrl/⌘ Enter"], action: "Copy the selected item's path" },
    { keys: ["Esc"], action: "Close the detail panel or dialog" },
    // R8-UI: the map is keyboard-operable end to end — panning/zooming here, and
    // the numeric North/South/West/East fields for the area selection itself.
    {
      keys: ["← ↑ → ↓", "+", "−"],
      action:
        "Map view: pan and zoom. Drag selects an area that filters the search (Shift+drag pans); the coordinate fields below the map do the same without a mouse",
    },
  ];

  let query = $state("");

  // Keys already placed in a topic; the remainder land under a synthetic "Other".
  const grouped = $derived.by(() => {
    const placed = new Set<string>();
    for (const t of HELP_TOPICS) for (const [k] of t.items) placed.add(k);
    const other: [string, string][] = Object.keys(HELP)
      .filter((k) => !placed.has(k))
      .map((k) => [k, k] as [string, string]);
    const topics: HelpTopic[] = [...HELP_TOPICS];
    if (other.length) topics.push({ title: "Other", items: other });
    return topics;
  });

  function matches(label: string, key: string, text: string): boolean {
    const q = query.trim().toLowerCase();
    if (!q) return true;
    return (
      label.toLowerCase().includes(q) ||
      key.toLowerCase().includes(q) ||
      text.toLowerCase().includes(q)
    );
  }

  // Topics with at least one matching item under the current filter.
  const visible = $derived(
    grouped
      .map((t) => ({
        title: t.title,
        items: t.items.filter(([k, label]) => matches(label, k, HELP[k] ?? "")),
      }))
      .filter((t) => t.items.length > 0),
  );
</script>

<section class="mx-auto max-w-3xl">
  <h2 class="mb-1 text-xl font-semibold">Help</h2>
  <p class="mb-6 text-sm text-slate-500 dark:text-slate-400">
    Documentation links, keyboard shortcuts, the search query syntax, and an
    explanation of every configurable setting.
  </p>

  <!-- Resources -->
  <div class="mb-8">
    <h3 class="mb-3 border-b border-slate-200 pb-1 text-sm font-semibold uppercase
               tracking-wide text-[var(--accent)] dark:border-slate-800">
      Resources
    </h3>
    <ul class="space-y-3">
      {#each RESOURCES as r (r.title)}
        <li>
          <a
            class="text-sm font-medium underline hover:text-[var(--accent)]"
            href={r.href}
            target="_blank"
            rel="noopener noreferrer">{r.title}</a>
          <p class="mt-0.5 text-sm leading-snug text-slate-600 dark:text-slate-400">
            {r.desc}
          </p>
        </li>
      {/each}
    </ul>
  </div>

  <!-- Keyboard shortcuts -->
  <div class="mb-8">
    <h3 class="mb-3 border-b border-slate-200 pb-1 text-sm font-semibold uppercase
               tracking-wide text-[var(--accent)] dark:border-slate-800">
      Keyboard shortcuts
    </h3>
    <dl class="space-y-2">
      {#each SHORTCUTS as s (s.action)}
        <div class="flex items-baseline gap-3">
          <dt class="flex shrink-0 gap-1">
            {#each s.keys as k (k)}
              <kbd
                class="rounded border border-slate-300 bg-slate-100 px-1.5 py-0.5 font-mono
                       text-[11px] text-slate-700 dark:border-slate-700 dark:bg-slate-800
                       dark:text-slate-300">{k}</kbd>
            {/each}
          </dt>
          <dd class="text-sm text-slate-600 dark:text-slate-400">{s.action}</dd>
        </div>
      {/each}
    </dl>
    <p class="mt-2 text-xs text-slate-400 dark:text-slate-500">
      The bare <kbd class="rounded border border-slate-300 bg-slate-100 px-1 font-mono text-[11px] dark:border-slate-700 dark:bg-slate-800">/</kbd>
      shortcut only fires when you are not already typing in a field.
    </p>
  </div>

  <!-- Query syntax (shared data with the query-box popover) -->
  <div class="mb-8">
    <h3 class="mb-3 border-b border-slate-200 pb-1 text-sm font-semibold uppercase
               tracking-wide text-[var(--accent)] dark:border-slate-800">
      Search query syntax
    </h3>
    <p class="mb-3 text-sm text-slate-500 dark:text-slate-400">
      Combine free text with <code class="font-mono">key:value</code> filters —
      everything is ANDed. Hover an example for what it matches.
    </p>
    <dl class="space-y-4">
      {#each DSL_SECTIONS as sec (sec.title)}
        <div>
          <dt class="text-sm font-medium text-slate-800 dark:text-slate-200">
            {sec.title}{#if sec.searchOnly}
              <span class="ml-1 rounded bg-slate-100 px-1 py-0.5 text-[10px] font-normal
                           text-slate-500 dark:bg-slate-800 dark:text-slate-400">search only</span>
            {/if}
          </dt>
          <dd class="mt-0.5 text-sm leading-snug text-slate-600 dark:text-slate-400">
            {sec.body}
            <span class="mt-1 flex flex-wrap gap-1.5">
              {#each sec.examples as ex (ex.q)}
                <code
                  class="rounded border border-slate-200 bg-slate-50 px-1.5 py-0.5 font-mono
                         text-xs text-slate-700 dark:border-slate-700 dark:bg-slate-900
                         dark:text-slate-300"
                  title={ex.note}>{ex.q}</code>
              {/each}
            </span>
          </dd>
        </div>
      {/each}
    </dl>
  </div>

  <!-- Settings field reference -->
  <h3 class="mb-1 border-b border-slate-200 pb-1 text-sm font-semibold uppercase
             tracking-wide text-[var(--accent)] dark:border-slate-800">
    Settings field reference
  </h3>
  <p class="mb-4 mt-2 text-sm text-slate-500 dark:text-slate-400">
    Explanations for every configurable setting, grouped by topic. The same text
    appears behind the small “?” buttons next to each field.
  </p>

  <input
    type="search"
    bind:value={query}
    placeholder="Filter field reference…"
    aria-label="Filter field reference"
    class="mb-6 w-full rounded-lg border border-slate-300 bg-white px-3 py-2 text-sm
           dark:border-slate-700 dark:bg-slate-900"
  />

  {#if visible.length === 0}
    <p class="text-sm text-slate-500 dark:text-slate-400">No help entries match “{query}”.</p>
  {/if}

  {#each visible as topic (topic.title)}
    <div class="mb-8">
      <h4 class="mb-3 border-b border-slate-200 pb-1 text-sm font-semibold
                 text-slate-700 dark:border-slate-800 dark:text-slate-300">
        {topic.title}
      </h4>
      <dl class="space-y-4">
        {#each topic.items as [key, label] (key)}
          <div>
            <dt class="text-sm font-medium text-slate-800 dark:text-slate-200">{label}</dt>
            <dd class="mt-0.5 text-sm leading-snug text-slate-600 dark:text-slate-400">
              {HELP[key]}
            </dd>
          </div>
        {/each}
      </dl>
    </div>
  {/each}
</section>
