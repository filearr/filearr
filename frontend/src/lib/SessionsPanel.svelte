<script lang="ts">
  import { onMount } from "svelte";
  import {
    listMySessions, revokeMySession, revokeAllMySessions,
    listUsers, listUserSessions, revokeUserSessions,
    getSessionSettings, patchSessionSettings,
    friendlyError, type AuthPrincipal, type AuthSession, type SessionSettings,
  } from "./api";
  import { numOrNull } from "./numInput";
  import { Alert, Badge, Button, DataTable, Field, Input, Select, StateBlock, confirm } from "./ui";

  // P6-T11 session management. Two views of one panel (2026-08-19 split):
  //   "mine"  — the signed-in user's OWN active sessions ("log out everywhere"),
  //             rendered on the Account page;
  //   "admin" — global session timeouts + force sign-out of any user, rendered
  //             on the Admin page.
  // Revocation takes effect on that session's very next request
  // (Postgres-backed instant revocation).
  let { view = "mine" }: { view?: "mine" | "admin" } = $props();
  const isAdmin = $derived(view === "admin");

  let mine = $state<AuthSession[]>([]);
  let mineLoading = $state(false);
  let mineError = $state(""); // load of the own-sessions list
  let revokeError = $state(""); // revoke one (row action)
  let revokeAllError = $state("");

  // admin view
  let users = $state<AuthPrincipal[]>([]);
  let selected = $state("");
  let theirs = $state<AuthSession[]>([]);
  let theirsLoading = $state(false);
  let theirsError = $state(""); // load of the selected user's sessions
  let forceError = $state("");

  // admin: global session-timeout settings (runtime override of the env
  // defaults; 0 clears back to env). Per-user overrides live in UsersPanel.
  let ss = $state<SessionSettings | null>(null);
  // type="number" bind:value — a number, or null when emptied (= unchanged).
  let ssIdle = $state<number | null>(null);
  let ssTtl = $state<number | null>(null);
  let ssSaved = $state(false);
  let ssLoadError = $state("");
  let ssSaveError = $state("");
  // A reset failure shows under the reset button that raised it.
  let ssResetError = $state<{ which: "inactivity_hours" | "ttl_hours"; message: string } | null>(null);

  async function refreshSettings() {
    ssLoadError = "";
    try {
      ss = await getSessionSettings();
      ssIdle = ss.inactivity_hours;
      ssTtl = ss.ttl_hours;
    } catch (e) {
      ssLoadError = friendlyError(e);
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
    ssSaveError = ""; ssSaved = false;
    // Blank (null) = leave as is; 0 clears back to env; else within range.
    const idle = numOrNull(ssIdle), ttl = numOrNull(ssTtl);
    const bad = (n: number | null) =>
      n !== null && n !== 0 && (!ss || n < ss.min_hours || n > ss.max_hours);
    if (bad(idle) || bad(ttl)) {
      ssSaveError = ss
        ? `Timeouts must be ${fmtHours(ss.min_hours)}–${fmtHours(ss.max_hours)} hours, or 0 to clear.`
        : "Timeouts must be numbers of hours.";
      return;
    }
    // Send only what changed: re-sending an untouched env value would turn it
    // into a "global" override equal to the env default (harmless but noisy).
    const patch: { inactivity_hours?: number; ttl_hours?: number } = {};
    if (ss && idle !== null && idle !== ss.inactivity_hours) patch.inactivity_hours = idle;
    if (ss && ttl !== null && ttl !== ss.ttl_hours) patch.ttl_hours = ttl;
    if (!Object.keys(patch).length) { ssSaved = true; return; }
    try {
      ss = await patchSessionSettings(patch);
      ssIdle = ss.inactivity_hours; ssTtl = ss.ttl_hours;
      ssSaved = true;
    } catch (err) {
      ssSaveError = friendlyError(err, "save");
    }
  }
  async function resetSetting(which: "inactivity_hours" | "ttl_hours") {
    ssResetError = null; ssSaved = false;
    try {
      ss = await patchSessionSettings({ [which]: 0 });
      ssIdle = ss.inactivity_hours; ssTtl = ss.ttl_hours;
    } catch (err) {
      ssResetError = { which, message: friendlyError(err, "save") };
    }
  }

  async function refreshMine() {
    mineError = "";
    mineLoading = true;
    try {
      mine = await listMySessions();
    } catch (e) {
      mineError = friendlyError(e);
    }
    mineLoading = false;
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
    theirsError = "";
    if (!selected) return;
    theirsLoading = true;
    try {
      theirs = await listUserSessions(selected);
    } catch (e) {
      theirsError = friendlyError(e);
    }
    theirsLoading = false;
  }

  onMount(() => {
    if (isAdmin) { refreshUsers(); refreshSettings(); }
    else refreshMine();
  });

  async function revokeOne(id: string) {
    revokeError = "";
    try {
      await revokeMySession(id);
      await refreshMine();
    } catch (e) {
      revokeError = friendlyError(e);
    }
  }

  async function revokeAll() {
    const ok = await confirm({
      title: "Log out everywhere?",
      message: "Sign out of ALL your sessions, including this one?",
      confirmLabel: "Log out everywhere",
      tone: "danger",
    });
    if (!ok) return;
    revokeAllError = "";
    try {
      await revokeAllMySessions();
      // This kills the current session too — reload to hit the login wall.
      location.reload();
    } catch (e) {
      revokeAllError = friendlyError(e);
    }
  }

  async function forceRevoke() {
    if (!selected) return;
    const u = users.find((x) => x.id === selected);
    const ok = await confirm({
      title: "Force sign-out?",
      message: `Force sign-out ALL sessions for "${u?.username ?? selected}"?`,
      confirmLabel: "Revoke all their sessions",
      tone: "danger",
    });
    if (!ok) return;
    forceError = "";
    try {
      await revokeUserSessions(selected);
      await loadTheirs();
    } catch (e) {
      forceError = friendlyError(e);
    }
  }

  function when(ts: string): string {
    try { return new Date(ts).toLocaleString(); } catch { return ts; }
  }
  function ua(s: string | null): string {
    return s ? (s.length > 60 ? s.slice(0, 60) + "…" : s) : "unknown client";
  }
  function currentHint(hours: number, src: "env" | "global", envHours: number): string {
    return `currently: ${hours} h — ${sourceLabel(src)}${src !== "env" ? ` (env default ${envHours} h)` : ""}`;
  }
</script>

{#snippet clientCell(s: AuthSession)}
  <span class="font-mono text-xs text-muted">{s.ip_address ?? "—"}</span>
  <span class="ml-2">{ua(s.user_agent)}</span>
  {#if s.current}<Badge tone="ok" class="ml-2">this session</Badge>{/if}
{/snippet}
{#snippet lastSeenCell(s: AuthSession)}<span class="text-xs text-muted">{when(s.last_seen_at)}</span>{/snippet}
{#snippet revokeCell(s: AuthSession)}
  {#if !s.current}
    <div class="flex justify-end">
      <Button size="sm" variant="danger" onclick={() => revokeOne(s.id)}>revoke</Button>
    </div>
  {/if}
{/snippet}

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

  {#if !isAdmin}
    <DataTable
      class="mt-3"
      caption="Your active sessions"
      captionHidden
      columns={[
        { key: "user_agent", label: "Client", cell: clientCell },
        { key: "last_seen_at", label: "Last seen", cell: lastSeenCell },
        { key: "actions", label: "Actions", align: "right", cell: revokeCell },
      ]}
      rows={mine}
      rowKey={(s) => s.id}
      loading={mineLoading}
      error={mineError || null}
      onRetry={refreshMine}
      emptyMessage="No active sessions. Sessions appear here once you sign in."
    />
    {#if revokeError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (revokeError = "")}>{revokeError}</Alert>
    {/if}

    <Button class="mt-3" onclick={revokeAll}>Log out everywhere</Button>
    {#if revokeAllError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (revokeAllError = "")}>{revokeAllError}</Alert>
    {/if}
  {/if}

  {#if isAdmin}
    <div class="mt-3 rounded-control border border-border p-3">
      <h3 class="text-sm font-semibold">Session timeouts (global)</h3>
      <p class="text-xs text-muted">
        Runtime override of the env defaults. Idle changes apply live to existing
        sessions; the absolute lifetime applies to new sessions. Per-user overrides
        live in the Users panel and win over these.
      </p>
      {#if ss}
        <form onsubmit={saveSettings} class="mt-2 flex flex-wrap items-start gap-3">
          <div class="flex flex-col items-start gap-1">
            <Field label="Idle timeout (hours)" class="w-56"
              hint={currentHint(ss.inactivity_hours, ss.inactivity_source, ss.env_inactivity_hours)}>
              <Input type="number" step="any" min="0" max={ss.max_hours} bind:value={ssIdle} />
            </Field>
            <Button size="sm" variant="ghost" disabled={ss.inactivity_source === "env"}
              onclick={() => resetSetting("inactivity_hours")}>Reset to default</Button>
            {#if ssResetError?.which === "inactivity_hours"}
              <Alert tone="danger" onDismiss={() => (ssResetError = null)}>{ssResetError.message}</Alert>
            {/if}
          </div>
          <div class="flex flex-col items-start gap-1">
            <Field label="Absolute lifetime (hours)" class="w-56"
              hint={currentHint(ss.ttl_hours, ss.ttl_source, ss.env_ttl_hours)}>
              <Input type="number" step="any" min="0" max={ss.max_hours} bind:value={ssTtl} />
            </Field>
            <Button size="sm" variant="ghost" disabled={ss.ttl_source === "env"}
              onclick={() => resetSetting("ttl_hours")}>Reset to default</Button>
            {#if ssResetError?.which === "ttl_hours"}
              <Alert tone="danger" onDismiss={() => (ssResetError = null)}>{ssResetError.message}</Alert>
            {/if}
          </div>
          <div class="flex items-center gap-2 pt-5">
            <Button type="submit" variant="primary">Save</Button>
            {#if ssSaved}<span class="text-xs text-ok" role="status">saved</span>{/if}
          </div>
          {#if ssSaveError}
            <Alert tone="danger" class="basis-full" onDismiss={() => (ssSaveError = "")}>{ssSaveError}</Alert>
          {/if}
          <p class="basis-full text-xs text-muted">
            Allowed range {fmtHours(ss.min_hours)}–{fmtHours(ss.max_hours)} h; 0 clears back to the env default.
          </p>
        </form>
      {:else if ssLoadError}
        <StateBlock state="error" compact message={ssLoadError} onRetry={refreshSettings} />
      {:else}
        <StateBlock state="loading" compact />
      {/if}
    </div>

    <div class="mt-4 rounded-control border border-border p-3">
      <h3 class="text-sm font-semibold">Admin: force sign-out a user</h3>
      <div class="mt-2 flex flex-wrap items-center gap-2">
        <div class="w-64">
          <Select aria-label="User" bind:value={selected} onchange={loadTheirs}>
            <option value="">Select a user…</option>
            {#each users as u (u.id)}
              <option value={u.id}>{u.username} ({u.global_role})</option>
            {/each}
          </Select>
        </div>
        <Button variant="danger" disabled={!selected || theirs.length === 0}
          onclick={forceRevoke}>Revoke all their sessions</Button>
      </div>
      {#if forceError}
        <Alert tone="danger" class="mt-2" onDismiss={() => (forceError = "")}>{forceError}</Alert>
      {/if}
      {#if selected}
        <DataTable
          class="mt-2"
          caption="Sessions of the selected user"
          captionHidden
          columns={[
            { key: "user_agent", label: "Client", cell: clientCell },
            { key: "last_seen_at", label: "Last seen", cell: lastSeenCell },
          ]}
          rows={theirs}
          rowKey={(s) => s.id}
          loading={theirsLoading}
          error={theirsError || null}
          onRetry={loadTheirs}
          emptyMessage="No active sessions. This user has nothing to sign out."
        />
      {/if}
    </div>
  {/if}
</section>
