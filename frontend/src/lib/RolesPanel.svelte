<script lang="ts">
  import { onMount } from "svelte";
  import RoleForm from "./RoleForm.svelte";
  import {
    listRoles, compareRoles, deleteRole, friendlyError,
    type RoleDef, type RoleCompare,
  } from "./api";

  // Roles admin panel (2026-08-16): list/create/edit/delete roles and a
  // side-by-side comparison matrix. Roles carry coarse API scopes plus a
  // "ceiling" of RBAC actions that path grants may hand out; a role with the
  // admin scope bypasses path grants entirely.
  let error = $state("");
  let roles = $state<RoleDef[]>([]);
  let cmp = $state<RoleCompare | null>(null);
  let showCompare = $state(false);
  // null = closed, "new" = create form, otherwise the role name being edited
  let editing = $state<string | "new" | null>(null);

  async function refresh() {
    error = "";
    try {
      [roles, cmp] = await Promise.all([listRoles(), compareRoles()]);
    } catch (e) {
      error = friendlyError(e);
    }
  }
  onMount(refresh);

  const editRole = $derived(editing && editing !== "new" ? roles.find((r) => r.name === editing) ?? null : null);

  function deleteReason(r: RoleDef): string {
    if (r.builtin) return "Builtin roles cannot be deleted";
    if (r.users > 0) return `In use by ${r.users} user${r.users === 1 ? "" : "s"} — reassign them first`;
    return "";
  }
  async function remove(r: RoleDef) {
    if (!confirm(`Delete role "${r.display_name || r.name}"?`)) return;
    error = "";
    try {
      await deleteRole(r.name);
      if (editing === r.name) editing = null;
      await refresh();
    } catch (e) {
      error = friendlyError(e, "delete");
    }
  }
  function onFormDone() { editing = null; refresh(); }

  const cell = (role: string, key: string) => !!cmp?.matrix?.[role]?.[key];
  function userList(role: string): string {
    const names = cmp?.users_by_role?.[role] ?? [];
    if (names.length === 0) return "—";
    return names.length > 5 ? `${names.slice(0, 5).join(", ")} +${names.length - 5}` : names.join(", ");
  }
</script>

