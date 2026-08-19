<script lang="ts">
  import { onMount } from "svelte";
  import {
    listMySessions, revokeMySession, revokeAllMySessions,
    listUsers, listUserSessions, revokeUserSessions,
    getSessionSettings, patchSessionSettings,
    friendlyError, type AuthPrincipal, type AuthSession, type SessionSettings,
  } from "./api";

  // P6-T11 session management. Two views of one panel (2026-08-19 split):
  //   "mine"  — the signed-in user's OWN active sessions ("log out everywhere"),
  //             rendered on the Account page;
  //   "admin" — global session timeouts + force sign-out of any user, rendered
  //             on the Admin page.
  // Revocation takes effect on that session's very next request
  // (Postgres-backed instant revocation).
  let { view = "mine" }: { view?: "mine" | "admin" } = $props();
  const isAdmin = $derived(view === "admin");

  let error = $state("");
  let mine = $state<AuthSession[]>([]);

  // admin view
  let users = $state<AuthPrincipal[]>([]);
  let selected = $state("");
  let theirs = $state<AuthSession[]>([]);

  // admin: global session-timeout settings (runtime override of the env
  // defaults; 0 clears back to env). Per-user overrides live in UsersPanel.
  let ss = $state<SessionSettings | null>(null);
  let ssIdle = $state("");
  let ssTtl = $state("");
  let ssSaved = $state(false);

  async function refreshSettings() {
    try {
      ss = await getSessionSettings();
      ssIdle = String(ss.inactivity_hours);
      ssTtl = String(ss.ttl_hours);
    } catch (e) {
      error = friendlyError(e);
    }
  }
  /** Trim float noise (0.08333… → 0.08) without padding whole numbers. */
  function fmtHours(h: number): string {
    return String(Math.round(h * 100) / 100);
  }
  function sourceLabel(src: "env" | "global"): string {
    return src === "env" ? "from env default" : "set here";
  }
  async function saveSettings(e: Event) {
    e.preventDefault();
    error = ""; ssSaved = false;
    const idle = Number(ssIdle), ttl = Number(ssTtl);
    if (!Number.isFinite(idle) || !Number.isFinite(ttl)) { error = "Timeouts must be numbers of hours."; return; }
    // Send only what changed: re-sending an untouched env value would turn it
    // into a "global" override equal to the env default (harmless but noisy).
    const patch: { inactivity_hours?: number; ttl_hours?: number } = {};
    if (ss && idle !== ss.inactivity_hours) patch.inactivity_hours = idle;
    if (ss && ttl !== ss.ttl_hours) patch.ttl_hours = ttl;
    if (!Object.keys(patch).length) { ssSaved = true; return; }
    try {
      ss = await patchSessionSettings(patch);
      ssIdle = String(ss.inactivity_hours); ssTtl = String(ss.ttl_hours);
      ssSaved = true;
    } catch (err) {
      error = friendlyError(err, "save");
    }
  }
  async function resetSetting(which: "inactivity_hours" | "ttl_hours") {
    error = ""; ssSaved = false;
    try {
      ss = await patchSessionSettings({ [which]: 0 });
      ssIdle = String(ss.inactivity_hours); ssTtl = String(ss.ttl_hours);
    } catch (err) {
      error = friendlyError(err, "save");
    }
  }

  async function refreshMine() {
    error = "";
    try {
      mine = await listMySessions();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function refreshUsers() {
    try {
      users = await listUsers();
    } catch {
      users = [];
    }
  }

  async function loadTheirs() {
    theirs = [];
    if (!selected) return;
    try {
      theirs = await listUserSessions(selected);
    } catch (e) {
      error = friendlyError(e);
    }
  }

  onMount(() => {
    if (isAdmin) { refreshUsers(); refreshSettings(); }
    else refreshMine();
  });

  async function revokeOne(id: string) {
    try {
      await revokeMySession(id);
      await refreshMine();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function revokeAll() {
    if (!confirm("Sign out of ALL your sessions, including this one?")) return;
    try {
      await revokeAllMySessions();
      // This kills the current session too — reload to hit the login wall.
      location.reload();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function forceRevoke() {
    if (!selected) return;
    const u = users.find((x) => x.id === selected);
    if (!confirm(`Force sign-out ALL sessions for "${u?.username ?? selected}"?`)) return;
    try {
      await revokeUserSessions(selected);
      await loadTheirs();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  function when(ts: string): string {
    try { return new Date(ts).toLocaleString(); } catch { return ts; }
  }
  function ua(s: string | null): string {
    return s ? (s.length > 60 ? s.slice(0, 60) + "…" : s) : "unknown client";
  }
</script>

<section class="mt-8">
  {#if isAdmin}
    <h2 class="text-lg font-semibold">Sessions</h2>
    <p class="text-xs text-slate-500">
      Global timeouts and forced sign-out. Your own sessions are on the Account page.
    </p>
  {:else}
    <h2 class="text-lg font-semibold">Active sessions</h2>
    <p class="text-xs text-slate-500">
      Revoking a session signs it out on its next request — no waiting for expiry.
    </p>
  {/if}

  {#if error}
    <p class="mt-2 text-sm text-red-600">{error}</p>
  {/if}

  {#if !isAdmin}
  <ul class="mt-3 divide-y divide-slate-200 text-sm dark:divide-slate-800">
    {#each mine as s (s.id)}
      <li class="flex items-center gap-3 py-2">
        <div class="grow">
          <span class="font-mono text-xs text-slate-500">{s.ip_address ?? "—"}</span>
          <span class="ml-2">{ua(s.user_agent)}</span>
          {#if s.current}
            <span class="ml-2 rounded bg-emerald-100 px-1.5 py-0.5 text-xs text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">this session</span>
          {/if}
          <div class="text-xs text-slate-400">last seen {when(s.last_seen_at)}</div>
        </div>
        {#if !s.current}
          <button class="text-xs text-red-600 underline" onclick={() => revokeOne(s.id)}>revoke</button>
        {/if}
      </li>
    {/each}
    {#if mine.length === 0}
      <li class="py-2 text-slate-400">No active sessions.</li>
    {/if}
  </ul>

  <button class="mt-3 rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
    onclick={revokeAll}>Log out everywhere</button>
  {/if}

  {#if isAdmin}
    <div class="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <h3 class="text-sm font-semibold">Session timeouts (global)</h3>
      <p class="text-xs text-slate-500">
        Runtime override of the env defaults. Idle changes apply live to existing
        sessions; the absolute lifetime applies to new sessions. Per-user overrides
        live in the Users panel and win over these.
      </p>
      {#if ss}
        <form onsubmit={saveSettings} class="mt-2 flex flex-wrap items-end gap-3">
          <label class="flex flex-col gap-1 text-sm">
            <span class="text-xs text-slate-500">Idle timeout (hours)</span>
            <input class="w-32 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
              type="number" step="any" min={ss.min_hours} max={ss.max_hours} bind:value={ssIdle} />
            <span class="text-xs text-slate-400">
              currently: {ss.inactivity_hours} h — {sourceLabel(ss.inactivity_source)}
              {#if ss.inactivity_source !== "env"}(env default {ss.env_inactivity_hours} h){/if}
            </span>
            <button type="button" class="self-start text-xs text-slate-500 underline disabled:opacity-40"
              disabled={ss.inactivity_source === "env"}
              onclick={() => resetSetting("inactivity_hours")}>Reset to default</button>
          </label>
          <label class="flex flex-col gap-1 text-sm">
            <span class="text-xs text-slate-500">Absolute lifetime (hours)</span>
            <input class="w-32 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
              type="number" step="any" min={ss.min_hours} max={ss.max_hours} bind:value={ssTtl} />
            <span class="text-xs text-slate-400">
              currently: {ss.ttl_hours} h — {sourceLabel(ss.ttl_source)}
              {#if ss.ttl_source !== "env"}(env default {ss.env_ttl_hours} h){/if}
            </span>
            <button type="button" class="self-start text-xs text-slate-500 underline disabled:opacity-40"
              disabled={ss.ttl_source === "env"}
              onclick={() => resetSetting("ttl_hours")}>Reset to default</button>
          </label>
          <div class="flex items-center gap-2 pb-6">
            <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white">Save</button>
            {#if ssSaved}<span class="text-xs text-emerald-600">saved</span>{/if}
          </div>
          <p class="basis-full text-xs text-slate-400">
            Allowed range {fmtHours(ss.min_hours)}–{fmtHours(ss.max_hours)} h; 0 clears back to the env default.
          </p>
        </form>
      {:else}
        <p class="mt-2 text-xs text-slate-400">Loading…</p>
      {/if}
    </div>

    <div class="mt-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <h3 class="text-sm font-semibold">Admin: force sign-out a user</h3>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <select class="rounded border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          bind:value={selected} onchange={loadTheirs}>
          <option value="">Select a user…</option>
          {#each users as u (u.id)}
            <option value={u.id}>{u.username} ({u.global_role})</option>
          {/each}
        </select>
        <button class="rounded-lg border border-red-300 px-3 py-1 text-sm text-red-600 disabled:opacity-40 dark:border-red-800"
          disabled={!selected || theirs.length === 0}
          onclick={forceRevoke}>Revoke all their sessions</button>
      </div>
      {#if selected}
        <ul class="mt-2 divide-y divide-slate-200 text-sm dark:divide-slate-800">
          {#each theirs as s (s.id)}
            <li class="py-1.5">
              <span class="font-mono text-xs text-slate-500">{s.ip_address ?? "—"}</span>
              <span class="ml-2">{ua(s.user_agent)}</span>
              <span class="ml-2 text-xs text-slate-400">last seen {when(s.last_seen_at)}</span>
            </li>
          {/each}
          {#if theirs.length === 0}
            <li class="py-1.5 text-slate-400">No active sessions.</li>
          {/if}
        </ul>
      {/if}
    </div>
  {/if}
</section>
