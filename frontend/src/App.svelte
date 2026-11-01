<script module lang="ts">
  // UI-T9 hash routing: the Page union and hash → page mapping live in
  // lib/routes.ts so they are unit-testable.
  import type { Page } from "./lib/routes";

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
    // Dev-only gallery: Vite folds import.meta.env.DEV to a literal, so the
    // production build drops the KitPage branch and never emits its chunk.
    kit: () => (import.meta.env.DEV ? import("./lib/KitPage.svelte") : import("./lib/KitUnavailable.svelte")),
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
  import { onDestroy, onMount, untrack } from "svelte";
  import LoginPage from "./lib/LoginPage.svelte";
  import {
    getVersion,
    authStatus,
    authMe,
    authLogout,
    errorMessage,
    errorStatus,
    setUnauthorizedHandler,
    type AuthMode,
    type AuthPrincipal,
  } from "./lib/api";
  import * as safeStorage from "./lib/safeStorage";
  import { safeHref } from "./lib/pathlinks";
  import { theme, applyTheme, applyServerPreferences } from "./lib/theme.svelte";
  import { shareFormat, setShareFormat, detectedPlatform } from "./lib/osFormat.svelte";
  import type { FormatPref } from "./lib/osFormat";
  import { routeFromHash, pageTitle } from "./lib/routes";
  import { announcement, announce } from "./lib/announce.svelte";
  // Direct paths, not the ./lib/ui barrel: ToastHost has no bits-ui
  // dependency, and ConfirmHost (which does) is imported lazily below.
  import ToastHost from "./lib/ui/ToastHost.svelte";
  import { cancelAllConfirms, hasPendingConfirm } from "./lib/ui/confirm.svelte";
  import { toast } from "./lib/ui/toast.svelte";
  import StateBlock from "./lib/ui/StateBlock.svelte";

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

  const initialRoute = routeFromHash(location.hash);
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
  // D9: /version's source_url is server data — only a safe scheme becomes a link.
  const sourceHref = $derived(safeHref(sourceUrl));
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
  // the current session principal (null when signed out). Nothing but the
  // splash renders until the first probe settles, so no page fires a request
  // the wall would have stopped. A failed probe fails CLOSED: "unreachable"
  // shows a retry screen, never the app.
  let authMode = $state<AuthMode>("disabled");
  let oidcEnabled = $state(false);
  let me = $state<AuthPrincipal | null>(null);
  let authState = $state<"probing" | "ready" | "unreachable">("probing");

  // Set by LoginPage before it leaves for the IdP; the OIDC callback lands on
  // "/", so the deep link is put back once the session is confirmed.
  const OIDC_RETURN_KEY = "oidc.returnHash";

  // Only the newest probe may write: a Retry double-click or a re-probe racing
  // the logout one must not land out of order.
  let probeSeq = 0;
  async function probeAuth() {
    const seq = ++probeSeq;
    // Retry feedback; other re-probes keep the screen (LoginPage keeps its message).
    if (authState === "unreachable") authState = "probing";
    try {
      const st = await authStatus();
      const principal = st.mode === "disabled" ? null : await authMe();
      if (seq !== probeSeq) return;
      authMode = st.mode;
      oidcEnabled = st.oidc_enabled;
      me = principal;
      // Server-side preferences follow the person across browsers: they win
      // over whatever this browser's localStorage remembers.
      if (me?.preferences) applyServerPreferences(me.preferences);
      if (me) {
        const back = safeStorage.get(OIDC_RETURN_KEY);
        if (back !== null) {
          safeStorage.remove(OIDC_RETURN_KEY);
          // Before "ready", so the default page never mounts first; replace,
          // so Back does not return to it.
          if (back.startsWith("#/")) {
            history.replaceState(null, "", location.pathname + location.search + back);
            onHashChange();
          }
        }
      }
      authState = "ready";
    } catch {
      // Network error / 5xx from either call (authMe answers a 401 with null).
      // authMode and me stay as they were: we cannot verify, so we do not open.
      if (seq === probeSeq) authState = "unreachable";
    }
  }
  probeAuth();

  // The wall shows when auth is active (enabled/bootstrap) and there is no
  // authenticated principal.
  const gated = $derived(authState === "ready" && authMode !== "disabled" && me === null);

  async function doLogout() {
    try {
      await authLogout();
      me = null;
    } catch (e) {
      // A 401 means the session was already gone: signed out either way.
      if (errorStatus(e) === 401) me = null;
      else toast(errorMessage(e), { tone: "danger" });
    }
    // Either way the server decides what is shown next.
    await probeAuth();
  }

  // Last hash that was a real route, so a stray `#main` can be undone.
  let lastRouteHash = location.hash;

  // React to nav links, back/forward and manual hash edits. Guarded so it only
  // reassigns when the derived page actually changes (avoids redundant $state
  // churn).
  function onHashChange() {
    if (location.hash === "#main") {
      // Safety net: the skip link handles its own click, so `#main` only lands
      // here if it is reached some other way. Not a route: restore the last one.
      history.replaceState(null, "", lastRouteHash || "#/search");
      return;
    }
    lastRouteHash = location.hash;
    const next = routeFromHash(location.hash);
    if (next.page !== page) page = next.page;
    if (next.browseLib !== browseLib) browseLib = next.browseLib;
    if (next.browsePath !== browsePath) browsePath = next.browsePath;
  }

  // Mirrors applyTheme's resolution so the toggle reports the shown theme.
  // matchMedia is not reactive, but toggleDark always sets an explicit mode.
  const isDark = $derived(
    theme.mode === "dark" || (theme.mode === "system" && window.matchMedia("(prefers-color-scheme: dark)").matches),
  );

  function toggleDark() {
    theme.mode = document.documentElement.classList.contains("dark") ? "light" : "dark";
    applyTheme();
  }

  let mainEl = $state<HTMLElement | undefined>();
  // The title follows `gated` too, so the login wall is not titled after the
  // page behind it. Kept apart from the focus/announce effect below, which
  // must not re-run on login/logout.
  $effect(() => {
    document.title = gated ? "Sign in · Filearr" : authState === "ready" ? pageTitle(page) : "Filearr";
  });
  // False until the first run, so the initial render neither steals focus nor
  // announces — only later route changes do.
  let routed = false;
  $effect(() => {
    // Only `page` should re-run this (read here, tracked): a login/logout
    // flipping `gated` is not a route change, so read the rest untracked.
    const p = page;
    untrack(() => {
      if (routed && authState === "ready" && !gated) {
        // Without this, a keyboard/screen-reader user stays on the nav link
        // after the content under it has been replaced.
        mainEl?.focus({ preventScroll: false });
        announce(pageTitle(p).replace(" · Filearr", "") + " page");
      }
    });
    routed = true;
  });

  // The ConfirmHost chunk failed to load (offline, or a deploy replaced it):
  // without this every pending and future confirm() would await forever.
  // Called from the template's {:catch}, which runs inside a derived where
  // writing $state throws (state_unsafe_mutation), so the work is deferred a
  // microtask. Returns "" so the template renders nothing.
  function onConfirmHostFailed(): string {
    queueMicrotask(() => {
      cancelAllConfirms();
      toast("Couldn't open the confirmation dialog — reload Filearr", { tone: "danger" });
    });
    return "";
  }

  onMount(() => {
    applyTheme();
    // Fetch the ConfirmHost chunk while idle so the first confirm() opens without a network wait.
    const warm = () => void import("./lib/ui/ConfirmHost.svelte").catch(() => {});
    "requestIdleCallback" in window ? requestIdleCallback(warm) : setTimeout(warm, 2000);
    // Normalise a bare URL to the default route so refresh/bookmark is stable.
    if (!location.hash) location.replace("#/search");
    window.addEventListener("hashchange", onHashChange);
    // A lapsed session mid-use: drop to the wall. Flipping `gated` unmounts
    // <main>, so every page's pollers stop in their onDestroy.
    setUnauthorizedHandler(() => {
      // Only a running app can lose its session; a late 401 while probing or
      // unreachable must not strand the splash.
      if (authState === "ready" && authMode !== "disabled" && me) {
        // A probe that read /auth/me just before expiry must not restore me.
        probeSeq++;
        me = null;
        // The page that asked is gone; its pending confirm must not hang.
        cancelAllConfirms();
        toast("Your session has expired. Sign in again.", { tone: "warn" });
      }
    });
  });

  onDestroy(() => {
    window.removeEventListener("hashchange", onHashChange);
    setUnauthorizedHandler(null);
  });
