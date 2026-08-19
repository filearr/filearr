<script lang="ts">
  import RbacPanel from "./RbacPanel.svelte";
  import RolesPanel from "./RolesPanel.svelte";
  import UsersPanel from "./UsersPanel.svelte";
  import SessionsPanel from "./SessionsPanel.svelte";
  import AuditPanel from "./AuditPanel.svelte";
  import LlmKeysPanel from "./LlmKeysPanel.svelte";
  import ApiKeysPanel from "./ApiKeysPanel.svelte";
  import ServiceAccountsPanel from "./ServiceAccountsPanel.svelte";
  import { isAdminPrincipal, type AuthPrincipal } from "./api";

  // Admin tab (2026-08-19): access control and platform administration only.
  // Library management moved to its own Libraries tab (LibrariesPage.svelte);
  // a user's own sessions moved to the Account page. `me` is the session
  // principal (null when auth is disabled) and gates the admin-only panels
  // (users, roles, keys, service accounts, sessions, audit — all meaningless
  // with auth off, so they stay session-gated). `authDisabled`
  // (FILEARR_AUTH_ENABLED=false) is kept for panels that exist WITHOUT auth.
  let { me = null, authDisabled = false }: { me?: AuthPrincipal | null; authDisabled?: boolean } =
    $props();
  const isAdmin = $derived(isAdminPrincipal(me));
</script>

<div class="mt-4">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Admin</h2>
    <span class="text-sm text-slate-500">
      Access control, users, API keys and the security audit.
      Libraries, presets and scans are on the
      <a class="underline hover:text-[var(--accent)]" href="#/libraries">Libraries</a> tab.
    </span>
  </div>

  {#if !isAdmin && !authDisabled}
    <p class="mt-4 text-sm text-slate-500">
      Your role can see access-control assignments here; user, key and audit
      administration needs the admin role.
    </p>
  {/if}

  {#if isAdmin}<RolesPanel />{/if}
  <RbacPanel />

  <LlmKeysPanel />
  {#if isAdmin}<ApiKeysPanel />{/if}
  {#if isAdmin}<ServiceAccountsPanel />{/if}

  {#if isAdmin}
    <UsersPanel />
    <SessionsPanel view="admin" />
    <AuditPanel />
  {/if}
</div>
