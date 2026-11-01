<script lang="ts">
  import { onMount } from "svelte";
  import RoleForm from "./RoleForm.svelte";
  import {
    listRoles, compareRoles, deleteRole, friendlyError,
    type RoleDef, type RoleCompare,
  } from "./api";
  import { Alert, Badge, Button, DataTable, confirm } from "./ui";

  // Roles admin panel (2026-08-16): list/create/edit/delete roles and a
  // side-by-side comparison matrix. Roles carry coarse API scopes plus a
  // "ceiling" of RBAC actions that path grants may hand out; a role with the
  // admin scope bypasses path grants entirely.
  let loadError = $state("");
  let loading = $state(false);
  let deleteError = $state("");
  let roles = $state<RoleDef[]>([]);
  let cmp = $state<RoleCompare | null>(null);
  let showCompare = $state(false);
  // null = closed, "new" = create form, otherwise the role name being edited
  let editing = $state<string | "new" | null>(null);

  async function refresh() {
    loadError = "";
    loading = true;
    try {
      [roles, cmp] = await Promise.all([listRoles(), compareRoles()]);
    } catch (e) {
      loadError = friendlyError(e);
    }
    loading = false;
  }
  onMount(refresh);

  const editRole = $derived(editing && editing !== "new" ? roles.find((r) => r.name === editing) ?? null : null);

  function deleteReason(r: RoleDef): string {
    if (r.builtin) return "Builtin roles cannot be deleted";
    if (r.users > 0) return `In use by ${r.users} user${r.users === 1 ? "" : "s"} — reassign them first`;
    return "";
  }
  async function remove(r: RoleDef) {
    const ok = await confirm({
      title: `Delete role "${r.display_name || r.name}"?`,
      confirmLabel: "Delete role",
      tone: "danger",
    });
    if (!ok) return;
    deleteError = "";
    try {
      await deleteRole(r.name);
      if (editing === r.name) editing = null;
      await refresh();
    } catch (e) {
      deleteError = friendlyError(e, "delete");
    }
  }
  function onFormDone() { editing = null; refresh(); }

  const cell = (role: string, key: string) => !!cmp?.matrix?.[role]?.[key];

  // Comparison matrix rows: one per scope, one per action, then the users row.
  // Each role is a column whose value the DataTable accessor reads.
  type MatrixRow = { key: string; kind: "scope" | "action" | "users"; name: string; help: string };
  const matrixRows = $derived<MatrixRow[]>(
    cmp
      ? [
          ...cmp.scopes.map((s) => ({ key: `scope:${s}`, kind: "scope" as const, name: s, help: cmp?.scope_help?.[s] ?? "" })),
          ...cmp.actions.map((a) => ({ key: `action:${a}`, kind: "action" as const, name: a, help: cmp?.action_help?.[a] ?? "" })),
          { key: "users", kind: "users" as const, name: "Users", help: "" },
        ]
      : [],
  );
  const matrixValue = (row: MatrixRow, role: string): string =>
    row.kind === "users" ? userList(role) : cell(role, row.key) ? "✓" : "—";
  function userList(role: string): string {
    const names = cmp?.users_by_role?.[role] ?? [];
    if (names.length === 0) return "—";
    return names.length > 5 ? `${names.slice(0, 5).join(", ")} +${names.length - 5}` : names.join(", ");
  }
</script>

