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
  import { COPY_FAILED, copyText } from "./clipboard";
  import { Alert, Badge, Button, Checkbox, DataTable, Field, Input, Select, confirm, toast } from "./ui";

  let keys = $state<ApiKeyRow[]>([]);
  let scopes = $state<ApiKeyScopeInfo[]>([]);
  let accounts = $state<ServiceAccountOut[]>([]);
  let mintAccount = $state("");
  let newAccountName = $state("");
  let loadError = $state("");
  let mintError = $state("");
  let revokeError = $state("");
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
      loadError = "";
    } catch (e) {
      loadError = friendlyError(e, "manage API keys");
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
    mintError = "";
    // The form is novalidate (it reports its own errors), so the name check
    // the native `required` used to make happens here.
    if (!mintName.trim()) {
      mintError = "Name the key.";
      return;
    }
    if (mintScopes.length === 0) {
      mintError = "Pick at least one scope.";
      return;
    }
    try {
      let owner = mintAccount;
      if (owner === "__new__") {
        if (!newAccountName.trim()) {
          mintError = "Name the new service account.";
          return;
        }
        owner = (await createServiceAccount({ name: newAccountName.trim() })).id;
        newAccountName = "";
      }
      if (!owner) {
        mintError = "Pick the service account this key belongs to.";
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
      mintError = friendlyError(e2, "mint an API key");
    }
  }

  async function revoke(k: ApiKeyRow) {
    const ok = await confirm({
      title: `Revoke API key "${k.name}" (${k.prefix}…)?`,
      message: "Anything using it stops working immediately.",
      confirmLabel: "Revoke",
      tone: "danger",
    });
    if (!ok) return;
    revokeError = "";
    try {
      await revokeApiKey(k.id);
      await load();
    } catch (e2) {
      revokeError = friendlyError(e2, "revoke this key");
    }
  }

  async function copyKey() {
    if (!minted?.key) return;
    if (!(await copyText(minted.key))) return toast(COPY_FAILED, { tone: "warn" });
    copied = true;
    setTimeout(() => (copied = false), 1500);
  }

  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
</script>

{#snippet nameCell(k: ApiKeyRow)}{k.name}{/snippet}
{#snippet accountCell(k: ApiKeyRow)}<span class="text-xs">{k.service_account ?? "—"}</span>{/snippet}
{#snippet prefixCell(k: ApiKeyRow)}<span class="font-mono text-xs">{k.prefix}…</span>{/snippet}
{#snippet scopesCell(k: ApiKeyRow)}
  <span class="inline-flex flex-wrap gap-1">
    {#each k.scopes as s (s)}
      <Badge tone={s === "admin" ? "danger" : "neutral"}>{s}</Badge>
    {/each}
  </span>
{/snippet}
{#snippet expiresCell(k: ApiKeyRow)}
  {fmt(k.expires_at)}{#if k.expired} <Badge tone="danger">expired</Badge>{/if}
{/snippet}
{#snippet lastUsedCell(k: ApiKeyRow)}{fmt(k.last_used_at)}{/snippet}
{#snippet createdCell(k: ApiKeyRow)}{fmt(k.created_at)}{/snippet}
{#snippet actionsCell(k: ApiKeyRow)}
  <div class="flex justify-end">
    <Button size="sm" variant="danger" onclick={() => revoke(k)}>Revoke</Button>
  </div>
{/snippet}

<section class="mt-8">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">API keys</h2>
    <Button
      variant={showMint ? "secondary" : "primary"}
      onclick={() => { showMint = !showMint; minted = null; mintError = ""; }}
    >{showMint ? "Cancel" : "Create key"}</Button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Bearer tokens for scripts, dashboards and <em>*arr</em>-style integrations:
    <code>Authorization: Bearer &lt;key&gt;</code> against <code>/api/v1</code>.
    Scopes are coarse (read / write / admin — admin implies the others). For LLM
    tool clients use the LLM access keys above instead.
  </p>

  {#if minted?.key}
    <Alert tone="warn" class="mt-3" title="Key created — copy it now; it is never shown again.">
      <div class="mt-2 flex items-center gap-2">
        <code class="break-all rounded-control bg-surface px-2 py-1 text-fg">{minted.key}</code>
        <Button size="sm" onclick={copyKey}>{copied ? "Copied" : "Copy"}</Button>
      </div>
    </Alert>
  {/if}

  {#if showMint}
    <form class="mt-3 grid max-w-2xl gap-3 rounded-control border border-border p-4" novalidate onsubmit={mint}>
      <Field label="Name" required>
        <Input bind:value={mintName} placeholder="grafana-readonly" />
      </Field>
      <Field
        label="Service account (owner)"
        hint="Every key belongs to a service account — disable the account to cut all its keys at once, delete it to revoke them. Manage accounts below."
      >
        <Select bind:value={mintAccount}>
          {#each accounts as a (a.id)}
            <option value={a.id} disabled={a.disabled}>{a.name}{a.disabled ? " (disabled)" : ""}</option>
          {/each}
          <option value="__new__">+ new service account…</option>
        </Select>
      </Field>
      {#if mintAccount === "__new__"}
        <Field label="New service account name">
          <Input bind:value={newAccountName} placeholder="e.g. grafana, sonarr, backup-script" />
        </Field>
      {/if}
      <fieldset class="grid gap-2">
        <legend class="mb-1 text-xs font-medium text-fg">Scopes</legend>
        {#each scopes as s (s.name)}
          <Checkbox
            label={s.name}
            description={s.description}
            checked={mintScopes.includes(s.name)}
            onchange={(e) => toggleScope(s.name, e.currentTarget.checked)}
          />
        {/each}
      </fieldset>
      <Field label="Expires (days, blank = never)" class="max-w-xs">
        <Input type="number" min="1" max="3650" bind:value={mintExpires} />
      </Field>
      <div>
        <Button type="submit" variant="primary">Create</Button>
      </div>
      {#if mintError}
        <Alert tone="danger" onDismiss={() => (mintError = "")}>{mintError}</Alert>
      {/if}
    </form>
  {/if}

  <DataTable
    class="mt-3"
    caption="API keys"
    captionHidden
    columns={[
      { key: "name", label: "Name", cell: nameCell },
      { key: "service_account", label: "Service account", cell: accountCell },
      { key: "prefix", label: "Prefix", cell: prefixCell },
      { key: "scopes", label: "Scopes", cell: scopesCell },
      { key: "expires_at", label: "Expires", cell: expiresCell },
      { key: "last_used_at", label: "Last used", cell: lastUsedCell },
      { key: "created_at", label: "Created", cell: createdCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={keys}
    rowKey={(k) => k.id}
    loading={!loaded}
    error={loadError || null}
    onRetry={load}
    emptyMessage="No API keys yet. Use “Create key” to mint one."
  />
  {#if revokeError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (revokeError = "")}>{revokeError}</Alert>
  {/if}
</section>
