<script lang="ts">
  // The About page (#/about) — "what is this deployment actually running?".
  //
  // WHY (user request, 2026-08-10): the console could not answer that question
  // anywhere, and it is the first question of every incident. Every number on
  // this page comes from the live backend process, the live database or the
  // live service (see GET /system/about); the frontend half is baked in at
  // build time because the image ships only dist/.
  //
  // NO OUTBOUND CALLS: the page links to upstream projects, it never fetches
  // from them, so it renders identically on an air-gapped box.
  //
  // The render helpers and the Markdown dump are in ./about (DOM-free, unit
  // tested) — this component is layout only.
  import { systemAbout, friendlyError } from "./api";
  import { copyText } from "./clipboard";
  import {
    aboutMarkdown,
    embeddingSummary,
    formatBytes,
    formatWhen,
    packageCell,
    serviceCell,
    shortSha,
    toolCell,
    type About,
    type Cell,
    type FrontendStack,
  } from "./about";

  let about = $state<About | null>(null);
  let error = $state("");
  let loaded = $state(false);
  let copied = $state("");

  // Build-time constant (vite.config.ts `define`). Guarded because the constant
  // is only substituted by a real build; a stray runtime without it must show
  // "unavailable" rather than throwing the page away.
  const frontend: FrontendStack | null =
    typeof __FRONTEND_STACK__ === "undefined" ? null : __FRONTEND_STACK__;

  $effect(() => {
    void (async () => {
      try {
        about = await systemAbout();
      } catch (e) {
        error = friendlyError(e, "view the build information for");
      } finally {
        loaded = true;
      }
    })();
  });

  async function copyMarkdown() {
    if (!about) return;
    const ok = await copyText(aboutMarkdown(about, frontend));
    copied = ok ? "Copied to the clipboard." : "Copy failed — select the text manually.";
    setTimeout(() => (copied = ""), 4000);
  }

  const TONE: Record<Cell["tone"], string> = {
    ok: "text-slate-800 dark:text-slate-100",
    bad: "text-red-600 dark:text-red-400",
    // A host tool below its recommended minimum: working, but not doing what
    // you think. Amber, matching the outdated chips on the Agents page — the
    // two surfaces report the same tools and must look the same doing it.
    warn: "text-amber-600 dark:text-amber-400",
    muted: "text-slate-400 dark:text-slate-500",
  };
</script>

