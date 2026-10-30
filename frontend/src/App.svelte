<script module lang="ts">
  // UI-T9 — hash-based routing so a refresh keeps the current tab and
  // back/forward toggles between them. `#/search` (default), `#/libraries`
  // (library management, split from admin 2026-08-19), `#/admin`, `#/jobs`
  // (UI-T10 jobs dashboard).
  type Page = "search" | "libraries" | "admin" | "jobs" | "alerts" | "browse" | "timeline" | "reports" | "agents" | "taxonomy" | "filter-builder" | "help" | "about" | "account";

  // Route-level code splitting: importing all pages statically made the entry
  // chunk ~870 kB, so each page is its own chunk fetched only when its route is
  // first shown. LoginPage stays eager (it is the first paint when gated).
  // `satisfies` makes a new Page without a loader a type error.
  const pageLoaders = {
    search: () => import("./lib/SearchPage.svelte"),
    libraries: () => import("./lib/LibrariesPage.svelte"),
    admin: () => import("./lib/AdminPage.svelte"),
    jobs: () => import("./lib/JobsPage.svelte"),
    alerts: () => import("./lib/AlertsPage.svelte"),
    browse: () => import("./lib/BrowsePage.svelte"),
    timeline: () => import("./lib/TimelinePage.svelte"),
    reports: () => import("./lib/ReportsPage.svelte"),
    agents: () => import("./lib/AgentsPage.svelte"),
    taxonomy: () => import("./lib/TaxonomyPage.svelte"),
    "filter-builder": () => import("./lib/FilterBuilderPage.svelte"),
    help: () => import("./lib/HelpPage.svelte"),
    about: () => import("./lib/AboutPage.svelte"),
    account: () => import("./lib/AccountPage.svelte"),
  } satisfies Record<Page, () => Promise<unknown>>;

  // One promise per page for the app's lifetime: `{#await}` gets the same
  // already-settled promise when a route is revisited, so it neither re-fetches
  // nor flashes "Loading…". A rejection stays cached too — the only recovery
  // offered is a full reload, which resets this map anyway.
  const pageModules = new Map<Page, Promise<unknown>>();
  function loadPage<P extends Page>(p: P): ReturnType<(typeof pageLoaders)[P]> {
    let mod = pageModules.get(p);
    if (!mod) {
      mod = pageLoaders[p]();
      pageModules.set(p, mod);
    }
    return mod as ReturnType<(typeof pageLoaders)[P]>;
  }
</script>

