<script lang="ts">
  import { onMount } from "svelte";
  import {
    listUsers, createUser, updateUser, deleteUser, listRoles,
    friendlyError, type AuthPrincipal, type RoleDef,
  } from "./api";
  import { numOrNull } from "./numInput";
  import { Alert, Badge, Button, DataTable, Field, Input, Select, confirm, toneForStatus } from "./ui";

  // P6-T12 user management (admin). Lists local + federated accounts with a
  // provider + kind badge, a role selector, a disable toggle, password reset and
  // delete. Federated (ldap/oidc/saml) accounts have no local password, so the
  // reset action is hidden for them. The details and password-reset editors
  // render below the table, keyed by the selected user.
  let loadError = $state("");
  let loading = $state(false);
  let users = $state<AuthPrincipal[]>([]);
  // Roles come from the server (builtins + operator-defined) — never hard-code.
  let roles = $state<RoleDef[]>([]);

  // Action errors, each shown next to the control that triggered it.
  let rowError = $state(""); // role change, enable/disable, delete (row actions)
  let addError = $state("");
  let detailsError = $state("");
  let resetError = $state("");

  // create form
  let nuName = $state("");
  let nuPass = $state("");
  let nuRole = $state("user");
  let nuEmail = $state("");

  // per-user details editor (profile + session-timeout overrides)
  let openFor = $state<string | null>(null);
  let dDisplay = $state("");
  let dPhone = $state("");
  // type="number" inputs: bind:value writes a number, or null when emptied.
  let dIdle = $state<number | null>(null);
  let dTtl = $state<number | null>(null);
  let dSaved = $state(false);

  // password reset
  let resetFor = $state<string | null>(null);
  let resetPass = $state("");

  const detailsUser = $derived(openFor ? users.find((u) => u.id === openFor) ?? null : null);
  const resetUser = $derived(resetFor ? users.find((u) => u.id === resetFor) ?? null : null);

  async function refresh() {
    loadError = "";
    loading = true;
    try {
      users = await listUsers();
    } catch (e) {
      loadError = friendlyError(e);
    }
    try {
      roles = await listRoles();
    } catch {
      roles = [];
    }
    loading = false;
    if (roles.length && !roles.some((r) => r.name === nuRole)) nuRole = roles[0].name;
  }
  onMount(refresh);

  function openDetails(u: AuthPrincipal) {
    if (openFor === u.id) { openFor = null; return; }
    openFor = u.id;
    dSaved = false;
    detailsError = "";
    dDisplay = u.display_name ?? "";
    dPhone = u.phone ?? "";
    dIdle = u.session_inactivity_hours ?? null;
    dTtl = u.session_ttl_hours ?? null;
  }

  function toggleReset(u: AuthPrincipal) {
    resetFor = resetFor === u.id ? null : u.id;
    resetPass = "";
    resetError = "";
  }

  /** Blank or 0 -> 0 (clears the override); otherwise the parsed hours. */
  function hoursOrClear(v: number | null): number | null {
    if (v === null) return 0;
    const n = numOrNull(v);
    return n !== null && n >= 0 ? n : null;
  }

  async function saveDetails(u: AuthPrincipal, e: Event) {
    e.preventDefault();
    detailsError = "";
    const idle = hoursOrClear(dIdle);
    const ttl = hoursOrClear(dTtl);
    if (idle === null || ttl === null) { detailsError = "Timeouts must be numbers of hours (0 or blank clears)."; return; }
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
      detailsError = friendlyError(err, "save");
    }
  }

  async function addUser(e: Event) {
    e.preventDefault();
    addError = "";
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
      addError = friendlyError(e);
    }
  }

  async function setRole(u: AuthPrincipal, role: string) {
    rowError = "";
    try {
      await updateUser(u.id, { global_role: role });
      await refresh();
    } catch (e) {
      rowError = friendlyError(e);
    }
  }

  async function toggleDisabled(u: AuthPrincipal) {
    rowError = "";
    try {
      await updateUser(u.id, { disabled: !u.disabled });
      await refresh();
    } catch (e) {
      rowError = friendlyError(e);
    }
  }

  async function doReset(u: AuthPrincipal, e: Event) {
    e.preventDefault();
    resetError = "";
    try {
      await updateUser(u.id, { password: resetPass });
      resetFor = null; resetPass = "";
    } catch (e) {
      resetError = friendlyError(e);
    }
  }

  async function remove(u: AuthPrincipal) {
    const ok = await confirm({
      title: `Delete user "${u.username}"?`,
      message: "This cannot be undone.",
      confirmLabel: "Delete user",
      tone: "danger",
    });
    if (!ok) return;
    rowError = "";
    try {
      await deleteUser(u.id);
      await refresh();
    } catch (e) {
      rowError = friendlyError(e);
    }
  }

  function providerBadge(p: string | undefined): string {
    return p && p !== "local" ? p.toUpperCase() : "local";
  }
  const isLocal = (u: AuthPrincipal) => !u.auth_provider || u.auth_provider === "local";
