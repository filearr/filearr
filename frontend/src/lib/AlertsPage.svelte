<script lang="ts">
  // P8-T12/T13 — the Alerts admin surface: notification channels, alert rules
  // (file-watch + read-only is_system operational rules) and a recent-events
  // view with failed-delivery surfacing. All admin-scope. Secret channel fields
  // are password inputs whose decrypted value NEVER round-trips (empty on edit =
  // keep: the key is omitted and the server merges; alertConfig.ts). Every dynamic string renders as
  // text (Svelte auto-escapes) — a crafted path/error can't inject markup.
  import { onDestroy, onMount } from "svelte";
  import { latest } from "./latest";
  import {
    CHANNEL_TYPES,
    DISPATCH_LOCALITIES,
    DIGEST_WINDOWS,
    EVENT_TYPES,
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
  import {
    buildChannelConfig,
    channelFormFromConfig,
    detectWebhookFormat,
    emptyChannelForm,
    hasStoredSecret,
    secretReentryRequired,
    parseRuleTimings,
    DEFAULT_GROUP_WAIT_S,
    DEFAULT_INHIBIT_WINDOW_S,
    type ChannelForm,
  } from "./alertConfig";
  import { fmtDateTime, fmtUtc } from "./format";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { usePolling } from "./polling";
  import { focusPanel, focusRowButton } from "./rowPanel";
  import { Alert, Badge, Button, Checkbox, DataTable, Field, Input, Select, StateBlock, confirm, toast, type SortState, type Tone } from "./ui";

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
    (signal, filters: Parameters<typeof listAlertEvents>[0]) => listAlertEvents(filters, signal),
  );
  const summaryLoader = latest((signal) => alertEventsSummary(undefined, signal));
  // B22: the failed-delivery banner is fed on load, after every mutation
  // (refresh) and by a slow poll, so it shows on every tab.
  const SUMMARY_POLL_MS = 60_000;
  let summaryPoller: { stop(): void } | null = null;
  onDestroy(() => {
    configLoader.cancel();
    eventsLoader.cancel();
    summaryLoader.cancel();
    summaryPoller?.stop();
  });

  // The banner is supplementary: a failed summary keeps the last counts and
  // never displaces the page (the config load reports connectivity).
  async function loadSummary() {
    try {
      const s = await summaryLoader.run();
      if (s !== undefined) summary = s;
    } catch {
      // keep the last summary
    }
  }

  async function refresh() {
    loading = true;
    void loadSummary();
    try {
      const r = await configLoader.run();
      if (r === undefined) return;
      [libraries, channels, rules] = r;
      // Drop selections whose rule no longer exists (deleted here or elsewhere).
      const live = new Set(rules.map((x) => x.id));
      if ([...bulkSel].some((id) => !live.has(id))) bulkSel = new Set([...bulkSel].filter((id) => live.has(id)));
      error = "";
      loading = false;
      loaded = true;
    } catch (e) {
      error = errorMessage(e);
      loading = false;
    }
  }
  onMount(() => {
    void refresh();
    summaryPoller = usePolling(loadSummary, SUMMARY_POLL_MS, { refreshOnReturn: true });
  });

  // ---- channels -------------------------------------------------------- //
  let chEditing = $state<AlertChannel | null>(null); // null while not editing
  let chNew = $state(false);
  let chName_ = $state("");
  let chType = $state<ChannelType>("webhook");
  let chLocality = $state<DispatchLocality>("central");
  let chEnabled = $state(true);
  // Per-type config fields; alertConfig.ts turns them into the API config
  // (B23: an edit sends only the changed keys, which the server merges).
  let chForm = $state<ChannelForm>(emptyChannelForm());
  // FIX-16: once the operator picks a format by hand, stop auto-detecting from URL.
  let chFormatTouched = $state(false);
  let chBusy = $state(false);
  let chErr = $state("");
  // Field messages, keyed by field; the form is novalidate.
  let chFieldErr = $state<Record<string, string>>({});
  let chRowErr = $state<Record<string, string>>({});
  let testResults = $state<Record<string, TestFireResult | { error: string }>>({});
  const chStoredSecret = $derived(!!chEditing && hasStoredSecret(chEditing.type, chEditing.config));
  // Editing the server/host of a channel with a stored secret: the server will
  // not carry the stored secret to the new destination, so it must be retyped.
  const chReentry = $derived(!!chEditing && secretReentryRequired(chType, chForm, chEditing.config));

  // The editor renders below the table (rowPanel.ts): opening it focuses it,
  // Cancel returns focus to the row's edit button.
  const uid = $props.id();
  const chPanelId = `${uid}-channel-editor`;
  const rlPanelId = `${uid}-rule-editor`;

  function resetChannelForm() {
    chEditing = null;
    chNew = false;
    chName_ = "";
    chType = "webhook";
    chLocality = "central";
    chEnabled = true;
    chForm = emptyChannelForm();
    chFormatTouched = false;
    chErr = "";
    chFieldErr = {};
  }

  function cancelChannel() {
    const id = chEditing?.id;
    resetChannelForm();
    if (id) void focusRowButton("data-ch-edit-for", id);
  }

  function startNewChannel() {
    resetChannelForm();
    chNew = true;
    void focusPanel(chPanelId);
  }

  function startEditChannel(c: AlertChannel) {
    resetChannelForm();
    chEditing = c;
    chName_ = c.name;
    chType = c.type;
    chLocality = c.dispatch_locality;
    chEnabled = c.enabled;
    chForm = channelFormFromConfig(c.type, c.config);
    chFormatTouched = true; // an existing channel keeps its stored format
    void focusPanel(chPanelId);
  }

  // FIX-16: auto-detect the format from the URL until the operator overrides it.
  function onWebhookUrlInput() {
    if (!chFormatTouched) chForm.webhookFormat = detectWebhookFormat(chForm.webhookUrl);
  }

  async function saveChannel(e: Event) {
    e.preventDefault();
    const built = buildChannelConfig(chType, chForm, chEditing?.config ?? null);
    const errs: Record<string, string> = built.ok ? {} : { ...built.errors };
    if (!chName_.trim()) errs.name = "Name is required.";
    chFieldErr = errs;
    if (!built.ok || Object.keys(errs).length) return;
    chBusy = true;
    chErr = "";
    try {
      if (chEditing) {
        const patch: Parameters<typeof updateAlertChannel>[1] = {};
        if (chName_.trim() !== chEditing.name) patch.name = chName_.trim();
        if (Object.keys(built.config).length) patch.config = built.config;
        if (chLocality !== chEditing.dispatch_locality) patch.dispatch_locality = chLocality;
        if (chEnabled !== chEditing.enabled) patch.enabled = chEnabled;
        const id = chEditing.id;
        if (Object.keys(patch).length) await updateAlertChannel(id, patch);
        resetChannelForm();
        void focusRowButton("data-ch-edit-for", id);
      } else {
        await createAlertChannel({
          name: chName_.trim(),
          type: chType,
          config: built.config,
          dispatch_locality: chLocality,
          enabled: chEnabled,
        });
        resetChannelForm();
      }
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
  // Number inputs: a number, or null when emptied (blank = the stated default).
  let rGroupWait = $state<number | null>(DEFAULT_GROUP_WAIT_S);
  let rDigest = $state<DigestWindow>("hourly");
  let rRepeat = $state<string>(""); // seconds, blank = none
  let rChannels = $state<string[]>([]);
  let rEnabled = $state(true);
  let rShowAdvanced = $state(false);
  // 2026-08-20 mass edit: selection + one change-set applied to many rules.
  let bulkSel = $state<Set<string>>(new Set());
  let bulkThrottle = $state<"" | "immediate" | "hourly" | "daily">("");
  let bulkGroupWait = $state<number | null>(DEFAULT_GROUP_WAIT_S);
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
    bulkErr = "";
    const n = bulkSel.size;
    const rulesWord = `${n} rule${n === 1 ? "" : "s"}`;
    const body: Record<string, unknown> = { rule_ids: [...bulkSel] };
    if (bulkThrottle) {
      body.set_throttle = true;
      body.digest_window = bulkThrottle === "immediate" ? null : bulkThrottle;
      if (bulkThrottle === "immediate") {
        const t = parseRuleTimings({ repeat: "", groupWait: bulkGroupWait, inhibitWindow: null });
        if (!t.ok) {
          bulkErr = t.errors.groupWait;
          return;
        }
        body.group_wait_s = t.values.group_wait_s;
      }
    }
    if (bulkTouchChannels) {
      if (bulkChannels.length === 0 && bulkChannelMode !== "set") {
        bulkErr = "Pick at least one channel.";
        return;
      }
      if (bulkChannels.length === 0) {
        // "Replace with" nothing detaches every channel: the rules stop notifying.
        const ok = await confirm({
          title: "Remove all channels?",
          message: `No channel is selected, so this removes every channel from ${rulesWord}.`,
          details: ["Those rules keep matching but send no notifications until a channel is attached again"],
          confirmLabel: "Remove all channels",
          tone: "danger",
        });
        if (!ok) return;
      }
      body.channel_mode = bulkChannelMode;
      body.channel_ids = bulkChannels;
    }
    bulkBusy = true;
    try {
      const r = await bulkUpdateAlertRules(body);
      const missing = r.missing.length
        ? ` ${r.missing.length} selected rule${r.missing.length === 1 ? " no longer exists" : "s no longer exist"}.`
        : "";
      toast(`Updated ${r.updated} rule${r.updated === 1 ? "" : "s"}.${missing}`, {
        tone: r.missing.length ? "warn" : "ok",
      });
      bulkSel = new Set();
      bulkThrottle = "";
      bulkTouchChannels = false;
      await refresh();
    } catch (e) {
      bulkErr = errorMessage(e, { verb: "manage alerts" });
    } finally {
      bulkBusy = false;
    }
  }
  // Roadmap §6 polish (2026-08-19): optional extra grouping keys and inhibition.
  let rGroupExtras = $state<string[]>([]);
  let rInhibitedBy = $state<string[]>([]);
  let rInhibitWindow = $state<number | null>(DEFAULT_INHIBIT_WINDOW_S);
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
    rGroupWait = DEFAULT_GROUP_WAIT_S;
    rDigest = "hourly";
    rRepeat = "";
    rChannels = [];
    rEnabled = true;
    rShowAdvanced = false;
    rGroupExtras = [];
    rInhibitedBy = [];
    rInhibitWindow = DEFAULT_INHIBIT_WINDOW_S;
    rlErr = "";
    rlFieldErr = {};
  }

  function startNewRule() {
    resetRuleForm();
    rlNew = true;
    void focusPanel(rlPanelId);
  }

  function cancelRule() {
    const id = rlEditing?.id;
    resetRuleForm();
    if (id) void focusRowButton("data-rl-edit-for", id);
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
    rInhibitWindow = r.inhibit_window_s ?? DEFAULT_INHIBIT_WINDOW_S;
    if (rGroupExtras.length || rInhibitedBy.length || r.repeat_interval_s != null) rShowAdvanced = true;
    void focusPanel(rlPanelId);
  }

  function toggle(list: string[], id: string): string[] {
    return list.includes(id) ? list.filter((x) => x !== id) : [...list, id];
  }

  async function saveRule(e: Event) {
    e.preventDefault();
    // The old native `required` on Name (system rules keep their fixed name).
    const errs: Record<string, string> = !isSystem && !rName.trim() ? { name: "Name is required." } : {};
    // B42: garbage → field errors (never NaN/null); cleared waits → defaults.
    // The group-wait box is disabled (and not sent) in digest mode.
    const t = parseRuleTimings({
      repeat: rRepeat,
      groupWait: rThrottle === "immediate" ? rGroupWait : null,
      inhibitWindow: rInhibitWindow,
    });
    if (!t.ok) {
      Object.assign(errs, t.errors);
      if (errs.repeat || errs.inhibitWindow) rShowAdvanced = true;
    }
    rlFieldErr = errs;
    if (!t.ok || Object.keys(errs).length) return;
    const v = t.values;
    rlBusy = true;
    rlErr = "";
    try {
      const timings = {
        group_wait_s: rThrottle === "immediate" ? v.group_wait_s : 0,
        digest_window: rThrottle === "digest" ? rDigest : null,
        repeat_interval_s: v.repeat_interval_s,
        inhibit_window_s: v.inhibit_window_s,
      };
      if (rlEditing) {
        // System rules: only channels + throttle/timings are editable.
        const patch: Record<string, unknown> = {
          enabled: rEnabled,
          ...timings,
          channel_ids: rChannels,
          inhibited_by: rInhibitedBy,
        };
        if (!rlEditing.is_system) {
          patch.name = rName;
          patch.library_id = rLibrary || null;
          patch.path_glob = rGlob || null;
          patch.event_types = rEvents;
          patch.hash_change_only = rHashOnly && rEvents.includes("modified");
          patch.group_by = ["event_type", "library_id", "rule_id", ...rGroupExtras];
        }
        const id = rlEditing.id;
        await updateAlertRule(id, patch);
        resetRuleForm();
        void focusRowButton("data-rl-edit-for", id);
      } else {
        await createAlertRule({
          name: rName,
          enabled: rEnabled,
          library_id: rLibrary || null,
          path_glob: rGlob || null,
          event_types: rEvents,
          hash_change_only: rHashOnly && rEvents.includes("modified"),
          ...timings,
          channel_ids: rChannels,
          group_by: ["event_type", "library_id", "rule_id", ...rGroupExtras],
          inhibited_by: rInhibitedBy,
        });
        resetRuleForm();
      }
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

  // B24: pages of EV_PAGE, newest first; "Load more" sends the last row as a
  // keyset cursor, so events arriving between loads never shift the pages.
  const EV_PAGE = 100;
  let evMore = $state(false);
  let evLoadingMore = $state(false);
  // Only the newest loadEvents run clears the loading flags: a superseded one
  // finishing late must not hide the spinner of the run that replaced it.
  let evSeq = 0;

  async function loadEvents(more = false) {
    const mine = ++evSeq;
    if (more) evLoadingMore = true;
    else evLoading = true;
    const last = more ? events[events.length - 1] : undefined;
    try {
      const page = await eventsLoader.run({
        rule_id: evRule || undefined,
        status: evStatus || undefined,
        limit: EV_PAGE,
        before: last ? { occurred_at: last.occurred_at, id: last.id } : undefined,
      });
      if (page === undefined) return;
      if (more) {
        // Belt and braces: never show a row twice.
        const seen = new Set(events.map((x) => x.id));
        events = [...events, ...page.filter((x) => !seen.has(x.id))];
      } else {
        events = page;
      }
      evMore = page.length === EV_PAGE;
      evError = "";
    } catch (e) {
      evError = errorMessage(e);
    } finally {
      if (mine === evSeq) {
        evLoading = false;
        evLoadingMore = false;
      }
    }
  }

  function refreshEvents() {
    void loadSummary();
    void loadEvents();
  }

  $effect(() => {
    if (tab === "events") {
      // re-run when filters change
      void evRule;
      void evStatus;
      loadEvents();
    }
  });

  // The summary is unfiltered by rule, so a total exists only without a rule
  // filter: the status count, or all three summed.
  // Locale digit grouping for counts (dates go through format.ts).
  const nf = new Intl.NumberFormat();
  const count = (v: number): string => nf.format(v);

  const evTotal = $derived.by(() => {
    if (!summary || evRule) return null;
    return evStatus ? summary[evStatus] : summary.delivered + summary.failed + summary.pending;
  });

  const ruleName = (id: string) => rules.find((r) => r.id === id)?.name ?? id.slice(0, 8);
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
    <Button size="sm" aria-controls={chEditing?.id === c.id ? chPanelId : undefined} data-ch-edit-for={c.id}
      onclick={() => startEditChannel(c)}>edit</Button>
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
    <Button size="sm" aria-controls={rlEditing?.id === r.id ? rlPanelId : undefined} data-rl-edit-for={r.id}
      onclick={() => startEditRule(r)}>edit</Button>
    {#if !r.is_system}<Button size="sm" variant="danger" onclick={() => removeRule(r)}>delete</Button>{/if}
  </div>
{/snippet}

<!-- ---- event cells ---- -->
{#snippet evOccurredCell(ev: AlertEvent)}<span class="whitespace-nowrap text-xs" title={fmtUtc(ev.occurred_at)}>{fmtDateTime(ev.occurred_at)}</span>{/snippet}
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
  <span class="whitespace-nowrap text-xs" title={ev.delivered_at ? fmtUtc(ev.delivered_at) : undefined}>{fmtDateTime(ev.delivered_at)}{#if ev.delivery_attempts > 0} ({ev.delivery_attempts} tries){/if}</span>
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
        // The count covers every rule, so the list drops any rule filter.
        evRule = "";
        evStatus = "failed";
      }}>
      ⚠ {count(summary.failed)} alert{summary.failed === 1 ? "" : "s"} failed to deliver (retries exhausted).
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
      <form id={chPanelId} tabindex="-1" aria-label={chEditing ? `Edit channel ${chEditing.name}` : "New channel"}
        class="space-y-3 rounded-panel border border-border p-4" onsubmit={saveChannel} novalidate>
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
            <Input bind:value={chForm.webhookUrl} oninput={onWebhookUrlInput} placeholder="https://…" />
          </Field>
          <div class="flex items-start gap-2">
            <Field
              label="Payload format"
              class="flex-1"
              hint="Auto-detected from the URL. generic = Filearr signed JSON; discord / slack reshape the body for those endpoints (and skip the HMAC signature).">
              <Select bind:value={chForm.webhookFormat} onchange={() => (chFormatTouched = true)}>
                {#each WEBHOOK_FORMATS as f (f)}<option value={f}>{f}</option>{/each}
              </Select>
            </Field>
            <span class="pt-0.5"><Help text={HELP.webhook_format} label="payload format" /></span>
          </div>
          <Field
            label="HMAC secret {chReentry ? '' : chStoredSecret ? '(leave blank to keep)' : '(optional)'}"
            required={chReentry && !chForm.clearWebhookSecret}
            error={chFieldErr.secret}
            hint={chForm.clearWebhookSecret
              ? "The stored secret is removed on Save."
              : chReentry
                ? "The host changed: re-enter the secret — the stored one is not used for a new destination."
              : chForm.webhookFormat !== "generic"
                ? `Not sent for ${chForm.webhookFormat} — signature is only added to the generic format.`
                : undefined}>
            <Input type="password" autocomplete="new-password" bind:value={chForm.webhookSecret}
              disabled={chForm.clearWebhookSecret} />
          </Field>
          {#if chStoredSecret}
            <Checkbox label="Clear stored secret" bind:checked={chForm.clearWebhookSecret} />
          {/if}
        {:else if chType === "email"}
          <div class="grid gap-3 sm:grid-cols-2">
            <Field label="SMTP host" required error={chFieldErr.host}>
              <Input bind:value={chForm.emailHost} />
            </Field>
            <Field label="Port" hint="Blank = 587." error={chFieldErr.port}>
              <Input type="number" min="1" max="65535" placeholder="587" bind:value={chForm.emailPort} />
            </Field>
            <Field label="Security">
              <Select bind:value={chForm.emailSecurity}>
                <option value="starttls">STARTTLS</option><option value="ssl">SSL</option><option value="plain">plain (insecure)</option>
              </Select>
            </Field>
            <Field label="From" required error={chFieldErr.from}>
              <Input bind:value={chForm.emailFrom} />
            </Field>
            {#if chForm.emailSecurity === "plain"}
              <div class="sm:col-span-2">
                <Checkbox
                  label="Allow insecure plaintext SMTP"
                  description="Mail and the SMTP password cross the network unencrypted. Delivery refuses plain SMTP without this."
                  bind:checked={chForm.allowInsecure} />
                {#if chFieldErr.allow_insecure}
                  <p class="mt-1 text-xs text-danger">{chFieldErr.allow_insecure}</p>
                {/if}
              </div>
            {/if}
            <Field label="To (comma-separated)" required error={chFieldErr.to} class="sm:col-span-2">
              <Input bind:value={chForm.emailTo} />
            </Field>
            <Field label="Username">
              <Input bind:value={chForm.emailUser} />
            </Field>
            <Field label="Password {chStoredSecret && !chReentry ? '(blank = keep)' : ''}"
              required={chReentry && !chForm.clearEmailPassword}
              error={chFieldErr.password}
              hint={chForm.clearEmailPassword
                ? "The stored password is removed on Save."
                : chReentry
                  ? "The server changed: re-enter the password — the stored one is never sent to a new server."
                  : undefined}>
              <Input type="password" autocomplete="new-password" bind:value={chForm.emailPassword}
                disabled={chForm.clearEmailPassword} />
            </Field>
            {#if chStoredSecret}
              <Checkbox label="Clear stored password" bind:checked={chForm.clearEmailPassword} />
            {/if}
          </div>
        {:else}
          <Field label="Apprise URL {chEditing ? '(blank = keep)' : ''} — the whole URL is stored encrypted"
            required={!chEditing} error={chFieldErr.apprise}>
            <Input type="password" autocomplete="new-password" bind:value={chForm.appriseUrl} placeholder="discord://… / tgram://… / ntfy://…" />
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
          <Button onclick={cancelChannel}>Cancel</Button>
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
      <form id={rlPanelId} tabindex="-1" aria-label={rlEditing ? `Edit rule ${rlEditing.name}` : "New rule"}
        class="space-y-3 rounded-panel border border-border p-4" onsubmit={saveRule} novalidate>
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
                <Input type="number" size="sm" min="0" placeholder={String(DEFAULT_GROUP_WAIT_S)} aria-label="Group wait (seconds)"
                  aria-invalid={rlFieldErr.groupWait ? true : undefined} bind:value={rGroupWait} disabled={rThrottle !== "immediate"} />
              </div>
              seconds <span class="text-xs text-muted">(blank = {DEFAULT_GROUP_WAIT_S})</span>
            </div>
            {#if rlFieldErr.groupWait}<p class="text-xs text-danger">{rlFieldErr.groupWait}</p>{/if}
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
          <Field label="Repeat interval (seconds; blank = never re-notify)" class="w-80 max-w-full" error={rlFieldErr.repeat}>
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
                <Input size="sm" type="number" min="0" max="86400" placeholder={String(DEFAULT_INHIBIT_WINDOW_S)}
                  aria-label="Inhibit window (seconds)" aria-invalid={rlFieldErr.inhibitWindow ? true : undefined}
                  bind:value={rInhibitWindow} />
              </div>
              seconds (blank = {DEFAULT_INHIBIT_WINDOW_S})
            </div>
            {#if rlFieldErr.inhibitWindow}<p class="text-xs text-danger">{rlFieldErr.inhibitWindow}</p>{/if}
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
          <Button onclick={cancelRule}>Cancel</Button>
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
        <!-- Summary counts cover every rule, whatever the rule filter. -->
        <span class="text-xs text-muted">
          {evRule ? "All rules: " : ""}{count(summary.delivered)} delivered ·
          <span class="text-warn">{count(summary.failed)} failed</span> · {count(summary.pending)} pending
        </span>
      {/if}
      <Button size="sm" onclick={refreshEvents}>Refresh</Button>
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
      onRetry={() => loadEvents()}
      emptyMessage="No events match these filters. Events appear here once a rule fires; widen the filters to see more." />
    {#if events.length}
      <!-- Pages load newest first, so a column sort reorders the loaded
           events, not the whole history. -->
      <div class="mt-1 flex flex-wrap items-center gap-3 text-xs text-muted">
        <span>
          Showing {count(events.length)}{#if evTotal != null} of {count(evTotal)}{/if}
          event{(evTotal ?? events.length) === 1 ? "" : "s"}. Sorting applies to the loaded events.
        </span>
        {#if evMore}
          <Button size="sm" disabled={evLoadingMore || evLoading} onclick={() => loadEvents(true)}>
            {evLoadingMore ? "Loading…" : `Load ${EV_PAGE} more`}
          </Button>
        {/if}
      </div>
    {/if}
  {/if}
</div>