{#snippet roleNameCell(r: RoleDef)}
  <span class="font-medium">{r.display_name || r.name}</span>
  <span class="ml-1 font-mono text-xs text-muted">{r.name}</span>
  {#if r.builtin}<Badge class="ml-1">builtin</Badge>{/if}
  {#if r.bypass}
    <span title="Has the admin scope: path grants and the ceiling are not consulted"><Badge tone="warn" class="ml-1">bypasses path grants</Badge></span>
  {/if}
  {#if r.description}<div class="text-xs text-muted">{r.description}</div>{/if}
{/snippet}

{#snippet scopesCell(r: RoleDef)}
  {#each r.scopes as s (s)}
    <Badge tone="accent" class="mr-1 font-mono">{s}</Badge>
  {:else}<span class="text-xs text-muted">none</span>{/each}
{/snippet}

{#snippet ceilingCell(r: RoleDef)}
  <span title={r.ceiling_actions.join(", ")}>
    {#if r.bypass}<span class="text-xs text-muted">n/a</span>
    {:else}{r.ceiling_actions.length} action{r.ceiling_actions.length === 1 ? "" : "s"}{/if}
  </span>
{/snippet}

{#snippet roleActionsCell(r: RoleDef)}
  <div class="flex justify-end gap-1 whitespace-nowrap">
    <Button size="sm" variant="ghost" aria-expanded={editing === r.name}
      onclick={() => (editing = editing === r.name ? null : r.name)}>
      {editing === r.name ? "close" : "edit"}
    </Button>
    <Button size="sm" variant="danger" disabled={!!deleteReason(r)} title={deleteReason(r)}
      onclick={() => remove(r)}>delete</Button>
  </div>
{/snippet}

{#snippet permissionCell(row: MatrixRow)}
  {#if row.kind === "users"}
    <span class="font-medium">Users</span>
  {:else}
    <span title={row.help}><span class="text-xs text-muted">{row.kind}</span> <span class="font-mono">{row.name}</span></span>
  {/if}
{/snippet}

<section class="mt-8">
  <div class="flex flex-wrap items-center gap-2">
    <h2 class="text-lg font-semibold">Roles</h2>
    <Button class="ml-auto" aria-expanded={showCompare} onclick={() => (showCompare = !showCompare)}>
      {showCompare ? "Hide comparison" : "Compare roles"}
    </Button>
    <Button variant="primary" aria-expanded={editing === "new"}
      onclick={() => (editing = editing === "new" ? null : "new")}>New role</Button>
  </div>
  <p class="text-xs text-slate-500">
    A role bundles coarse API scopes (read / write / admin) with a ceiling of
    path-grant actions. Path grants narrow a user within the ceiling and never
    widen it; a role with the admin scope bypasses path grants entirely.
    Changing a user's role signs that user out everywhere.
  </p>

  <DataTable
    class="mt-3"
    caption="Roles"
    captionHidden
    columns={[
      { key: "name", label: "Role", cell: roleNameCell },
      { key: "scopes", label: "Scopes", cell: scopesCell },
      { key: "ceiling_actions", label: "Ceiling", cell: ceilingCell },
      { key: "users", label: "Users" },
      { key: "actions", label: "Actions", align: "right", cell: roleActionsCell },
    ]}
    rows={roles}
    rowKey={(r) => r.name}
    {loading}
    error={loadError || null}
    onRetry={refresh}
    emptyMessage="No roles yet. Create one with New role."
  />
  {#if deleteError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (deleteError = "")}>{deleteError}</Alert>
  {/if}

  {#if editing === "new"}
    <RoleForm {roles} meta={cmp} onDone={onFormDone} onCancel={() => (editing = null)} />
  {:else if editRole}
    {#key editRole.name}
      <RoleForm role={editRole} {roles} meta={cmp} onDone={onFormDone} onCancel={() => (editing = null)} />
    {/key}
  {/if}

  {#if showCompare && cmp}
    <div class="mt-4 rounded-control border border-border p-3">
      <h3 class="text-sm font-semibold">Role comparison</h3>
      <p class="text-xs text-muted">
        Pick the leftmost column that has every ✓ the person needs. Hover a row
        label for what it means.
      </p>
      <DataTable
        class="mt-2"
        caption="Role comparison"
        captionHidden
        columns={[
          { key: "permission", label: "Permission", cell: permissionCell },
          ...cmp.roles.map((r) => ({ key: r.name, label: r.display_name || r.name })),
        ]}
        rows={matrixRows}
        rowKey={(row) => row.key}
        accessor={(row, role) => matrixValue(row, role)}
      />
    </div>
  {/if}
</section>
