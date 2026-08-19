<script lang="ts">
  // Ordinary API keys: read / write / admin Bearer tokens for *arr-style
  // integrations, scripts and dashboards. Distinct from LLM access keys (those
  // carry a facade role and are pinned to read). Key material is shown exactly
  // once at mint time.
  import {
    listApiKeys,
    listApiKeyScopes,
    mintApiKey,
    revokeApiKey,
    listServiceAccounts,
    createServiceAccount,
    friendlyError,
    type ApiKeyRow,
    type ApiKeyScope,
    type ApiKeyScopeInfo,
    type ServiceAccountOut,
  } from "./api";

  let keys = $state<ApiKeyRow[]>([]);
  let scopes = $state<ApiKeyScopeInfo[]>([]);
  let accounts = $state<ServiceAccountOut[]>([]);
  let mintAccount = $state("");
  let newAccountName = $state("");
  let error = $state("");
  let loaded = $state(false);

  let showMint = $state(false);
  let mintName = $state("");
  let mintScopes = $state<ApiKeyScope[]>(["read"]);
  let mintExpires = $state("");
  let minted = $state<ApiKeyRow | null>(null);
  let copied = $state(false);

  async function load() {
    try {
      const [k, s, a] = await Promise.all([listApiKeys(), listApiKeyScopes(), listServiceAccounts()]);
      keys = k.keys;
      scopes = s.scopes;
      accounts = a.service_accounts;
      if (!mintAccount && accounts.length) mintAccount = accounts.find((x) => !x.disabled)?.id ?? "";
      loaded = true;
      error = "";
    } catch (e) {
      error = friendlyError(e, "manage API keys");
    }
  }
  $effect(() => {
    void load();
  });

  function toggleScope(name: ApiKeyScope, on: boolean) {
    if (name === "admin" && on) {
      mintScopes = ["read", "write", "admin"];
      return;
    }
    const set = new Set(mintScopes);
    if (on) set.add(name);
    else set.delete(name);
    if (!on && name !== "admin") set.delete("admin"); // admin implies both
    mintScopes = (["read", "write", "admin"] as ApiKeyScope[]).filter((s) => set.has(s));
  }

  async function mint(e: SubmitEvent) {
    e.preventDefault();
    error = "";
    if (mintScopes.length === 0) {
      error = "Pick at least one scope.";
      return;
    }
    try {
      let owner = mintAccount;
      if (owner === "__new__") {
        if (!newAccountName.trim()) {
          error = "Name the new service account.";
          return;
        }
        owner = (await createServiceAccount({ name: newAccountName.trim() })).id;
        newAccountName = "";
      }
      if (!owner) {
        error = "Pick the service account this key belongs to.";
        return;
      }
      minted = await mintApiKey({
        name: mintName.trim(),
        scopes: mintScopes,
        expires_days: mintExpires ? Number(mintExpires) : null,
        service_account_id: owner,
      });
      mintAccount = owner;
      mintName = "";
      mintScopes = ["read"];
      mintExpires = "";
      showMint = false;
      await load();
    } catch (e2) {
      error = friendlyError(e2, "mint an API key");
    }
  }

  async function revoke(k: ApiKeyRow) {
    if (!confirm(`Revoke API key "${k.name}" (${k.prefix}…)? Anything using it stops working immediately.`)) return;
    try {
      await revokeApiKey(k.id);
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
    <h2 class="text-lg font-semibold">API keys</h2>
    <button
      class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white"
      onclick={() => { showMint = !showMint; minted = null; }}
    >{showMint ? "Cancel" : "Create key"}</button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Bearer tokens for scripts, dashboards and <em>*arr</em>-style integrations:
    <code>Authorization: Bearer &lt;key&gt;</code> against <code>/api/v1</code>.
    Scopes are coarse (read / write / admin — admin implies the others). For LLM
    tool clients use the LLM access keys above instead.
  </p>

  {#if error}
    <p class="mt-2 rounded bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>
  {/if}

  {#if minted?.key}
    <div class="mt-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm dark:border-amber-600 dark:bg-amber-950">
      <p class="font-medium">Key created — copy it now; it is never shown again.</p>
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
        <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" required bind:value={mintName} placeholder="grafana-readonly" />
      </label>
      <label class="grid gap-1 text-sm">
        <span>Service account (owner)</span>
        <select class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={mintAccount}>
          {#each accounts as a (a.id)}
            <option value={a.id} disabled={a.disabled}>{a.name}{a.disabled ? " (disabled)" : ""}</option>
          {/each}
          <option value="__new__">+ new service account…</option>
        </select>
        {#if mintAccount === "__new__"}
          <input class="mt-1 rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={newAccountName} placeholder="e.g. grafana, sonarr, backup-script" />
        {/if}
        <span class="text-xs text-slate-500 dark:text-slate-400">Every key belongs to a service account — disable the account to cut all its keys at once, delete it to revoke them. Manage accounts below.</span>
      </label>
      <fieldset class="grid gap-2 text-sm">
        <legend class="mb-1">Scopes</legend>
        {#each scopes as s (s.name)}
          <label class="flex items-start gap-2">
            <input
              type="checkbox"
              class="mt-1"
              checked={mintScopes.includes(s.name)}
              onchange={(e) => toggleScope(s.name, (e.currentTarget as HTMLInputElement).checked)}
            />
            <span>
              <span class="font-medium">{s.name}</span>
              <span class="block text-xs text-slate-500 dark:text-slate-400">{s.description}</span>
            </span>
          </label>
        {/each}
      </fieldset>
      <label class="grid max-w-xs gap-1 text-sm">
        <span>Expires (days, blank = never)</span>
        <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" type="number" min="1" max="3650" bind:value={mintExpires} />
      </label>
      <div>
        <button class="rounded bg-[var(--accent)] px-4 py-1.5 text-sm text-white" type="submit">Create</button>
      </div>
    </form>
  {/if}

  {#if loaded && keys.length === 0}
    <p class="mt-3 text-sm text-slate-500 dark:text-slate-400">No API keys yet.</p>
  {:else if keys.length > 0}
    <div class="mt-3 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2">Name</th>
            <th class="px-3 py-2">Service account</th>
            <th class="px-3 py-2">Prefix</th>
            <th class="px-3 py-2">Scopes</th>
            <th class="px-3 py-2">Expires</th>
            <th class="px-3 py-2">Last used</th>
            <th class="px-3 py-2">Created</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {#each keys as k (k.id)}
            <tr class="border-t border-slate-100 dark:border-slate-800" class:opacity-60={k.expired}>
              <td class="px-3 py-2">{k.name}</td>
              <td class="px-3 py-2 text-xs">{k.service_account ?? "—"}</td>
              <td class="px-3 py-2 font-mono text-xs">{k.prefix}…</td>
              <td class="px-3 py-2">
                {#each k.scopes as s (s)}
                  <span class="mr-1 rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800" class:bg-red-200={s === "admin"} class:dark:bg-red-900={s === "admin"}>{s}</span>
                {/each}
              </td>
              <td class="px-3 py-2">{fmt(k.expires_at)}{#if k.expired} <span class="text-xs text-red-600">expired</span>{/if}</td>
              <td class="px-3 py-2">{fmt(k.last_used_at)}</td>
              <td class="px-3 py-2">{fmt(k.created_at)}</td>
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
