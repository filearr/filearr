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
  import { Alert, Badge, Button, DataTable, Field, Input, confirm, toneForStatus } from "./ui";

  let rows = $state<ServiceAccountOut[]>([]);
  let loadError = $state("");
  let createError = $state("");
  let rowError = $state(""); // rename / enable-disable / delete
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
      loadError = "";
    } catch (e) {
      loadError = friendlyError(e, "load service accounts");
    }
  }
  $effect(() => {
    void load();
  });

  async function create(e: SubmitEvent) {
    e.preventDefault();
    createError = "";
    try {
      await createServiceAccount({ name: newName.trim(), description: newDesc.trim() || null });
      newName = ""; newDesc = "";
      await load();
    } catch (e2) {
      createError = friendlyError(e2, "create the service account");
    }
  }
  async function toggle(a: ServiceAccountOut) {
    const n = a.key_count + a.llm_key_count;
    const ok = await confirm(
      a.disabled
        ? { title: `Enable "${a.name}"?`, message: `Its ${n} key(s) start working again.`, confirmLabel: "Enable" }
        : {
            title: `Disable "${a.name}"?`,
            message: `Every key it owns (${n}) is refused from the next request on. Reversible.`,
            confirmLabel: "Disable",
            tone: "danger",
          },
    );
    if (!ok) return;
    rowError = "";
    try {
      await patchServiceAccount(a.id, { disabled: !a.disabled });
      await load();
    } catch (e2) {
      rowError = friendlyError(e2, "update the service account");
    }
  }
  async function saveEdit(a: ServiceAccountOut) {
    rowError = "";
    try {
      await patchServiceAccount(a.id, { name: editName.trim(), description: editDesc.trim() || null });
      editing = null;
      await load();
    } catch (e2) {
      rowError = friendlyError(e2, "rename the service account");
    }
  }
  async function remove(a: ServiceAccountOut) {
    const n = a.key_count + a.llm_key_count;
    const ok = await confirm({
      title: `Delete "${a.name}"?`,
      message: n ? `This REVOKES its ${n} key(s) permanently.` : undefined,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    rowError = "";
    try {
      await deleteServiceAccount(a.id);
      await load();
    } catch (e2) {
      rowError = friendlyError(e2, "delete the service account");
    }
  }
  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
</script>

{#snippet accountCell(a: ServiceAccountOut)}
  {#if editing === a.id}
    <div class="flex w-72 flex-col items-start gap-1">
      <Input size="sm" aria-label="Name" bind:value={editName} />
      <Input size="sm" aria-label="Description" bind:value={editDesc} placeholder="description" />
      <div class="flex gap-1">
        <Button size="sm" variant="primary" onclick={() => saveEdit(a)}>save</Button>
        <Button size="sm" variant="ghost" onclick={() => (editing = null)}>cancel</Button>
      </div>
    </div>
  {:else}
    <div class="font-medium">{a.name}</div>
    {#if a.description}<div class="text-xs text-muted">{a.description}</div>{/if}
  {/if}
{/snippet}
{#snippet keysCell(a: ServiceAccountOut)}<span class="tabular-nums">{a.key_count}</span>{/snippet}
{#snippet llmKeysCell(a: ServiceAccountOut)}<span class="tabular-nums">{a.llm_key_count}</span>{/snippet}
{#snippet lastUsedCell(a: ServiceAccountOut)}{fmt(a.last_used_at)}{/snippet}
{#snippet statusCell(a: ServiceAccountOut)}
  {@const status = a.disabled ? "disabled" : "active"}
  <Badge tone={toneForStatus(status)}>{status}</Badge>
{/snippet}
{#snippet actionsCell(a: ServiceAccountOut)}
  <div class="flex justify-end gap-1 whitespace-nowrap">
    <Button size="sm" variant="ghost" onclick={() => { editing = a.id; editName = a.name; editDesc = a.description ?? ""; }}>rename</Button>
    <Button size="sm" variant="ghost" onclick={() => toggle(a)}>{a.disabled ? "enable" : "disable"}</Button>
    <Button size="sm" variant="danger" onclick={() => remove(a)}>delete</Button>
  </div>
{/snippet}

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
  <form class="mt-3 flex flex-wrap items-end gap-2" onsubmit={create}>
    <Field label="Name" required><Input bind:value={newName} placeholder="grafana" /></Field>
    <Field label="Description (optional)" class="w-72">
      <Input bind:value={newDesc} placeholder="dashboards on the ops box" />
    </Field>
    <Button type="submit" variant="primary">Create</Button>
  </form>
  {#if createError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (createError = "")}>{createError}</Alert>
  {/if}
  <DataTable
    class="mt-3"
    caption="Service accounts"
    captionHidden
    columns={[
      { key: "name", label: "Account", cell: accountCell },
      { key: "key_count", label: "Keys", cell: keysCell },
      { key: "llm_key_count", label: "LLM keys", cell: llmKeysCell },
      { key: "last_used_at", label: "Last used", cell: lastUsedCell },
      { key: "disabled", label: "Status", cell: statusCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    {rows}
    rowKey={(a) => a.id}
    loading={!loaded}
    error={loadError || null}
    onRetry={load}
    emptyMessage="No service accounts yet — create one above, or from the API keys form."
  />
  {#if rowError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (rowError = "")}>{rowError}</Alert>
  {/if}
</section>
