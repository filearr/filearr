<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import { listAudit, friendlyError, type SecurityEvent } from "./api";
  import { Alert, Badge, Button, DataTable, Field, Input, Select, type Tone } from "./ui";

  // P6-T9 security audit feed (admin). Filterable by event type + time range,
  // keyset-paginated (Load more appends the next page).
  // A failed reset (filter change, Apply, first load) replaces the table with
  // a retryable error; a failed "Load more" shows under its button and keeps
  // the rows already loaded.
  let loadError = $state("");
  let moreError = $state("");
  let events = $state<SecurityEvent[]>([]);
  let nextCursor = $state<string | null>(null);
  let loading = $state(false);
  let loadingMore = $state(false);

  let fType = $state("");
  let fSince = $state("");
  let fUntil = $state("");

  const EVENT_TYPES = [
    "", "login_success", "ldap_login", "oidc_login", "login_failure", "lockout",
    "logout", "bootstrap", "password_change", "user_created", "user_disabled",
    "user_enabled", "role_changed", "user_deleted", "grant_created", "grant_deleted",
    "group_created", "group_deleted", "group_membership_changed", "session_revoked",
    "search",
  ];

  function isoOrEmpty(v: string): string | undefined {
    if (!v) return undefined;
    const d = new Date(v);
    return isNaN(d.getTime()) ? undefined : d.toISOString();
  }

  // Last-request-wins: a filter change (load(true)) aborts an older fetch, so a
  // stale page never replaces — or appends to — the newer result.
  const auditLoader = latest((signal, filters: Parameters<typeof listAudit>[0]) =>
    listAudit(filters, signal),
  );
  onDestroy(() => auditLoader.cancel());

  async function load(reset: boolean) {
    if (reset) loadError = "";
    moreError = "";
    loading = true;
    loadingMore = !reset;
    try {
      const page = await auditLoader.run({
        event_type: fType || undefined,
        since: isoOrEmpty(fSince),
        until: isoOrEmpty(fUntil),
        cursor: reset ? undefined : nextCursor || undefined,
        limit: 50,
      });
      if (page === undefined) return;
      events = reset ? page.events : [...events, ...page.events];
      nextCursor = page.next_cursor;
      loading = false;
      loadingMore = false;
    } catch (e) {
      if (reset) loadError = friendlyError(e);
      else moreError = friendlyError(e);
      loading = false;
      loadingMore = false;
    }
  }

  onMount(() => load(true));

  function when(ts: string): string {
    try { return new Date(ts).toLocaleString(); } catch { return ts; }
  }
  function eventTone(t: string): Tone {
    if (t === "login_failure" || t === "lockout" || t === "user_deleted") return "danger";
    if (t.endsWith("_login") || t === "login_success" || t === "bootstrap") return "ok";
    return "neutral";
  }
</script>

{#snippet whenCell(e: SecurityEvent)}<span class="whitespace-nowrap text-xs text-muted">{when(e.ts)}</span>{/snippet}
{#snippet eventCell(e: SecurityEvent)}<Badge tone={eventTone(e.event_type)}>{e.event_type}</Badge>{/snippet}
{#snippet whoCell(e: SecurityEvent)}
  <span class="text-xs">{e.username_attempted ?? (e.principal_id ? e.principal_id.slice(0, 8) : "—")}</span>
{/snippet}
{#snippet ipCell(e: SecurityEvent)}<span class="font-mono text-xs text-muted">{e.ip ?? "—"}</span>{/snippet}
{#snippet detailsCell(e: SecurityEvent)}
  <span class="text-xs text-muted">{e.details ? JSON.stringify(e.details) : ""}</span>
{/snippet}

<section class="mt-8">
  <h2 class="text-lg font-semibold">Security audit</h2>
  <p class="text-xs text-slate-500">
    Login/logout, account lifecycle, permission changes and lockouts.
  </p>

  <div class="mt-3 flex flex-wrap items-end gap-2">
    <Field label="Event type">
      <Select bind:value={fType} onchange={() => load(true)}>
        {#each EVENT_TYPES as t (t)}
          <option value={t}>{t === "" ? "all" : t}</option>
        {/each}
      </Select>
    </Field>
    <Field label="Since"><Input type="datetime-local" bind:value={fSince} /></Field>
    <Field label="Until"><Input type="datetime-local" bind:value={fUntil} /></Field>
    <Button onclick={() => load(true)}>Apply</Button>
  </div>

  <DataTable
    class="mt-3"
    caption="Security events"
    captionHidden
    columns={[
      { key: "ts", label: "When", cell: whenCell },
      { key: "event_type", label: "Event", cell: eventCell },
      { key: "username_attempted", label: "Who", cell: whoCell },
      { key: "ip", label: "IP", cell: ipCell },
      { key: "details", label: "Details", cell: detailsCell },
    ]}
    rows={events}
    rowKey={(e) => e.id}
    loading={loading && !loadingMore}
    error={loadError || null}
    onRetry={() => load(true)}
    emptyMessage="No events match these filters. Widen the time range or pick all event types."
  />

  {#if nextCursor}
    <Button class="mt-3" disabled={loading} onclick={() => load(false)}>
      {loading ? "Loading…" : "Load more"}
    </Button>
  {/if}
  {#if moreError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (moreError = "")}>{moreError}</Alert>
  {/if}
</section>