<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import LoginPage from "./lib/LoginPage.svelte";
  import { getVersion, authStatus, authMe, authLogout, type AuthMode, type AuthPrincipal } from "./lib/api";
  import { theme, applyTheme, applyServerPreferences } from "./lib/theme.svelte";
  import { shareFormat, setShareFormat, detectedPlatform } from "./lib/osFormat.svelte";
  import type { FormatPref } from "./lib/osFormat";
  import { parseBrowseHash } from "./lib/routes";

  function routeFromHash(): { page: Page; browseLib: string; browsePath: string } {
    const browse = parseBrowseHash(location.hash);
    if (browse) return { page: "browse", browseLib: browse.libraryId, browsePath: browse.path };
    if (location.hash === "#/libraries") return { page: "libraries", browseLib: "", browsePath: "" };
    if (location.hash === "#/admin") return { page: "admin", browseLib: "", browsePath: "" };
    if (location.hash === "#/jobs") return { page: "jobs", browseLib: "", browsePath: "" };
    if (location.hash === "#/alerts") return { page: "alerts", browseLib: "", browsePath: "" };
    if (location.hash === "#/timeline") return { page: "timeline", browseLib: "", browsePath: "" };
    if (location.hash === "#/reports") return { page: "reports", browseLib: "", browsePath: "" };
    if (location.hash === "#/agents") return { page: "agents", browseLib: "", browsePath: "" };
    if (location.hash === "#/taxonomy") return { page: "taxonomy", browseLib: "", browsePath: "" };
    if (location.hash === "#/filter-builder") return { page: "filter-builder", browseLib: "", browsePath: "" };
    if (location.hash === "#/help") return { page: "help", browseLib: "", browsePath: "" };
    // #/account — self-service profile/password/preferences (header badge link).
    if (location.hash === "#/account") return { page: "account", browseLib: "", browsePath: "" };
    // #/about — the running build stack (versions of everything). Footer-linked
    // rather than a nav tab: it is a reference surface, not a daily workflow.
    if (location.hash === "#/about") return { page: "about", browseLib: "", browsePath: "" };
    return { page: "search", browseLib: "", browsePath: "" };
  }

  // UI-T15: human label for the detected OS, shown in the "Auto" option so the
  // user knows which spelling Auto resolves to.
  const platformLabel =
    detectedPlatform === "windows"
      ? "Windows"
      : detectedPlatform === "mac"
        ? "Mac"
        : detectedPlatform === "linux"
          ? "Linux"
          : "other";

  const initialRoute = routeFromHash();
  let page = $state<Page>(initialRoute.page);
  let browseLib = $state<string>(initialRoute.browseLib);
  let browsePath = $state<string>(initialRoute.browsePath);
  // Running-build stamp (deploy-injected). Lets anyone check at a glance that
  // the page/backend match a specific deploy — pairs with the deploy verifier.
  let buildStamp = $state<string | null>(null);
  // AGPL §13 Source link target. Prefer the runtime value from /version
  // (FILEARR_SOURCE_URL) so a fork can point users at its modified source without
  // a frontend rebuild; fall back to the Vite build-time default.
  let sourceUrl = $state<string>(__SOURCE_URL__);
  // P5-T1/W6-D4: the distributed-agent feature is opt-in (FILEARR_AGENTS_ENABLED);
  // gate the Agents nav entry so it only shows when the surface is live.
  let agentsEnabled = $state(false);
  getVersion()
    .then((v) => {
      buildStamp = v.build_stamp;
      if (v.source_url) sourceUrl = v.source_url;
      agentsEnabled = !!v.agents_enabled;
    })
    .catch(() => {});

  // P6-T1 auth gate. `authMode` drives whether a login wall is shown; `me` is
  // the current session principal (null when signed out). `authReady` gates the
  // initial render so we never flash the app before knowing the mode.
  let authMode = $state<AuthMode>("disabled");
  let oidcEnabled = $state(false);
  let me = $state<AuthPrincipal | null>(null);
  let authReady = $state(false);

  async function probeAuth() {
    try {
      const st = await authStatus();
      authMode = st.mode;
      oidcEnabled = st.oidc_enabled;
      me = st.mode === "disabled" ? null : await authMe();
      // Server-side preferences follow the person across browsers: they win
      // over whatever this browser's localStorage remembers.
      if (me?.preferences) applyServerPreferences(me.preferences);
    } catch {
      // If the probe fails (e.g. backend unreachable), fail OPEN to the app
      // rather than trapping the user behind a wall we cannot verify.
      authMode = "disabled";
      me = null;
    } finally {
      authReady = true;
    }
  }
  probeAuth();

  // The wall shows when auth is active (enabled/bootstrap) and there is no
  // authenticated principal yet.
  const gated = $derived(authReady && authMode !== "disabled" && me === null);

  async function doLogout() {
    await authLogout();
    me = null;
    await probeAuth();
  }

  function goto(p: Page) {
    page = p;
    const hash = p === "search" ? "#/search" : `#/${p}`;
    if (location.hash !== hash) location.hash = hash;
  }

  // React to back/forward + manual hash edits. Guarded so it only reassigns when
  // the derived page actually changes (avoids redundant $state churn).
  function onHashChange() {
    const next = routeFromHash();
    if (next.page !== page) page = next.page;
    if (next.browseLib !== browseLib) browseLib = next.browseLib;
    if (next.browsePath !== browsePath) browsePath = next.browsePath;
  }

  function toggleDark() {
    theme.mode = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme();
  }

  onMount(() => {
    applyTheme();
    // Normalise a bare URL to the default route so refresh/bookmark is stable.
    if (!location.hash) location.replace("#/search");
    window.addEventListener("hashchange", onHashChange);
  });

  onDestroy(() => window.removeEventListener("hashchange", onHashChange));
</script>