<div class="space-y-6">
  <header class="flex flex-wrap items-baseline gap-3">
    <h2 class="text-xl font-semibold text-slate-800 dark:text-slate-100">About this deployment</h2>
    <p class="grow text-xs text-slate-500">
      Every version below is read from the running process, database or service —
      not from a configuration file. This page makes no outbound network
      requests; the links open upstream documentation in a new tab.
    </p>
    <button
      class="rounded-lg border border-slate-300 px-3 py-1 text-sm hover:border-[var(--accent)] disabled:opacity-50 dark:border-slate-700"
      disabled={!about}
      onclick={copyMarkdown}
      title="Copy the whole stack as a Markdown table — paste it straight into a bug report.">
      Copy as Markdown
    </button>
  </header>
  {#if copied}
    <p class="text-xs text-[var(--accent)]">{copied}</p>
  {/if}

  {#if error}
    <p class="text-sm text-red-600 dark:text-red-400">{error}</p>
  {:else if !loaded}
    <p class="text-sm text-slate-500">Loading…</p>
  {:else if about}
    {@const app = about.application}

    <!-- Application ------------------------------------------------------- -->
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-3 text-sm font-semibold text-slate-700 dark:text-slate-200">Application</h3>
      <dl class="grid grid-cols-1 gap-x-6 gap-y-2 text-xs sm:grid-cols-2">
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500">Version</dt>
          <dd class="font-mono break-all text-slate-800 dark:text-slate-100">{app.app_version}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500" title="Written into the image by the deploy script — the ground truth for which source this container is running.">Build stamp</dt>
          <dd class="font-mono break-all {app.build_stamp ? 'text-slate-800 dark:text-slate-100' : 'text-slate-400'}">
            {app.build_stamp ?? "none (dev checkout)"}
          </dd>
        </div>
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500">Python</dt>
          <dd class="text-slate-800 dark:text-slate-100">
            {app.python_implementation}
            {app.python_version}
          </dd>
        </div>
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500">Platform</dt>
          <dd class="break-all text-slate-800 dark:text-slate-100">{app.platform} · {app.machine}</dd>
        </div>
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500">License</dt>
          <dd>
            <a class="underline hover:text-[var(--accent)]" href={app.license_url} target="_blank" rel="noopener noreferrer">{app.license}</a>
          </dd>
        </div>
        <div class="flex gap-2">
          <dt class="w-36 shrink-0 text-slate-500" title="AGPL-3.0 §13: the Corresponding Source of this running instance.">Source</dt>
          <dd class="break-all">
            <a class="underline hover:text-[var(--accent)]" href={app.source_url} target="_blank" rel="noopener noreferrer">{app.source_url}</a>
          </dd>
        </div>
      </dl>
    </section>

    <!-- Services ---------------------------------------------------------- -->
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">Services</h3>
      <p class="mb-3 text-xs text-slate-500">
        Probed live, one at a time. A service that cannot be reached says so here
        rather than blanking the row — that is itself the answer.
      </p>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[32rem] text-left text-xs">
          <thead class="text-slate-500">
            <tr><th class="py-1 pr-4 font-medium">Service</th><th class="py-1 pr-4 font-medium">Running version</th><th class="py-1 font-medium">Notes</th></tr>
          </thead>
          <tbody>
            {#each about.services as s (s.name)}
              {@const cell = serviceCell(s)}
              <tr class="border-t border-slate-100 dark:border-slate-800">
                <td class="py-1.5 pr-4">
                  <a class="underline hover:text-[var(--accent)]" href={s.url} target="_blank" rel="noopener noreferrer">{s.name}</a>
                </td>
                <td class="py-1.5 pr-4 font-mono break-all {TONE[cell.tone]}" title={cell.hint ?? ""}>{cell.text}</td>
                <td class="py-1.5 text-slate-400">{s.detail ?? ""}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Backend dependencies ---------------------------------------------- -->
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">
        Backend dependencies (Python)
      </h3>
      <p class="mb-3 text-xs text-slate-500">
        The direct dependencies, with the version each is actually running in
        this process — not the version the project pins.
      </p>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[32rem] text-left text-xs">
          <thead class="text-slate-500">
            <tr><th class="py-1 pr-4 font-medium">Package</th><th class="py-1 pr-4 font-medium">Running version</th><th class="py-1 font-medium">Documentation</th></tr>
          </thead>
          <tbody>
            {#each about.python_packages as p (p.name)}
              {@const cell = packageCell(p)}
              <tr class="border-t border-slate-100 dark:border-slate-800">
                <td class="py-1.5 pr-4 text-slate-700 dark:text-slate-200">
                  {p.name}{#if p.optional}<span class="ml-1 text-slate-400" title="Installed as an optional extra.">(extra)</span>{/if}
                </td>
                <td class="py-1.5 pr-4 font-mono {TONE[cell.tone]}">{cell.text}</td>
                <td class="py-1.5 break-all">
                  <a class="text-slate-500 underline hover:text-[var(--accent)]" href={p.url} target="_blank" rel="noopener noreferrer">{p.url}</a>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Frontend bundle ---------------------------------------------------- -->
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">
        Frontend bundle
      </h3>
      {#if frontend}
        <p class="mb-3 text-xs text-slate-500">
          Recorded when this bundle was compiled — the deployed image ships only
          the built assets, so these are the versions baked into the JavaScript
          you are running. Built with Node
          <span class="font-mono">{frontend.node}</span> at
          <span class="font-mono">{formatWhen(frontend.built_at)}</span>.
        </p>
        <div class="overflow-x-auto">
          <table class="w-full min-w-[32rem] text-left text-xs">
            <thead class="text-slate-500">
              <tr><th class="py-1 pr-4 font-medium">Package</th><th class="py-1 pr-4 font-medium">Built version</th><th class="py-1 pr-4 font-medium">Role</th><th class="py-1 font-medium">Documentation</th></tr>
            </thead>
            <tbody>
              {#each frontend.packages as p (p.name)}
                <tr class="border-t border-slate-100 dark:border-slate-800">
                  <td class="py-1.5 pr-4 text-slate-700 dark:text-slate-200">{p.name}</td>
                  <td class="py-1.5 pr-4 font-mono text-slate-800 dark:text-slate-100">{p.version}</td>
                  <td class="py-1.5 pr-4 text-slate-400">{p.kind === "runtime" ? "ships in the bundle" : "build tooling"}</td>
                  <td class="py-1.5 break-all">
                    <a class="text-slate-500 underline hover:text-[var(--accent)]" href={p.url} target="_blank" rel="noopener noreferrer">{p.url}</a>
                  </td>
                </tr>
              {/each}
            </tbody>
          </table>
        </div>
      {:else}
        <p class="text-xs text-slate-400">
          Build-time frontend information is unavailable in this build.
        </p>
      {/if}
    </section>

    <!-- Host tools --------------------------------------------------------- -->
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">
        Extraction tools on this server
      </h3>
      <p class="mb-3 text-xs text-slate-500">
        External binaries the extractors shell out to. A missing tool disables
        only the capability that needs it; agents report their own tools
        separately on the Agents page. A version shown in
        <span class="text-amber-600 dark:text-amber-400">amber</span> is below the
        minimum Filearr recommends — still working, but hover it for what that
        costs.
      </p>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[36rem] text-left text-xs">
          <thead class="text-slate-500">
            <tr><th class="py-1 pr-4 font-medium">Tool</th><th class="py-1 pr-4 font-medium">Used for</th><th class="py-1 pr-4 font-medium">Version</th><th class="py-1 font-medium">Path</th></tr>
          </thead>
          <tbody>
            {#each about.host_tools as t (t.name)}
              {@const cell = toolCell(t)}
              <tr class="border-t border-slate-100 dark:border-slate-800">
                <td class="py-1.5 pr-4">
                  <a class="underline hover:text-[var(--accent)]" href={t.url} target="_blank" rel="noopener noreferrer">{t.name}</a>
                </td>
                <td class="py-1.5 pr-4 text-slate-500">{t.purpose}</td>
                <td class="py-1.5 pr-4 font-mono {TONE[cell.tone]}" title={cell.hint ?? ""}>{cell.text}</td>
                <td class="py-1.5 font-mono break-all text-slate-400">{t.path ?? "—"}</td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </section>

    <!-- Agent fleet -------------------------------------------------------- -->
    {#if about.agents}
      <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
        <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">
          Agent fleet ({about.agents.total})
        </h3>
        <p class="mb-3 text-xs text-slate-500">
          Which agent build each enrolled machine is running — a rollout that has
          not finished shows as more than one row.
        </p>
        {#if about.agents.error}
          <p class="text-xs text-red-600 dark:text-red-400">{about.agents.error}</p>
        {:else if about.agents.versions.length === 0}
          <p class="text-xs text-slate-400">No agents enrolled.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full min-w-[20rem] text-left text-xs">
              <thead class="text-slate-500">
                <tr><th class="py-1 pr-4 font-medium">Agent version</th><th class="py-1 font-medium">Agents</th></tr>
              </thead>
              <tbody>
                {#each about.agents.versions as v (v.version ?? "unknown")}
                  <tr class="border-t border-slate-100 dark:border-slate-800">
                    <td class="py-1.5 pr-4 font-mono {v.version ? 'text-slate-800 dark:text-slate-100' : 'text-slate-400'}">
                      {v.version ?? "unknown (never reported)"}
                    </td>
                    <td class="py-1.5 text-slate-600 dark:text-slate-300">{v.count}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </section>
    {/if}

    <!-- Embedding model ---------------------------------------------------- -->
    {@const e = about.embedding}
    <section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
      <h3 class="mb-1 text-sm font-semibold text-slate-700 dark:text-slate-200">
        Semantic-search embedding model
      </h3>
      <p class="mb-3 text-xs text-slate-500">
        {embeddingSummary(e).text}. {e.license_note}
      </p>
      <div class="overflow-x-auto">
        <table class="w-full min-w-[32rem] text-left text-xs">
          <tbody>
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="w-48 py-1.5 pr-4 text-slate-500">Model</td>
              <td class="py-1.5 text-slate-800 dark:text-slate-100">{e.name} · {e.dimensions} dimensions</td>
            </tr>
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="py-1.5 pr-4 text-slate-500">Hugging Face repository</td>
              <td class="py-1.5 break-all">
                <a class="underline hover:text-[var(--accent)]" href={e.model_url} target="_blank" rel="noopener noreferrer">{e.repo}</a>
                <span class="text-slate-400"> · {e.file}</span>
              </td>
            </tr>
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="py-1.5 pr-4 text-slate-500">Cached revision</td>
              <td class="py-1.5 break-all">
                {#if e.revision}
                  <a class="font-mono underline hover:text-[var(--accent)]" href={e.revision_url} target="_blank" rel="noopener noreferrer" title={e.revision}>
                    {shortSha(e.revision)}
                  </a>
                  <span class="text-slate-400"> — the exact upstream commit these weights came from</span>
                {:else}
                  <span class="text-slate-400">not downloaded</span>
                {/if}
              </td>
            </tr>
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="py-1.5 pr-4 text-slate-500">Cached file</td>
              <td class="py-1.5 break-all">
                {#if e.downloaded}
                  <span class="text-slate-800 dark:text-slate-100">{formatBytes(e.size)}</span>
                  <span class="font-mono text-slate-400"> · {e.path}</span>
                {:else}
                  <span class="text-slate-400">nothing cached under {e.cache_dir}</span>
                {/if}
              </td>
            </tr>
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="py-1.5 pr-4 text-slate-500">
                <span
                  class="cursor-help underline decoration-dotted decoration-slate-400 underline-offset-2"
                  title="When THIS machine downloaded the file. The model's publication date is not shown because determining it would mean calling Hugging Face, and this page makes no outbound requests. Use the revision link for exact provenance.">
                  Downloaded here
                </span>
              </td>
              <td class="py-1.5 {e.downloaded_at ? 'text-slate-800 dark:text-slate-100' : 'text-slate-400'}">
                {e.downloaded_at ? formatWhen(e.downloaded_at) : "never"}
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </section>
  {/if}
</div>
