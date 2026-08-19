<script lang="ts">
  // P6-T10: service accounts -- the non-human principals that own API keys.
  import {
    listServiceAccounts,
    createServiceAccount,
    patchServiceAccount,
    deleteServiceAccount,
    friendlyError,
    type ServiceAccountOut,
  } from "./api";

  let rows = $state<ServiceAccountOut[]>([]);
  let error = $state("");
  let loaded = $state(false);
  let newName = $state("");
  let newDesc = $state("");
  let editing = $state<string | null>(null);
  let editName = $state("");
  let editDesc = $state("");

  async function load() {
    try {
      rows = (await listServiceAccounts()).service_accounts;
      loaded = true;
      error = "";
    } catch (e) {
      error = friendlyError(e, "load service accounts");
    }
  }
  $effect(() => {
    void load();
  });

  async function create(e: SubmitEvent) {
    e.preventDefault();
    try {
      await createServiceAccount({ name: newName.trim(), description: newDesc.trim() || null });
      newName = ""; newDesc = "";
      await load();
    } catch (e2) {
      error = friendlyError(e2, "create the service account");
    }
  }
  async function toggle(a: ServiceAccountOut) {
    const msg = a.disabled
      ? `Enable "${a.name}"? Its ${a.key_count + a.llm_key_count} key(s) start working again.`
      : `Disable "${a.name}"? Every key it owns (${a.key_count + a.llm_key_count}) is refused from the next request on. Reversible.`;
    if (!confirm(msg)) return;
    try {
      await patchServiceAccount(a.id, { disabled: !a.disabled });
      await load();
    } catch (e2) {
      error = friendlyError(e2, "update the service account");
    }
  }
  async function saveEdit(a: ServiceAccountOut) {
    try {
      await patchServiceAccount(a.id, { name: editName.trim(), description: editDesc.trim() || null });
      editing = null;
      await load();
    } catch (e2) {
      error = friendlyError(e2, "rename the service account");
    }
  }
  async function remove(a: ServiceAccountOut) {
    const n = a.key_count + a.llm_key_count;
    if (!confirm(`Delete "${a.name}"?${n ? ` This REVOKES its ${n} key(s) permanently.` : ""}`)) return;
    try {
      await deleteServiceAccount(a.id);
      await load();
    } catch (e2) {
      error = friendlyError(e2, "delete the service account");
    }
  }
  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
</script>

<section class="mt-8">
  <h2 class="text-lg font-semibold">Service accounts</h2>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Non-human identities that own API keys (and LLM access keys). One per
    integration — <em>sonarr</em>, <em>grafana</em>, <em>backup-script</em> — so
    a compromised or retired integration is one switch: <strong>disable</strong>
    refuses every key it owns immediately, <strong>delete</strong> revokes them.
    Keys minted before service accounts existed sit under
    <em>Pre-existing keys</em>.
  </p>
  {#if error}
    <p class="mt-2 rounded bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>
  {/if}
  <form class="mt-3 flex flex-wrap items-end gap-2" onsubmit={create}>
    <label class="grid gap-1 text-sm">
      <span>Name</span>
      <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" required bind:value={newName} placeholder="grafana" />
    </label>
    <label class="grid gap-1 text-sm">
      <span>Description (optional)</span>
      <input class="w-72 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={newDesc} placeholder="dashboards on the ops box" />
    </label>
    <button class="rounded bg-[var(--accent)] px-3 py-1.5 text-sm text-white" type="submit">Create</button>
  </form>
  {#if loaded && rows.length}
    <div class="mt-3 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2">Account</th>
            <th class="px-3 py-2">Keys</th>
            <th class="px-3 py-2">LLM keys</th>
            <th class="px-3 py-2">Last used</th>
            <th class="px-3 py-2">Status</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {#each rows as a (a.id)}
            <tr class="border-t border-slate-100 align-top dark:border-slate-800" class:opacity-60={a.disabled}>
              <td class="px-3 py-2">
                {#if editing === a.id}
                  <input class="rounded border border-slate-300 px-2 py-0.5 text-sm dark:border-slate-700 dark:bg-slate-900" bind:value={editName} />
                  <input class="mt-1 block w-72 rounded border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700 dark:bg-slate-900" bind:value={editDesc} placeholder="description" />
                  <button class="mt-1 text-xs text-sky-600" onclick={() => saveEdit(a)}>save</button>
                  <button class="ml-2 mt-1 text-xs text-slate-500" onclick={() => (editing = null)}>cancel</button>
                {:else}
                  <div class="font-medium">{a.name}</div>
                  {#if a.description}<div class="text-xs text-slate-500">{a.description}</div>{/if}
                {/if}
              </td>
              <td class="px-3 py-2 tabular-nums">{a.key_count}</td>
              <td class="px-3 py-2 tabular-nums">{a.llm_key_count}</td>
              <td class="px-3 py-2">{fmt(a.last_used_at)}</td>
              <td class="px-3 py-2">
                {#if a.disabled}<span class="rounded bg-red-200 px-2 py-0.5 text-xs dark:bg-red-900">disabled</span>{:else}<span class="rounded bg-emerald-200 px-2 py-0.5 text-xs dark:bg-emerald-900">active</span>{/if}
              </td>
              <td class="px-3 py-2 text-right whitespace-nowrap">
                <button class="text-sky-600 dark:text-sky-400" onclick={() => { editing = a.id; editName = a.name; editDesc = a.description ?? ""; }}>rename</button>
                <button class="ml-3 text-amber-600 dark:text-amber-400" onclick={() => toggle(a)}>{a.disabled ? "enable" : "disable"}</button>
                <button class="ml-3 text-red-600 dark:text-red-400" onclick={() => remove(a)}>delete</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {:else if loaded}
    <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">No service accounts yet — create one above, or from the API keys form.</p>
  {/if}
</section>