<section class="mt-8">
  <div class="flex flex-wrap items-center gap-2">
    <h2 class="text-lg font-semibold">Roles</h2>
    <button class="ml-auto rounded-lg border border-slate-300 px-3 py-1 text-sm dark:border-slate-700"
      onclick={() => (showCompare = !showCompare)}>
      {showCompare ? "Hide comparison" : "Compare roles"}
    </button>
    <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white"
      onclick={() => (editing = editing === "new" ? null : "new")}>New role</button>
  </div>
  <p class="text-xs text-slate-500">
    A role bundles coarse API scopes (read / write / admin) with a ceiling of
    path-grant actions. Path grants narrow a user within the ceiling and never
    widen it; a role with the admin scope bypasses path grants entirely.
    Changing a user's role signs that user out everywhere.
  </p>
  {#if error}<p class="mt-2 text-sm text-red-600">{error}</p>{/if}

  <div class="mt-3 overflow-x-auto">
    <table class="w-full text-sm">
      <thead class="text-left text-xs uppercase text-slate-400">
        <tr>
          <th class="py-1 pr-3">Role</th>
          <th class="py-1 pr-3">Scopes</th>
          <th class="py-1 pr-3">Ceiling</th>
          <th class="py-1 pr-3">Users</th>
          <th class="py-1 pr-3"></th>
        </tr>
      </thead>
      <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
        {#each roles as r (r.name)}
          <tr>
            <td class="py-2 pr-3">
              <span class="font-medium">{r.display_name || r.name}</span>
              <span class="ml-1 font-mono text-xs text-slate-400">{r.name}</span>
              {#if r.builtin}
                <span class="ml-1 rounded bg-slate-100 px-1.5 py-0.5 text-xs dark:bg-slate-800">builtin</span>
              {/if}
              {#if r.bypass}
                <span class="ml-1 rounded bg-amber-100 px-1.5 py-0.5 text-xs text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                  title="Has the admin scope: path grants and the ceiling are not consulted">bypasses path grants</span>
              {/if}
              {#if r.description}<div class="text-xs text-slate-500">{r.description}</div>{/if}
            </td>
            <td class="py-2 pr-3">
              {#each r.scopes as s (s)}
                <span class="mr-1 rounded bg-indigo-100 px-1.5 py-0.5 font-mono text-xs text-indigo-700 dark:bg-indigo-900/40 dark:text-indigo-300">{s}</span>
              {:else}<span class="text-xs text-slate-400">none</span>{/each}
            </td>
            <td class="py-2 pr-3" title={r.ceiling_actions.join(", ")}>
              {#if r.bypass}<span class="text-xs text-slate-400">n/a</span>
              {:else}{r.ceiling_actions.length} action{r.ceiling_actions.length === 1 ? "" : "s"}{/if}
            </td>
            <td class="py-2 pr-3">{r.users}</td>
            <td class="py-2 pr-3 text-right whitespace-nowrap">
              <button class="text-xs text-slate-500 underline"
                onclick={() => (editing = editing === r.name ? null : r.name)}>
                {editing === r.name ? "close" : "edit"}
              </button>
              <button class="ml-2 text-xs text-red-600 underline disabled:cursor-not-allowed disabled:opacity-40"
                disabled={!!deleteReason(r)} title={deleteReason(r)}
                onclick={() => remove(r)}>delete</button>
            </td>
          </tr>
        {:else}
          <tr><td colspan="5" class="py-3 text-slate-400">No roles.</td></tr>
        {/each}
      </tbody>
    </table>
  </div>

  {#if editing === "new"}
    <RoleForm {roles} meta={cmp} onDone={onFormDone} onCancel={() => (editing = null)} />
  {:else if editRole}
    {#key editRole.name}
      <RoleForm role={editRole} {roles} meta={cmp} onDone={onFormDone} onCancel={() => (editing = null)} />
    {/key}
  {/if}

  {#if showCompare && cmp}
    <div class="mt-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
      <h3 class="text-sm font-semibold">Role comparison</h3>
      <p class="text-xs text-slate-500">
        Pick the leftmost column that has every ✓ the person needs. Hover a row
        label for what it means.
      </p>
      <div class="mt-2 overflow-x-auto">
        <table class="w-full text-sm">
          <thead class="text-left text-xs uppercase text-slate-400">
            <tr>
              <th class="py-1 pr-3">Permission</th>
              {#each cmp.roles as r (r.name)}
                <th class="px-2 py-1 text-center" title={r.description}>{r.display_name || r.name}</th>
              {/each}
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
            {#each cmp.scopes as s (s)}
              <tr>
                <td class="py-1 pr-3" title={cmp.scope_help?.[s] ?? ""}>
                  <span class="text-xs text-slate-400">scope</span> <span class="font-mono">{s}</span>
                </td>
                {#each cmp.roles as r (r.name)}
                  <td class="px-2 py-1 text-center {cell(r.name, `scope:${s}`) ? 'text-emerald-600' : 'text-slate-300 dark:text-slate-700'}">
                    {cell(r.name, `scope:${s}`) ? "✓" : "—"}
                  </td>
                {/each}
              </tr>
            {/each}
            {#each cmp.actions as a (a)}
              <tr>
                <td class="py-1 pr-3" title={cmp.action_help?.[a] ?? ""}>
                  <span class="text-xs text-slate-400">action</span> <span class="font-mono">{a}</span>
                </td>
                {#each cmp.roles as r (r.name)}
                  <td class="px-2 py-1 text-center {cell(r.name, `action:${a}`) ? 'text-emerald-600' : 'text-slate-300 dark:text-slate-700'}"
                    title={r.bypass ? "Admin scope: bypasses path grants" : ""}>
                    {cell(r.name, `action:${a}`) ? "✓" : "—"}
                  </td>
                {/each}
              </tr>
            {/each}
          </tbody>
          <tfoot class="border-t border-slate-300 text-xs dark:border-slate-700">
            <tr>
              <td class="py-2 pr-3 font-medium">Users</td>
              {#each cmp.roles as r (r.name)}
                <td class="px-2 py-2 text-center text-slate-500" title={(cmp.users_by_role?.[r.name] ?? []).join(", ")}>
                  {userList(r.name)}
                </td>
              {/each}
            </tr>
          </tfoot>
        </table>
      </div>
    </div>
  {/if}
</section>
