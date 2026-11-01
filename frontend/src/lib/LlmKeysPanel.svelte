<script lang="ts">
  // LLM API keys (M1): mint role-scoped keys for LLM tool clients (OpenWebUI,
  // Ollama loops, MCP). The key material is shown exactly once at mint time;
  // the panel also surfaces the per-key system-prompt / capabilities URLs.
  import {
    createServiceAccount,
    listLlmKeys,
    listLlmRoles,
    listServiceAccounts,
    mintLlmKey,
    revokeLlmKey,
    friendlyError,
    type LlmKey,
    type LlmRoleInfo,
    type ServiceAccountOut,
  } from "./api";
  import { COPY_FAILED, copyText } from "./clipboard";
  import { intOrNull } from "./numInput";
  import { Alert, Badge, Button, DataTable, Field, Input, Select, confirm, toast } from "./ui";

  let keys = $state<LlmKey[]>([]);
  let roles = $state<LlmRoleInfo[]>([]);
  let loadError = $state("");
  let mintError = $state("");
  let revokeError = $state("");
  let loaded = $state(false);

  let showMint = $state(false);
  let mintName = $state("");
  // 2026-08-20: LLM keys are owned by a service account like plain API keys.
  let accounts = $state<ServiceAccountOut[]>([]);
  let mintAccount = $state<string>("");
  let newAccountName = $state("");
  let mintRole = $state("librarian");
  // bind:value on a number input writes a number, or null when emptied.
  let mintExpires = $state<number | null>(null);
  let expiresError = $state("");
  let mintPathScope = $state("");
  let minted = $state<LlmKey | null>(null);
  let copied = $state(false);

  async function load() {
    try {
      const [k, r, a] = await Promise.all([
        listLlmKeys(),
        listLlmRoles(),
        listServiceAccounts().catch(() => ({ service_accounts: [] as ServiceAccountOut[] })),
      ]);
      keys = k.keys;
      roles = r.roles;
      accounts = a.service_accounts;
      if (!mintAccount && accounts.length) mintAccount = accounts[0].id;
      loaded = true;
      loadError = "";
    } catch (e) {
      loadError = friendlyError(e, "manage LLM keys");
    }
  }
  $effect(() => {
    void load();
  });

  const roleInfo = $derived(roles.find((r) => r.name === mintRole));

  async function mint(e: SubmitEvent) {
    e.preventDefault();
    mintError = "";
    expiresError = "";
    // The form is novalidate (it reports its own errors), so the name check
    // the native `required` used to make happens here.
    if (!mintName.trim()) {
      mintError = "Name the key.";
      return;
    }
    // The novalidate form drops the native min/max, so the expiry range is
    // checked here, before a service account can be created for a key that
    // would then fail (0 would otherwise mint a never-expiring key).
    const days = intOrNull(mintExpires, { min: 1, max: 3650 });
    if (mintExpires !== null && days === null) {
      expiresError = "Expiry must be 1–3650 whole days, or blank.";
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
      minted = await mintLlmKey({
        name: mintName.trim(),
        service_account_id: owner,
        role: mintRole,
        path_scope: mintPathScope.trim() || null,
        expires_days: days,
      });
      mintName = "";
      mintPathScope = "";
      mintExpires = null;
      showMint = false;
      await load();
    } catch (e2) {
      mintError = friendlyError(e2, "mint an LLM key");
    }
  }

  async function revoke(k: LlmKey) {
    const ok = await confirm({
      title: `Revoke LLM key "${k.name}" (${k.prefix}…)?`,
      message: "Clients using it stop working immediately.",
      confirmLabel: "Revoke",
      tone: "danger",
    });
    if (!ok) return;
    revokeError = "";
    try {
      await revokeLlmKey(k.id);
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

{#snippet nameCell(k: LlmKey)}{k.name}{/snippet}
{#snippet prefixCell(k: LlmKey)}<span class="font-mono text-xs">{k.prefix}…</span>{/snippet}
{#snippet roleCell(k: LlmKey)}
  <Badge>{k.role}</Badge>
  {#if k.role_description}<div class="mt-0.5 text-xs text-muted">{k.role_description}</div>{/if}
  {#if k.path_scope}<div class="mt-0.5 text-xs text-muted">scoped: <span class="font-mono">{k.path_scope}</span></div>{/if}
{/snippet}
{#snippet contentCell(k: LlmKey)}{k.content_access ? "yes" : "no"}{/snippet}
{#snippet expiresCell(k: LlmKey)}{fmt(k.expires_at)}{/snippet}
{#snippet callsCell(k: LlmKey)}
  <span class="tabular-nums">{(k.tool_calls ?? 0).toLocaleString()}</span>
  {#if k.last_call_at}<div class="text-xs text-muted">last tool call {fmt(k.last_call_at)}</div>{/if}
{/snippet}
{#snippet lastUsedCell(k: LlmKey)}{fmt(k.last_used_at)}{/snippet}
{#snippet actionsCell(k: LlmKey)}
  <div class="flex justify-end">
    <Button size="sm" variant="danger" onclick={() => revoke(k)}>Revoke</Button>
  </div>
{/snippet}

<section class="mt-8">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">LLM access keys</h2>
    <Button
      variant={showMint ? "secondary" : "primary"}
      onclick={() => { showMint = !showMint; minted = null; mintError = ""; expiresError = ""; }}
    >{showMint ? "Cancel" : "Mint key"}</Button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Role-scoped keys for LLM tool clients (OpenWebUI external tools, Ollama
    loops, MCP). Point the client at <code>/api/llm/v1</code> with the key as a
    Bearer token; fetch <code>/api/llm/v1/system-prompt</code> with the same key
    for the ready-made, role-accurate system prompt.
  </p>

  {#if minted?.key}
    <Alert tone="warn" class="mt-3" title="Key minted — copy it now; it is never shown again.">
      <div class="mt-2 flex items-center gap-2">
        <code class="break-all rounded-control bg-surface px-2 py-1 text-fg">{minted.key}</code>
        <Button size="sm" onclick={copyKey}>{copied ? "Copied" : "Copy"}</Button>
      </div>
    </Alert>
  {/if}

  {#if showMint}
    <form class="mt-3 grid max-w-2xl gap-3 rounded-control border border-border p-4" novalidate onsubmit={mint}>
      <Field label="Name" required>
        <Input bind:value={mintName} placeholder="openwebui-librarian" />
      </Field>
      <Field
        label="Service account (owner)"
        hint="Disable the account to cut all its keys at once; delete it to revoke them (accounts are managed on the Admin tab)."
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
          <Input bind:value={newAccountName} placeholder="e.g. openwebui, home-assistant" />
        </Field>
      {/if}
      <Field
        label="Role"
        hint={roleInfo ? `${roleInfo.description} Tools: ${roleInfo.tools.join(", ")}.` : undefined}
      >
        <Select bind:value={mintRole}>
          {#each roles as r (r.name)}
            <option value={r.name}>{r.name}</option>
          {/each}
        </Select>
      </Field>
      <div class="grid grid-cols-2 gap-3">
        <Field label="Expires (days, blank = never)" error={expiresError}>
          <Input type="number" min="1" max="3650" bind:value={mintExpires} />
        </Field>
        <Field label="Path scope (optional, ltree)">
          <Input bind:value={mintPathScope} placeholder="lib_<uuid>.media.movies" />
        </Field>
      </div>
      <div>
        <Button type="submit" variant="primary">Mint</Button>
      </div>
      {#if mintError}
        <Alert tone="danger" onDismiss={() => (mintError = "")}>{mintError}</Alert>
      {/if}
    </form>
  {/if}

  <DataTable
    class="mt-3"
    caption="LLM access keys"
    captionHidden
    columns={[
      { key: "name", label: "Name", cell: nameCell },
      { key: "prefix", label: "Prefix", cell: prefixCell },
      { key: "role", label: "Role", cell: roleCell },
      { key: "content_access", label: "Content", cell: contentCell },
      { key: "rate_limit", label: "Rate/min" },
      { key: "expires_at", label: "Expires", cell: expiresCell },
      { key: "tool_calls", label: "Calls", align: "right", cell: callsCell },
      { key: "last_used_at", label: "Last used", cell: lastUsedCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={keys}
    rowKey={(k) => k.id}
    loading={!loaded}
    error={loadError || null}
    onRetry={load}
    emptyMessage="No LLM keys yet. Use “Mint key” to create one for an LLM tool client."
  />
  {#if revokeError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (revokeError = "")}>{revokeError}</Alert>
  {/if}
</section>