</script>

{#snippet userCell(u: AuthPrincipal)}
  {#if u.display_name}
    <span class="font-medium">{u.display_name}</span>
    <div class="text-xs text-muted">{u.username}{#if u.email} · {u.email}{/if}</div>
  {:else}
    <span class="font-medium">{u.username}</span>
    {#if u.email}<span class="ml-1 text-xs text-muted">{u.email}</span>{/if}
  {/if}
{/snippet}

{#snippet sourceCell(u: AuthPrincipal)}
  <Badge>{providerBadge(u.auth_provider)}</Badge>
  {#if u.kind && u.kind !== "user"}<Badge tone="accent" class="ml-1">{u.kind}</Badge>{/if}
{/snippet}

{#snippet roleCell(u: AuthPrincipal)}
  <Select size="sm" aria-label="Role for {u.username}" value={u.global_role}
    onchange={(e) => setRole(u, e.currentTarget.value)}>
    {#each roles as r (r.name)}
      <option value={r.name}>{r.display_name || r.name}</option>
    {/each}
    {#if !roles.some((r) => r.name === u.global_role)}
      <option value={u.global_role}>{u.global_role}</option>
    {/if}
  </Select>
{/snippet}

{#snippet statusCell(u: AuthPrincipal)}
  {@const status = u.disabled ? "disabled" : "active"}
  <Badge tone={toneForStatus(status)}>{status}</Badge>
{/snippet}

{#snippet actionsCell(u: AuthPrincipal)}
  <div class="flex flex-wrap justify-end gap-1">
    <Button size="sm" variant="ghost" title="Profile and session-timeout overrides"
      aria-label="Profile and session-timeout overrides for {u.username}"
      aria-expanded={openFor === u.id}
      onclick={() => openDetails(u)}>{openFor === u.id ? "close" : "…"}</Button>
    <Button size="sm" variant="ghost" onclick={() => toggleDisabled(u)}>
      {u.disabled ? "enable" : "disable"}
    </Button>
    {#if isLocal(u)}
      <Button size="sm" variant="ghost" aria-expanded={resetFor === u.id} onclick={() => toggleReset(u)}>
        reset password
      </Button>
    {/if}
    <Button size="sm" variant="danger" onclick={() => remove(u)}>delete</Button>
  </div>
{/snippet}

<section class="mt-8">
  <h2 class="text-lg font-semibold">Users</h2>
  <p class="text-xs text-slate-500">
    Local and federated accounts. Roles are defined in the Roles panel; changing a
    user's role signs them out everywhere. Federated accounts sign in through
    their identity provider.
  </p>

  <DataTable
    class="mt-3"
    caption="Users"
    captionHidden
    columns={[
      { key: "username", label: "User", cell: userCell },
      { key: "auth_provider", label: "Source", cell: sourceCell },
      { key: "global_role", label: "Role", cell: roleCell },
      { key: "disabled", label: "Status", cell: statusCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={users}
    rowKey={(u) => u.id}
    {loading}
    error={loadError || null}
    onRetry={refresh}
    emptyMessage="No users yet. Add one with the form below."
  />
  {#if rowError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (rowError = "")}>{rowError}</Alert>
  {/if}

  {#if resetUser}
    {#key resetUser.id}
      <form onsubmit={(e) => doReset(resetUser, e)}
        class="mt-3 rounded-control border border-border bg-surface-2 p-3">
        <h3 class="text-sm font-semibold">Reset password: {resetUser.username}</h3>
        <div class="mt-2 flex flex-wrap items-end gap-2">
          <Field label="New password (min 8)" class="w-64">
            <Input type="password" autocomplete="new-password" bind:value={resetPass} />
          </Field>
          <Button type="submit" variant="primary" disabled={resetPass.length < 8}>save</Button>
          <Button variant="secondary" onclick={() => toggleReset(resetUser)}>cancel</Button>
        </div>
        {#if resetError}
          <Alert tone="danger" class="mt-2" onDismiss={() => (resetError = "")}>{resetError}</Alert>
        {/if}
      </form>
    {/key}
  {/if}

  {#if detailsUser}
    {#key detailsUser.id}
      {@const u = detailsUser}
      <div class="mt-3 rounded-control border border-border bg-surface-2 p-3">
        <h3 class="text-sm font-semibold">Details: {u.display_name || u.username}</h3>
        <form onsubmit={(e) => saveDetails(u, e)} class="mt-2 flex flex-wrap items-end gap-2">
          <Field label="Display name"><Input bind:value={dDisplay} /></Field>
          <Field label="Phone"><Input bind:value={dPhone} /></Field>
          <Field label="Idle timeout (h)" class="w-28">
            <Input type="number" min="0" step="any" placeholder="global" bind:value={dIdle} />
          </Field>
          <Field label="Absolute lifetime (h)" class="w-36">
            <Input type="number" min="0" step="any" placeholder="global" bind:value={dTtl} />
          </Field>
          <Button type="submit" variant="primary">Save</Button>
          {#if dSaved}<span class="text-xs text-ok" role="status">saved</span>{/if}
          {#if detailsError}
            <Alert tone="danger" class="basis-full" onDismiss={() => (detailsError = "")}>{detailsError}</Alert>
          {/if}
          <p class="basis-full text-xs text-muted">
            Timeouts override the global setting for this user only — blank or 0
            clears the override (uses global). Idle changes apply live; the absolute
            lifetime applies to new sessions.
            {#if !isLocal(u)}
              Federated account: display name may be re-synced by the identity provider.
            {/if}
          </p>
        </form>
        {#if !isLocal(u)}
          {@const prof = (u.external_profile ?? {}) as Record<string, unknown>}
          {@const groups = Array.isArray(prof.groups) ? (prof.groups as string[]) : []}
          <div class="mt-3 border-t border-border pt-2">
            <div class="mb-1 text-xs font-semibold uppercase tracking-wide text-muted">Identity source ({u.auth_provider})</div>
            <dl class="grid grid-cols-[9rem_1fr] gap-x-3 gap-y-0.5 text-xs">
              {#if u.external_issuer}<dt class="text-muted">Server / issuer</dt><dd class="font-mono break-all">{u.external_issuer}</dd>{/if}
              {#if u.external_subject}<dt class="text-muted">Stable subject</dt><dd class="font-mono break-all">{u.external_subject}</dd>{/if}
              {#if prof.dn}<dt class="text-muted">Distinguished name</dt><dd class="font-mono break-all">{prof.dn}</dd>{/if}
              {#if prof.upn}<dt class="text-muted">UPN</dt><dd class="font-mono break-all">{prof.upn}</dd>{/if}
              {#if prof.netbios}<dt class="text-muted">NetBIOS name</dt><dd class="font-mono break-all">{prof.netbios}</dd>{/if}
              {#if prof.sam}<dt class="text-muted">Account name</dt><dd class="font-mono break-all">{prof.sam}</dd>{/if}
              {#if prof.mapped_role}<dt class="text-muted">Role from directory</dt><dd>{prof.mapped_role} <span class="text-muted">(re-applied at every login — a manual role change lasts until then)</span></dd>{/if}
              {#if u.last_login_at}<dt class="text-muted">Last login</dt><dd>{new Date(u.last_login_at).toLocaleString()}</dd>{/if}
              {#if groups.length}
                <dt class="text-muted">Directory groups</dt>
                <dd class="flex flex-wrap gap-1">
                  {#each groups as g (g)}<Badge>{g}</Badge>{/each}
                </dd>
              {/if}
            </dl>
          </div>
        {/if}
      </div>
    {/key}
  {/if}

  <form onsubmit={addUser} class="mt-4 flex flex-wrap items-end gap-2">
    <Field label="Username" required><Input bind:value={nuName} autocomplete="off" /></Field>
    <Field label="Password (min 8)" required>
      <Input type="password" autocomplete="new-password" bind:value={nuPass} minlength={8} />
    </Field>
    <Field label="Email (optional)"><Input bind:value={nuEmail} /></Field>
    <Field label="Role">
      <Select bind:value={nuRole}>
        {#each roles as r (r.name)}
          <option value={r.name}>{r.display_name || r.name}</option>
        {:else}
          <option value="user">user</option>
        {/each}
      </Select>
    </Field>
    <Button type="submit" variant="primary"
      disabled={nuName.trim().length === 0 || nuPass.length < 8}>Add user</Button>
  </form>
  {#if addError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (addError = "")}>{addError}</Alert>
  {/if}
</section>