{#if gated}
  <LoginPage mode={authMode} {oidcEnabled} onAuthed={probeAuth} />
{:else}
<div class="mx-auto max-w-screen-2xl px-4 py-4 sm:px-6 lg:px-8">
  <!-- 2026-08-26 (user report, phone): this row used to be a single
       non-wrapping flex line — nine nav buttons plus account / paths / theme —
       so on a 390 px screen the document was ~900 px wide, Safari zoomed out
       and every page panned sideways. Wrap instead: the nav flows onto extra
       lines and the utility controls sit on their own line under it. -->
  <header class="flex flex-wrap items-center gap-x-3 gap-y-2 py-4">
    <h1 class="text-2xl font-bold" style="color: var(--accent)">Filearr</h1>
    <nav class="flex min-w-0 flex-wrap gap-1 sm:ml-4">
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'search' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("search")}>Search</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'libraries' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("libraries")}>Libraries</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'admin' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("admin")}>Admin</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'jobs' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("jobs")}>Jobs</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'alerts' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("alerts")}>Alerts</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'timeline' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("timeline")}>Timeline</button>
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'reports' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("reports")}>Reports</button>
      {#if agentsEnabled}
        <button
          class="rounded-lg px-3 py-1 text-sm {page === 'agents' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
          onclick={() => goto("agents")}>Agents</button>
      {/if}
      <button
        class="rounded-lg px-3 py-1 text-sm {page === 'filter-builder' ? 'bg-[var(--accent)] text-white' : 'text-slate-500'}"
        onclick={() => goto("filter-builder")}>Filter builder</button>
    </nav>
    <div class="grow"></div>
    <div class="flex flex-wrap items-center gap-2">
    {#if me}
      <a class="rounded-lg px-2 py-1 text-xs {page === 'account' ? 'bg-[var(--accent)] text-white' : 'bg-slate-100 text-slate-600 hover:text-[var(--accent)] dark:bg-slate-800 dark:text-slate-300'}"
        href="#/account"
        title={`${me.username} · ${me.global_role} — account settings`}>
        {me.display_name || me.username}<span class="ml-1 {page === 'account' ? 'text-white/70' : 'text-slate-400'}">({me.global_role})</span>
      </a>
      <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={doLogout}>Sign out</button>
    {/if}
    <label class="flex items-center gap-1 text-xs text-slate-500" title="Which network-path spelling to show for Open/Copy actions. smb:// suits Linux/macOS; \\host\share (UNC) suits Windows.">
      <span class="hidden sm:inline">Paths</span>
      <select
        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
        aria-label="Network path format"
        value={shareFormat.pref}
        onchange={(e) => setShareFormat((e.currentTarget as HTMLSelectElement).value as FormatPref)}>
        <option value="auto">Auto ({platformLabel})</option>
        <option value="url">smb:// URL</option>
        <option value="unc">Windows UNC</option>
      </select>
    </label>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
      onclick={toggleDark}>Theme</button>
    </div>
  </header>

  <!-- Each branch awaits its own page chunk (see pageLoaders); the branches
       stay separate because every page takes different props. -->
  {#snippet pageLoading()}
    <p class="py-8 text-sm text-slate-500">Loading…</p>
  {/snippet}
  {#snippet pageLoadFailed()}
    <!-- Realistic cause: a deploy replaced the hashed chunk this tab still
         references. A reload picks up the new index.html and its chunk names. -->
    <div class="py-8 text-sm text-slate-500">
      <p>This page failed to load — Filearr may have been updated since this tab was opened.</p>
      <button class="mt-2 rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={() => location.reload()}>Reload</button>
    </div>
  {/snippet}

  {#if page === "libraries"}
    {#await loadPage("libraries")}{@render pageLoading()}{:then m}<m.default {me} />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "admin"}
    {#await loadPage("admin")}{@render pageLoading()}{:then m}<m.default {me} authDisabled={authMode === "disabled"} />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "jobs"}
    {#await loadPage("jobs")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "alerts"}
    {#await loadPage("alerts")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "browse"}
    {#await loadPage("browse")}{@render pageLoading()}{:then m}<m.default libraryId={browseLib} path={browsePath} />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "timeline"}
    {#await loadPage("timeline")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "reports"}
    {#await loadPage("reports")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "agents"}
    {#await loadPage("agents")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "taxonomy"}
    {#await loadPage("taxonomy")}{@render pageLoading()}{:then m}<m.default {me} authDisabled={authMode === "disabled"} />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "filter-builder"}
    {#await loadPage("filter-builder")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "help"}
    {#await loadPage("help")}{@render pageLoading()}{:then m}<m.default {sourceUrl} />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "about"}
    {#await loadPage("about")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "account"}
    {#if me}
      {#await loadPage("account")}{@render pageLoading()}{:then m}<m.default {me} onUpdated={(p) => (me = p)} />{:catch}{@render pageLoadFailed()}{/await}
    {:else}
      <p class="py-8 text-sm text-slate-500">Authentication is disabled on this deployment — there is no account to manage.</p>
    {/if}
  {:else}
    {#await loadPage("search")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {/if}

  <footer class="mt-12 border-t border-slate-200 py-4 text-xs text-slate-500 dark:border-slate-800">
    <span title={buildStamp ?? "no build stamp (dev)"}>
      Filearr {__APP_VERSION__}{#if buildStamp}
        <span class="font-mono text-slate-400"> · build {buildStamp.slice(0, 12)}</span>
      {/if}
    </span>
    <span class="px-1">·</span>
    <a class="underline hover:text-[var(--accent)]" href="#/help">Help</a>
    <span class="px-1">·</span>
    <!-- The running build stack: every dependency version this deployment is
         actually running, for pasting into a bug report. -->
    <a class="underline hover:text-[var(--accent)]" href="#/about">About</a>
    <span class="px-1">·</span>
    <!-- Bundled mkdocs manual served by this instance (main.py /docs mount). -->
    <a
      class="underline hover:text-[var(--accent)]"
      href="/docs/"
      target="_blank"
      rel="noopener noreferrer">Docs</a>
    <span class="px-1">·</span>
    <a
      class="underline hover:text-[var(--accent)]"
      href="/api/docs"
      target="_blank"
      rel="noopener noreferrer">API Docs</a>
    <span class="px-1">·</span>
    <!-- AGPL-3.0 §13: offer the running instance's Corresponding Source. -->
    <a
      class="underline hover:text-[var(--accent)]"
      href={sourceUrl}
      target="_blank"
      rel="noopener noreferrer">Source</a>
    <span class="px-1">·</span>
    <span>AGPL-3.0-or-later</span>
  </footer>
</div>
{/if}
