<script lang="ts">
  import { onMount } from "svelte";
  import FolderPicker from "./FolderPicker.svelte";
  import {
    addMember, createGrant, createGroup, deleteGrant, deleteGroup, listGrants,
    listGroups, listMembers, listRbacActions, listUsers, listLibraries, rbacPreview,
    removeMember,
    type AuthPrincipal, type Library, type RbacActions, type RbacDecision,
    type RbacGrant, type RbacGroup, type RbacMember,
    errorMessage,
  } from "./api";
  import { Alert, Badge, Button, DataTable, Field, Input, Select, StateBlock, confirm } from "./ui";

  // P6-T2 RBAC admin panel: groups + members, path grants, and a decision
  // preview. Functional, not fancy. Enforcement lives in security.require_permission
  // (search scope + data endpoints) with the generation-invalidated grant cache.
  let loadError = $state("");
  let loading = $state(false);
  // Action errors by area, each rendered next to the control that raised it:
  // group (add group), groups (row actions), members, grant (add form),
  // grants (row delete), preview.
  type Area = "group" | "groups" | "members" | "grant" | "grants" | "preview";
  let errors = $state<Partial<Record<Area, string>>>({});
  const fail = (area: Area, e: unknown) => (errors[area] = errorMessage(e, { verb: "manage permissions" }));
  let users = $state<AuthPrincipal[]>([]);
  let groups = $state<RbacGroup[]>([]);
  let grants = $state<RbacGrant[]>([]);
  let libraries = $state<Library[]>([]);
  let meta = $state<RbacActions>({ actions: [], role_ceilings: {} });

  // group create + member management
  let newGroupName = $state("");
  let newGroupDesc = $state("");
  let openGroup = $state<string | null>(null);
  let members = $state<RbacMember[]>([]);
  let addMemberId = $state("");

  // grant create form
  let gSubjectKind = $state<"principal" | "group">("group");
  let gSubjectId = $state("");
  let gLibraryId = $state("");
  let gRelPath = $state("");
  let gAction = $state("search_metadata");
  let gEffect = $state<"allow" | "deny">("allow");
  let showPicker = $state(false);

  // preview form
  let pPrincipal = $state("");
  let pLibrary = $state("");
  let pPath = $state("");
  let pAction = $state("search_metadata");
  let decision = $state<RbacDecision | null>(null);

  async function refresh() {
    loadError = "";
    loading = true;
    try {
      [users, groups, grants, libraries, meta] = await Promise.all([
        listUsers().catch(() => []),
        listGroups(),
        listGrants(),
        listLibraries(),
        listRbacActions(),
      ]);
      if (meta.actions.length && !meta.actions.includes(gAction)) gAction = meta.actions[0];
    } catch (e) {
      loadError = errorMessage(e, { verb: "manage permissions" });
    }
    loading = false;
  }
  onMount(refresh);

  async function addGroup() {
    if (!newGroupName.trim()) return;
    errors.group = undefined;
    try {
      await createGroup(newGroupName.trim(), newGroupDesc.trim() || null);
      newGroupName = "";
      newGroupDesc = "";
      await refresh();
    } catch (e) { fail("group", e); }
  }
  async function removeGroup(g: RbacGroup) {
    const ok = await confirm({
      title: `Delete group "${g.name}"?`,
      message: "Delete this group and its grants?",
      confirmLabel: "Delete group",
      tone: "danger",
    });
    if (!ok) return;
    errors.groups = undefined;
    try { await deleteGroup(g.id); if (openGroup === g.id) openGroup = null; await refresh(); }
    catch (e) { fail("groups", e); }
  }
  async function toggleMembers(id: string) {
    if (openGroup === id) { openGroup = null; return; }
    openGroup = id;
    errors.groups = undefined;
    errors.members = undefined;
    try { members = await listMembers(id); } catch (e) { fail("groups", e); }
  }
  async function doAddMember() {
    if (!openGroup || !addMemberId) return;
    errors.members = undefined;
    try { await addMember(openGroup, addMemberId); members = await listMembers(openGroup); await refresh(); }
    catch (e) { fail("members", e); }
  }
  async function doRemoveMember(pid: string) {
    if (!openGroup) return;
    errors.members = undefined;
    try { await removeMember(openGroup, pid); members = await listMembers(openGroup); await refresh(); }
    catch (e) { fail("members", e); }
  }

  function onPickPath(abs: string) {
    showPicker = false;
    const lib = libraries.find((l) => l.id === gLibraryId);
    if (lib && abs.startsWith(lib.root_path)) {
      gRelPath = abs.slice(lib.root_path.length).replace(/^\/+/, "");
    } else {
      gRelPath = abs.replace(/^\/+/, "");
    }
  }

  function browseFolder() {
    if (!gLibraryId) { errors.grant = "Pick a library first"; } else { errors.grant = undefined; showPicker = true; }
  }

  async function addGrant() {
    errors.grant = undefined;
    if (!gSubjectId || !gLibraryId) { errors.grant = "Pick a subject and a library"; return; }
    try {
      await createGrant({
        subject_kind: gSubjectKind, subject_id: gSubjectId, library_id: gLibraryId,
        rel_path: gRelPath.trim(), action: gAction, effect: gEffect,
      });
      gRelPath = "";
      await refresh();
    } catch (e) { fail("grant", e); }
  }
  async function removeGrant(id: string) {
    errors.grants = undefined;
    try { await deleteGrant(id); await refresh(); } catch (e) { fail("grants", e); }
  }

  async function runPreview() {
    errors.preview = undefined;
    if (!pPrincipal || !pLibrary) { errors.preview = "Pick a user and a library"; return; }
    try { decision = await rbacPreview(pPrincipal, pLibrary, pPath, pAction); }
    catch (e) { fail("preview", e); decision = null; }
  }

  const libName = (id: string) => libraries.find((l) => l.id === id)?.name ?? id;
  const openGroupRow = $derived(openGroup ? groups.find((g) => g.id === openGroup) ?? null : null);