</script>

<!-- Outside the gate: a toast raised as the wall appears (session expired,
     sign-out failed, password changed) must survive the flip, both the visual
     stack and its spoken copy in the live region. -->
<ToastHost />
<!-- The one polite live region; lib/announce.svelte.ts feeds it. Outside
     <main> so page swaps never unmount it mid-announcement. -->
<div role="status" aria-live="polite" aria-atomic="true" class="sr-only">{announcement.text}</div>
{#if authState === "probing"}
  <div class="flex min-h-screen items-center justify-center px-4">
    <p role="status" class="text-sm text-slate-500">Connecting…</p>
  </div>
{:else if authState === "unreachable"}
  <div class="flex min-h-screen flex-col items-center justify-center px-4">
    <h1 class="text-2xl font-bold text-accent-text">Filearr</h1>
    <StateBlock state="error" message="Can't reach the Filearr server." onRetry={probeAuth} />
  </div>
{:else if gated}
  <LoginPage mode={authMode} {oidcEnabled} onAuthed={probeAuth} />
{:else}
<div class="mx-auto max-w-screen-2xl px-4 py-4 sm:px-6 lg:px-8">
  <!-- href kept for semantics; the click handler focuses <main> itself so the
       skip does not push a `#main` history entry. -->
  <a href="#main" onclick={(e) => { e.preventDefault(); mainEl?.focus(); }} class="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-50 focus:rounded-lg focus:bg-white focus:px-3 focus:py-2 focus:text-sm focus:shadow dark:focus:bg-slate-900">Skip to main content</a>
  <!-- 2026-08-26 (user report, phone): this row used to be a single
       non-wrapping flex line — nine nav buttons plus account / paths / theme —
       so on a 390 px screen the document was ~900 px wide, Safari zoomed out
       and every page panned sideways. Wrap instead: the nav flows onto extra
       lines and the utility controls sit on their own line under it. -->
  <header class="flex flex-wrap items-center gap-x-3 gap-y-2 py-4">
    <h1 class="text-2xl font-bold text-accent-text">Filearr</h1>
    <!-- Real links (not buttons) so they can be opened in a new tab and the
         hash router does the navigation. aria-current + font weight mark the
         active page without relying on colour alone. -->
    {#snippet navLink(p: Page, label: string)}
      <a
        href="#/{p}"
        aria-current={page === p ? "page" : undefined}
        class="rounded-lg px-3 py-1 text-sm {page === p ? 'bg-accent font-semibold text-accent-fg' : 'text-muted hover:text-accent-text'}">{label}</a>
    {/snippet}
    <nav aria-label="Primary" class="flex min-w-0 flex-wrap gap-1 sm:ml-4">
      {@render navLink("search", "Search")}
      {@render navLink("libraries", "Libraries")}
      {@render navLink("admin", "Admin")}
      {@render navLink("jobs", "Jobs")}
      {@render navLink("alerts", "Alerts")}
      {@render navLink("timeline", "Timeline")}
      {@render navLink("reports", "Reports")}
      {#if agentsEnabled}
        {@render navLink("agents", "Agents")}
      {/if}
      {@render navLink("filter-builder", "Filter builder")}
    </nav>
    <div class="grow"></div>
    <div class="flex flex-wrap items-center gap-2">
    {#if me}
      <a class="rounded-lg px-2 py-1 text-xs {page === 'account' ? 'bg-accent text-accent-fg' : 'bg-surface-3 text-fg hover:text-accent-text'}"
        href="#/account"
        title={`${me.username} · ${me.global_role} — account settings`}>
        {me.display_name || me.username}<span class="ml-1 {page === 'account' ? 'text-accent-fg' : 'text-muted'}">({me.global_role})</span>
      </a>
      <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
        onclick={doLogout}>Sign out</button>
    {/if}
    <label class="flex items-center gap-1 text-xs text-muted" title="Which network-path spelling to show for Open/Copy actions. smb:// suits Linux/macOS; \\host\share (UNC) suits Windows.">
      <span class="hidden sm:inline">Paths</span>
      <select aria-label="Network path format"
        class="rounded-control border border-border-strong bg-surface px-2 py-1 text-sm text-fg"
        value={shareFormat.pref}
        onchange={(e) => setShareFormat((e.currentTarget as HTMLSelectElement).value as FormatPref)}>
        <option value="auto">Auto ({platformLabel})</option>
        <option value="url">smb:// URL</option>
        <option value="unc">Windows UNC</option>
      </select>
    </label>
    <button class="rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
      aria-pressed={isDark} aria-label="Toggle dark theme"
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

  <!-- tabindex=-1: focusable by script (route change) and by the skip link,
       but not a Tab stop. outline-none: a ring round the whole page on
       programmatic focus is noise, not a cue. -->
  <main id="main" tabindex="-1" class="outline-none" bind:this={mainEl}>
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
  {:else if page === "kit"}
    {#await loadPage("kit")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {:else if page === "account"}
    {#if me}
      {#await loadPage("account")}{@render pageLoading()}{:then m}<m.default {me} onUpdated={(p) => (me = p)} onSignedOut={probeAuth} />{:catch}{@render pageLoadFailed()}{/await}
    {:else}
      <p class="py-8 text-sm text-slate-500">Authentication is disabled on this deployment — there is no account to manage.</p>
    {/if}
  {:else}
    {#await loadPage("search")}{@render pageLoading()}{:then m}<m.default />{:catch}{@render pageLoadFailed()}{/await}
  {/if}
  </main>

  <!-- confirm() / toast() render here; both portal or position themselves
       over the page, so where they sit in the tree only matters for staying
       mounted across route changes. ConfirmHost is a lazy chunk: bits-ui
       roughly doubled the entry bundle when it was imported eagerly. -->
  {#if hasPendingConfirm()}
    {#await import("./lib/ui/ConfirmHost.svelte") then m}<m.default />{:catch}{onConfirmHostFailed()}{/await}
  {/if}

  <footer class="mt-12 border-t border-border py-4 text-xs text-muted">
    <span title={buildStamp ?? "no build stamp (dev)"}>
      Filearr {__APP_VERSION__}{#if buildStamp}
        <span class="font-mono text-muted"> · build {buildStamp.slice(0, 12)}</span>
      {/if}
    </span>
    <span class="px-1">·</span>
    <a class="underline hover:text-accent-text" href="#/help">Help</a>
    <span class="px-1">·</span>
    <!-- The running build stack: every dependency version this deployment is
         actually running, for pasting into a bug report. -->
    <a class="underline hover:text-accent-text" href="#/about">About</a>
    <span class="px-1">·</span>
    <!-- Bundled mkdocs manual served by this instance (main.py /docs mount). -->
    <a
      class="underline hover:text-accent-text"
      href="/docs/"
      target="_blank"
      rel="noopener noreferrer">Docs</a>
    <span class="px-1">·</span>
    <a
      class="underline hover:text-accent-text"
      href="/api/docs"
      target="_blank"
      rel="noopener noreferrer">API Docs</a>
    <span class="px-1">·</span>
    <!-- AGPL-3.0 §13: offer the running instance's Corresponding Source. -->
    {#if sourceHref}
      <a
        class="underline hover:text-accent-text"
        href={sourceHref}
        target="_blank"
        rel="noopener noreferrer">Source</a>
    {:else}
      <span>Source: {sourceUrl}</span>
    {/if}
    <span class="px-1">·</span>
    <span>AGPL-3.0-or-later</span>
  </footer>
</div>
{/if}
