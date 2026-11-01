<script lang="ts">
  import { createRole, patchRole, friendlyError, type RoleDef, type RoleCompare } from "./api";
  import { Alert, Button, Checkbox, Field, Input, Select, StateBlock } from "./ui";

  // Create/edit form for a role. Mirrors the server's scope normalisation live
  // (write => read, admin => read+write) so what the operator sees is what gets
  // stored. Builtin "admin" cannot lose the admin scope (server 409s anyway).
  let {
    role = null,
    roles = [],
    meta = null,
    onDone,
    onCancel,
  }: {
    role?: RoleDef | null;
    roles?: RoleDef[];
    meta?: RoleCompare | null;
    onDone: () => void;
    onCancel: () => void;
  } = $props();

  const SCOPES = ["read", "write", "admin"];
  const isEdit = $derived(role !== null);
  const lockAdmin = $derived(!!role && role.builtin && role.name === "admin");

  // Initial values only — RolesPanel re-keys this component per role.
  // svelte-ignore state_referenced_locally
  let name = $state(role?.name ?? "");
  // svelte-ignore state_referenced_locally
  let displayName = $state(role?.display_name ?? "");
  // svelte-ignore state_referenced_locally
  let description = $state(role?.description ?? "");
  // svelte-ignore state_referenced_locally
  let scopes = $state<string[]>(role ? [...role.scopes] : ["read"]);
  // svelte-ignore state_referenced_locally
  let actions = $state<string[]>(role ? [...role.ceiling_actions] : []);
  let cloneFrom = $state("");
  let error = $state("");
  let busy = $state(false);

  const actionList = $derived(meta?.actions ?? []);
  const isAdminScope = $derived(scopes.includes("admin"));

  /** Effective scopes after the server's normalisation. */
  function normalise(s: string[]): string[] {
    const set = new Set(s);
    if (set.has("admin")) { set.add("write"); set.add("read"); }
    if (set.has("write")) set.add("read");
    return SCOPES.filter((x) => set.has(x));
  }
  const effective = $derived(normalise(scopes));
  /** A scope is implied (and so cannot be unchecked) by a higher one. */
  const implied = (s: string) =>
    (s === "read" && (scopes.includes("write") || scopes.includes("admin"))) ||
    (s === "write" && scopes.includes("admin"));

  function toggleScope(s: string) {
    if (lockAdmin && s === "admin") return;
    if (implied(s)) return;
    scopes = scopes.includes(s) ? scopes.filter((x) => x !== s) : normalise([...scopes, s]);
  }
  function toggleAction(a: string) {
    actions = actions.includes(a) ? actions.filter((x) => x !== a) : [...actions, a];
  }
  function applyClone() {
    const src = roles.find((r) => r.name === cloneFrom);
    if (!src) return;
    scopes = [...src.scopes];
    actions = [...src.ceiling_actions];
    if (!description) description = src.description;
  }

  async function submit(e: Event) {
    e.preventDefault();
    error = "";
    busy = true;
    try {
      const body = {
        display_name: displayName.trim(),
        description: description.trim(),
        scopes: effective,
        ceiling_actions: actions,
      };
      if (role) await patchRole(role.name, body);
      else await createRole({ name: name.trim(), ...body, ...(cloneFrom ? { clone_from: cloneFrom } : {}) });
      onDone();
    } catch (err) {
      error = friendlyError(err, "save");
    } finally {
      busy = false;
    }
  }
</script>

<form onsubmit={submit} class="mt-3 rounded-control border border-border bg-surface-2 p-3 text-sm">
  <h3 class="font-medium">{isEdit ? `Edit role: ${role?.display_name || role?.name}` : "New role"}</h3>

  <div class="mt-2 grid grid-cols-1 gap-2 sm:grid-cols-3">
    {#if !isEdit}
      <Field label="Name (slug, a-z 0-9 _ -)" required>
        <Input class="font-mono" bind:value={name} pattern={"[a-z0-9][a-z0-9_\\-]{1,31}"} placeholder="e.g. curator" />
      </Field>
    {/if}
    <Field label="Display name" required><Input bind:value={displayName} /></Field>
    <Field label="Description"><Input bind:value={description} /></Field>
    {#if !isEdit}
      <Field label="Clone permissions from">
        <Select bind:value={cloneFrom} onchange={applyClone}>
          <option value="">— start empty —</option>
          {#each roles as r (r.name)}<option value={r.name}>{r.display_name || r.name}</option>{/each}
        </Select>
      </Field>
    {/if}
  </div>

  <h4 class="mt-3 text-xs font-semibold uppercase text-slate-600 dark:text-slate-400">API scopes</h4>
  <div class="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-3">
    {#each SCOPES as s (s)}
      <div class="rounded-control border border-border p-2"
        title={lockAdmin && s === "admin" ? "The builtin admin role must keep the admin scope" : implied(s) ? `Implied by a higher scope` : ""}>
        <Checkbox
          label={implied(s) ? `${s} (implied)` : s}
          description={meta?.scope_help?.[s] ?? ""}
          checked={effective.includes(s)}
          disabled={(lockAdmin && s === "admin") || implied(s)}
          onchange={() => toggleScope(s)} />
      </div>
    {/each}
  </div>
  <p class="mt-1 text-xs text-slate-500">
    write implies read; admin implies everything. A role with the admin scope
    <strong>bypasses path grants entirely</strong>.
  </p>

  <h4 class="mt-3 text-xs font-semibold uppercase text-slate-600 dark:text-slate-400">Ceiling actions</h4>
  <p class="text-xs text-slate-500">
    The ceiling is the maximum a path grant can hand this role — grants narrow it,
    never widen it (admin bypasses the ceiling).
  </p>
  {#if isAdminScope}
    <p class="mt-1 text-xs text-warn">This role has the admin scope, so the ceiling is not consulted.</p>
  {/if}
  <div class="mt-1 grid grid-cols-1 gap-2 sm:grid-cols-2 lg:grid-cols-3">
    {#each actionList as a (a)}
      <div class="rounded-control border border-border p-2">
        <Checkbox
          label={a}
          description={meta?.action_help?.[a] ?? ""}
          checked={actions.includes(a)}
          onchange={() => toggleAction(a)} />
      </div>
    {:else}
      <StateBlock state="empty" compact class="col-span-full" message="No actions published by the server. Check that the server is up to date." />
    {/each}
  </div>

  <div class="mt-3 flex gap-2">
    <Button type="submit" variant="primary" disabled={busy || !displayName.trim() || (!isEdit && !name.trim())}>
      {isEdit ? "Save changes" : "Create role"}
    </Button>
    <Button variant="secondary" onclick={onCancel}>Cancel</Button>
  </div>
  {#if error}
    <Alert tone="danger" class="mt-2" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}
</form>