</script>

{#snippet errorFor(area: Area)}
  {#if errors[area]}
    <Alert tone="danger" class="mt-2" onDismiss={() => (errors[area] = undefined)}>{errors[area]}</Alert>
  {/if}
{/snippet}

{#snippet groupNameCell(g: RbacGroup)}
  {g.name}{#if g.description}<span class="ml-1 text-muted">— {g.description}</span>{/if}
{/snippet}

{#snippet groupActionsCell(g: RbacGroup)}
  <div class="flex justify-end gap-1">
    <Button size="sm" variant="ghost" aria-expanded={openGroup === g.id} onclick={() => toggleMembers(g.id)}>
      {openGroup === g.id ? "hide" : "members"}
    </Button>
    {#if g.source === "local"}
      <Button size="sm" variant="danger" onclick={() => removeGroup(g)}>delete</Button>
    {/if}
  </div>
{/snippet}

{#snippet subjectCell(gr: RbacGrant)}
  {gr.subject_label ?? gr.subject_id} <span class="text-muted">({gr.subject_kind})</span>
{/snippet}
{#snippet libraryCell(gr: RbacGrant)}{libName(gr.library_id)}{/snippet}
{#snippet scopeCell(gr: RbacGrant)}<span class="font-mono text-xs">{gr.scope}</span>{/snippet}
{#snippet effectCell(gr: RbacGrant)}
  <Badge tone={gr.effect === "deny" ? "danger" : "ok"}>{gr.effect}</Badge>
{/snippet}
{#snippet grantActionsCell(gr: RbacGrant)}
  <div class="flex justify-end">
    <Button size="sm" variant="danger" onclick={() => removeGrant(gr.id)}>delete</Button>
  </div>
{/snippet}

<section class="mt-8">
  <h2 class="text-lg font-semibold">Access control (RBAC)</h2>
  <p class="text-sm text-slate-500">
    Groups + path-scoped grants. Grants only ever narrow a principal within its
    global-role ceiling and are enforced on search and every data endpoint
    (items, browse, timeline, reports). Use the preview below to check what a
    principal can see before saving.
  </p>

  <!-- Groups -->
  <h3 class="mt-4 font-medium">Groups</h3>
  <div class="mt-2 flex flex-wrap items-end gap-2">
    <Field label="Group name"><Input placeholder="new group name" bind:value={newGroupName} /></Field>
    <Field label="Description (optional)"><Input bind:value={newGroupDesc} /></Field>
    <Button variant="primary" onclick={addGroup}>Add group</Button>
  </div>
  {@render errorFor("group")}
  <DataTable
    class="mt-2"
    caption="Groups"
    captionHidden
    columns={[
      { key: "name", label: "Name", cell: groupNameCell },
      { key: "source", label: "Source" },
      { key: "member_count", label: "Members" },
      { key: "actions", label: "Actions", align: "right", cell: groupActionsCell },
    ]}
    rows={groups}
    rowKey={(g) => g.id}
    {loading}
    error={loadError || null}
    onRetry={refresh}
    emptyMessage="No groups yet. Add one with the form above."
  />
  {@render errorFor("groups")}
  {#if openGroupRow}
    {#key openGroupRow.id}
      <div class="mt-2 rounded-control border border-border bg-surface-2 p-3 text-sm">
        <h4 class="font-medium">Members of {openGroupRow.name}</h4>
        <div class="mt-2 flex flex-wrap items-end gap-2">
          <Field label="Add member">
            <Select bind:value={addMemberId}>
              <option value="">add member…</option>
              {#each users as u (u.id)}<option value={u.id}>{u.username} ({u.global_role})</option>{/each}
            </Select>
          </Field>
          <Button onclick={doAddMember}>Add</Button>
        </div>
        {@render errorFor("members")}
        <ul class="mt-2 flex flex-col gap-1">
          {#each members as m (m.principal_id)}
            <li class="flex items-center gap-2">
              <span>{m.username ?? m.principal_id} <span class="text-muted">({m.global_role})</span></span>
              <Button size="sm" variant="ghost" onclick={() => doRemoveMember(m.principal_id)}>remove</Button>
            </li>
          {:else}
            <li><StateBlock state="empty" compact message="No members yet. Add one above." /></li>
          {/each}
        </ul>
      </div>
    {/key}
  {/if}

  <!-- Grants -->
  <h3 class="mt-6 font-medium">Path grants</h3>
  <div class="mt-2 grid grid-cols-1 items-start gap-2 sm:grid-cols-3 lg:grid-cols-6">
    <Field label="Subject type">
      <Select bind:value={gSubjectKind}>
        <option value="group">group</option>
        <option value="principal">user</option>
      </Select>
    </Field>
    <Field label="Subject">
      <Select bind:value={gSubjectId}>
        <option value="">subject…</option>
        {#if gSubjectKind === "group"}
          {#each groups as g (g.id)}<option value={g.id}>{g.name}</option>{/each}
        {:else}
          {#each users as u (u.id)}<option value={u.id}>{u.username}</option>{/each}
        {/if}
      </Select>
    </Field>
    <Field label="Library">
      <Select bind:value={gLibraryId}>
        <option value="">library…</option>
        {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
      </Select>
    </Field>
    <Field label="Path" hint="Blank = whole library">
      <Input placeholder="rel path" bind:value={gRelPath} />
    </Field>
    <Field label="Action">
      <Select bind:value={gAction}>
        {#each meta.actions as a (a)}<option value={a}>{a}</option>{/each}
      </Select>
    </Field>
    <div class="flex items-end gap-2">
      <Field label="Effect">
        <Select bind:value={gEffect}>
          <option value="allow">allow</option>
          <option value="deny">deny</option>
        </Select>
      </Field>
      <Button variant="primary" onclick={addGrant}>Add</Button>
    </div>
  </div>
  <Button class="mt-1" size="sm" variant="ghost" onclick={browseFolder}>browse folder…</Button>
  {@render errorFor("grant")}
  {#if showPicker}
    {@const lib = libraries.find((l) => l.id === gLibraryId)}
    <FolderPicker initial={lib?.root_path ?? ""} onPick={onPickPath} onClose={() => (showPicker = false)} />
  {/if}
  <DataTable
    class="mt-2"
    caption="Path grants"
    captionHidden
    columns={[
      { key: "subject_id", label: "Subject", cell: subjectCell },
      { key: "library_id", label: "Library", cell: libraryCell },
      { key: "scope", label: "Scope", cell: scopeCell },
      { key: "action", label: "Action" },
      { key: "effect", label: "Effect", cell: effectCell },
      { key: "actions", label: "Actions", align: "right", cell: grantActionsCell },
    ]}
    rows={grants}
    rowKey={(gr) => gr.id}
    {loading}
    error={loadError || null}
    onRetry={refresh}
    emptyMessage="No grants yet. Add one with the form above."
  />
  {@render errorFor("grants")}

  <!-- Preview -->
  <h3 class="mt-6 font-medium">Decision preview</h3>
  <div class="mt-2 flex flex-wrap items-end gap-2">
    <Field label="User">
      <Select bind:value={pPrincipal}>
        <option value="">user…</option>
        {#each users as u (u.id)}<option value={u.id}>{u.username} ({u.global_role})</option>{/each}
      </Select>
    </Field>
    <Field label="Library">
      <Select bind:value={pLibrary}>
        <option value="">library…</option>
        {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
      </Select>
    </Field>
    <Field label="Path"><Input placeholder="rel path" bind:value={pPath} /></Field>
    <Field label="Action">
      <Select bind:value={pAction}>
        {#each meta.actions as a (a)}<option value={a}>{a}</option>{/each}
      </Select>
    </Field>
    <Button onclick={runPreview}>Evaluate</Button>
  </div>
  {@render errorFor("preview")}
  {#if decision}
    <div class="mt-2 rounded-control border border-border p-2 text-sm" role="status">
      <span class="font-semibold {decision.allowed ? 'text-ok' : 'text-danger'}">
        {decision.allowed ? "ALLOWED" : "DENIED"}
      </span>
      <span class="ml-2 text-muted">reason: {decision.reason} · role: {decision.role}</span>
      <div class="mt-1 font-mono text-xs text-muted">scope: {decision.item_scope}</div>
      {#if decision.winning_grant}
        <div class="mt-1 text-xs">
          winning grant: {decision.winning_grant.effect} {decision.winning_grant.action}
          @ <span class="font-mono">{decision.winning_grant.scope}</span>
          ({decision.winning_grant.subject_kind})
        </div>
      {/if}
    </div>
  {/if}
</section>
