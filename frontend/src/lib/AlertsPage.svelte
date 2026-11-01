<script lang="ts">
  // P8-T12/T13 — the Alerts admin surface: notification channels, alert rules
  // (file-watch + read-only is_system operational rules) and a recent-events
  // view with failed-delivery surfacing. All admin-scope. Secret channel fields
  // are password inputs whose decrypted value NEVER round-trips (empty on edit =
  // keep, via the "__unchanged__" sentinel). Every dynamic string renders as
  // text (Svelte auto-escapes) — a crafted path/error can't inject markup.
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import {
    CHANNEL_TYPES,
    DISPATCH_LOCALITIES,
    DIGEST_WINDOWS,
    EVENT_TYPES,
    UNCHANGED,
    alertEventsSummary,
    createAlertChannel,
    createAlertRule,
    deleteAlertChannel,
    deleteAlertRule,
    listAlertChannels,
    listAlertEvents,
    bulkUpdateAlertRules,
    listAlertRules,
    listLibraries,
    testAlertChannel,
    updateAlertChannel,
    updateAlertRule,
    WEBHOOK_FORMATS,
    detectWebhookFormat,
    type AlertChannel,
    type AlertEvent,
    type AlertEventStatus,
    type AlertEventSummary,
    type AlertRule,
    type ChannelType,
    type DispatchLocality,
    type DigestWindow,
    type WebhookFormat,
    type Library,
    type TestFireResult,
    errorMessage,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { focusPanel, focusRowButton } from "./rowPanel";
  import { Alert, Badge, Button, Checkbox, DataTable, Field, Input, Select, StateBlock, confirm, type SortState, type Tone } from "./ui";

  type Tab = "channels" | "rules" | "events";
  let tab = $state<Tab>("channels");

  let libraries = $state<Library[]>([]);
  let channels = $state<AlertChannel[]>([]);
  let rules = $state<AlertRule[]>([]);
  // `error` is the config load: in place of the page until the first load
  // lands, then an Alert over the kept data. Action errors live beside their
  // control (chErr/rlErr/bulkErr under Save/Apply, chRowErr/rlRowErr per row).
  let error = $state("");
  let loading = $state(true);
  let loaded = $state(false);

  const libName = (id: string | null) =>
    id ? (libraries.find((l) => l.id === id)?.name ?? id) : "All libraries";
  const chName = (id: string) => channels.find((c) => c.id === id)?.name ?? id;

  // Last-request-wins for both loaders: back-to-back mutations each refresh,
  // and filter flips each reload events; only the newest response lands.
  const configLoader = latest((signal) =>
    Promise.all([listLibraries(signal), listAlertChannels(signal), listAlertRules(signal)]),
  );
  const eventsLoader = latest(
    (signal, filters: { rule_id?: string; status?: AlertEventStatus; limit: number }) =>
      Promise.all([listAlertEvents(filters, signal), alertEventsSummary(undefined, signal)]),
  );
  onDestroy(() => {
    configLoader.cancel();
    eventsLoader.cancel();
  });

  async function refresh() {
    loading = true;
    try {
      const r = await configLoader.run();
      if (r === undefined) return;
      [libraries, channels, rules] = r;
      error = "";
      loading = false;
      loaded = true;
    } catch (e) {
      error = errorMessage(e);
      loading = false;
    }
  }
  onMount(refresh);

  // ---- channels -------------------------------------------------------- //
  let chEditing = $state<AlertChannel | null>(null); // null while not editing
  let chNew = $state(false);
  let chName_ = $state("");
  let chType = $state<ChannelType>("webhook");
  let chLocality = $state<DispatchLocality>("central");
  let chEnabled = $state(true);
  // per-type config fields
  let cWebhookUrl = $state("");
  let cWebhookSecret = $state("");
  let cWebhookFormat = $state<WebhookFormat>("generic");
  // FIX-16: once the operator picks a format by hand, stop auto-detecting from URL.
  let chFormatTouched = $state(false);
  let cEmailHost = $state("");
  let cEmailPort = $state(587);
  let cEmailSecurity = $state("starttls");
  let cEmailFrom = $state("");
  let cEmailTo = $state("");
  let cEmailUser = $state("");
  let cEmailPassword = $state("");
  let cAppriseUrl = $state("");
  let chBusy = $state(false);
  let chErr = $state("");
  // Required-field messages, keyed by field; the form is novalidate.
  let chFieldErr = $state<Record<string, string>>({});
  let chRowErr = $state<Record<string, string>>({});
  let testResults = $state<Record<string, TestFireResult | { error: string }>>({});

  function resetChannelForm() {
    chEditing = null;
    chNew = false;
    chName_ = "";
    chType = "webhook";
    chLocality = "central";
    chEnabled = true;
    cWebhookUrl = cWebhookSecret = "";
    cWebhookFormat = "generic";
    chFormatTouched = false;
    cEmailHost = cEmailFrom = cEmailTo = cEmailUser = cEmailPassword = "";
    cEmailPort = 587;
    cEmailSecurity = "starttls";
    cAppriseUrl = "";
    chErr = "";
    chFieldErr = {};
  }

  function startNewChannel() {
    resetChannelForm();
    chNew = true;
  }

  function startEditChannel(c: AlertChannel) {
    resetChannelForm();
    chEditing = c;
    chName_ = c.name;
    chType = c.type;
    chLocality = c.dispatch_locality;
    chEnabled = c.enabled;
    const cfg = c.config ?? {};
    if (c.type === "webhook") {
      cWebhookUrl = String(cfg.url ?? "");
      cWebhookSecret = ""; // secret redacted on read; empty = keep
      cWebhookFormat = (String(cfg.webhook_format ?? "generic") as WebhookFormat);
      chFormatTouched = true; // an existing channel keeps its stored format
    } else if (c.type === "email") {
      cEmailHost = String(cfg.host ?? "");
      cEmailPort = Number(cfg.port ?? 587);
      cEmailSecurity = String(cfg.security ?? "starttls");
      cEmailFrom = String(cfg.from_addr ?? cfg.from ?? "");
      const to = cfg.to ?? cfg.to_addrs ?? [];
      cEmailTo = Array.isArray(to) ? to.join(", ") : String(to);
      cEmailUser = String(cfg.username ?? "");
      cEmailPassword = ""; // keep unless typed
    } else {
      cAppriseUrl = ""; // whole url is the secret; empty = keep
    }
  }

  // Build the config for the API. `editing` decides whether an empty secret
  // means "keep" (send the UNCHANGED sentinel) rather than "clear".
  function buildConfig(editing: boolean): Record<string, unknown> {
    if (chType === "webhook") {
      const secret = cWebhookSecret
        ? cWebhookSecret
        : editing
          ? UNCHANGED
          : undefined;
      return {
        url: cWebhookUrl,
        webhook_format: cWebhookFormat,
        ...(secret !== undefined ? { secret } : {}),
      };
    }
    if (chType === "email") {
      const password = cEmailPassword
        ? cEmailPassword
        : editing
          ? UNCHANGED
          : undefined;
      return {
        host: cEmailHost,
        port: cEmailPort,
        security: cEmailSecurity,
        from_addr: cEmailFrom,
        to: cEmailTo.split(/[\n,]/).map((s) => s.trim()).filter(Boolean),
        username: cEmailUser,
        ...(password !== undefined ? { password } : {}),
      };
    }
    // apprise: the whole url is the secret
    const url = cAppriseUrl ? cAppriseUrl : editing ? UNCHANGED : "";
    return { url };
  }

  // FIX-16: auto-detect the format from the URL until the operator overrides it.
  function onWebhookUrlInput() {
    if (!chFormatTouched) cWebhookFormat = detectWebhookFormat(cWebhookUrl);
  }

  // The checks the old native `required` attributes made, per channel type.
  function validateChannel(): Record<string, string> {
    const errs: Record<string, string> = {};
    const need = (key: string, v: string, label: string) => {
      if (!v.trim()) errs[key] = `${label} is required.`;
    };
    need("name", chName_, "Name");
    if (chType === "webhook") need("url", cWebhookUrl, "URL");
    else if (chType === "email") {
      need("host", cEmailHost, "SMTP host");
      need("from", cEmailFrom, "From");
      need("to", cEmailTo, "To");
    }
    return errs;
  }

  async function saveChannel(e: Event) {
    e.preventDefault();
    chFieldErr = validateChannel();
    if (Object.keys(chFieldErr).length) return;
    chBusy = true;
    chErr = "";
    try {
      if (chEditing) {
        await updateAlertChannel(chEditing.id, {
          name: chName_,
          config: buildConfig(true),
          dispatch_locality: chLocality,
          enabled: chEnabled,
        });
      } else {
        await createAlertChannel({
          name: chName_,
          type: chType,
          config: buildConfig(false),
          dispatch_locality: chLocality,
          enabled: chEnabled,
        });
      }
      resetChannelForm();
      await refresh();
    } catch (err) {
      chErr = errorMessage(err, { verb: "manage alerts" });
    } finally {
      chBusy = false;
    }
  }

  async function removeChannel(c: AlertChannel) {
    const ok = await confirm({
      title: "Delete channel?",
      message: `Delete channel "${c.name}"? Rules using it lose this destination.`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    chRowErr[c.id] = "";
    try {
      await deleteAlertChannel(c.id);
      await refresh();
    } catch (err) {
      chRowErr[c.id] = errorMessage(err, { verb: "manage alerts" });
    }
  }

  async function toggleChannelEnabled(c: AlertChannel) {
    chRowErr[c.id] = "";
    try {
      await updateAlertChannel(c.id, { enabled: !c.enabled });
      await refresh();
    } catch (err) {
      chRowErr[c.id] = errorMessage(err, { verb: "manage alerts" });
    }
  }

  // A channel's test progress/result renders below the table (rowPanel.ts):
  // firing focuses it, dismissing returns focus to the row's test button.
  const uid = $props.id();
  const testPanelId = (channelId: string) => `${uid}-test-${channelId}`;

  function dismissTest(c: AlertChannel) {
    const { [c.id]: _, ...rest } = testResults;
    testResults = rest;
    void focusRowButton("data-test-for", c.id);
  }

  async function fireTest(c: AlertChannel) {
    testResults = { ...testResults, [c.id]: { error: "…" } as unknown as TestFireResult };
    void focusPanel(testPanelId(c.id));
    try {
      testResults = { ...testResults, [c.id]: await testAlertChannel(c.id) };
    } catch (err) {
      testResults = { ...testResults, [c.id]: { error: errorMessage(err) } };
    }
  }

  // ---- rules ----------------------------------------------------------- //
  let rlEditing = $state<AlertRule | null>(null);
  let rlNew = $state(false);
  let rName = $state("");
  let rLibrary = $state<string>(""); // "" = all libraries
  let rGlob = $state("");
  let rEvents = $state<string[]>(["created"]);
  let rHashOnly = $state(false);
  let rThrottle = $state<"immediate" | "digest">("immediate");
  let rGroupWait = $state(30);
  let rDigest = $state<DigestWindow>("hourly");
  let rRepeat = $state<string>(""); // seconds, blank = none
  let rChannels = $state<string[]>([]);
  let rEnabled = $state(true);
  let rShowAdvanced = $state(false);
  // 2026-08-20 mass edit: selection + one change-set applied to many rules.
  let bulkSel = $state<Set<string>>(new Set());
  let bulkThrottle = $state<"" | "immediate" | "hourly" | "daily">("");
  let bulkGroupWait = $state(30);
  let bulkChannelMode = $state<"set" | "add" | "remove">("set");
  let bulkChannels = $state<string[]>([]);
  let bulkTouchChannels = $state(false);
  let bulkBusy = $state(false);
  let bulkErr = $state("");

  function bulkToggle(id: string) {
    const next = new Set(bulkSel);
    if (next.has(id)) next.delete(id);
    else next.add(id);
    bulkSel = next;
  }

  async function applyBulk() {
    if (bulkSel.size === 0) return;
    bulkBusy = true;
    bulkErr = "";
    try {
      const body: Record<string, unknown> = { rule_ids: [...bulkSel] };
      if (bulkThrottle) {
        body.set_throttle = true;
        body.digest_window = bulkThrottle === "immediate" ? null : bulkThrottle;
        if (bulkThrottle === "immediate") body.group_wait_s = bulkGroupWait;
      }
      if (bulkTouchChannels) {
        body.channel_mode = bulkChannelMode;
        body.channel_ids = bulkChannels;
      }
      await bulkUpdateAlertRules(body);
      bulkSel = new Set();
      bulkThrottle = "";
      bulkTouchChannels = false;
      await refresh();
    } catch (e) {
      bulkErr = errorMessage(e);
    } finally {
      bulkBusy = false;
    }
  }
  // Roadmap §6 polish (2026-08-19): optional extra grouping keys and inhibition.
  let rGroupExtras = $state<string[]>([]);
  let rInhibitedBy = $state<string[]>([]);
  let rInhibitWindow = $state(900);
  const GROUP_EXTRAS: { key: string; label: string; hint: string }[] = [
    { key: "folder", label: "top-level folder", hint: "one notification per first-level folder that changed" },
    { key: "extension", label: "file extension", hint: "one notification per extension" },
    { key: "file", label: "each file", hint: "one notification per file — no batching" },
  ];
  let rlBusy = $state(false);
  let rlErr = $state("");
  let rlFieldErr = $state<Record<string, string>>({});
  let rlRowErr = $state<Record<string, string>>({});

  const isSystem = $derived(rlEditing?.is_system ?? false);

  function resetRuleForm() {
    rlEditing = null;
    rlNew = false;
    rName = "";
    rLibrary = "";
    rGlob = "";
    rEvents = ["created"];
    rHashOnly = false;
    rThrottle = "immediate";
    rGroupWait = 30;
    rDigest = "hourly";
    rRepeat = "";
    rChannels = [];
    rEnabled = true;
    rShowAdvanced = false;
    rGroupExtras = [];
    rInhibitedBy = [];
    rInhibitWindow = 900;
    rlErr = "";
    rlFieldErr = {};
  }

  function startNewRule() {
    resetRuleForm();
    rlNew = true;
  }

  function startEditRule(r: AlertRule) {
    resetRuleForm();
    rlEditing = r;
    rName = r.name;
    rLibrary = r.library_id ?? "";
    rGlob = r.path_glob ?? "";
    rEvents = [...r.event_types];
    rHashOnly = r.hash_change_only;
    rThrottle = r.digest_window ? "digest" : "immediate";
    rGroupWait = r.group_wait_s;
    rDigest = r.digest_window ?? "hourly";
    rRepeat = r.repeat_interval_s != null ? String(r.repeat_interval_s) : "";
    rChannels = [...r.channel_ids];
    rEnabled = r.enabled;
    rGroupExtras = (r.group_by ?? []).filter((g) => GROUP_EXTRAS.some((x) => x.key === g));
    rInhibitedBy = [...(r.inhibited_by ?? [])];
    rInhibitWindow = r.inhibit_window_s ?? 900;
    if (rGroupExtras.length || rInhibitedBy.length) rShowAdvanced = true;
  }

  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  async function saveRule(e: Event) {
    e.preventDefault();
    // The old native `required` on Name (system rules keep their fixed name).
    rlFieldErr = !isSystem && !rName.trim() ? { name: "Name is required." } : {};
    if (Object.keys(rlFieldErr).length) return;
    rlBusy = true;
    rlErr = "";
    try {
      const repeat = rRepeat.trim() === "" ? null : Number(rRepeat);
      if (rlEditing) {
        // System rules: only channels + throttle/timings are editable.
        const patch: Record<string, unknown> = {
          enabled: rEnabled,
          group_wait_s: rThrottle === "immediate" ? rGroupWait : 0,
          digest_window: rThrottle === "digest" ? rDigest : null,
          repeat_interval_s: repeat,
          channel_ids: rChannels,
          inhibited_by: rInhibitedBy,
          inhibit_window_s: rInhibitWindow,
        };
        if (!rlEditing.is_system) {
          patch.name = rName;
          patch.library_id = rLibrary || null;
          patch.path_glob = rGlob || null;
          patch.event_types = rEvents;
          patch.hash_change_only = rHashOnly && rEvents.includes("modified");
          patch.group_by = ["event_type", "library_id", "rule_id", ...rGroupExtras];
        }
        await updateAlertRule(rlEditing.id, patch);
      } else {
        await createAlertRule({
          name: rName,
          enabled: rEnabled,
          library_id: rLibrary || null,
          path_glob: rGlob || null,
          event_types: rEvents,
          hash_change_only: rHashOnly && rEvents.includes("modified"),
          group_wait_s: rThrottle === "immediate" ? rGroupWait : 0,
          digest_window: rThrottle === "digest" ? rDigest : null,
          repeat_interval_s: repeat,
          channel_ids: rChannels,
          group_by: ["event_type", "library_id", "rule_id", ...rGroupExtras],
          inhibited_by: rInhibitedBy,
          inhibit_window_s: rInhibitWindow,
        });
      }
      resetRuleForm();
      await refresh();
    } catch (err) {
      rlErr = errorMessage(err, { verb: "manage alerts" });
    } finally {
      rlBusy = false;
    }
  }

  async function removeRule(r: AlertRule) {
    const ok = await confirm({
      title: "Delete rule?",
      message: `Delete rule "${r.name}"?`,
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    rlRowErr[r.id] = "";
    try {
      await deleteAlertRule(r.id);
      await refresh();
    } catch (err) {
      rlRowErr[r.id] = errorMessage(err, { verb: "manage alerts" });
    }
  }

  async function toggleRuleEnabled(r: AlertRule) {
    rlRowErr[r.id] = "";
    try {
      await updateAlertRule(r.id, { enabled: !r.enabled });
      await refresh();
    } catch (err) {
      rlRowErr[r.id] = errorMessage(err, { verb: "manage alerts" });
    }
  }

  function throttleLabel(r: AlertRule): string {
    const base = r.digest_window ? `${r.digest_window} digest` : `immediate (group ${r.group_wait_s}s)`;
    const extras = (r.group_by ?? []).filter((g) => GROUP_EXTRAS.some((x) => x.key === g));
    const parts = [base];
    if (extras.length) parts.push(`by ${extras.join("+")}`);
    if (r.inhibited_by?.length) parts.push(`inhibited by ${r.inhibited_by.length}`);
    return parts.join(" · ");
  }

  // ---- events ---------------------------------------------------------- //
  let events = $state<AlertEvent[]>([]);
  let summary = $state<AlertEventSummary | null>(null);
  let evRule = $state<string>("");
  let evStatus = $state<AlertEventStatus | "">("");
  let evLoading = $state(false);
  let evError = $state("");

  async function loadEvents() {
    evLoading = true;
    try {
      const r = await eventsLoader.run({
        rule_id: evRule || undefined,
        status: evStatus || undefined,
        limit: 100,
      });
      if (r === undefined) return;
      [events, summary] = r;
      evError = "";
      evLoading = false;
    } catch (e) {
      evError = errorMessage(e);
      evLoading = false;
    }
  }

  $effect(() => {
    if (tab === "events") {
      // re-run when filters change
      void evRule;
      void evStatus;
      loadEvents();
    }
  });

  const ruleName = (id: string) => rules.find((r) => r.id === id)?.name ?? id.slice(0, 8);
  const fmt = (s: string | null) => (s ? new Date(s).toLocaleString() : "—");
  // Delivered reads as success, failed as danger, anything else (pending) neutral.
  const evTone = (st: string): Tone => (st === "failed" ? "danger" : st === "delivered" ? "ok" : "neutral");

  let chSort = $state<SortState>(null);
  let ruleSort = $state<SortState>(null);
  let evSort = $state<SortState>(null);

  const allSelected = $derived(rules.length > 0 && bulkSel.size === rules.length);
  function toggleAll() {
    bulkSel = bulkSel.size === rules.length ? new Set() : new Set(rules.map((x) => x.id));
  }

  // Sort values for computed columns read what the cells show.
  function ruleValue(r: AlertRule, key: string): unknown {
    if (key === "scope") return libName(r.library_id);
    if (key === "events") return r.event_types.join(", ");
    if (key === "throttle") return throttleLabel(r);
    if (key === "channels") return r.channel_ids.length;
    return (r as unknown as Record<string, unknown>)[key];
  }
  function eventValue(ev: AlertEvent, key: string): unknown {
    if (key === "rule") return ruleName(ev.rule_id);
    if (key === "library") return libName(ev.library_id);
    return (ev as unknown as Record<string, unknown>)[key];
  }
</script>


<!-- ---- channel cells ---- -->
{#snippet chNameCell(c: AlertChannel)}<span class="font-medium">{c.name}</span>{/snippet}
{#snippet chEnabledCell(c: AlertChannel)}
  <Button size="sm" onclick={() => toggleChannelEnabled(c)} title={c.enabled ? "Click to disable" : "Click to enable"}>
    {c.enabled ? "on" : "off"}
  </Button>
{/snippet}
{#snippet chActionsCell(c: AlertChannel)}
  <div class="flex justify-end gap-1">
    <Button size="sm" aria-controls={testResults[c.id] ? testPanelId(c.id) : undefined} data-test-for={c.id}
      onclick={() => fireTest(c)}>test</Button>
    <Button size="sm" onclick={() => startEditChannel(c)}>edit</Button>
    <Button size="sm" variant="danger" onclick={() => removeChannel(c)}>delete</Button>
  </div>
{/snippet}

<!-- ---- rule cells ---- -->
{#snippet ruleNameCell(r: AlertRule)}
  <!-- The rule's own name labels its bulk-selection box. -->
  <span class="inline-flex flex-wrap items-center gap-1 font-medium">
    <Checkbox size="sm" label={r.name} checked={bulkSel.has(r.id)} onchange={() => bulkToggle(r.id)} />
    {#if r.is_system}<Badge>system</Badge>{/if}
  </span>
{/snippet}
{#snippet ruleEventsCell(r: AlertRule)}<span class="text-xs">{r.event_types.join(", ")}</span>{/snippet}
{#snippet ruleThrottleCell(r: AlertRule)}<span class="text-xs">{throttleLabel(r)}</span>{/snippet}
{#snippet ruleEnabledCell(r: AlertRule)}
  <Button size="sm" onclick={() => toggleRuleEnabled(r)} title={r.enabled ? "Click to disable" : "Click to enable"}>
    {r.enabled ? "on" : "off"}
  </Button>
{/snippet}
{#snippet ruleActionsCell(r: AlertRule)}
  <div class="flex justify-end gap-1">
    <Button size="sm" onclick={() => startEditRule(r)}>edit</Button>
    {#if !r.is_system}<Button size="sm" variant="danger" onclick={() => removeRule(r)}>delete</Button>{/if}
  </div>
{/snippet}

<!-- ---- event cells ---- -->
{#snippet evOccurredCell(ev: AlertEvent)}<span class="whitespace-nowrap text-xs">{fmt(ev.occurred_at)}</span>{/snippet}
{#snippet evTypeCell(ev: AlertEvent)}<span class="text-xs">{ev.event_type}</span>{/snippet}
{#snippet evLibraryCell(ev: AlertEvent)}<span class="text-xs">{libName(ev.library_id)}</span>{/snippet}
{#snippet evStatusCell(ev: AlertEvent)}
  {#if ev.last_error}
    <!-- The delivery error behind a disclosure, reachable by keyboard and touch. -->
    <details class="max-w-md">
      <summary class="cursor-pointer text-xs"><Badge tone={evTone(ev.status)}>{ev.status}</Badge> why</summary>
      <div class="mt-1 break-words text-xs text-danger">{ev.last_error}</div>
    </details>
  {:else}
    <Badge tone={evTone(ev.status)}>{ev.status}</Badge>
  {/if}
{/snippet}
{#snippet evDeliveredCell(ev: AlertEvent)}
  <span class="whitespace-nowrap text-xs">{fmt(ev.delivered_at)}{#if ev.delivery_attempts > 0} ({ev.delivery_attempts} tries){/if}</span>
{/snippet}

<div class="space-y-4">
  <div class="flex items-center gap-2">
    <h2 class="text-lg font-semibold">Alerts</h2>
    <div class="grow"></div>
    <!-- Section switch: segment chrome, not action buttons. -->
    <nav class="flex gap-1 text-sm" aria-label="Alerts sections">
      {#each [["channels", "Channels"], ["rules", "Rules"], ["events", "Events"]] as [id, label] (id)}
        <button
          type="button"
          class="rounded-control px-3 py-1 {tab === id ? 'bg-accent text-accent-fg' : 'text-muted hover:text-fg'}"
          aria-pressed={tab === id}
          onclick={() => (tab = id as Tab)}>{label}</button>
      {/each}
    </nav>
  </div>

  {#if error && loaded}
    <!-- A failed refresh keeps the last channels/rules on screen. -->
    <Alert tone="danger" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  {#if summary && summary.failed > 0}
    <button
      type="button"
      class="w-full rounded-control bg-warn-bg p-3 text-left text-sm text-warn"
      onclick={() => {
        tab = "events";
        evStatus = "failed";
      }}>
      ⚠ {summary.failed} alert{summary.failed === 1 ? "" : "s"} failed to deliver (retries exhausted).
      Click to review.
    </button>
  {/if}

  {#if !loaded}
    {#if error}
      <StateBlock state="error" message={error} onRetry={refresh} />
    {:else}
      <StateBlock state="loading" />
    {/if}
  {:else if tab === "channels"}
    <!-- ============================ CHANNELS ============================ -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-muted">Notification destinations.</p>
      <Button size="sm" onclick={startNewChannel}>+ New channel</Button>
    </div>

    <DataTable
      caption="Notification channels"
      captionHidden
      columns={[
        { key: "name", label: "Name", sortable: true, cell: chNameCell },
        { key: "type", label: "Type", sortable: true },
        { key: "dispatch_locality", label: "Locality", sortable: true },
        { key: "enabled", label: "Enabled", sortable: true, cell: chEnabledCell },
        { key: "actions", label: "Actions", align: "right", cell: chActionsCell },
      ]}
      rows={channels}
      rowKey={(c) => c.id}
      bind:sort={chSort}
      {loading}
      emptyMessage="No channels yet. Add a channel to receive alerts." />
    <!-- Per-channel test results and action errors, keyed by row. -->
    {#each channels as c (c.id)}
      {#if testResults[c.id]}
        {@const t = testResults[c.id]}
        <div id={testPanelId(c.id)} tabindex="-1" aria-label="Test of {c.name}" role="group">
          {#if "error" in t && t.error === "…"}
            <p class="text-xs text-muted" role="status">{c.name}: testing…</p>
          {:else if "error" in t}
            <Alert tone="danger" onDismiss={() => dismissTest(c)}>
              {c.name}: {t.error}
            </Alert>
          {:else}
            {@const r = t as TestFireResult}
            <Alert tone={r.ok ? "ok" : "danger"} onDismiss={() => dismissTest(c)}>
              {c.name}: {r.ok ? "delivered ✓" : `failed: ${r.detail}`}
              {#if r.status_code}(HTTP {r.status_code}){/if}
            </Alert>
          {/if}
        </div>
      {/if}
      {#if chRowErr[c.id]}
        <Alert tone="danger" onDismiss={() => (chRowErr[c.id] = "")}>{c.name}: {chRowErr[c.id]}</Alert>
      {/if}
    {/each}

    {#if chNew || chEditing}
      <form class="space-y-3 rounded-panel border border-border p-4" onsubmit={saveChannel} novalidate>
        <h3 class="font-medium">{chEditing ? "Edit channel" : "New channel"}</h3>
        <div class="grid gap-3 sm:grid-cols-2">
          <Field label="Name" required error={chFieldErr.name}>
            <Input bind:value={chName_} />
          </Field>
          <Field label="Type">
            <Select bind:value={chType} disabled={!!chEditing}>
              {#each CHANNEL_TYPES as t (t)}<option value={t}>{t}</option>{/each}
            </Select>
          </Field>
        </div>

        {#if chType === "webhook"}
          <Field label="URL" required error={chFieldErr.url}>
            <Input bind:value={cWebhookUrl} oninput={onWebhookUrlInput} placeholder="https://…" />
          </Field>
          <div class="flex items-start gap-2">
            <Field
              label="Payload format"
              class="flex-1"
              hint="Auto-detected from the URL. generic = Filearr signed JSON; discord / slack reshape the body for those endpoints (and skip the HMAC signature).">
              <Select bind:value={cWebhookFormat} onchange={() => (chFormatTouched = true)}>
                {#each WEBHOOK_FORMATS as f (f)}<option value={f}>{f}</option>{/each}
              </Select>
            </Field>
            <span class="pt-0.5"><Help text={HELP.webhook_format} label="payload format" /></span>
          </div>
          <Field
            label="HMAC secret {chEditing ? '(leave blank to keep)' : '(optional)'}"
            hint={cWebhookFormat !== "generic"
              ? `Not sent for ${cWebhookFormat} — signature is only added to the generic format.`
              : undefined}>
            <Input type="password" autocomplete="new-password" bind:value={cWebhookSecret} />
          </Field>
        {:else if chType === "email"}
          <div class="grid gap-3 sm:grid-cols-2">
            <Field label="SMTP host" required error={chFieldErr.host}>
              <Input bind:value={cEmailHost} />
            </Field>
            <Field label="Port">
              <Input type="number" bind:value={cEmailPort} />
            </Field>
            <Field label="Security">
              <Select bind:value={cEmailSecurity}>
                <option value="starttls">STARTTLS</option><option value="ssl">SSL</option><option value="plain">plain (insecure)</option>
              </Select>
            </Field>
            <Field label="From" required error={chFieldErr.from}>
              <Input bind:value={cEmailFrom} />
            </Field>
            <Field label="To (comma-separated)" required error={chFieldErr.to} class="sm:col-span-2">
              <Input bind:value={cEmailTo} />
            </Field>
            <Field label="Username">
              <Input bind:value={cEmailUser} />
            </Field>
            <Field label="Password {chEditing ? '(blank = keep)' : ''}">
              <Input type="password" autocomplete="new-password" bind:value={cEmailPassword} />
            </Field>
          </div>
        {:else}
          <Field label="Apprise URL {chEditing ? '(blank = keep)' : ''} — the whole URL is stored encrypted">
            <Input type="password" autocomplete="new-password" bind:value={cAppriseUrl} placeholder="discord://… / tgram://… / ntfy://…" />
          </Field>
        {/if}

        <div class="grid items-end gap-3 sm:grid-cols-2">
          <Field label="Dispatch locality">
            <Select bind:value={chLocality}>
              {#each DISPATCH_LOCALITIES as l (l)}<option value={l}>{l}</option>{/each}
            </Select>
          </Field>
          <Checkbox label="Enabled" bind:checked={chEnabled} />
        </div>

        <div class="flex gap-2">
          <Button type="submit" variant="primary" disabled={chBusy}>
            {chBusy ? "Saving…" : "Save"}
          </Button>
          <Button onclick={resetChannelForm}>Cancel</Button>
        </div>
        {#if chErr}
          <Alert tone="danger" onDismiss={() => (chErr = "")}>{chErr}</Alert>
        {/if}
      </form>
    {/if}
  {:else if tab === "rules"}
    <!-- ============================= RULES ============================== -->
    <div class="flex items-center justify-between">
      <p class="text-sm text-muted">File-watch rules and built-in system rules.</p>
      <Button size="sm" onclick={startNewRule}>+ New rule</Button>
    </div>

    {#if bulkSel.size > 0}
      <div class="mt-2 flex flex-wrap items-center gap-3 rounded-control border border-accent bg-surface-2 px-3 py-2 text-sm">
        <span class="font-medium">{bulkSel.size} selected</span>
        <div class="flex items-center gap-1 text-xs">
          <span aria-hidden="true">Throttle</span>
          <div>
            <Select size="sm" aria-label="Bulk throttle" bind:value={bulkThrottle}>
              <option value="">(leave)</option>
              <option value="immediate">immediate</option>
              <option value="hourly">hourly digest</option>
              <option value="daily">daily digest</option>
            </Select>
          </div>
          {#if bulkThrottle === "immediate"}
            <span aria-hidden="true">wait</span>
            <div class="w-16">
              <Input size="sm" type="number" min="0" aria-label="Bulk group wait (seconds)" bind:value={bulkGroupWait} />
            </div>s
          {/if}
        </div>
        <div class="flex items-center gap-1 text-xs">
          <Checkbox size="sm" label="channels:" bind:checked={bulkTouchChannels} />
          <div>
            <Select size="sm" aria-label="Bulk channel change" disabled={!bulkTouchChannels} bind:value={bulkChannelMode}>
              <option value="set">replace with</option>
              <option value="add">also publish to</option>
              <option value="remove">stop publishing to</option>
            </Select>
          </div>
        </div>
        {#if bulkTouchChannels}
          <span class="flex flex-wrap gap-2 text-xs">
            {#each channels as c (c.id)}
              <Checkbox
                size="sm"
                label="{c.name}{c.enabled ? '' : ' (disabled)'}"
                checked={bulkChannels.includes(c.id)}
                onchange={() => (bulkChannels = toggle(bulkChannels, c.id))} />
            {/each}
          </span>
        {/if}
        <div class="grow"></div>
        <Button variant="primary" size="sm"
          disabled={bulkBusy || (!bulkThrottle && !bulkTouchChannels)} onclick={applyBulk}>
          {bulkBusy ? "Applying…" : "Apply to selected"}</Button>
        <Button variant="ghost" size="sm" onclick={() => (bulkSel = new Set())}>clear</Button>
      </div>
      {#if bulkErr}
        <Alert tone="danger" onDismiss={() => (bulkErr = "")}>{bulkErr}</Alert>
      {/if}
    {/if}

    {#if rules.length > 0}
      <Checkbox
        size="sm"
        label="Select all"
        aria-label="Select all rules"
        checked={allSelected}
        indeterminate={bulkSel.size > 0 && !allSelected}
        onchange={toggleAll} />
    {/if}
    <DataTable
      caption="Alert rules"
      captionHidden
      columns={[
        { key: "name", label: "Name", sortable: true, cell: ruleNameCell },
        { key: "scope", label: "Scope", sortable: true },
        { key: "events", label: "Events", cell: ruleEventsCell },
        { key: "throttle", label: "Throttle", cell: ruleThrottleCell },
        { key: "channels", label: "Channels", align: "right", sortable: true },
        { key: "enabled", label: "Enabled", sortable: true, cell: ruleEnabledCell },
        { key: "actions", label: "Actions", align: "right", cell: ruleActionsCell },
      ]}
      rows={rules}
      rowKey={(r) => r.id}
      accessor={ruleValue}
      bind:sort={ruleSort}
      {loading}
      emptyMessage="No rules yet. Add a rule to be notified when files change." />
    {#each rules as r (r.id)}
      {#if rlRowErr[r.id]}
        <Alert tone="danger" onDismiss={() => (rlRowErr[r.id] = "")}>{r.name}: {rlRowErr[r.id]}</Alert>
      {/if}
    {/each}

    {#if rlNew || rlEditing}
      <form class="space-y-3 rounded-panel border border-border p-4" onsubmit={saveRule} novalidate>
        <h3 class="font-medium">
          {rlEditing ? (isSystem ? "Edit system rule" : "Edit rule") : "New rule"}
        </h3>
        {#if isSystem}
          <p class="text-xs text-muted">
            System rule: its match logic is fixed. You can attach channels and tune the throttle/timings only.
          </p>
        {/if}

        <Field label="Name" required={!isSystem} error={rlFieldErr.name}>
          <Input bind:value={rName} disabled={isSystem} />
        </Field>

        {#if !isSystem}
          <div class="grid gap-3 sm:grid-cols-2">
            <Field label="Library scope">
              <Select bind:value={rLibrary}>
                <option value="">All libraries</option>
                {#each libraries as l (l.id)}<option value={l.id}>{l.name}</option>{/each}
              </Select>
            </Field>
            <div class="flex items-start gap-2">
              <Field label="Path glob" class="flex-1">
                <Input class="font-mono" bind:value={rGlob} placeholder="**/*.mkv (blank = all files)" />
              </Field>
              <span class="pt-0.5"><Help text={HELP.exclude_globs} label="path glob" /></span>
            </div>
          </div>

          <fieldset class="text-sm">
            <legend class="text-xs font-medium text-fg">Event types</legend>
            <div class="mt-1 flex flex-wrap gap-3">
              {#each EVENT_TYPES as et (et)}
                <Checkbox label={et} checked={rEvents.includes(et)} onchange={() => (rEvents = toggle(rEvents, et))} />
              {/each}
            </div>
          </fieldset>

          <Checkbox
            label="Only when the content hash changes (modified events)"
            bind:checked={rHashOnly}
            disabled={!rEvents.includes("modified")} />
        {/if}

        <fieldset class="text-sm">
          <legend class="text-xs font-medium text-fg">Throttle</legend>
          <!-- No kit radio: native radios with token accent; each companion
               control sits outside the radio's label and is named on its own. -->
          <div class="mt-1 space-y-2">
            <div class="flex flex-wrap items-center gap-2">
              <label class="flex items-center gap-2">
                <input type="radio" name="rule-throttle" class="size-4 accent-accent" value="immediate" bind:group={rThrottle} />
                Immediate — group matches for
              </label>
              <div class="w-20">
                <Input type="number" size="sm" min="0" aria-label="Group wait (seconds)" bind:value={rGroupWait} disabled={rThrottle !== "immediate"} />
              </div>
              seconds
            </div>
            <div class="flex flex-wrap items-center gap-2">
              <label class="flex items-center gap-2">
                <input type="radio" name="rule-throttle" class="size-4 accent-accent" value="digest" bind:group={rThrottle} />
                Digest —
              </label>
              <div>
                <Select size="sm" aria-label="Digest window" bind:value={rDigest} disabled={rThrottle !== "digest"}>
                  {#each DIGEST_WINDOWS as w (w)}<option value={w}>{w}</option>{/each}
                </Select>
              </div>
              roll-up
            </div>
          </div>
        </fieldset>

        <Button variant="ghost" size="sm" aria-expanded={rShowAdvanced} onclick={() => (rShowAdvanced = !rShowAdvanced)}>
          {rShowAdvanced ? "Hide" : "Show"} advanced
        </Button>
        {#if rShowAdvanced}
          <Field label="Repeat interval (seconds; blank = never re-notify)" class="w-80 max-w-full">
            <Input bind:value={rRepeat} placeholder="e.g. 3600" />
          </Field>
          {#if !rlEditing?.is_system}
            <fieldset class="text-sm">
              <legend class="text-xs font-medium text-fg">Group notifications by</legend>
              <p class="text-xs text-muted">
                Always per event type + library + rule. Add finer keys to split one
                library-wide batch into several notifications.
              </p>
              <div class="mt-1 flex flex-wrap gap-3">
                {#each GROUP_EXTRAS as g (g.key)}
                  <Checkbox
                    label={g.label}
                    description={g.hint}
                    checked={rGroupExtras.includes(g.key)}
                    onchange={() => (rGroupExtras = toggle(rGroupExtras, g.key))} />
                {/each}
              </div>
            </fieldset>
          {/if}
          <fieldset class="text-sm">
            <legend class="text-xs font-medium text-fg">Inhibited by</legend>
            <div class="mt-1 flex flex-wrap items-center gap-1 text-xs text-muted">
              Mute this rule while any selected rule has fired in the last
              <div class="w-20">
                <Input size="sm" type="number" min="0" max="86400" aria-label="Inhibit window (seconds)" bind:value={rInhibitWindow} />
              </div>
              seconds
            </div>
            <p class="text-xs text-muted">
              (same library, or a library-wide inhibitor). Muted batches are recorded as
              "suppressed: inhibited by …" and never sent — e.g. let <em>agent offline</em>
              silence <em>replication stalled</em>.
            </p>
            <div class="mt-1 flex flex-wrap gap-3">
              {#each rules.filter((x) => x.id !== rlEditing?.id) as x (x.id)}
                <Checkbox
                  label={x.name}
                  checked={rInhibitedBy.includes(x.id)}
                  onchange={() => (rInhibitedBy = toggle(rInhibitedBy, x.id))} />
              {/each}
            </div>
          </fieldset>
        {/if}

        <fieldset class="text-sm">
          <legend class="text-xs font-medium text-fg">Channels</legend>
          {#if channels.length === 0}
            <p class="text-xs text-muted">No channels defined — create one first.</p>
          {:else}
            <div class="mt-1 flex flex-wrap gap-3">
              {#each channels as c (c.id)}
                <Checkbox label={c.name} checked={rChannels.includes(c.id)} onchange={() => (rChannels = toggle(rChannels, c.id))} />
              {/each}
            </div>
          {/if}
        </fieldset>

        <Checkbox label="Enabled" bind:checked={rEnabled} />

        <div class="flex gap-2">
          <Button type="submit" variant="primary" disabled={rlBusy}>
            {rlBusy ? "Saving…" : "Save"}
          </Button>
          <Button onclick={resetRuleForm}>Cancel</Button>
        </div>
        {#if rlErr}
          <Alert tone="danger" onDismiss={() => (rlErr = "")}>{rlErr}</Alert>
        {/if}
      </form>
    {/if}
  {:else}
    <!-- ============================= EVENTS ============================= -->
    <div class="flex flex-wrap items-end gap-3 text-sm">
      <Field label="Rule">
        <Select size="sm" bind:value={evRule}>
          <option value="">All</option>
          {#each rules as r (r.id)}<option value={r.id}>{r.name}</option>{/each}
        </Select>
      </Field>
      <Field label="Status">
        <Select size="sm" bind:value={evStatus}>
          <option value="">All</option>
          <option value="delivered">delivered</option>
          <option value="failed">failed</option>
          <option value="pending">pending</option>
        </Select>
      </Field>
      {#if summary}
        <span class="text-xs text-muted">
          {summary.delivered} delivered · <span class="text-warn">{summary.failed} failed</span> · {summary.pending} pending
        </span>
      {/if}
      <Button size="sm" onclick={loadEvents}>Refresh</Button>
    </div>

    {#if evError && events.length}
      <!-- A failed reload keeps the last events on screen. -->
      <Alert tone="danger" onDismiss={() => (evError = "")}>{evError}</Alert>
    {/if}
    <DataTable
      caption="Alert events"
      captionHidden
      columns={[
        { key: "occurred_at", label: "Occurred", sortable: true, cell: evOccurredCell },
        { key: "rule", label: "Rule", sortable: true },
        { key: "event_type", label: "Event", sortable: true, cell: evTypeCell },
        { key: "library", label: "Library", sortable: true, cell: evLibraryCell },
        { key: "status", label: "Status", sortable: true, cell: evStatusCell },
        { key: "delivered_at", label: "Delivered", sortable: true, cell: evDeliveredCell },
      ]}
      rows={events}
      rowKey={(ev) => ev.id}
      accessor={eventValue}
      bind:sort={evSort}
      loading={evLoading}
      error={events.length ? null : evError || null}
      onRetry={loadEvents}
      emptyMessage="No events match these filters. Events appear here once a rule fires; widen the filters to see more." />
    {#if events.length}
      <!-- Only the newest 100 events are fetched, so a column sort reorders
           those, not the whole history. -->
      <p class="mt-1 text-xs text-muted">Sorting applies to the events shown on this page.</p>
    {/if}
  {/if}
</div>
