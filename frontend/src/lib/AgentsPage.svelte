<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { copyText } from "./clipboard";
  import {
    ApiError,
    getAgentSummary,
    listAgents,
    listAgentPolicies,
    putAgentPolicy,
    listEnrollmentTokens,
    mintEnrollmentToken,
    revokeAgent,
    triggerAgentUpdate,
    deleteAgent,
    revokeEnrollmentToken,
    listConfigGroups,
    createConfigGroup,
    updateConfigGroup,
    deleteConfigGroup,
    assignConfigGroup,
    issueInstallerConfig,
    AGENT_LOG_LEVELS,
    SCAN_PRESET_NAMES,
    type AgentOut,
    type AgentFleetSummary,
    type EnrollmentTokenOut,
    type ConfigGroupOut,
    type ConfigGroupIn,
    type GroupSettings,
    type ScanSelection,
    type InstallerConfigOut,
  } from "./api";

  // W6-D4 — the agent management page: fleet status header, the agents table
  // (with inline config-group assignment), enrollment + console-installer card,
  // and config-group CRUD. Relocated/extended from the old Admin AgentsPanel.
  let error = $state("");
  let agents = $state<AgentOut[]>([]);
  let tokens = $state<EnrollmentTokenOut[]>([]);
  let groups = $state<ConfigGroupOut[]>([]);
  let summary = $state<AgentFleetSummary | null>(null);

  // Server-side pagination for the registered-agents table: a large fleet can
  // reach hundreds/thousands of agents, so the console only ever loads one
  // window. The status header keeps its own one-query /agents/summary tallies.
  const AGENTS_PAGE = 50;
  let agentsTotal = $state(0);
  let agentsOffset = $state(0);

  async function agentsPage(delta: number) {
    const next = agentsOffset + delta * AGENTS_PAGE;
    agentsOffset = Math.max(0, Math.min(next, Math.max(0, agentsTotal - 1)));
    await refresh();
  }

  // Online window for the per-row dot. The AUTHORITATIVE connected/disconnected
  // split is the /agents/summary tally (server applies the configured threshold);
  // this dot mirrors the default 5-minute window for an at-a-glance row hint.
  const ONLINE_WINDOW_MS = 5 * 60 * 1000;

  function errDetail(e: unknown): string {
    if (e instanceof ApiError) {
      try {
        const j = JSON.parse(e.body);
        if (j?.detail) return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* body not JSON */
      }
      return e.body || String(e);
    }
    return String(e);
  }

  async function refreshSummary() {
    try {
      summary = await getAgentSummary();
    } catch {
      /* transient — keep last-known tallies */
    }
  }

  async function refresh() {
    error = "";
    try {
      const [page, toks, grps] = await Promise.all([
        listAgents(AGENTS_PAGE, agentsOffset),
        listEnrollmentTokens(),
        listConfigGroups(),
      ]);
      agents = page.items;
      agentsTotal = page.total;
      // A shrink (revoke-purge on the last page) can strand the offset past
      // the end — snap back and refetch the final page.
      if (agentsOffset > 0 && agentsOffset >= page.total) {
        agentsOffset = Math.max(0, (Math.ceil(page.total / AGENTS_PAGE) - 1) * AGENTS_PAGE);
        agents = (await listAgents(AGENTS_PAGE, agentsOffset)).items;
      }
      tokens = toks;
      groups = grps;
      await Promise.all([refreshSummary(), reloadPolicies()]);
    } catch (e) {
      error = errDetail(e);
    }
  }

  let summaryTimer: ReturnType<typeof setInterval>;
  onMount(() => {
    refresh();
    summaryTimer = setInterval(refreshSummary, 15000); // status header auto-refresh
  });
  onDestroy(() => clearInterval(summaryTimer));

  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
  function relTime(iso: string | null): string {
    if (!iso) return "never";
    const s = Math.max(0, Math.floor((Date.now() - new Date(iso).getTime()) / 1000));
    if (s < 60) return `${s}s ago`;
    const m = Math.floor(s / 60);
    if (m < 60) return `${m}m ago`;
    const h = Math.floor(m / 60);
    if (h < 24) return `${h}h ago`;
    return `${Math.floor(h / 24)}d ago`;
  }
  // Compact tooltip from the agent's self-reported health snapshot (rides its
  // command poll; stored verbatim on the agent row). Pre-2026-08 agent builds
  // send none — the tooltip degrades to the last-seen timestamp alone.
  function healthTitle(a: AgentOut): string {
    const lines: string[] = [
      a.last_seen_at ? `last seen ${new Date(a.last_seen_at).toLocaleString()}` : "never seen",
    ];
    const h = a.health;
    if (h) {
      if (typeof h.uptime_s === "number") {
        const s = h.uptime_s;
        const up = s >= 86400 ? `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
          : s >= 3600 ? `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
          : `${Math.floor(s / 60)}m`;
        lines.push(`uptime ${up}`);
      }
      if (typeof h.outbox_pending === "number") lines.push(`replication backlog ${h.outbox_pending} event${h.outbox_pending === 1 ? "" : "s"}`);
      if (typeof h.index_items === "number") lines.push(`local index ${h.index_items.toLocaleString()} items`);
      const scan = h.scan as Record<string, unknown> | undefined;
      if (scan && typeof scan.status === "string") {
        lines.push(`scan: ${scan.status}${typeof scan.seen === "number" ? ` (${scan.seen} seen)` : ""}`);
      }
      if (a.health_at) lines.push(`health as of ${new Date(a.health_at).toLocaleString()}`);
    }
    return lines.join("\n");
  }

  function isOnline(a: AgentOut): boolean {
    if (a.status !== "active" || !a.last_seen_at) return false;
    return Date.now() - new Date(a.last_seen_at).getTime() <= ONLINE_WINDOW_MS;
  }
  function statusClass(s: string): string {
    if (s === "active") return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300";
    if (s === "revoked") return "bg-red-100 text-red-700 dark:bg-red-900/40 dark:text-red-300";
    return "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300";
  }
  function tokenStatusClass(s: string): string {
    if (s === "active") return "text-emerald-600";
    if (s === "consumed") return "text-slate-400";
    return "text-amber-600";
  }

  // --- agents table: inline config-group assignment --------------------------
  let assigning = $state<Record<string, boolean>>({});
  async function assignGroup(a: AgentOut, groupId: string) {
    assigning[a.id] = true;
    error = "";
    try {
      await assignConfigGroup(a.id, groupId || null);
      a.config_group_id = groupId || null;
      agents = agents; // trigger reactivity
      await Promise.all([refreshSummary(), reloadGroups()]);
    } catch (e) {
      error = errDetail(e);
      await refresh(); // resync the dropdown to server truth
    } finally {
      assigning[a.id] = false;
    }
  }

  async function reloadGroups() {
    try {
      groups = await listConfigGroups();
    } catch {
      /* keep last-known */
    }
  }

  // --- Local access policy (P7-T4 policy channel, GLOBAL scope) --------------
  // web_ui_enabled / local_access_enabled / auth_required are POLICY keys, not
  // config-group settings — they ride the append-only agent-policies channel
  // with most-specific-wins (agent > rollout-group > global) and NO key
  // merging. This card edits the GLOBAL document (fleet-wide defaults);
  // narrower scopes are API-only and override it wholesale.
  let globalPolicy = $state<Record<string, unknown> | null>(null);
  let policyBusy = $state(false);
  let policyError = $state("");

  const policyWebUI = $derived((globalPolicy?.web_ui_enabled ?? false) === true);
  const policyLocalAccess = $derived((globalPolicy?.local_access_enabled ?? true) === true);
  const policyAuthRequired = $derived((globalPolicy?.auth_required ?? true) === true);

  async function reloadPolicies() {
    try {
      const rows = await listAgentPolicies();
      globalPolicy = rows.find((r) => r.scope === "global")?.policy ?? {};
    } catch {
      globalPolicy = null; // card renders a load-failure note
    }
  }

  async function setPolicyKey(key: string, value: boolean) {
    if (policyBusy) return;
    policyBusy = true;
    policyError = "";
    try {
      // No key merging across scopes AND append-only versions: always write
      // the full current global document with just this key changed.
      const next = { ...(globalPolicy ?? {}), [key]: value };
      const row = await putAgentPolicy("global", next);
      globalPolicy = row.policy;
    } catch (e) {
      policyError = errDetail(e);
      await reloadPolicies(); // resync to server truth
    } finally {
      policyBusy = false;
    }
  }

  // Per-agent "update at next check-in": queue a self_update command. The
  // button only renders when the list said update_available && !update_pending,
  // but a 409 can still race (someone else clicked / agent just updated) — the
  // refresh resyncs either way.
  let updating: Record<string, boolean> = $state({});
  async function updateAgentNow(id: string, name: string) {
    updating[id] = true;
    try {
      await triggerAgentUpdate(id);
      await refresh();
    } catch (e) {
      error = `update ${name}: ${errDetail(e)}`;
      await refresh();
    } finally {
      updating[id] = false;
    }
  }

  async function dropAgent(id: string, name: string) {
    if (!confirm(`Revoke agent "${name}"? It will be denied all replication/config access.`)) return;
    try {
      await revokeAgent(id);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }
  // Hard delete: 409 while the agent still owns libraries/items — that message
  // surfaces verbatim (preserve the 409-owns-data messaging).
  async function purgeAgent(id: string, name: string) {
    if (!confirm(`DELETE agent "${name}" permanently?`)) return;
    try {
      await deleteAgent(id);
      await refresh();
    } catch (e) {
      const detail = errDetail(e);
      // The agent owns libraries/items: offer the one-action cascade instead
      // of sending the operator off to delete each library by hand first.
      if (e instanceof ApiError && e.status === 409 && detail.includes("replicated data")) {
        if (
          confirm(
            `Agent "${name}" still owns replicated data:
${detail}

` +
              "Delete the agent AND all of its libraries (including their " +
              "items and scan history)? This cannot be undone.",
          )
        ) {
          try {
            await deleteAgent(id, true);
            await refresh();
            return;
          } catch (e2) {
            error = errDetail(e2);
            return;
          }
        }
        return;
      }
      error = detail;
    }
  }

  // --- enrollment tokens -----------------------------------------------------
  let newGroup = $state("default");
  let newTtl = $state<number | undefined>(undefined);
  let minting = $state(false);
  let minted = $state<{ token: string; expires_at: string } | null>(null);
  let mintedCopied = $state(false);

  async function mint() {
    if (!newGroup.trim()) return;
    minting = true;
    mintedCopied = false;
    error = "";
    try {
      const r = await mintEnrollmentToken(newGroup.trim(), newTtl || undefined);
      minted = { token: r.token, expires_at: r.expires_at };
      await refresh();
    } catch (e) {
      error = errDetail(e);
    } finally {
      minting = false;
    }
  }
  async function copyMinted() {
    if (minted) mintedCopied = await copyText(minted.token);
  }
  async function dropToken(hash: string, force = false) {
    if (force && !confirm("Delete this consumed token row? Its consumed-by link is preserved in the audit log.")) return;
    try {
      await revokeEnrollmentToken(hash, force);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }

  // --- console installer -----------------------------------------------------
  let insName = $state("");
  let insGroupId = $state("");
  let insLogLevel = $state("");
  let issuing = $state(false);
  let installer = $state<InstallerConfigOut | null>(null);
  let sidecarCopied = $state(false);
  let hintCopied = $state<string | null>(null);

  const sidecarJson = $derived(installer ? JSON.stringify(installer.sidecar, null, 2) : "");

  async function issueInstaller() {
    issuing = true;
    sidecarCopied = false;
    hintCopied = null;
    error = "";
    try {
      installer = await issueInstallerConfig({
        agent_name: insName.trim() || null,
        config_group_id: insGroupId || null,
        log_level: insLogLevel || null,
      });
      await refresh(); // the mint created a new token row
    } catch (e) {
      error = errDetail(e);
    } finally {
      issuing = false;
    }
  }
  async function copySidecar() {
    if (sidecarJson) sidecarCopied = await copyText(sidecarJson);
  }
  function downloadSidecar() {
    if (!sidecarJson) return;
    const blob = new Blob([sidecarJson], { type: "application/json" });
    const url = URL.createObjectURL(blob);
    const a = document.createElement("a");
    a.href = url;
    a.download = "filearr-agent.json";
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
  }
  async function copyHint(os: "windows" | "linux" | "macos") {
    if (!installer) return;
    if (await copyText(installer.install_hint[os])) hintCopied = os;
  }

  // --- config-group CRUD dialog ---------------------------------------------
  type SelRow = {
    preset: string;
    pathsText: string;
    includeText: string;
    excludeText: string;
    enabled: boolean;
  };
  type GroupForm = {
    id: string | null; // null = create
    name: string;
    description: string;
    logLevel: string;
    cron: string;
    inventoryEnabled: boolean;
    collectorsText: string;
    selections: SelRow[];
    // tri-state local-surface gates: "" = inherit, "on" | "off" explicit
    webUI: string;
    localAccess: string;
    authRequired: string;
  };
  let dialog = $state<GroupForm | null>(null);
  let dialogError = $state("");
  let dialogBusy = $state(false);

  function emptySel(): SelRow {
    return { preset: "", pathsText: "", includeText: "", excludeText: "", enabled: true };
  }

  function openCreate() {
    dialogError = "";
    dialog = {
      id: null,
      name: "",
      description: "",
      logLevel: "",
      cron: "",
      inventoryEnabled: false,
      collectorsText: "",
      selections: [],
      webUI: "",
      localAccess: "",
      authRequired: "",
    };
  }

  function openEdit(g: ConfigGroupOut) {
    dialogError = "";
    const s = g.settings ?? {};
    dialog = {
      id: g.id,
      name: g.name,
      description: g.description ?? "",
      logLevel: s.log_level ?? "",
      cron: s.scan_schedule_cron ?? "",
      inventoryEnabled: s.inventory?.enabled ?? false,
      collectorsText: (s.inventory?.collectors ?? []).join(", "),
      selections: (s.scan_selections ?? []).map((sel) => ({
        preset: sel.preset ?? "",
        pathsText: (sel.paths ?? []).join("\n"),
        includeText: (sel.include_regex ?? []).join("\n"),
        excludeText: (sel.exclude_regex ?? []).join("\n"),
        enabled: sel.enabled ?? true,
      })),
      webUI: toTri(s.web_ui_enabled),
      localAccess: toTri(s.local_access_enabled),
      authRequired: toTri(s.auth_required),
    };
  }

  const toTri = (v: boolean | null | undefined): string => (v === true ? "on" : v === false ? "off" : "");

  const splitLines = (t: string): string[] =>
    t.split("\n").map((x) => x.trim()).filter(Boolean);
  const splitTags = (t: string): string[] =>
    t.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

  // Build the typed settings object, omitting empty keys so the doc stays minimal
  // (the backend rejects unknown keys — we only ever send the four known ones).
  function buildSettings(f: GroupForm): GroupSettings {
    const settings: GroupSettings = {};
    if (f.logLevel) settings.log_level = f.logLevel as GroupSettings["log_level"];
    if (f.webUI) settings.web_ui_enabled = f.webUI === "on";
    if (f.localAccess) settings.local_access_enabled = f.localAccess === "on";
    if (f.authRequired) settings.auth_required = f.authRequired === "on";
    if (f.cron.trim()) settings.scan_schedule_cron = f.cron.trim();
    if (f.inventoryEnabled || f.collectorsText.trim()) {
      settings.inventory = {
        enabled: f.inventoryEnabled,
        collectors: splitTags(f.collectorsText),
      };
    }
    if (f.selections.length) {
      settings.scan_selections = f.selections.map((r): ScanSelection => {
        const sel: ScanSelection = { enabled: r.enabled };
        if (r.preset) sel.preset = r.preset;
        const paths = splitLines(r.pathsText);
        if (paths.length) sel.paths = paths;
        const inc = splitLines(r.includeText);
        if (inc.length) sel.include_regex = inc;
        const exc = splitLines(r.excludeText);
        if (exc.length) sel.exclude_regex = exc;
        return sel;
      });
    }
    return settings;
  }

  async function saveGroup() {
    if (!dialog) return;
    if (!dialog.name.trim()) {
      dialogError = "Name is required.";
      return;
    }
    dialogBusy = true;
    dialogError = "";
    try {
      const settings = buildSettings(dialog);
      if (dialog.id === null) {
        const body: ConfigGroupIn = {
          name: dialog.name.trim(),
          description: dialog.description.trim() || null,
          settings,
        };
        await createConfigGroup(body);
      } else {
        await updateConfigGroup(dialog.id, {
          name: dialog.name.trim(),
          description: dialog.description.trim() || null,
          settings,
        });
      }
      dialog = null;
      await refresh();
    } catch (e) {
      // Surface the backend's 422 detail inline (unknown key / bad regex / bad
      // cron / bad preset / oversize) or a 409 duplicate-name message.
      dialogError = errDetail(e);
    } finally {
      dialogBusy = false;
    }
  }

  async function removeGroup(g: ConfigGroupOut) {
    const n = g.member_count;
    if (!confirm(
      `Delete config group "${g.name}"?` +
        (n > 0 ? ` ${n} member agent(s) will reset to built-in defaults.` : "")
    )) return;
    try {
      await deleteConfigGroup(g.id);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }
</script>

<div class="mt-4">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Agents</h2>
    <span class="text-xs text-slate-500">distributed fleet</span>
    <div class="grow"></div>
    <button
      class="rounded-lg border border-slate-300 px-3 py-1 text-sm text-slate-600 dark:border-slate-700 dark:text-slate-300"
      onclick={refresh}>Refresh</button>
  </div>

  {#if error}<p class="mt-2 text-sm text-red-600">{error}</p>{/if}

  <!-- Status header: fleet count tiles (auto-refresh ~15s) -->
  <div class="mt-4 grid grid-cols-2 gap-3 sm:grid-cols-4">
    <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <div class="flex items-center gap-2 text-xs text-slate-500">
        <span class="h-2 w-2 rounded-full bg-emerald-500"></span>Connected
      </div>
      <div class="mt-1 text-3xl font-bold tabular-nums text-emerald-600 dark:text-emerald-400">
        {summary?.connected ?? "—"}
      </div>
    </div>
    <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <div class="flex items-center gap-2 text-xs text-slate-500">
        <span class="h-2 w-2 rounded-full bg-slate-400"></span>Disconnected
      </div>
      <div class="mt-1 text-3xl font-bold tabular-nums text-slate-600 dark:text-slate-300">
        {summary?.disconnected ?? "—"}
      </div>
    </div>
    <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <div class="flex items-center gap-2 text-xs text-slate-500">
        <span class="h-2 w-2 rounded-full bg-amber-500"></span>Pending
      </div>
      <div class="mt-1 text-3xl font-bold tabular-nums text-amber-600 dark:text-amber-400">
        {summary?.pending ?? "—"}
      </div>
    </div>
    <div class="rounded-xl border border-slate-200 p-3 dark:border-slate-800">
      <div class="flex items-center gap-2 text-xs text-slate-500">
        <span class="h-2 w-2 rounded-full bg-red-500"></span>Revoked
      </div>
      <div class="mt-1 text-3xl font-bold tabular-nums text-red-600 dark:text-red-400">
        {summary?.revoked ?? "—"}
      </div>
    </div>
  </div>
  {#if summary}
    <p class="mt-1 text-xs text-slate-400">{summary.total} agent(s) total.</p>
  {/if}

  <!-- Registered agents renders at the BOTTOM of the page (below enrollment +
       config groups): in a big fleet it is the longest element, and it pages
       server-side (AGENTS_PAGE per window) so thousands of agents never land
       in one response. Defined as a snippet here, rendered at the end. -->
  {#snippet registeredAgents()}
    <h3 class="mt-8 font-medium">Registered agents</h3>
    {#if agentsTotal === 0}
      <p class="py-2 text-slate-400">No agents registered.</p>
    {:else}
      <div class="mt-1 overflow-x-auto">
        <table class="w-full min-w-[56rem] text-sm">
          <thead class="text-left text-slate-500">
            <tr class="border-b border-slate-200 dark:border-slate-800">
              <th class="py-2 pr-3 font-medium">Name</th>
              <th class="py-2 pr-3 font-medium">Hostname</th>
              <th class="py-2 pr-3 font-medium">Platform</th>
              <th class="py-2 pr-3 font-medium">Status</th>
              <th class="py-2 pr-3 font-medium">Online</th>
              <th class="py-2 pr-3 font-medium">Config group</th>
              <th class="py-2 pr-3 font-medium">Version</th>
              <th class="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
          {#each agents as a (a.id)}
            <tr class="align-middle">
              <td class="py-2 pr-3 font-medium">{a.name}</td>
              <td class="py-2 pr-3 font-mono text-xs text-slate-500">{a.hostname}</td>
              <td class="py-2 pr-3 text-slate-500">{a.platform}</td>
              <td class="py-2 pr-3">
                <span class="rounded-full px-2 py-0.5 text-xs font-medium {statusClass(a.status)}">{a.status}</span>
              </td>
              <td class="py-2 pr-3">
                {#if isOnline(a)}
                  <span class="inline-flex items-center gap-1 text-xs text-emerald-600 dark:text-emerald-400" title={healthTitle(a)}>
                    <span class="h-1.5 w-1.5 rounded-full bg-emerald-500"></span>online
                    <span class="tabular-nums text-slate-400" title="Last heartbeat: every authenticated request from the agent (command poll ~60s, update poll, replication) refreshes it.">· {relTime(a.last_seen_at)}</span>
                  </span>
                {:else}
                  <span class="inline-flex items-center gap-1 text-xs text-slate-500" title={healthTitle(a)}>
                    <span class="h-1.5 w-1.5 rounded-full bg-slate-400"></span>{relTime(a.last_seen_at)}
                  </span>
                {/if}
                {#if a.last_auth_mode === "mtls"}
                  <span class="ml-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                    title="Central verified this agent's last authenticated request via the mTLS proxy path (client-certificate SAN identity) — observed server-side, not self-reported.">mTLS</span>
                {:else if a.last_auth_mode === "bearer"}
                  <span class="ml-1 rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                    title="This agent's last authenticated request used the interim bearer (fingerprint) path — it has not been switched to the mTLS endpoint yet. See the mode-flip runbook (docs: TLS).">bearer</span>
                {/if}
              </td>
              <td class="py-2 pr-3">
                <select
                  class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs disabled:opacity-50 dark:border-slate-700 dark:bg-slate-800"
                  disabled={assigning[a.id] || a.status === "revoked"}
                  value={a.config_group_id ?? ""}
                  onchange={(e) => assignGroup(a, (e.currentTarget as HTMLSelectElement).value)}>
                  <option value="">Default (built-in)</option>
                  {#each groups as g (g.id)}
                    <option value={g.id}>{g.name}</option>
                  {/each}
                </select>
              </td>
              <td class="py-2 pr-3 text-slate-500">
                <span class="font-mono text-xs">{a.agent_version ?? "—"}</span>
                {#if a.update_pending}
                  <span
                    class="ml-1 rounded-full bg-sky-100 px-2 py-0.5 text-xs font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
                    title={`Updating to ${a.update_target ?? "the current central version"} at its next check-in`}
                    >update queued</span>
                {:else if a.update_available && a.capabilities?.container}
                  <span
                    class="ml-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title={`A newer agent build exists (${a.update_target}). This agent runs in a container, which updates by pulling the new agent image on its host — central never swaps binaries inside a container.`}
                    >newer image available</span>
                {:else if a.update_available}
                  <span
                    class="ml-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title={`Central offers ${a.update_target}`}>update available</span>
                {/if}
              </td>
              <td class="py-2 text-right whitespace-nowrap">
                {#if a.status !== "revoked"}
                  {#if a.update_available && !a.update_pending && !a.capabilities?.container}
                    <button
                      class="text-sky-600 disabled:opacity-50 dark:text-sky-400"
                      disabled={updating[a.id]}
                      title={`Queue an update to ${a.update_target} — applied at the agent's next check-in (~1 min)`}
                      onclick={() => updateAgentNow(a.id, a.name)}>update</button>
                  {/if}
                  <button class="ml-3 text-red-600" onclick={() => dropAgent(a.id, a.name)}>revoke</button>
                {/if}
                <button
                  class="ml-3 text-red-600"
                  title="Hard delete — only while the agent owns no libraries/items"
                  onclick={() => purgeAgent(a.id, a.name)}>delete</button>
              </td>
            </tr>
          {/each}
          </tbody>
        </table>
      </div>
      <div class="mt-2 flex items-center gap-3 text-xs text-slate-500">
        <span>
          {agentsOffset + 1}–{Math.min(agentsOffset + AGENTS_PAGE, agentsTotal)} of {agentsTotal}
        </span>
        <button
          class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
          onclick={() => agentsPage(-1)}
          disabled={agentsOffset === 0}>Prev</button>
        <button
          class="rounded border border-slate-300 px-2 py-0.5 disabled:opacity-40 dark:border-slate-700"
          onclick={() => agentsPage(1)}
          disabled={agentsOffset + AGENTS_PAGE >= agentsTotal}>Next</button>
      </div>
    {/if}
  {/snippet}

  <!-- Enrollment & installer card -->
  <div class="mt-8 rounded-xl border border-slate-200 p-4 dark:border-slate-800">
    <h3 class="font-medium">Enrollment &amp; installer</h3>
    <p class="mt-1 text-xs text-slate-500">
      Mint a single-use, short-lived enrollment token — or generate a full installer
      sidecar (<code class="font-mono">filearr-agent.json</code>) the console agent
      consumes directly. Tokens are shown once and never stored in the clear.
    </p>
    <p class="mt-2 text-xs text-slate-500"
      title="One script for the whole Windows agent lifecycle, pre-configured with this central's URL: on a machine without the agent it provisions (mints its own token via the API, installs the service, sets scan roots); on a machine with the agent it updates the binary and applies config changes (-ScanRoot, -MtlsUrl). Requires an elevated PowerShell; pass -ApiKey <admin key> when authentication is enabled.">
      <b>Windows one-script lifecycle:</b>
      <code class="font-mono break-all select-all">irm {location.origin}/api/v1/agent-dist/manage-windows-agent.ps1 -OutFile manage-windows-agent.ps1</code>
      — then <code class="font-mono">.\manage-windows-agent.ps1 -ScanRoot D:\media</code> (elevated); the same script updates and reconfigures on later runs.
    </p>

    <!-- Simple token mint -->
    <div class="mt-3 flex flex-wrap items-end gap-2">
      <label class="text-xs text-slate-500">
        rollout group
        <input class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700" bind:value={newGroup} />
      </label>
      <label class="text-xs text-slate-500">
        TTL (min, blank = default)
        <input class="mt-1 block w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          type="number" min="1" placeholder="60" bind:value={newTtl} />
      </label>
      <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white disabled:opacity-50"
        disabled={minting} onclick={mint}>Mint token</button>
    </div>

    {#if minted}
      <div class="mt-2 rounded-lg border border-amber-400 bg-amber-50 p-2 text-sm dark:bg-amber-950/30">
        <p class="font-medium">Copy this token now — it will not be shown again.</p>
        <div class="mt-1 flex items-center gap-2">
          <code class="grow break-all font-mono text-xs">{minted.token}</code>
          <button class="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onclick={copyMinted}>
            {mintedCopied ? "Copied" : "Copy"}
          </button>
        </div>
        <p class="mt-1 text-xs text-slate-500">Expires {fmt(minted.expires_at)}</p>
      </div>
    {/if}

    <!-- Installer sidecar generator -->
    <div class="mt-5 border-t border-slate-200 pt-4 dark:border-slate-800">
      <h4 class="text-sm font-medium">Generate installer sidecar</h4>
      <div class="mt-2 flex flex-wrap items-end gap-2">
        <label class="text-xs text-slate-500">
          agent name (optional)
          <input class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
            placeholder="(auto from hostname)" bind:value={insName} />
        </label>
        <label class="text-xs text-slate-500">
          config group
          <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
            bind:value={insGroupId}>
            <option value="">Default (built-in)</option>
            {#each groups as g (g.id)}
              <option value={g.id}>{g.name}</option>
            {/each}
          </select>
        </label>
        <label class="text-xs text-slate-500">
          log level
          <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
            bind:value={insLogLevel}>
            <option value="">(default)</option>
            {#each AGENT_LOG_LEVELS as lvl}
              <option value={lvl}>{lvl}</option>
            {/each}
          </select>
        </label>
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white disabled:opacity-50"
          disabled={issuing} onclick={issueInstaller}>Generate</button>
      </div>

      {#if installer}
        <div class="mt-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-sm dark:bg-amber-950/30">
          <div class="flex flex-wrap items-center gap-2">
            <p class="font-medium">Sidecar (<code class="font-mono">filearr-agent.json</code>) — contains a show-once token.</p>
            <span class="grow"></span>
            <span class="text-xs text-slate-500">Token expires {fmt(installer.expires_at)}</span>
          </div>
          <pre class="mt-2 max-h-64 overflow-auto rounded bg-slate-900/90 p-2 font-mono text-xs text-slate-100">{sidecarJson}</pre>
          <div class="mt-2 flex flex-wrap gap-2">
            <button class="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700" onclick={copySidecar}>
              {sidecarCopied ? "Copied" : "Copy JSON"}
            </button>
            <button class="rounded bg-[var(--accent)] px-2 py-1 text-xs text-white" onclick={downloadSidecar}>
              Download filearr-agent.json
            </button>
          </div>

          <div class="mt-3">
            <p class="text-xs font-medium text-slate-600 dark:text-slate-300">Install one-liners</p>
            {#each [["windows", "Windows"], ["linux", "Linux"], ["macos", "macOS"]] as [os, label]}
              <div class="mt-1.5">
                <div class="flex items-center gap-2">
                  <span class="w-16 text-xs text-slate-500">{label}</span>
                  <button class="rounded border border-slate-300 px-2 py-0.5 text-[11px] dark:border-slate-700"
                    onclick={() => copyHint(os as "windows" | "linux" | "macos")}>
                    {hintCopied === os ? "Copied" : "Copy"}
                  </button>
                </div>
                <pre class="mt-1 overflow-auto rounded bg-slate-100 p-2 font-mono text-[11px] text-slate-700 dark:bg-slate-800 dark:text-slate-200">{installer.install_hint[os as "windows" | "linux" | "macos"]}</pre>
              </div>
            {/each}
            <p class="mt-1 text-[11px] text-slate-400">
              Replace <code class="font-mono">{"{agent_id}"}</code> / <code class="font-mono">{"{version}"}</code>
              from the fleet console after enrollment (the artifact path is agent-authenticated).
            </p>
          </div>
        </div>
      {/if}
    </div>

    <!-- Enrollment tokens -->
    <h4 class="mt-5 text-sm font-medium">Enrollment tokens</h4>
    {#if tokens.length === 0}
      <p class="py-2 text-slate-400">No tokens.</p>
    {:else}
      <div class="overflow-x-auto">
        <table class="mt-1 w-full text-sm">
          <thead class="text-left text-slate-500">
            <tr><th class="py-1 pr-3">hash</th><th class="pr-3">group</th><th class="pr-3">status</th><th class="pr-3">expires</th><th></th></tr>
          </thead>
          <tbody>
            {#each tokens as t (t.token_hash)}
              <tr class="border-t border-slate-200 dark:border-slate-800">
                <td class="py-1 pr-3 font-mono text-xs">{t.token_hash.slice(0, 12)}…</td>
                <td class="pr-3">{t.rollout_group}</td>
                <td class="pr-3 {tokenStatusClass(t.status)}">{t.status}</td>
                <td class="pr-3 text-slate-500">{fmt(t.expires_at)}</td>
                <td class="text-right">
                  {#if t.status === "active"}
                    <button class="text-red-600" onclick={() => dropToken(t.token_hash)}>revoke</button>
                  {:else}
                    <button class="text-red-600" onclick={() => dropToken(t.token_hash, true)}>delete</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- Config groups -->
  <div class="mt-8">
    <div class="flex items-center gap-3">
      <h3 class="font-medium">Config groups</h3>
      <span class="text-xs text-slate-500">reusable remote-configuration bundles</span>
      <div class="grow"></div>
      <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={openCreate}>New group</button>
    </div>

    {#if groups.length === 0}
      <p class="py-2 text-slate-400">No config groups. Agents without a group use built-in defaults.</p>
    {:else}
      <div class="mt-2 overflow-x-auto">
        <table class="w-full min-w-[40rem] text-sm">
          <thead class="text-left text-slate-500">
            <tr class="border-b border-slate-200 dark:border-slate-800">
              <th class="py-2 pr-3 font-medium">Name</th>
              <th class="py-2 pr-3 font-medium">Description</th>
              <th class="py-2 pr-3 font-medium">Members</th>
              <th class="py-2 pr-3 font-medium">Updated</th>
              <th class="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
            {#each groups as g (g.id)}
              <tr>
                <td class="py-2 pr-3 font-medium">{g.name}</td>
                <td class="max-w-[20rem] truncate py-2 pr-3 text-slate-500" title={g.description ?? ""}>{g.description ?? "—"}</td>
                <td class="py-2 pr-3 tabular-nums text-slate-500">{g.member_count}</td>
                <td class="py-2 pr-3 text-slate-500">{relTime(g.updated_at)}</td>
                <td class="py-2 text-right whitespace-nowrap">
                  <button class="text-[var(--accent)]" onclick={() => openEdit(g)}>edit</button>
                  <button class="ml-3 text-red-600" onclick={() => removeGroup(g)}>delete</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  <!-- Local access policy (fleet-wide, the P7-T4 policy channel) -->
  <div class="mt-8 rounded-xl border border-slate-200 p-4 dark:border-slate-800">
    <div class="flex items-center gap-3">
      <h3 class="font-medium">Local access policy</h3>
      <span class="text-xs text-slate-500">fleet-wide defaults (global scope)</span>
    </div>
    <p class="mt-1 text-xs text-slate-500">
      Gates every agent's on-device query surfaces. Delivered over the signed
      policy channel on the agents' next poll; the web UI fails closed when an
      agent's cached policy goes stale. Per-agent / per-rollout-group overrides
      exist via <code class="font-mono">PUT /api/v1/agent-policies/&lt;scope&gt;</code>
      and take precedence <em>wholesale</em> (no key merging).
    </p>
    {#if globalPolicy === null}
      <p class="mt-2 text-sm text-amber-600">Could not load the current policy — refresh to retry.</p>
    {:else}
      {#if policyError}<p class="mt-2 text-sm text-red-600">{policyError}</p>{/if}
      <div class="mt-3 flex flex-col gap-2 text-sm">
        <label class="flex items-start gap-2">
          <input type="checkbox" class="mt-0.5" disabled={policyBusy}
            checked={policyWebUI}
            onchange={(e) => setPolicyKey("web_ui_enabled", (e.currentTarget as HTMLInputElement).checked)} />
          <span>
            <span class="font-medium">Local web UI</span>
            <span class="block text-xs text-slate-500">
              Read-only browser search on each agent (port 8686; containers also
              need the template's remote-access toggle). Default off — a
              never-contacted agent serves nothing.
            </span>
          </span>
        </label>
        <label class="flex items-start gap-2">
          <input type="checkbox" class="mt-0.5" disabled={policyBusy}
            checked={policyAuthRequired}
            onchange={(e) => setPolicyKey("auth_required", (e.currentTarget as HTMLInputElement).checked)} />
          <span>
            <span class="font-medium">Web UI requires auth token</span>
            <span class="block text-xs text-slate-500">
              The UI demands the agent's bootstrap token before serving. Turn
              off only on a trusted LAN.
            </span>
          </span>
        </label>
        <label class="flex items-start gap-2">
          <input type="checkbox" class="mt-0.5" disabled={policyBusy}
            checked={policyLocalAccess}
            onchange={(e) => setPolicyKey("local_access_enabled", (e.currentTarget as HTMLInputElement).checked)} />
          <span>
            <span class="font-medium">Local query API / CLI</span>
            <span class="block text-xs text-slate-500">
              The on-device <code class="font-mono">filearr query</code>
              socket. Default on; an explicit off persists through offline
              periods.
            </span>
          </span>
        </label>
      </div>
    {/if}
  </div>

  {@render registeredAgents()}
</div>

<!-- Config-group create/edit dialog -->
{#if dialog}
  <div class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
    <div class="my-8 w-full max-w-2xl rounded-xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-900">
      <div class="flex items-center gap-3">
        <h3 class="text-lg font-semibold">{dialog.id === null ? "New config group" : "Edit config group"}</h3>
        <div class="grow"></div>
        <button class="text-slate-500" onclick={() => (dialog = null)}>✕</button>
      </div>

      {#if dialogError}
        <p class="mt-2 rounded-lg border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">{dialogError}</p>
      {/if}

      <div class="mt-3 flex flex-col gap-3">
        <label class="text-xs text-slate-500">Name
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" bind:value={dialog.name} />
        </label>
        <label class="text-xs text-slate-500">Description
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" bind:value={dialog.description} />
        </label>

        <div class="flex flex-wrap gap-4">
          <label class="text-xs text-slate-500">Log level
            <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.logLevel}>
              <option value="">(unset)</option>
              {#each AGENT_LOG_LEVELS as lvl}
                <option value={lvl}>{lvl}</option>
              {/each}
            </select>
          </label>
          <label class="text-xs text-slate-500">Scan schedule (cron)
            <input class="mt-1 block w-56 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-slate-700" placeholder="0 3 * * *" bind:value={dialog.cron} />
          </label>
        </div>

        <!-- Local access (per-group overrides of the fleet-wide policy card) -->
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <span class="text-xs font-medium text-slate-500">Local access (per-group)</span>
          <p class="mt-0.5 text-xs text-slate-400">
            Inherit = the fleet-wide Local access policy applies. These override
            the global policy for this group's members; an explicit per-agent /
            rollout-group policy row (API) still outranks them.
          </p>
          <div class="mt-2 flex flex-wrap gap-4">
            <label class="text-xs text-slate-500">
              Web UI
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.webUI}>
                <option value="">Inherit</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
            <label class="text-xs text-slate-500">
              Web UI auth token
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.authRequired}>
                <option value="">Inherit</option>
                <option value="on">Required</option>
                <option value="off">Not required</option>
              </select>
            </label>
            <label class="text-xs text-slate-500">
              Local query API / CLI
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={dialog.localAccess}>
                <option value="">Inherit</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
          </div>
        </div>

        <!-- Inventory -->
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <label class="inline-flex items-center gap-2 text-sm">
            <input type="checkbox" bind:checked={dialog.inventoryEnabled} /> Inventory collection enabled
          </label>
          <label class="mt-2 block text-xs text-slate-500">Collectors (comma or newline separated)
            <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700" placeholder="os, hardware, packages" bind:value={dialog.collectorsText} />
          </label>
        </div>

        <!-- Scan selections -->
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium">Scan selections</span>
            <div class="grow"></div>
            <button class="rounded border border-slate-300 px-2 py-0.5 text-xs dark:border-slate-700"
              onclick={() => dialog && (dialog.selections = [...dialog.selections, emptySel()])}>+ add selection</button>
          </div>
          {#if dialog.selections.length === 0}
            <p class="mt-2 text-xs text-slate-400">No selections — the agent falls back to its defaults.</p>
          {/if}
          {#each dialog.selections as sel, i (i)}
            <div class="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <div class="flex flex-wrap items-center gap-3">
                <label class="text-xs text-slate-500">Preset
                  <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800" bind:value={sel.preset}>
                    <option value="">(none)</option>
                    {#each SCAN_PRESET_NAMES as p}
                      <option value={p}>{p}</option>
                    {/each}
                  </select>
                </label>
                <label class="inline-flex items-center gap-2 text-sm">
                  <input type="checkbox" bind:checked={sel.enabled} /> enabled
                </label>
                <div class="grow"></div>
                <button class="text-xs text-red-600" onclick={() => dialog && (dialog.selections = dialog.selections.filter((_, j) => j !== i))}>remove</button>
              </div>
              <label class="mt-2 block text-xs text-slate-500">Path specs (one per line — env tokens / globs allowed)
                <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700" placeholder={"%USERPROFILE%/Documents\n/home/*/documents"} bind:value={sel.pathsText}></textarea>
              </label>
              <div class="mt-2 flex flex-wrap gap-3">
                <label class="grow text-xs text-slate-500">Include regex (one per line)
                  <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700" bind:value={sel.includeText}></textarea>
                </label>
                <label class="grow text-xs text-slate-500">Exclude regex (one per line)
                  <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700" bind:value={sel.excludeText}></textarea>
                </label>
              </div>
            </div>
          {/each}
        </div>
      </div>

      <div class="mt-4 flex justify-end gap-2">
        <button class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700" onclick={() => (dialog = null)}>Cancel</button>
        <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-50" disabled={dialogBusy} onclick={saveGroup}>
          {dialogBusy ? "Saving…" : dialog.id === null ? "Create" : "Save"}
        </button>
      </div>
    </div>
  </div>
{/if}
