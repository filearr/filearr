<script lang="ts">
  // LLM API keys (M1): mint role-scoped keys for LLM tool clients (OpenWebUI,
  // Ollama loops, MCP). The key material is shown exactly once at mint time;
  // the panel also surfaces the per-key system-prompt / capabilities URLs.
  import {
    listLlmKeys,
    listLlmRoles,
    mintLlmKey,
    revokeLlmKey,
    friendlyError,
    type LlmKey,
    type LlmRoleInfo,
  } from "./api";

  let keys = $state<LlmKey[]>([]);
  let roles = $state<LlmRoleInfo[]>([]);
  let error = $state("");
  let loaded = $state(false);

  let showMint = $state(false);
  let mintName = $state("");
  let mintRole = $state("librarian");
  let mintExpires = $state("");
  let mintPathScope = $state("");
  let minted = $state<LlmKey | null>(null);
  let copied = $state(false);

  async function load() {
    try {
      const [k, r] = await Promise.all([listLlmKeys(), listLlmRoles()]);
      keys = k.keys;
      roles = r.roles;
      loaded = true;
      error = "";
    } catch (e) {
      error = friendlyError(e, "manage LLM keys");
    }
  }
  $effect(() => {
    void load();
  });

  const roleInfo = $derived(roles.find((r) => r.name === mintRole));

  async function mint(e: SubmitEvent) {
    e.preventDefault();
    error = "";
    try {
      minted = await mintLlmKey({
        name: mintName.trim(),
        role: mintRole,
        path_scope: mintPathScope.trim() || null,
        expires_days: mintExpires ? Number(mintExpires) : null,
      });
      mintName = "";
      mintPathScope = "";
      mintExpires = "";
      showMint = false;
      await load();
    } catch (e2) {
      error = friendlyError(e2, "mint an LLM key");
    }
  }

  async function revoke(k: LlmKey) {
    if (!confirm(`Revoke LLM key "${k.name}" (${k.prefix}…)? Clients using it stop working immediately.`)) return;
    try {
      await revokeLlmKey(k.id);
      await load();
    } catch (e2) {
      error = friendlyError(e2, "revoke this key");
    }
  }

  async function copyKey() {
    if (!minted?.key) return;
    await navigator.clipboard.writeText(minted.key);
    copied = true;
    setTimeout(() => (copied = false), 1500);
  }

  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
</script>

<section class="mt-8">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">LLM access keys</h2>
    <button
      class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white"
      onclick={() => { showMint = !showMint; minted = null; }}
    >{showMint ? "Cancel" : "Mint key"}</button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Role-scoped keys for LLM tool clients (OpenWebUI external tools, Ollama
    loops, MCP). Point the client at <code>/api/llm/v1</code> with the key as a
    Bearer token; fetch <code>/api/llm/v1/system-prompt</code> with the same key
    for the ready-made, role-accurate system prompt.
  </p>

  {#if error}
    <p class="mt-2 rounded bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>
  {/if}

  {#if minted?.key}
    <div class="mt-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm dark:border-amber-600 dark:bg-amber-950">
      <p class="font-medium">Key minted — copy it now; it is never shown again.</p>
      <div class="mt-2 flex items-center gap-2">
        <code class="break-all rounded bg-white px-2 py-1 dark:bg-slate-900">{minted.key}</code>
        <button class="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onclick={copyKey}>
          {copied ? "Copied" : "Copy"}
        </button>
      </div>
    </div>
  {/if}

  {#if showMint}
    <form class="mt-3 grid max-w-2xl gap-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800" onsubmit={mint}>
      <label class="grid gap-1 text-sm">
        <span>Name</span>
        <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" required bind:value={mintName} placeholder="openwebui-librarian" />
      </label>
      <label class="grid gap-1 text-sm">
        <span>Role</span>
        <select class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={mintRole}>
          {#each roles as r (r.name)}
            <option value={r.name}>{r.name}</option>
          {/each}
        </select>
        {#if roleInfo}
          <span class="text-xs text-slate-500 dark:text-slate-400">
            {roleInfo.description} Tools: {roleInfo.tools.join(", ")}.
          </span>
        {/if}
      </label>
      <div class="grid grid-cols-2 gap-3">
        <label class="grid gap-1 text-sm">
          <span>Expires (days, blank = never)</span>
          <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" type="number" min="1" max="3650" bind:value={mintExpires} />
        </label>
        <label class="grid gap-1 text-sm">
          <span>Path scope (optional, ltree)</span>
          <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={mintPathScope} placeholder="lib_<uuid>.media.movies" />
        </label>
      </div>
      <div>
        <button class="rounded bg-[var(--accent)] px-4 py-1.5 text-sm text-white" type="submit">Mint</button>
      </div>
    </form>
  {/if}

  {#if loaded && keys.length === 0}
    <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">No LLM keys yet.</p>
  {:else if keys.length > 0}
    <div class="mt-3 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2">Name</th>
            <th class="px-3 py-2">Prefix</th>
            <th class="px-3 py-2">Role</th>
            <th class="px-3 py-2">Content</th>
            <th class="px-3 py-2">Rate/min</th>
            <th class="px-3 py-2">Expires</th>
            <th class="px-3 py-2">Last used</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {#each keys as k (k.id)}
            <tr class="border-t border-slate-100 dark:border-slate-800">
              <td class="px-3 py-2">{k.name}</td>
              <td class="px-3 py-2 font-mono text-xs">{k.prefix}…</td>
              <td class="px-3 py-2">
                <span class="rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800" title={k.role_description ?? ""}>{k.role}</span>
                {#if k.path_scope}<span class="ml-1 text-xs text-slate-500" title={k.path_scope}>scoped</span>{/if}
              </td>
              <td class="px-3 py-2">{k.content_access ? "yes" : "no"}</td>
              <td class="px-3 py-2">{k.rate_limit}</td>
              <td class="px-3 py-2">{fmt(k.expires_at)}</td>
              <td class="px-3 py-2">{fmt(k.last_used_at)}</td>
              <td class="px-3 py-2 text-right">
                <button class="rounded border border-red-300 px-2 py-0.5 text-xs text-red-700 dark:border-red-800 dark:text-red-400" onclick={() => revoke(k)}>Revoke</button>
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
