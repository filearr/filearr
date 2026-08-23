<script lang="ts">
  import { onMount } from "svelte";
  import {
    listUsers, createUser, updateUser, deleteUser, listRoles,
    friendlyError, type AuthPrincipal, type RoleDef,
  } from "./api";

  // P6-T12 user management (admin). Lists local + federated accounts with a
  // provider + kind badge, a role selector, a disable toggle, password reset and
  // delete. Federated (ldap/oidc/saml) accounts have no local password, so the
  // reset action is hidden for them.
  let error = $state("");
  let users = $state<AuthPrincipal[]>([]);
  // Roles come from the server (builtins + operator-defined) — never hard-code.
  let roles = $state<RoleDef[]>([]);

  // create form
  let nuName = $state("");
  let nuPass = $state("");
  let nuRole = $state("user");
  let nuEmail = $state("");

  // per-user details drawer (profile + session-timeout overrides)
  let openFor = $state<string | null>(null);
  let dDisplay = $state("");
  let dPhone = $state("");
  let dIdle = $state("");
  let dTtl = $state("");
  let dSaved = $state(false);

  // password reset
  let resetFor = $state<string | null>(null);
  let resetPass = $state("");

  async function refresh() {
    error = "";
    try {
      users = await listUsers();
    } catch (e) {
      error = friendlyError(e);
    }
    try {
      roles = await listRoles();
    } catch {
      roles = [];
    }
    if (roles.length && !roles.some((r) => r.name === nuRole)) nuRole = roles[0].name;
  }
  onMount(refresh);

  function openDetails(u: AuthPrincipal) {
    if (openFor === u.id) { openFor = null; return; }
    openFor = u.id;
    dSaved = false;
    dDisplay = u.display_name ?? "";
    dPhone = u.phone ?? "";
    dIdle = u.session_inactivity_hours == null ? "" : String(u.session_inactivity_hours);
    dTtl = u.session_ttl_hours == null ? "" : String(u.session_ttl_hours);
  }

  /** Blank or 0 -> 0 (clears the override); otherwise the parsed hours. */
  function hoursOrClear(v: string): number | null {
    const t = v.trim();
    if (t === "") return 0;
    const n = Number(t);
    return Number.isFinite(n) && n >= 0 ? n : null;
  }

  async function saveDetails(u: AuthPrincipal, e: Event) {
    e.preventDefault();
    error = "";
    const idle = hoursOrClear(dIdle);
    const ttl = hoursOrClear(dTtl);
    if (idle === null || ttl === null) { error = "Timeouts must be numbers of hours (0 or blank clears)."; return; }
    try {
      await updateUser(u.id, {
        display_name: dDisplay.trim(),
        phone: dPhone.trim(),
        session_inactivity_hours: idle,
        session_ttl_hours: ttl,
      });
      dSaved = true;
      await refresh();
    } catch (err) {
      error = friendlyError(err, "save");
    }
  }

  async function addUser(e: Event) {
    e.preventDefault();
    error = "";
    try {
      await createUser({
        username: nuName.trim(),
        password: nuPass,
        global_role: nuRole,
        email: nuEmail.trim() || null,
      });
      nuName = ""; nuPass = ""; nuEmail = "";
      nuRole = roles.some((r) => r.name === "user") ? "user" : (roles[0]?.name ?? "user");
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function setRole(u: AuthPrincipal, role: string) {
    error = "";
    try {
      await updateUser(u.id, { global_role: role });
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function toggleDisabled(u: AuthPrincipal) {
    error = "";
    try {
      await updateUser(u.id, { disabled: !u.disabled });
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function doReset(u: AuthPrincipal) {
    error = "";
    try {
      await updateUser(u.id, { password: resetPass });
      resetFor = null; resetPass = "";
    } catch (e) {
      error = friendlyError(e);
    }
  }

  async function remove(u: AuthPrincipal) {
    if (!confirm(`Delete user "${u.username}"? This cannot be undone.`)) return;
    error = "";
    try {
      await deleteUser(u.id);
      await refresh();
    } catch (e) {
      error = friendlyError(e);
    }
  }

  function providerBadge(p: string | undefined): string {
    return p && p !== "local" ? p.toUpperCase() : "local";
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Users</h2>
  <p class="text-xs text-slate-500">
    Local and federated accounts. Roles are defined in the Roles panel; changing a
    user's role signs them out everywhere. Federated accounts sign in through
    their identity provider.
  </p>

  {#if error}
    <p class="mt-2 text-sm text-red-600">{error}</p>
  {/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase text-slate-400">
        <tr>
          <th class="py-1 pr-3">User</th>
          <th class="py-1 pr-3">Source</th>
          <th class="py-1 pr-3">Role</th>
          <th class="py-1 pr-3">Status</th>
          <th class="py-1 pr-3"></th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each users as u (u.id)}
          <tr>
            <td class="py-2 pr-3">
              {#if u.display_name}
                <span class="font-medium">{u.display_name}</span>
                <div class="text-xs text-slate-400">{u.username}{#if u.email} · {u.email}{/if}</div>
              {:else}
                <span class="font-medium">{u.username}</span>
                {#if u.email}<span class="ml-1 text-xs text-slate-400">{u.email}</span>{/if}
              {/if}
            </td>
            <td class="py-2 pr-3">
              <span class="rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">
                {providerBadge(u.auth_provider)}
              </span>
              {#if u.kind && u.kind !== "user"}
                <span class="ml-1 rounded bg-indigo-100 px-1.5 py-0.5 text-xs text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">
                  {u.kind}
                </span>
              {/if}
            </td>
            <td class="py-2 pr-3">
              <select
                class="rounded border border-slate-300 bg-transparent px-1 py-0.5 text-sm dark:border-slate-700"
                value={u.global_role}
                onchange={(e) => setRole(u, (e.currentTarget as HTMLSelectElement).value)}>
                {#each roles as r (r.name)}
                  <option value={r.name}>{r.display_name || r.name}</option>
                {/each}
                {#if !roles.some((r) => r.name === u.global_role)}
                  <option value={u.global_role}>{u.global_role}</option>
                {/if}
              </select>
            </td>
            <td class="py-2 pr-3">
              {#if u.disabled}
                <span class="text-amber-600">disabled</span>
              {:else}
                <span class="text-emerald-600">active</span>
              {/if}
            </td>
            <td class="py-2 pr-3 text-right">
              <button class="text-xs text-slate-500 underline" title="Profile and session-timeout overrides"
                onclick={() => openDetails(u)}>{openFor === u.id ? "close" : "…"}</button>
              <button class="ml-2 text-xs text-slate-500 underline" onclick={() => toggleDisabled(u)}>
                {u.disabled ? "enable" : "disable"}
              </button>
              {#if !u.auth_provider || u.auth_provider === "local"}
                <button class="ml-2 text-xs text-slate-500 underline"
                  onclick={() => { resetFor = resetFor === u.id ? null : u.id; resetPass = ""; }}>
                  reset password
                </button>
              {/if}
              <button class="ml-2 text-xs text-red-600 underline" onclick={() => remove(u)}>
                delete
              </button>
              {#if resetFor === u.id}
                <div class="mt-1 flex items-center gap-2">
                  <input
                    class="rounded border border-slate-300 px-2 py-0.5 text-sm dark:border-slate-700 dark:bg-slate-900"
                    type="password" placeholder="new password (min 8)"
                    bind:value={resetPass} />
                  <button class="rounded bg-[var(--accent)] px-2 py-0.5 text-xs text-white"
                    disabled={resetPass.length < 8}
                    onclick={() => doReset(u)}>save</button>
                </div>
              {/if}
            </td>
          </tr>
          {#if openFor === u.id}
            <tr class="bg-slate-50 dark:bg-slate-900/40">
              <td colspan="5" class="p-3">
                <form onsubmit={(e) => saveDetails(u, e)} class="flex flex-wrap items-end gap-2">
                  <label class="flex flex-col gap-1 text-sm">
                    <span class="text-xs text-slate-500">Display name</span>
                    <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
                      bind:value={dDisplay} />
                  </label>
                  <label class="flex flex-col gap-1 text-sm">
                    <span class="text-xs text-slate-500">Phone</span>
                    <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
                      bind:value={dPhone} />
                  </label>
                  <label class="flex flex-col gap-1 text-sm">
                    <span class="text-xs text-slate-500">Idle timeout (h)</span>
                    <input class="w-28 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
                      type="number" min="0" step="any" placeholder="global" bind:value={dIdle} />
                  </label>
                  <label class="flex flex-col gap-1 text-sm">
                    <span class="text-xs text-slate-500">Absolute lifetime (h)</span>
                    <input class="w-28 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
                      type="number" min="0" step="any" placeholder="global" bind:value={dTtl} />
                  </label>
                  <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white">Save</button>
                  {#if dSaved}<span class="text-xs text-emerald-600">saved</span>{/if}
                  <p class="basis-full text-xs text-slate-500">
                    Timeouts override the global setting for this user only — blank or 0
                    clears the override (uses global). Idle changes apply live; the absolute
                    lifetime applies to new sessions.
                    {#if u.auth_provider && u.auth_provider !== "local"}
                      Federated account: display name may be re-synced by the identity provider.
                    {/if}
                  </p>
                </form>
                {#if u.auth_provider && u.auth_provider !== "local"}
                  {@const prof = (u.external_profile ?? {}) as Record<string, unknown>}
                  {@const groups = Array.isArray(prof.groups) ? (prof.groups as string[]) : []}
                  <div class="mt-3 border-t border-slate-200 pt-2 dark:border-slate-800">
                    <div class="mb-1 text-xs font-semibold uppercase tracking-wide text-slate-500">Identity source ({u.auth_provider})</div>
                    <dl class="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-0.5 text-xs">
                      {#if u.external_issuer}<dt class="text-slate-500">Server / issuer</dt><dd class="font-mono break-all">{u.external_issuer}</dd>{/if}
                      {#if u.external_subject}<dt class="text-slate-500">Stable subject</dt><dd class="font-mono break-all">{u.external_subject}</dd>{/if}
                      {#if prof.dn}<dt class="text-slate-500">Distinguished name</dt><dd class="font-mono break-all">{prof.dn}</dd>{/if}
                      {#if prof.upn}<dt class="text-slate-500">UPN</dt><dd class="font-mono break-all">{prof.upn}</dd>{/if}
                      {#if prof.netbios}<dt class="text-slate-500">NetBIOS name</dt><dd class="font-mono break-all">{prof.netbios}</dd>{/if}
                      {#if prof.sam}<dt class="text-slate-500">Account name</dt><dd class="font-mono break-all">{prof.sam}</dd>{/if}
                      {#if prof.mapped_role}<dt class="text-slate-500">Role from directory</dt><dd>{prof.mapped_role} <span class="text-slate-400">(re-applied at every login — a manual role change lasts until then)</span></dd>{/if}
                      {#if u.last_login_at}<dt class="text-slate-500">Last login</dt><dd>{new Date(u.last_login_at).toLocaleString()}</dd>{/if}
                      {#if groups.length}
                        <dt class="text-slate-500">Directory groups</dt>
                        <dd class="flex flex-wrap gap-1">
                          {#each groups as g (g)}<span class="rounded bg-slate-100 px-1.5 py-0.5 dark:bg-slate-800">{g}</span>{/each}
                        </dd>
                      {/if}
                    </dl>
                  </div>
                {/if}
              </td>
            </tr>
          {/if}
        {/each}
        {#if users.length === 0}
          <tr><td colspan="5" class="py-3 text-slate-400">No users yet.</td></tr>
        {/if}
      </tbody>
    </table>
  </div>

  <form onsubmit={addUser} class="mt-4 flex flex-wrap items-end gap-2">
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Username</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        bind:value={nuName} required />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Password (min 8)</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        type="password" bind:value={nuPass} minlength="8" required />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Email (optional)</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900"
        bind:value={nuEmail} />
    </label>
    <label class="flex flex-col gap-1 text-sm">
      <span class="text-xs text-slate-500">Role</span>
      <select class="rounded border border-slate-300 bg-transparent px-2 py-1 dark:border-slate-700"
        bind:value={nuRole}>
        {#each roles as r (r.name)}
          <option value={r.name}>{r.display_name || r.name}</option>
        {:else}
          <option value="user">user</option>
        {/each}
      </select>
    </label>
    <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white"
      disabled={nuName.trim().length === 0 || nuPass.length < 8}>Add user</button>
  </form>
</section>
