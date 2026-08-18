<script lang="ts">
  import { onDestroy, onMount } from "svelte";
  import { copyText } from "./clipboard";
  import {
    CAPABILITY_TOOLS,
    POLICY_FIELDS,
    POLICY_SECTIONS,
    RESERVED_POLICY_KEYS,
    blankPolicyForm,
    buildPolicyDoc,
    formFromDoc,
    ignoredPolicySettings,
    passthroughFromDoc,
    unknownPolicyKeys,
    unparsedPolicyKeys,
    validatePolicyForm,
    type AgentCapabilities,
    type AgentPolicyDoc,
    type PolicyFieldSpec,
    type PolicyFormState,
  } from "./agentPolicyDoc";
  import {
    MAX_ROLLOUT_TIERS,
    describeRollout,
    mergeDocuments,
    provenanceFor,
    tierEtaMinutes,
    validateTiers,
    type ConfigLayer,
  } from "./configGroups";
  import {
    minimumsByName,
    toolChip,
    type HostToolMinimum,
  } from "./hostTools";
  import {
    agentAboutMarkdown,
    agentToolCell,
    buildRows,
    modulesSummary,
    orUnknown,
    rehashCell,
    toolPathCell,
    type AgentAbout,
  } from "./agentAbout";
  import {
    MAX_COLLECTORS,
    addCollectorName,
    collectorEditorFromFetch,
    collectorStanding,
    collectorsToSave,
    preservedUnknownCollectors,
    toggleCollector,
    type CollectorEditor,
  } from "./inventoryCollectors";
  import {
    ApiError,
    agentAbout,
    getAgentSummary,
    getEffectiveConfig,
    listAgentCommands,
    listAgents,
    listConfigGroupHistory,
    listConfigRollouts,
    listEnrollmentTokens,
    listPresets,
    mintEnrollmentToken,
    promoteConfigRollout,
    cancelConfigRollout,
    rollbackConfigGroup,
    setAgentConfigGroups,
    reextractAgent,
    rehashSweepAgent,
    revokeAgent,
    runAgentMaintenance,
    suspendAgent,
    triggerAgentUpdate,
    deleteAgent,
    revokeEnrollmentToken,
    listConfigGroups,
    listInventoryCollectors,
    listHostToolMinimums,
    createConfigGroup,
    updateConfigGroup,
    deleteConfigGroup,
    issueInstallerConfig,
    AGENT_LOG_LEVELS,
    SCAN_PRESET_NAMES,
    MAX_RETAIN_SNAPSHOTS,
    type AgentOut,
    type AgentFleetSummary,
    type EnrollmentTokenOut,
    type ConfigGroupOut,
    type ConfigGroupIn,
    type ConfigGroupUpdateIn,
    type ConfigVersionOut,
    type EffectiveConfigOut,
    type GroupSettings,
    type RolloutOut,
    type RolloutTier,
    type ScanSelection,
    type InventoryConfig,
    type PermissionsConfig,
    type AuditConfig,
    type InstallerConfigOut,
  } from "./api";

  // W6-D4 — the agent management page: fleet status header, the agents table,
  // enrollment + console-installer card, and configuration-group CRUD.
  //
  // P13 (2026-08-11) collapsed the console's two competing groupings into ONE.
  // The page used to show two adjacent, differently-named group controls: one
  // selected a whole policy document by precedence walk (and doubled as the
  // release cohort for un-promoted builds), the other an orthogonal settings
  // bundle that policy resolution never consulted. Operators read the second and
  // got the first. Now there is a single kind of CONFIGURATION GROUP: an agent
  // belongs to the permanent Global group plus any number of others, they layer
  // per key in ascending priority (later wins), and every policy key is edited
  // inside the group dialog.
  let error = $state("");
  let agents = $state<AgentOut[]>([]);
  let tokens = $state<EnrollmentTokenOut[]>([]);
  let groups = $state<ConfigGroupOut[]>([]);
  let rollouts = $state<RolloutOut[]>([]);
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
        // Live rollouts feed the rollouts card and the per-group status chip.
        // Swallowed on failure: a rollout read must not blank the fleet table.
        reloadRollouts(),
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
      await Promise.all([refreshSummary(), refreshSweeps()]);
    } catch (e) {
      error = errDetail(e);
    }
  }

  let summaryTimer: ReturnType<typeof setInterval>;
  onMount(() => {
    refresh();
    loadToolMinimums();
    loadPresets();
    // Status header auto-refresh; the sweep set rides the same tick so a
    // finished re-extract clears its badge without a manual reload. Rollouts
    // ride it too — the engine promotes on central's own minute tick, so a
    // static card would show a stale tier for as long as the page is open.
    summaryTimer = setInterval(() => {
      refreshSummary();
      refreshSweeps();
      reloadRollouts();
    }, 15000);
  });
  onDestroy(() => clearInterval(summaryTimer));

  function fmt(iso: string | null): string {
    return iso ? new Date(iso).toLocaleString() : "—";
  }
  /** Countdown to a FUTURE timestamp. `relTime` clamps at zero (it only ever
   *  looks backwards), so reusing it for a scheduled promotion would render
   *  every pending tier as "0s ago". */
  function untilTime(iso: string | null): string {
    if (!iso) return "—";
    const s = Math.floor((new Date(iso).getTime() - Date.now()) / 1000);
    if (s <= 0) return "due now";
    if (s < 60) return `in ${s}s`;
    const m = Math.floor(s / 60);
    if (m < 60) return `in ${m}m`;
    const h = Math.floor(m / 60);
    if (h < 24) return `in ${h}h ${m % 60}m`;
    return `in ${Math.floor(h / 24)}d ${h % 24}h`;
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
      // Parity phase 3: the last re-extraction sweep. This tooltip is a FIXED
      // key list, so an agent-reported key that is not named here is collected
      // and stored and then never seen by anyone — the reason to render it is
      // that "has the backfill finished on this box" is otherwise only
      // answerable from the command history.
      const rx = h.reextract as Record<string, unknown> | undefined;
      if (rx && typeof rx.started === "string") {
        const enriched =
          typeof rx.extracted === "number" && typeof rx.seen === "number"
            ? ` (${rx.extracted.toLocaleString()} enriched of ${rx.seen.toLocaleString()} seen)`
            : "";
        const when =
          rx.complete && typeof rx.finished === "string"
            ? `completed ${new Date(rx.finished).toLocaleString()}`
            : `in progress since ${new Date(rx.started).toLocaleString()}`;
        lines.push(`re-extract: ${when}${enriched}`);
      }
      // QH-T6: the quick_hash migration. Same fixed-key-list caveat as the block
      // above — and this one has nowhere else to be seen at a glance, since
      // central cannot derive an agent's hash provenance from the catalogue.
      const rh = h.rehash as Record<string, unknown> | undefined;
      if (rh && typeof rh.started === "string") {
        // changed/verified stay apart: "0 corrected, 40,000 already correct" is
        // a converged agent, and summing them hides exactly that.
        const counts =
          typeof rh.changed === "number" && typeof rh.verified === "number"
            ? ` (${rh.changed.toLocaleString()} corrected, ${rh.verified.toLocaleString()} already correct)`
            : "";
        const when =
          rh.complete && typeof rh.finished === "string"
            ? `completed ${new Date(rh.finished).toLocaleString()}`
            : `in progress since ${new Date(rh.started).toLocaleString()}`;
        lines.push(`hash migration: ${when}${counts}`);
      }
      // 2026-08-10 local scan controls. Same reason as the re-extract block
      // above: this tooltip is a FIXED key list, so an agent-reported key that
      // is not named here is collected, stored, and then never seen by anyone.
      // Both of these change what the agent is DOING, so both have to surface:
      // an agent paused by its local operator looks identical to a healthy idle
      // one from central, and a locally-edited schedule means this agent's
      // group policy is not the whole story for it.
      if (h.local_scan_paused === true) lines.push("scanning paused locally (on the agent)");
      const lo = h.local_overrides as Record<string, unknown> | undefined;
      if (lo) {
        const bits: string[] = [];
        if (typeof lo.scan_cron === "string") bits.push(`cron ${lo.scan_cron}`);
        if (typeof lo.scan_interval_seconds === "number")
          bits.push(`every ${lo.scan_interval_seconds}s`);
        if (typeof lo.scan_on_start === "boolean")
          bits.push(`on start ${lo.scan_on_start ? "yes" : "no"}`);
        if (typeof lo.roots_edited_at === "string")
          bits.push(`roots edited ${new Date(lo.roots_edited_at).toLocaleString()}`);
        if (bits.length) lines.push(`local overrides: ${bits.join(", ")}`);
      }
      // 2026-08-10 share mappings. Same fixed-key-list caveat as above. Central
      // renders network-open links from the hints these mappings produce, so a
      // fleet where nothing is mapped, or where an entry was skipped as
      // malformed, is invisible here without this line — the agent only ever
      // logs the skip.
      const sm = h.share_map as Record<string, unknown> | undefined;
      if (sm && typeof sm.roots === "number") {
        const mapped = typeof sm.mapped === "number" ? sm.mapped : 0;
        const rejected = typeof sm.rejected === "number" ? sm.rejected : 0;
        let line = `share map: ${mapped}/${sm.roots} scan root${sm.roots === 1 ? "" : "s"} resolve to a network location`;
        if (rejected > 0)
          line += `, ${rejected} malformed entr${rejected === 1 ? "y" : "ies"} skipped (fix them on the agent)`;
        lines.push(line);
      }
      if (a.health_at) lines.push(`health as of ${new Date(a.health_at).toLocaleString()}`);
    }
    return lines.join("\n");
  }

  // The health snapshot is an opaque blob, so narrow the share-map counts once
  // here rather than at each use — the badge and the tooltip must agree on what
  // counts as an error.
  function shareMapRejects(a: AgentOut): number {
    const sm = a.health?.share_map as Record<string, unknown> | undefined;
    return sm && typeof sm.rejected === "number" ? sm.rejected : 0;
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

  // --- configuration groups: the ONE grouping --------------------------------
  // `groups` arrives in MERGE order (the server sorts by priority then name), so
  // the table and every checkbox list below can render it as-is: top to bottom
  // reads as "what overrides what". Global is always first.
  const groupsById = $derived(new Map(groups.map((g) => [g.id, g])));

  /** The EXPLICIT groups an agent is in, in merge order. Global is implicit and
   *  deliberately not listed here — it is on every row, so printing it on every
   *  row would be noise; the effective-config viewer names it where it matters. */
  function groupsOf(a: AgentOut): ConfigGroupOut[] {
    return groups.filter((g) => !g.is_system && a.config_group_ids.includes(g.id));
  }

  async function reloadGroups() {
    try {
      groups = await listConfigGroups();
    } catch {
      /* keep last-known */
    }
  }

  async function reloadRollouts() {
    try {
      // No status filter = live only (scheduled + running), which is exactly
      // what the card renders. Finished rollouts are history, not status.
      rollouts = await listConfigRollouts();
    } catch {
      /* keep last-known — an advisory card must not fail the page */
    }
  }

  // --- per-agent membership editing (agent detail row) -----------------------
  // A full REPLACE endpoint, so the editor holds a draft set and PUTs the whole
  // thing. Editing in place against `a.config_group_ids` would make each
  // checkbox its own round trip and let a slow response undo a later tick.
  let memberDraft = $state<Record<string, string[]>>({});
  let savingMembers = $state<Record<string, boolean>>({});

  const memberIds = (a: AgentOut): string[] => memberDraft[a.id] ?? a.config_group_ids;

  function toggleMembership(a: AgentOut, groupId: string, on: boolean) {
    const current = memberIds(a);
    memberDraft[a.id] = on
      ? [...current, groupId]
      : current.filter((id) => id !== groupId);
  }

  const membersDirty = (a: AgentOut): boolean => {
    const draft = memberDraft[a.id];
    if (!draft) return false;
    const before = [...a.config_group_ids].sort().join("|");
    return [...draft].sort().join("|") !== before;
  };

  async function saveMembership(a: AgentOut) {
    const ids = memberIds(a);
    // Consequential and fleet-visible (it changes the document the machine
    // receives), so it confirms like the other agent-scoped actions.
    const names = ids.map((id) => groupsById.get(id)?.name ?? id);
    if (
      !confirm(
        `Set agent "${a.name}" configuration groups to: ${
          names.length ? names.join(", ") : "(Global only)"
        }?\n\n` +
          "Groups layer in priority order and the highest-priority group wins " +
          "each key it sets. The new document lands on the agent's next poll " +
          "(~1 min).",
      )
    )
      return;
    savingMembers[a.id] = true;
    error = "";
    try {
      const res = await setAgentConfigGroups(a.id, ids);
      a.config_group_ids = res.group_ids;
      agents = agents; // trigger reactivity
      delete memberDraft[a.id];
      // The membership change rewrites the effective document, so the cached
      // viewer payload is stale the instant this returns.
      delete effective[a.id];
      await Promise.all([reloadGroups(), refreshSummary()]);
    } catch (e) {
      error = `configuration groups for ${a.name}: ${errDetail(e)}`;
      delete memberDraft[a.id];
      await refresh(); // resync the checkboxes to server truth
    } finally {
      savingMembers[a.id] = false;
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

  // Suspend/resume + local maintenance (2026-08-09): queue agent-scoped
  // commands, applied at the next check-in; `health.suspended` is the applied
  // truth the badge renders (so a just-clicked suspend shows after ~1 min).
  let suspending: Record<string, boolean> = $state({});
  async function toggleSuspend(a: AgentOut) {
    const want = !a.health?.suspended;
    if (
      want &&
      !confirm(
        `Suspend agent "${a.name}"? It stops scanning and replicating until resumed (it keeps checking in for commands, so you can resume it from here).`,
      )
    )
      return;
    suspending[a.id] = true;
    try {
      await suspendAgent(a.id, want);
      await refresh();
    } catch (e) {
      error = `suspend ${a.name}: ${errDetail(e)}`;
    } finally {
      suspending[a.id] = false;
    }
  }

  let maintaining: Record<string, boolean> = $state({});
  async function maintainAgent(a: AgentOut) {
    maintaining[a.id] = true;
    try {
      await runAgentMaintenance(a.id);
      await refresh();
    } catch (e) {
      error = `maintenance ${a.name}: ${errDetail(e)}`;
    } finally {
      maintaining[a.id] = false;
    }
  }

  // Re-extract (extraction parity phase 3, 2026-08-10): the agent sweeps its
  // EXISTING local index and re-runs extraction over items a scan will never
  // touch again — everything catalogued before `extract_enabled` was on, or
  // before that host gained ffprobe/exiftool/poppler/tesseract. Central holds
  // no cursor: it enqueues the command and the agent resumes its own sweep.
  let reextracting: Record<string, boolean> = $state({});
  // Agent ids with a queued/in-flight `reextract`. The equivalent flag for
  // self_update (`update_pending`) is computed on the agents list; there is no
  // per-kind flag for this one, so the page asks the command endpoint directly —
  // TWO requests for the whole table (pending + picked_up), not one per row.
  let sweeping = $state<Set<string>>(new Set());
  // Agent ids with a queued/in-flight `rehash_sweep` (QH-T6). A SEPARATE set
  // from `sweeping`, not a merged one: the two sweeps have independent cursors
  // and independent 409 guards, so an agent can legitimately be running both,
  // and each button must disable only on its own kind.
  let rehashing = $state<Set<string>>(new Set());
  async function refreshSweeps() {
    try {
      const [queued, running, rhQueued, rhRunning] = await Promise.all([
        listAgentCommands(undefined, 200, { kind: "reextract", state: "pending" }),
        listAgentCommands(undefined, 200, { kind: "reextract", state: "picked_up" }),
        listAgentCommands(undefined, 200, { kind: "rehash_sweep", state: "pending" }),
        listAgentCommands(undefined, 200, { kind: "rehash_sweep", state: "picked_up" }),
      ]);
      sweeping = new Set([...queued, ...running].map((c) => c.agent_id));
      rehashing = new Set([...rhQueued, ...rhRunning].map((c) => c.agent_id));
    } catch {
      /* transient — keep the last-known sweep sets (the badges are advisory) */
    }
  }

  async function reextract(a: AgentOut) {
    // Fleet-visible and potentially hours long on a large index, so it confirms
    // first — same idiom as suspend/revoke (a plain confirm() naming the agent
    // and stating the cost).
    if (
      !confirm(
        `Re-extract metadata on agent "${a.name}"? It sweeps every item in that agent's index and can run for hours on a large library. The sweep is resumable and re-emits metadata only — file contents never leave the agent.`,
      )
    )
      return;
    reextracting[a.id] = true;
    try {
      await reextractAgent(a.id);
      await refresh();
    } catch (e) {
      // 409 = the single-sweep guard (two sweeps would fight over one cursor).
      // Say that plainly instead of surfacing the raw endpoint detail.
      error =
        e instanceof ApiError && e.status === 409
          ? `re-extract ${a.name}: a sweep is already running on this agent`
          : `re-extract ${a.name}: ${errDetail(e)}`;
      await refreshSweeps(); // resync the badge — a 409 means one IS in flight
    } finally {
      reextracting[a.id] = false;
    }
  }

  // Re-hash (quick_hash migration, QH-T6, 2026-08-12): the agent re-reads every
  // file in its index between 64 KiB and 128 KiB and corrects the hashes the
  // pre-2026-07-18 hasher got wrong (it read a fixed 64 KiB head and skipped the
  // rest of the band, producing false duplicates). Nothing else can fix those
  // rows: the agent's scan only re-hashes files whose size or mtime moved, and
  // central neither holds the files nor any hash provenance for agent rows.
  let rehashingNow: Record<string, boolean> = $state({});
  // Advanced knobs, per agent so two open rows cannot clobber each other's
  // draft. Empty string = "use the default", which is the case that must stay
  // one click away — the defect band is the right answer for ~every operator.
  let rehashBand: Record<string, { min: string; max: string; items: string }> = $state({});
  const bandOf = (id: string) => rehashBand[id] ?? { min: "", max: "", items: "" };

  async function rehashSweep(a: AgentOut) {
    const b = bandOf(a.id);
    const min = b.min.trim() === "" ? 65537 : Number(b.min);
    const max = b.max.trim() === "" ? 131072 : Number(b.max);
    const items = b.items.trim() === "" ? undefined : Number(b.items);
    if (!Number.isFinite(min) || !Number.isFinite(max) || min < 1 || min > max) {
      error = `re-hash ${a.name}: the size band must satisfy 0 < min ≤ max`;
      return;
    }
    // The confirm has to state the REAL cost, because this action's cost is
    // invisible from the console and lands entirely on someone else's machine:
    // it is a full read of every file in the band, over whatever network mount
    // that agent uses, for hours.
    const inBand = a.health?.index_items
      ? `That index holds about ${a.health.index_items.toLocaleString()} items in total; typically a few percent fall in this band. `
      : "";
    if (
      !confirm(
        `Re-hash files on agent "${a.name}"?\n\n` +
          `It re-reads EVERY indexed file between ${min.toLocaleString()} and ${max.toLocaleString()} bytes — ` +
          `the whole file, not a sample — to recompute its hashes. ${inBand}` +
          `On the live fleet this was ~99,000 files and ran for hours; over a network mount, longer.\n\n` +
          `It is safe to stop (suspend the agent) and resume: the cursor is durable. ` +
          `Only files whose stored hash is actually wrong are re-sent, and file contents never leave the agent.`,
      )
    )
      return;
    rehashingNow[a.id] = true;
    try {
      await rehashSweepAgent(a.id, {
        min_size: min,
        max_size: max,
        ...(items !== undefined ? { max_items: items } : {}),
      });
      await refresh();
    } catch (e) {
      // 409 = the single-sweep guard (two sweeps would fight over one cursor).
      error =
        e instanceof ApiError && e.status === 409
          ? `re-hash ${a.name}: a hash migration is already running on this agent`
          : `re-hash ${a.name}: ${errDetail(e)}`;
      await refreshSweeps(); // resync the badge — a 409 means one IS in flight
    } finally {
      rehashingNow[a.id] = false;
    }
  }

  // --- per-agent detail: capabilities vs. effective policy -------------------
  // "Which of my settings does THIS agent actually honour?" Extraction capability
  // is a property of the agent HOST (ffprobe/tesseract/exiftool on PATH), not of
  // the build, so a fleet-wide `extract_ocr: true` can be silently dead on some
  // machines. The effective document is fetched LAZILY on expand — one request per
  // agent an operator actually opens, never one per table row.
  const EXTRACTION_FIELDS = POLICY_FIELDS.filter((f) => f.section === "extraction");

  let expanded = $state<string | null>(null);
  let effective = $state<Record<string, EffectiveConfigOut>>({});
  let effLoading = $state<Record<string, boolean>>({});
  let effError = $state<Record<string, string>>({});
  /** The FULL effective-configuration report is a second, opt-in disclosure
   *  inside the already-expanded detail row: ~30 keys with source badges is a
   *  wall, and the common question ("what will this agent ignore") is answered
   *  by the extraction summary above it. */
  let effFull = $state<Record<string, boolean>>({});

  const capsOf = (a: AgentOut): AgentCapabilities | null =>
    (a.capabilities as AgentCapabilities | null) ?? null;

  // Host-tool MINIMUM versions (2026-08-11). The per-agent judgement already
  // rides each row as `tool_verdicts` — central computes it, because the same
  // comparator has to judge central's own tools on the About page and two
  // implementations would eventually disagree. What this fetch adds is the
  // PROSE behind a verdict: the number and the one-line consequence that make
  // an amber chip actionable instead of merely alarming.
  //
  // Fetched once on mount (static, fleet-wide, ~7 rows) and deliberately
  // swallowed on failure: losing it costs a tooltip its numbers, and failing
  // the whole Agents page over a tooltip would be a far worse trade.
  let toolMinimums = $state<Record<string, HostToolMinimum>>({});
  async function loadToolMinimums() {
    try {
      toolMinimums = minimumsByName(await listHostToolMinimums());
    } catch {
      /* see above — the verdict still colours the chip */
    }
  }

  const TOOL_CHIP_CLASS: Record<"ok" | "warn" | "muted", string> = {
    ok: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300",
    // Amber is reserved for `outdated`, and matches the "this agent will ignore"
    // chips below: both mean "working, but not doing what you think".
    warn: "bg-amber-100 text-amber-700 dark:bg-amber-900/40 dark:text-amber-300",
    muted: "bg-slate-100 text-slate-500 dark:bg-slate-800 dark:text-slate-400",
  };

  async function loadEffective(id: string) {
    if (effective[id] || effLoading[id]) return;
    effLoading[id] = true;
    effError[id] = "";
    try {
      effective[id] = await getEffectiveConfig(id);
    } catch (e) {
      effError[id] = errDetail(e);
    } finally {
      effLoading[id] = false;
    }
  }

  async function toggleDetail(a: AgentOut) {
    if (expanded === a.id) {
      expanded = null;
      return;
    }
    expanded = a.id;
    await loadEffective(a.id);
  }

  // ---- per-agent About / dependency report (2026-08-11) --------------------
  //
  // The console could always answer "which build is this SERVER running" (the
  // About page) and knew exactly one string about a remote agent's software:
  // its `agent_version`. This panel is the agent-side equivalent — build stack,
  // Go module dependencies, and host tools with version, resolved PATH and
  // central's verdict.
  //
  // A THIRD, opt-in disclosure inside the already-expanded row, alongside the
  // effective-configuration one and for the same reason: it is long, and the
  // question it answers ("what exactly is installed over there") is a
  // deliberate investigation, not something to scroll past while checking
  // membership. The capability CHIPS above stay exactly as they are — they are
  // the at-a-glance view scanned across a fleet, this is the detailed view read
  // about one machine.
  //
  // Fetched lazily on first open, one request per agent an operator actually
  // asks about, and cached for the page's lifetime: it is a snapshot of what
  // the agent last reported, so re-fetching on every toggle would cost a
  // request to show the same bytes.
  let aboutOpen = $state<Record<string, boolean>>({});
  let about = $state<Record<string, AgentAbout>>({});
  let aboutLoading = $state<Record<string, boolean>>({});
  let aboutError = $state<Record<string, string>>({});
  /** The module table is collapsed inside the panel that is itself collapsed:
   *  ~120 rows of Go modules is the longest thing on this page by far, and it
   *  is read once a year when a CVE lands in a transitive dependency. */
  let modulesOpen = $state<Record<string, boolean>>({});
  let aboutCopied = $state<string | null>(null);

  async function toggleAbout(a: AgentOut) {
    aboutOpen[a.id] = !aboutOpen[a.id];
    if (!aboutOpen[a.id] || about[a.id] || aboutLoading[a.id]) return;
    aboutLoading[a.id] = true;
    aboutError[a.id] = "";
    try {
      about[a.id] = await agentAbout(a.id);
    } catch (e) {
      aboutError[a.id] = errDetail(e);
    } finally {
      aboutLoading[a.id] = false;
    }
  }

  async function copyAbout(a: AgentOut) {
    const report = about[a.id];
    if (!report) return;
    await copyText(agentAboutMarkdown(report));
    aboutCopied = a.id;
    setTimeout(() => (aboutCopied = null), 2000);
  }

  /** Tone → classes for the About panel's cells. Same vocabulary the About page
   *  uses; `warn` stays reserved for a tool below its minimum so amber keeps
   *  meaning "act on this", and `bad` appears only for the path rule violation
   *  that must never happen. */
  const ABOUT_TONE_CLASS: Record<"ok" | "bad" | "warn" | "muted", string> = {
    ok: "text-slate-700 dark:text-slate-200",
    bad: "text-red-600 dark:text-red-400",
    warn: "text-amber-600 dark:text-amber-400",
    muted: "text-slate-400",
  };

  function fmtPolicyValue(v: unknown): string {
    if (v === undefined) return "not set";
    if (Array.isArray(v) || (v && typeof v === "object")) return JSON.stringify(v);
    return String(v);
  }

  /** The merged POLICY half of the delivered document: every top-level key except
   *  `group`, which is the settings section the composer folds in. Rendering the
   *  whole blob would show that nested object as one unreadable line. */
  function policyOf(eff: EffectiveConfigOut): Record<string, unknown> {
    const { group: _group, ...policy } = eff.document;
    return policy;
  }

  const settingsOf = (eff: EffectiveConfigOut): Record<string, unknown> =>
    (eff.document.group as Record<string, unknown> | undefined) ?? {};

  /** Keys in the merged policy that no POLICY_FIELDS entry renders — forward-compat
   *  keys from a newer agent build. Listed rather than hidden: they are part of
   *  what this agent receives, and silence would read as "not set". */
  function extraPolicyKeys(eff: EffectiveConfigOut): string[] {
    const known = new Set(POLICY_FIELDS.map((f) => f.key));
    return Object.keys(policyOf(eff))
      .filter((k) => !known.has(k))
      .sort();
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
  // The token carries group NAMES, not ids: it is minted before the agent
  // exists and is frequently pasted into an installer by hand. Every enrolling
  // agent joins Global regardless, so an empty selection is a valid, complete
  // answer rather than "no configuration".
  let newGroupNames = $state<string[]>([]);
  let newTtl = $state<number | undefined>(undefined);
  let minting = $state(false);
  let minted = $state<{ token: string; expires_at: string } | null>(null);
  let mintedCopied = $state(false);

  function toggleMintGroup(name: string, on: boolean) {
    newGroupNames = on
      ? [...newGroupNames, name]
      : newGroupNames.filter((n) => n !== name);
  }

  async function mint() {
    minting = true;
    mintedCopied = false;
    error = "";
    try {
      const r = await mintEnrollmentToken(newGroupNames, newTtl || undefined);
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
  // Ids here (unlike the token mint): the installer request resolves them
  // server-side and writes the resulting NAMES into the sidecar.
  let insGroupIds = $state<string[]>([]);
  let insLogLevel = $state("");

  function toggleInsGroup(id: string, on: boolean) {
    insGroupIds = on ? [...insGroupIds, id] : insGroupIds.filter((g) => g !== id);
  }
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
        config_group_ids: insGroupIds,
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
    /** Global: name + priority are immutable server-side (409), so the fields
     *  render disabled rather than letting an operator author a rejected save. */
    isSystem: boolean;
    currentVersion: number;
    memberCount: number;
    name: string;
    description: string;
    /** Merge rank. LOWER applies first, so a HIGHER number wins a contested key. */
    priority: number;
    /** Free-text note stored on the published snapshot — the only place a
     *  "why" survives into the version history. */
    note: string;
    /** The POLICY section of the group document: tri-state per key, plus the
     *  keys this console does not model, preserved verbatim. See
     *  ./agentPolicyDoc — dropping a forward-compat key is silent data loss. */
    policyForm: PolicyFormState;
    policyPassthrough: Record<string, unknown>;
    /** What was stored when the dialog opened — drives the unknown/unparsed
     *  key callouts and the raw-JSON escape hatch. */
    storedPolicy: AgentPolicyDoc;
    logLevel: string;
    cron: string;
    inventoryEnabled: boolean;
    collectorsText: string;
    selections: SelRow[];
    // tri-state local-surface gates: "" = inherit, "on" | "off" explicit
    webUI: string;
    localAccess: string;
    authRequired: string;
    // --- W7 permissions collector (advanced; omitted entirely unless
    // `permsConfigured`, so a group that never touches it keeps a minimal doc) --
    permsConfigured: boolean;
    permsEnabled: boolean;
    permsResolveNames: boolean;
    permsIncludeInherited: boolean;
    permsIncludeEffective: boolean;
    permsExcludeWellKnown: boolean;
    permsCollectShareAcls: boolean;
    permsExcludePrincipalsText: string;
    auditConfigured: boolean;
    auditEnabled: boolean;
    auditRetain: number;
    auditAlertOnChange: boolean;
    auditWatchPathsText: string;
  };
  /** Advanced blocks stay collapsed until asked for. */
  let advancedOpen = $state(false);
  let dialog = $state<GroupForm | null>(null);
  let dialogError = $state("");
  let dialogBusy = $state(false);

  // --- dialog section accordion ----------------------------------------------
  // The dialog now holds ~40 fields (settings + every policy key), so everything
  // except General is COLLAPSED on open. An accordion beats a sidebar here
  // because the sections are of wildly different heights and an operator
  // usually came to change exactly one thing.
  const SETTINGS_SECTIONS: { id: string; label: string; blurb: string }[] = [
    {
      id: "delivery",
      label: "Log level & group scan schedule",
      blurb: "Agent-wide settings delivered under the document's `group` section.",
    },
    {
      id: "surface",
      label: "Local access (settings)",
      blurb:
        "The per-group form of the three local-surface gates. A value set here " +
        "is LIFTED over the policy key of the same name in the delivered document.",
    },
    { id: "inventory", label: "Inventory", blurb: "Host inventory collection." },
    {
      id: "selections",
      label: "Scan selections",
      blurb: "Per-OS presets and path specs the agent expands into scan roots.",
    },
  ];

  let openSections = $state<Record<string, boolean>>({ general: true });
  const toggleSection = (id: string) => (openSections[id] = !openSections[id]);

  /** Known preset names for the policy form's preset check. Static and small;
   *  a failure degrades to server-side validation only. */
  let presetNames = $state<string[]>([]);
  async function loadPresets() {
    try {
      presetNames = (await listPresets()).presets.map((p) => p.name);
    } catch {
      presetNames = [];
    }
  }

  const policyErrors = $derived(
    dialog ? validatePolicyForm(dialog.policyForm, { knownPresets: presetNames }) : {},
  );
  const policyDraft = $derived(
    dialog ? buildPolicyDoc(dialog.policyForm, dialog.policyPassthrough) : {},
  );
  const unknownKeys = $derived(dialog ? unknownPolicyKeys(dialog.storedPolicy) : []);
  const unparsedKeys = $derived(
    dialog ? unparsedPolicyKeys(dialog.storedPolicy, dialog.policyForm) : [],
  );

  /** Number of policy keys this group will contribute to the merge. `0` is a
   *  perfectly good answer (a settings-only group) and reads better than a
   *  blank. */
  const policyKeyCount = $derived(Object.keys(policyDraft).length);

  /** Client-side merge PREVIEW: keys this draft sets that a HIGHER-priority
   *  group also sets, and therefore loses.
   *
   *  This is the "why is my value not applying" answer, and it is the one
   *  question the server cannot pre-answer — the document has not been saved
   *  yet. It runs the same layering the backend does (./configGroups, which is
   *  unit-tested against exactly that contract) over the draft plus every other
   *  group.
   *
   *  Deliberately phrased as a conditional: it compares against ALL groups, not
   *  the ones any particular agent is in, so the honest claim is "for an agent
   *  in both, that group wins" — never "this value is dead". */
  const shadowedKeys = $derived.by<{ key: string; by: string }[]>(() => {
    // Bound to a local so the null-narrowing survives into the callbacks below.
    const d = dialog;
    if (!d) return [];
    const draftId = d.id ?? "__draft__";
    const layers: ConfigLayer[] = [
      ...groups
        .filter((g) => g.id !== d.id)
        .map((g) => ({
          id: g.id,
          name: g.name,
          priority: g.priority,
          policy: g.policy,
        })),
      {
        id: draftId,
        name: d.name || "(this group)",
        priority: d.priority,
        policy: policyDraft,
      },
    ];
    const merged = mergeDocuments(layers);
    return Object.keys(policyDraft)
      .filter((k) => merged.provenance[`policy.${k}`]?.group_id !== draftId)
      .sort()
      .map((key) => ({
        key,
        by: merged.provenance[`policy.${key}`]?.group_name ?? "another group",
      }));
  });

  // --- policy field editing (tri-state) --------------------------------------
  // "Inherit (not set)" no longer means "fall back to a broader SCOPE": the key
  // is simply absent from this group's contribution, so a lower-priority group
  // — or the agent's built-in default — supplies it.
  function setBoolMode(key: string, mode: string) {
    if (!dialog) return;
    if (mode === "") dialog.policyForm[key] = { set: false, value: dialog.policyForm[key]?.value ?? "" };
    else dialog.policyForm[key] = { set: true, value: mode };
  }
  function setExplicit(key: string, on: boolean) {
    if (!dialog) return;
    dialog.policyForm[key] = { set: on, value: dialog.policyForm[key]?.value ?? "" };
  }
  function setPolicyValue(key: string, value: string) {
    if (!dialog) return;
    dialog.policyForm[key] = { set: true, value };
  }
  const boolMode = (key: string): string => {
    const s = dialog?.policyForm[key];
    return s?.set ? s.value : "";
  };
  const sectionFields = (section: string): PolicyFieldSpec[] =>
    POLICY_FIELDS.filter((f) => f.section === section);

  /** Hover text for a policy field's label and control: the visible hint plus
   *  the key and the absent-key behaviour — the two things needed before
   *  deciding whether this group should set it at all. */
  const fieldTitle = (f: PolicyFieldSpec): string =>
    `${f.label} (${f.key}) — ${f.hint} Not set here → a lower-priority group supplies it, or ${f.fallback}.`;

  // --- raw JSON escape hatch (extra="allow" keys) ----------------------------
  let rawOpen = $state(false);
  let rawText = $state("");
  let rawError = $state("");

  function openRaw() {
    rawOpen = !rawOpen;
    if (rawOpen) {
      rawText = JSON.stringify(policyDraft, null, 2);
      rawError = "";
    }
  }
  function applyRaw() {
    if (!dialog) return;
    rawError = "";
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawText);
    } catch (e) {
      rawError = `Not valid JSON: ${String(e)}`;
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      rawError = "The policy section must be a JSON object.";
      return;
    }
    const doc = parsed as AgentPolicyDoc;
    dialog.policyForm = formFromDoc(doc);
    dialog.policyPassthrough = passthroughFromDoc(doc, dialog.policyForm);
    rawOpen = false;
  }

  // --- phased rollout (dialog footer) ----------------------------------------
  // Publishing with tiers leaves `current_version` alone: uncovered agents keep
  // the old document and each tier widens the hash-bucket share that receives
  // the new one. Cancelling mid-flight therefore ROLLS BACK the covered agents.
  let rolloutOpen = $state(false);
  let tiers = $state<RolloutTier[]>([]);
  let rolloutStartsAt = $state("");
  const tierError = $derived(rolloutOpen ? validateTiers(tiers) : null);

  function openRollout() {
    rolloutOpen = true;
    if (!tiers.length)
      // A sane default an operator can accept as-is: a small first wave, a half
      // fleet an hour later, everyone an hour after that.
      tiers = [
        { percent: 10, delay_minutes: 0 },
        { percent: 50, delay_minutes: 60 },
        { percent: 100, delay_minutes: 60 },
      ];
  }
  function addTier() {
    if (tiers.length >= MAX_ROLLOUT_TIERS) return;
    const last = tiers[tiers.length - 1];
    tiers = [
      ...tiers,
      { percent: Math.min(100, (last?.percent ?? 0) + 25), delay_minutes: 60 },
    ];
  }

  // --- version history (in-dialog view) --------------------------------------
  let historyView = $state(false);
  let historyRows = $state<ConfigVersionOut[]>([]);
  let historyLoading = $state(false);
  let historyError = $state("");
  let historyOpenSeq = $state<number | null>(null);

  async function openHistory() {
    if (!dialog?.id) return;
    historyView = true;
    historyOpenSeq = null;
    historyLoading = true;
    historyError = "";
    try {
      historyRows = await listConfigGroupHistory(dialog.id, 20);
    } catch (e) {
      historyError = errDetail(e);
    } finally {
      historyLoading = false;
    }
  }

  async function restoreVersion(v: ConfigVersionOut) {
    if (!dialog?.id) return;
    if (
      !confirm(
        `Restore version ${v.version} of "${dialog.name}"?\n\n` +
          "It is copied forward as a NEW version and published IMMEDIATELY — " +
          "reverting a bad configuration should not wait behind a rollout, so " +
          "any live rollout for this group is cancelled and the agents it had " +
          "already covered fall back with everyone else. Unsaved edits in this " +
          "dialog are discarded.",
      )
    )
      return;
    dialogBusy = true;
    dialogError = "";
    try {
      const g = await rollbackConfigGroup(dialog.id, v.version);
      historyView = false;
      openEdit(g); // re-seed the form from what is now stored
      await refresh();
    } catch (e) {
      dialogError = errDetail(e);
    } finally {
      dialogBusy = false;
    }
  }

  // --- inventory-collector picker (see ./inventoryCollectors) ----------------
  // The vocabulary used to be an operator's guess: a comma-separated box whose
  // legal values only existed in the agent's Go source. It is now a checkbox
  // list built from GET /agents/inventory-collectors — admin-only and a query
  // over the agents table, so it is fetched when the dialog OPENS, never on page
  // load. `dialog.collectorsText` stays the live value while the request is in
  // flight and permanently if it fails, so a failure degrades to the old
  // free-text editing instead of an empty list that would read as "none exist".
  let collectorEditor = $state<CollectorEditor>({ mode: "loading" });
  let collectorAdd = $state("");
  let collectorAddError = $state("");

  // Guards against a slow response for a dialog the operator already closed
  // landing on top of the next one's (which would merge the WRONG group's
  // stored names, i.e. write group A's collectors into group B).
  let collectorReq = 0;

  async function loadCollectorCatalogue(stored: string[]) {
    const req = ++collectorReq;
    collectorEditor = { mode: "loading" };
    collectorAdd = "";
    collectorAddError = "";
    try {
      const catalogue = await listInventoryCollectors();
      if (req !== collectorReq) return;
      collectorEditor = collectorEditorFromFetch(stored, { ok: true, catalogue });
    } catch (e) {
      if (req !== collectorReq) return;
      collectorEditor = collectorEditorFromFetch(stored, {
        ok: false,
        error: errDetail(e),
      });
    }
  }

  function setCollector(name: string, checked: boolean) {
    if (collectorEditor.mode !== "list") return;
    collectorEditor = {
      mode: "list",
      choices: toggleCollector(collectorEditor.choices, name, checked),
    };
  }

  function addCollector() {
    if (collectorEditor.mode !== "list") return;
    const res = addCollectorName(collectorEditor.choices, collectorAdd);
    collectorAddError = res.error;
    if (res.error) return;
    collectorEditor = { mode: "list", choices: res.choices };
    collectorAdd = "";
  }

  function emptySel(): SelRow {
    return { preset: "", pathsText: "", includeText: "", excludeText: "", enabled: true };
  }

  /** Field defaults mirror filearr.agent_config.PermissionsConfig / AuditConfig
   *  so an operator who ticks "configure" starts from the same posture the
   *  backend would apply. */
  const PERMS_DEFAULTS = {
    permsConfigured: false,
    permsEnabled: false,
    permsResolveNames: true,
    permsIncludeInherited: false,
    permsIncludeEffective: false,
    permsExcludeWellKnown: true,
    permsCollectShareAcls: false,
    permsExcludePrincipalsText: "",
    auditConfigured: false,
    auditEnabled: false,
    auditRetain: 10,
    auditAlertOnChange: false,
    auditWatchPathsText: "",
  };

  /** Reset every piece of dialog-adjacent state, so reopening never inherits
   *  the previous group's accordion, tier draft, history page or raw JSON. */
  function resetDialogChrome() {
    dialogError = "";
    openSections = { general: true };
    rolloutOpen = false;
    tiers = [];
    rolloutStartsAt = "";
    historyView = false;
    historyRows = [];
    historyOpenSeq = null;
    historyError = "";
    rawOpen = false;
    rawText = "";
    rawError = "";
  }

  function openCreate() {
    resetDialogChrome();
    advancedOpen = false;
    dialog = {
      id: null,
      isSystem: false,
      currentVersion: 0,
      memberCount: 0,
      name: "",
      description: "",
      // 100 is the server default and the band every hand-made group lands in;
      // Global sits alone at 0 so it always applies first.
      priority: 100,
      note: "",
      policyForm: blankPolicyForm(),
      policyPassthrough: {},
      storedPolicy: {},
      logLevel: "",
      cron: "",
      inventoryEnabled: false,
      collectorsText: "",
      selections: [],
      webUI: "",
      localAccess: "",
      authRequired: "",
      ...PERMS_DEFAULTS,
    };
    void loadCollectorCatalogue([]);
  }

  function openEdit(g: ConfigGroupOut) {
    resetDialogChrome();
    const s = g.settings ?? {};
    const p = s.inventory?.permissions ?? null;
    const a = p?.audit ?? null;
    advancedOpen = p !== null;
    const policy = g.policy ?? {};
    const policyForm = formFromDoc(policy);
    dialog = {
      id: g.id,
      isSystem: g.is_system,
      currentVersion: g.current_version,
      memberCount: g.member_count,
      name: g.name,
      description: g.description ?? "",
      priority: g.priority,
      note: "",
      policyForm,
      policyPassthrough: passthroughFromDoc(policy, policyForm),
      storedPolicy: policy,
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
      permsConfigured: p !== null,
      permsEnabled: p?.enabled ?? PERMS_DEFAULTS.permsEnabled,
      permsResolveNames: p?.resolve_names ?? PERMS_DEFAULTS.permsResolveNames,
      permsIncludeInherited: p?.include_inherited ?? PERMS_DEFAULTS.permsIncludeInherited,
      permsIncludeEffective:
        p?.include_effective_access ?? PERMS_DEFAULTS.permsIncludeEffective,
      permsExcludeWellKnown: p?.exclude_well_known ?? PERMS_DEFAULTS.permsExcludeWellKnown,
      permsCollectShareAcls: p?.collect_share_acls ?? PERMS_DEFAULTS.permsCollectShareAcls,
      permsExcludePrincipalsText: (p?.exclude_principals ?? []).join(", "),
      auditConfigured: a !== null,
      auditEnabled: a?.enabled ?? PERMS_DEFAULTS.auditEnabled,
      auditRetain: a?.retain_snapshots ?? PERMS_DEFAULTS.auditRetain,
      auditAlertOnChange: a?.alert_on_change ?? PERMS_DEFAULTS.auditAlertOnChange,
      auditWatchPathsText: (a?.watch_paths ?? []).join("\n"),
    };
    // The STORED names drive the merge: any of them the catalogue does not know
    // comes back as a ticked "unrecognised" row, so an edit can never drop it.
    void loadCollectorCatalogue(s.inventory?.collectors ?? []);
  }

  const toTri = (v: boolean | null | undefined): string => (v === true ? "on" : v === false ? "off" : "");

  const splitLines = (t: string): string[] =>
    t.split("\n").map((x) => x.trim()).filter(Boolean);
  const splitTags = (t: string): string[] =>
    t.split(/[,\n]/).map((x) => x.trim()).filter(Boolean);

  // Build the typed settings object, omitting empty keys so the doc stays minimal
  // (the backend rejects unknown keys — we only ever send the four known ones).
  //
  // `collectors` is computed by the caller from the checkbox editor (or, when
  // the catalogue never loaded, from the free-text field) — see
  // ./inventoryCollectors.collectorsToSave. Passed in rather than derived here
  // so the payload rule stays in the DOM-free, unit-tested module.
  function buildSettings(f: GroupForm, collectors: string[]): GroupSettings {
    const settings: GroupSettings = {};
    if (f.logLevel) settings.log_level = f.logLevel as GroupSettings["log_level"];
    if (f.webUI) settings.web_ui_enabled = f.webUI === "on";
    if (f.localAccess) settings.local_access_enabled = f.localAccess === "on";
    if (f.authRequired) settings.auth_required = f.authRequired === "on";
    if (f.cron.trim()) settings.scan_schedule_cron = f.cron.trim();
    if (f.inventoryEnabled || collectors.length || f.permsConfigured) {
      const inventory: InventoryConfig = {
        enabled: f.inventoryEnabled,
        // Unticking every box emits `[]`, NOT a dropped key: "inventory on,
        // no collectors" is a real (and different) document from "inventory
        // never configured".
        collectors,
      };
      // `settings` is extra="forbid" but every optional field accepts null;
      // we still OMIT unconfigured blocks so a group's doc stays minimal (and
      // so "never configured" reads differently from "configured, all off").
      if (f.permsConfigured) {
        const permissions: PermissionsConfig = {
          enabled: f.permsEnabled,
          resolve_names: f.permsResolveNames,
          include_inherited: f.permsIncludeInherited,
          include_effective_access: f.permsIncludeEffective,
          exclude_well_known: f.permsExcludeWellKnown,
          collect_share_acls: f.permsCollectShareAcls,
        };
        const principals = splitTags(f.permsExcludePrincipalsText);
        if (principals.length) permissions.exclude_principals = principals;
        if (f.auditConfigured) {
          const audit: AuditConfig = {
            enabled: f.auditEnabled,
            retain_snapshots: f.auditRetain,
            alert_on_change: f.auditAlertOnChange,
          };
          const watch = splitLines(f.auditWatchPathsText);
          if (watch.length) audit.watch_paths = watch;
          permissions.audit = audit;
        }
        inventory.permissions = permissions;
      }
      settings.inventory = inventory;
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

  /** Publish the dialog. `mode` picks the publication strategy:
   *   - "now": the new version becomes `current_version` and every member gets
   *     it on its next poll;
   *   - "rollout": `current_version` stays put and the new version reaches the
   *     fleet through the tier schedule (server 422s if nothing actually
   *     changed — a rollout of an identical document would never finish
   *     meaning anything). */
  async function saveGroup(mode: "now" | "rollout" = "now") {
    if (!dialog) return;
    if (!dialog.name.trim()) {
      dialogError = "Name is required.";
      return;
    }
    if (Object.keys(policyErrors).length) {
      dialogError = "Fix the highlighted policy fields first.";
      return;
    }
    if (mode === "rollout") {
      const err = tierError ?? validateTiers(tiers);
      if (err) {
        dialogError = err;
        return;
      }
    }
    // Mirror the server bound so a typo doesn't cost a round trip.
    if (
      dialog.permsConfigured &&
      dialog.auditConfigured &&
      (!Number.isInteger(dialog.auditRetain) ||
        dialog.auditRetain < 1 ||
        dialog.auditRetain > MAX_RETAIN_SNAPSHOTS)
    ) {
      dialogError = `Audit "retain snapshots" must be a whole number from 1 to ${MAX_RETAIN_SNAPSHOTS}.`;
      return;
    }
    const collectors = collectorsToSave(collectorEditor, dialog.collectorsText);
    if (collectors.length > MAX_COLLECTORS) {
      dialogError = `At most ${MAX_COLLECTORS} inventory collectors can be selected (${collectors.length} selected).`;
      return;
    }
    dialogBusy = true;
    dialogError = "";
    try {
      const settings = buildSettings(dialog, collectors);
      const policy = buildPolicyDoc(dialog.policyForm, dialog.policyPassthrough);
      if (dialog.id === null) {
        const body: ConfigGroupIn = {
          name: dialog.name.trim(),
          description: dialog.description.trim() || null,
          priority: dialog.priority,
          settings,
          policy,
        };
        await createConfigGroup(body);
      } else {
        const patch: ConfigGroupUpdateIn = {
          description: dialog.description.trim() || null,
          settings,
          policy,
        };
        // Global's name and priority are fixed (409 if sent at all), so they
        // are omitted rather than sent unchanged — an unchanged value is still
        // "a change to name/priority" as far as the endpoint is concerned.
        if (!dialog.isSystem) {
          patch.name = dialog.name.trim();
          patch.priority = dialog.priority;
        }
        if (dialog.note.trim()) patch.note = dialog.note.trim();
        if (mode === "rollout")
          patch.rollout = {
            tiers,
            starts_at: rolloutStartsAt
              ? new Date(rolloutStartsAt).toISOString()
              : null,
          };
        await updateConfigGroup(dialog.id, patch);
      }
      dialog = null;
      await refresh();
    } catch (e) {
      // Surface the backend's detail inline: 422 (unknown key / bad regex / bad
      // cron / bad preset / bad tiers / a rollout with no document change), 409
      // (duplicate name, Global's fixed fields, a second live rollout) or 413.
      dialogError = errDetail(e);
    } finally {
      dialogBusy = false;
    }
  }

  async function removeGroup(g: ConfigGroupOut) {
    const n = g.member_count;
    if (!confirm(
      `Delete configuration group "${g.name}"?` +
        (n > 0
          ? ` ${n} member agent(s) stop receiving its keys — each of those keys ` +
            "falls back to the next-highest group that sets it, or to the " +
            "agent's built-in default."
          : "")
    )) return;
    try {
      await deleteConfigGroup(g.id);
      await refresh();
    } catch (e) {
      error = errDetail(e);
    }
  }

  // --- priority reordering (groups table) ------------------------------------
  // Two numeric PATCHes rather than a drag handle: no DnD dependency, and the
  // resulting priorities stay legible numbers an operator can also type into the
  // dialog. Deliberately NOT confirm()-guarded — it is a one-click-reversible
  // move whose result is visible in the row order immediately, and a prompt per
  // arrow press would make reordering unusable.
  let reordering = $state(false);
  /** Global is pinned at priority 0 and is never part of the movable run. */
  const reorderable = $derived(groups.filter((g) => !g.is_system));

  async function reorderGroup(g: ConfigGroupOut, dir: -1 | 1) {
    // Global is pinned at priority 0 and always applies first, so it is never
    // part of the reorderable run.
    const list = groups.filter((x) => !x.is_system);
    const i = list.findIndex((x) => x.id === g.id);
    const j = i + dir;
    if (i < 0 || j < 0 || j >= list.length) return;
    const other = list[j];

    let mine = other.priority;
    let theirs = g.priority;
    if (g.priority === other.priority) {
      // Equal priorities are legal (the server tie-breaks by name), so swapping
      // the numbers would be a no-op. Step one of them past the other instead,
      // keeping clear of Global's 0.
      if (dir < 0 && other.priority > 1) {
        mine = other.priority - 1;
        theirs = other.priority;
      } else if (dir < 0) {
        mine = other.priority;
        theirs = other.priority + 1; // push the other one later instead
      } else {
        mine = other.priority + 1;
        theirs = other.priority;
      }
    }

    reordering = true;
    error = "";
    try {
      if (mine !== g.priority) await updateConfigGroup(g.id, { priority: mine });
      if (theirs !== other.priority)
        await updateConfigGroup(other.id, { priority: theirs });
      await reloadGroups();
    } catch (e) {
      error = `reorder ${g.name}: ${errDetail(e)}`;
      await reloadGroups(); // a half-applied swap must not linger on screen
    } finally {
      reordering = false;
    }
  }

  // --- live rollouts card ----------------------------------------------------
  let rolloutBusy = $state<Record<string, boolean>>({});

  async function promoteRollout(r: RolloutOut) {
    if (
      !confirm(
        `Promote "${r.group_name}" to the next tier now?\n\n` +
          `Version ${r.target_version} immediately reaches the next tier's share ` +
          "of the fleet instead of waiting out the configured delay. This cannot " +
          "be un-promoted — a tier only ever widens.",
      )
    )
      return;
    rolloutBusy[r.id] = true;
    error = "";
    try {
      await promoteConfigRollout(r.id);
      await Promise.all([reloadRollouts(), reloadGroups()]);
    } catch (e) {
      error = `promote ${r.group_name}: ${errDetail(e)}`;
      await reloadRollouts();
    } finally {
      rolloutBusy[r.id] = false;
    }
  }

  async function cancelRollout(r: RolloutOut) {
    if (
      !confirm(
        `Cancel the rollout of version ${r.target_version} for "${r.group_name}"?\n\n` +
          `The group stays on its current version, so the ${r.covered_percent}% of ` +
          "agents already covered ROLL BACK to it on their next poll. The version " +
          "itself is kept — start a new rollout, or publish it immediately, to " +
          "deliver it later.",
      )
    )
      return;
    rolloutBusy[r.id] = true;
    error = "";
    try {
      await cancelConfigRollout(r.id);
      await Promise.all([reloadRollouts(), reloadGroups()]);
    } catch (e) {
      error = `cancel ${r.group_name}: ${errDetail(e)}`;
      await reloadRollouts();
    } finally {
      rolloutBusy[r.id] = false;
    }
  }
</script>

<!-- Honesty chip: the setting is validated, stored and delivered over the config
     channel, but no shipped agent build acts on it yet. Better to say so here
     than to let an operator conclude the fleet is misbehaving. Declared first
     because both the agent-detail panel and the group dialog render it. -->
{#snippet notEnforced(why: string)}
  <span
    class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
    title={why}>not enforced yet</span>
{/snippet}

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

  <!-- Per-agent detail row: what this agent can DO (its advertised capability +
       host-tool matrix) versus what its effective policy asks of it, and — the
       point of the whole surface — the settings it will therefore ignore. -->
  {#snippet agentDetail(a: AgentOut)}
    {@const caps = capsOf(a)}
    <div class="flex flex-col gap-3 rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-800">
      <!-- Capability advertisement -->
      <div class="flex flex-wrap items-center gap-2">
        <span class="font-medium text-slate-500">Capabilities</span>
        {#if caps === null}
          <span class="text-slate-400">
            Not reported yet — an agent advertises its capabilities on its command
            poll (about a minute after it starts).
          </span>
        {:else}
          {#if caps.extract === true}
            <span
              class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
              title="This agent runs the content-extraction pass locally and ships the result with its replication events. Central never opens a file on an agent host.">
              extraction{caps.extract_schema ? ` · schema ${caps.extract_schema}` : ""}
            </span>
          {:else}
            <span
              class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
              title="This agent advertises no extraction pass — either an older build, or extraction is unavailable on the host. Its items carry identity only (path/size/mtime/hashes).">
              no extraction
            </span>
          {/if}
          {#each CAPABILITY_TOOLS as tool (tool)}
            <!-- The chip carries a VERDICT, not just presence (2026-08-11).
                 "present" and "present and good enough" are different answers:
                 a tesseract 4.1.1 and a 5.3.4 used to render as the same green
                 chip, and the version alone did not help anyone who does not
                 carry upstream's release history in their head. Central judges
                 (`tool_verdicts`), ./hostTools turns that into a tone + tooltip,
                 and `unknown` is styled like neither good nor bad news because
                 an ffmpeg git build is usually the newest one in the fleet. -->
            {@const chip = toolChip(tool, caps, a.tool_verdicts, toolMinimums)}
            <span
              class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {TOOL_CHIP_CLASS[chip.tone]}"
              title={chip.title}>
              <!-- The version stays INLINE rather than tooltip-only: it is the
                   fact an operator compares across rows. -->
              {chip.label}{chip.verdict === "outdated" ? " ⚠" : ""}
            </span>
          {/each}
          {#if caps.formats?.length}
            <span class="text-slate-400">formats:</span>
            {#each caps.formats as f (f)}
              <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">{f}</span>
            {/each}
          {/if}
        {/if}
      </div>

      <!-- Configuration-group membership. A FULL-REPLACE editor: tick the
           groups this agent should be in and save once. Global is shown ticked
           and disabled because membership in it is implicit — there are no join
           rows for it and the endpoint 400s if its id is submitted. -->
      <div class="border-t border-slate-100 pt-2 dark:border-slate-800/60">
        <div class="flex flex-wrap items-center gap-2">
          <span class="font-medium text-slate-500">Configuration groups</span>
          <span class="text-slate-400">
            applied in priority order, lowest first — the last group to set a key wins it
          </span>
        </div>
        <div class="mt-1.5 flex flex-wrap gap-2">
          {#each groups as g (g.id)}
            <label
              class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 dark:border-slate-800"
              title={g.is_system
                ? "The permanent Global group. Every agent is a member and it always applies first, so it is the fleet-wide baseline every other group overrides. It cannot be left."
                : `Priority ${g.priority} — a higher priority applies later and wins any key it sets. ${g.member_count} member agent(s).`}>
              <input
                type="checkbox"
                checked={g.is_system || memberIds(a).includes(g.id)}
                disabled={g.is_system || savingMembers[a.id] || a.status === "revoked"}
                onchange={(e) => toggleMembership(a, g.id, e.currentTarget.checked)} />
              <span>{g.name}</span>
              <span class="tabular-nums text-slate-400">p{g.priority}</span>
              {#if g.is_system}
                <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300">always</span>
              {/if}
            </label>
          {/each}
        </div>
        {#if membersDirty(a)}
          <div class="mt-1.5 flex items-center gap-2">
            <button
              class="rounded-lg bg-[var(--accent)] px-2 py-1 text-[11px] text-white disabled:opacity-50"
              disabled={savingMembers[a.id]}
              onclick={() => saveMembership(a)}>
              {savingMembers[a.id] ? "Saving…" : "Save membership"}
            </button>
            <button
              class="text-[11px] text-slate-500"
              onclick={() => delete memberDraft[a.id]}>discard</button>
          </div>
        {/if}
      </div>

      <!-- Effective extraction policy + the ignored-settings verdict -->
      <div class="border-t border-slate-100 pt-2 dark:border-slate-800/60">
        <span class="font-medium text-slate-500">Effective content-extraction settings</span>
        {#if effLoading[a.id]}
          <p class="mt-1 text-slate-400">Loading…</p>
        {:else if effError[a.id]}
          <p class="mt-1 text-red-600">Could not load the effective configuration: {effError[a.id]}</p>
        {:else if effective[a.id]}
          {@const eff = effective[a.id]}
          {@const merged = policyOf(eff)}
          {@const ignored = ignoredPolicySettings(merged, caps)}
          <div class="mt-1 flex flex-wrap gap-x-5 gap-y-1">
            {#each EXTRACTION_FIELDS as f (f.key)}
              <span class="text-slate-500">
                <code class="font-mono text-[11px]">{f.key}</code>
                <b class="text-slate-700 dark:text-slate-200">{fmtPolicyValue(merged[f.key])}</b>
                <span class="text-slate-400">
                  ({provenanceFor(eff.provenance, eff.groups, "policy", f.key)})
                </span>
              </span>
            {/each}
          </div>
          {#if ignored.length}
            <div class="mt-2 flex flex-wrap items-center gap-2">
              <span class="text-slate-500">This agent will ignore:</span>
              {#each ignored as ig (ig.key)}
                <span
                  class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                  title={`${ig.key} is set in this agent's effective configuration, but ${ig.reason}. The agent logs the ignored setting once and carries on.`}>
                  {ig.key} — {ig.reason}
                </span>
              {/each}
            </div>
          {:else if caps !== null}
            <p class="mt-2 text-emerald-600 dark:text-emerald-400">
              Nothing in this agent's effective configuration is beyond what it advertises.
            </p>
          {/if}
        {/if}
      </div>

      <!-- The FULL effective configuration: every merged key with the group and
           version that supplied it. Opt-in inside an already-expanded row —
           ~40 keys with badges is a wall, and the extraction summary above
           answers the common question on its own. -->
      {#if effective[a.id]}
        {@const eff = effective[a.id]}
        <div class="border-t border-slate-100 pt-2 dark:border-slate-800/60">
          <div class="flex flex-wrap items-center gap-2">
            <button
              class="font-medium text-[var(--accent)]"
              title="Every key of the document this agent receives on its next poll, after all of its groups have been merged — with the group and version each value came from."
              onclick={() => (effFull[a.id] = !effFull[a.id])}>
              {effFull[a.id] ? "▾" : "▸"} Effective configuration
            </button>
            <span class="text-slate-400" title="The delivered generation: max(version seq) across the group snapshots that composed this document. The agent echoes it back on its next poll.">
              generation <b class="tabular-nums text-slate-600 dark:text-slate-300">{eff.generation}</b>
            </span>
            {#if eff.confirmed_generation !== eff.generation}
              <span
                class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                title={`This agent last confirmed generation ${eff.confirmed_generation ?? "none"}. A poll (~1 min) or an offline agent explains a brief lag; a persistent one means it is not reaching central.`}>
                agent has not confirmed latest (at {eff.confirmed_generation ?? "none"})
              </span>
            {:else}
              <span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
                title="The agent has echoed back this exact generation, so what is shown below is what it is running.">confirmed</span>
            {/if}
            <span class="font-mono text-slate-400" title="First 12 hex of the sha256 over the canonical document — the same hash that rides the delivery ETag.">{eff.hash}</span>
          </div>

          {#if effFull[a.id]}
            <!-- Computed ONCE for the whole report: the verdict walks the tool
                 matrix, and re-deriving it per field would run it ~30 times. -->
            {@const merged = policyOf(eff)}
            {@const ignoredKeys = new Set(
              ignoredPolicySettings(merged, caps).map((ig) => ig.key),
            )}
            <!-- Contributing layers, in merge order. Naming them (and their
                 versions) is what makes a surprising value traceable. -->
            <div class="mt-1.5 flex flex-wrap items-center gap-1.5">
              <span class="text-slate-500">Layers:</span>
              {#each eff.groups as g, i (g.id)}
                {#if i > 0}<span class="text-slate-400">→</span>{/if}
                <span
                  class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                  title={`Priority ${g.priority}, version ${g.version_used}${g.via_rollout ? " — delivered by a phased rollout this agent's bucket is covered by, so other members of the same group may still be on the previous version" : ""}.`}>
                  {g.name} v{g.version_used}{g.via_rollout ? " · rollout" : ""}
                </span>
              {/each}
            </div>

            {#each POLICY_SECTIONS as section (section.id)}
              {@const fields = POLICY_FIELDS.filter((f) => f.section === section.id)}
              <div class="mt-2">
                <span class="font-medium text-slate-500">{section.label}</span>
                <div class="mt-1 grid gap-x-4 gap-y-1 md:grid-cols-2">
                  {#each fields as f (f.key)}
                    <div class="flex flex-wrap items-baseline gap-1.5">
                      <code class="font-mono text-[11px] text-slate-500">{f.key}</code>
                      <b class="break-all text-slate-700 dark:text-slate-200">{fmtPolicyValue(merged[f.key])}</b>
                      <span
                        class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                        title="The group version that supplied this value. 'built-in default' means no group sets the key, so the agent's own default applies.">
                        {provenanceFor(eff.provenance, eff.groups, "policy", f.key)}
                      </span>
                      {#if ignoredKeys.has(f.key)}
                        {@render notEnforced(`This agent cannot honour ${f.key} — see the ignored-settings chips above.`)}
                      {/if}
                    </div>
                  {/each}
                </div>
              </div>
            {/each}

            {#if extraPolicyKeys(eff).length}
              <div class="mt-2">
                <span class="font-medium text-slate-500">Other delivered keys</span>
                <span class="text-slate-400"> — set by a group but not modelled by this console</span>
                <div class="mt-1 grid gap-x-4 gap-y-1 md:grid-cols-2">
                  {#each extraPolicyKeys(eff) as key (key)}
                    <div class="flex flex-wrap items-baseline gap-1.5">
                      <code class="font-mono text-[11px] text-slate-500">{key}</code>
                      <b class="break-all text-slate-700 dark:text-slate-200">{fmtPolicyValue(merged[key])}</b>
                      <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {provenanceFor(eff.provenance, eff.groups, "policy", key)}
                      </span>
                    </div>
                  {/each}
                </div>
              </div>
            {/if}

            <!-- The settings half, delivered under the document's `group` key. -->
            <div class="mt-2">
              <span class="font-medium text-slate-500">Group settings</span>
              <span class="text-slate-400"> — delivered under <code class="font-mono">group</code></span>
              {#if Object.keys(settingsOf(eff)).length === 0}
                <p class="mt-1 text-slate-400">No group supplies any settings — the agent uses its own configuration.</p>
              {:else}
                <div class="mt-1 grid gap-x-4 gap-y-1 md:grid-cols-2">
                  {#each Object.entries(settingsOf(eff)) as [key, value] (key)}
                    <div class="flex flex-wrap items-baseline gap-1.5">
                      <code class="font-mono text-[11px] text-slate-500">{key}</code>
                      <b class="break-all text-slate-700 dark:text-slate-200">{fmtPolicyValue(value)}</b>
                      <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300">
                        {provenanceFor(eff.provenance, eff.groups, "settings", key)}
                      </span>
                    </div>
                  {/each}
                </div>
              {/if}
            </div>
          {/if}
        </div>
      {/if}

      <!-- Hash-migration knobs (QH-T6). Advanced by placement, not by a fold:
           three small number boxes that only matter if the operator is doing
           the opt-in wide backfill, sitting next to the row action they modify.
           Left blank they are absent from the request and the defect band
           applies, which is what the overwhelming majority of runs want. -->
      <div class="border-t border-slate-100 pt-2 dark:border-slate-800/60">
        <div class="flex flex-wrap items-center gap-2">
          <span
            class="font-medium text-slate-500"
            title="Bounds for this agent's 're-hash' action. The default is the defect band — files between 65,537 and 131,072 bytes, the only sizes the pre-2026-07-18 hasher got wrong. A file of 65,536 bytes or less was already hashed in full and is correct; above 131,072 bytes the hash was sampled then and is sampled now, so neither is worth re-reading.">
            Re-hash band
          </span>
          <label class="text-slate-500">
            min
            <input
              type="number" min="1" max="131072" placeholder="65537"
              class="ml-1 w-24 rounded border border-slate-300 px-1 py-0.5 text-[11px] dark:border-slate-700 dark:bg-slate-900"
              bind:value={
                () => bandOf(a.id).min,
                (v) => (rehashBand[a.id] = { ...bandOf(a.id), min: String(v ?? "") })
              } />
          </label>
          <label class="text-slate-500">
            max
            <input
              type="number" min="1" max="131072" placeholder="131072"
              class="ml-1 w-24 rounded border border-slate-300 px-1 py-0.5 text-[11px] dark:border-slate-700 dark:bg-slate-900"
              bind:value={
                () => bandOf(a.id).max,
                (v) => (rehashBand[a.id] = { ...bandOf(a.id), max: String(v ?? "") })
              } />
          </label>
          <label
            class="text-slate-500"
            title="Stop after this many candidate files and leave the cursor there; the next run resumes from it. Leave blank to sweep the whole band in one command.">
            max files
            <input
              type="number" min="1" placeholder="all"
              class="ml-1 w-24 rounded border border-slate-300 px-1 py-0.5 text-[11px] dark:border-slate-700 dark:bg-slate-900"
              bind:value={
                () => bandOf(a.id).items,
                (v) => (rehashBand[a.id] = { ...bandOf(a.id), items: String(v ?? "") })
              } />
          </label>
          <span class="text-[11px] text-slate-400">
            Widening the floor to 1 runs the separate small-file content-hash
            backfill instead — roughly ten times the reads, for exact identity on
            files that were never mis-hashed. Not the default for that reason.
          </span>
        </div>
      </div>

      <!-- About / versions: what this agent IS, as opposed to what it can do.
           A third opt-in disclosure for the same reason as the one above — it
           is long, and it answers a deliberate investigation ("which exiftool
           is actually on that box, and how old is it") rather than the
           at-a-glance question the chips at the top already answer. -->
      <div class="border-t border-slate-100 pt-2 dark:border-slate-800/60">
        <div class="flex flex-wrap items-center gap-2">
          <button
            class="font-medium text-[var(--accent)]"
            title="This agent's build stack, its Go module dependencies, and its host tools with version, resolved path and how each measures up to the recommended minimum. All self-reported on its command poll — central never queries an agent."
            onclick={() => toggleAbout(a)}>
            {aboutOpen[a.id] ? "▾" : "▸"} About / versions
          </button>
          {#if about[a.id]}
            <button
              class="text-[11px] text-slate-500"
              title="Copy the whole report as a Markdown table — paste it straight into a bug report."
              onclick={() => copyAbout(a)}>Copy as Markdown</button>
            {#if aboutCopied === a.id}
              <span class="text-[11px] text-[var(--accent)]">copied</span>
            {/if}
          {/if}
        </div>

        {#if aboutOpen[a.id]}
          {#if aboutLoading[a.id]}
            <p class="mt-1 text-slate-400">Loading…</p>
          {:else if aboutError[a.id]}
            <p class="mt-1 text-red-600">Could not load this agent's About report: {aboutError[a.id]}</p>
          {:else if about[a.id]}
            {@const rep = about[a.id]}
            {@const mods = modulesSummary(rep)}
            {#if !rep.reported}
              <!-- Not an error and not zeros: an enrolled agent that has not
                   polled yet genuinely has nothing to report, and saying so is
                   a different statement from "everything is absent". -->
              <p class="mt-1 text-slate-400">
                This agent has never sent a capability advertisement — it has not polled
                yet (a fresh enrollment), or it runs a build older than the capability
                channel. Nothing below is missing; there is nothing yet to report.
              </p>
            {/if}

            <!-- Build stack -->
            <div class="mt-2 grid gap-x-6 gap-y-1 md:grid-cols-2">
              {#each buildRows(rep) as row (row.label)}
                <div class="flex flex-wrap items-baseline gap-1.5" title={row.hint}>
                  <span class="text-slate-500">{row.label}</span>
                  <b class="break-all font-mono text-[11px] text-slate-700 dark:text-slate-200">{row.value}</b>
                </div>
              {/each}
            </div>

            <!-- Hash migration (QH-T6). Sits directly under the build stack
                 rather than in a fold: it is ONE line, it is the only place this
                 fact exists (central cannot derive an agent's hash provenance
                 from the catalogue — it holds none for agent-owned rows), and
                 "has this box been migrated" is a question an operator arrives
                 with rather than one they discover. -->
            {@const rehash = rehashCell(rep)}
            <div class="mt-3 flex flex-wrap items-baseline gap-1.5">
              <span
                class="text-slate-500"
                title="The 2026-07-18 fix corrected a defect where a file between 64 KiB and 128 KiB had only its first 64 KiB hashed, so different files with matching headers looked like duplicates. Stored hashes were NOT corrected by that fix: this agent's scan re-hashes a file only when its size or modification time changes, so a stable file in that band keeps its wrong hash until this migration runs.">
                Hash migration
              </span>
              <b class="{ABOUT_TONE_CLASS[rehash.tone]}" title={rehash.hint}>{rehash.text}</b>
              {#if rep.rehash?.fp}
                <span class="font-mono text-[11px] text-slate-400"
                  title="The scheme and size band this cursor belongs to. A repeat sweep at the same fingerprint short-circuits; changing the band, or a future change to the hashing itself, invalidates it and re-sweeps.">{rep.rehash.fp}</span>
              {/if}
            </div>

            <!-- Host tools: version · location · verdict. The three things the
                 boolean matrix above cannot say. -->
            <div class="mt-3">
              <span class="font-medium text-slate-500">Host tools on this agent</span>
              <span class="text-slate-400">
                — the resolved path is which copy the agent will actually run; the agent
                only ever resolves tools from machine-wide, admin-writable locations
              </span>
              {#if rep.host_tools.length === 0}
                <p class="mt-1 text-slate-400">Not reported.</p>
              {:else}
                <div class="mt-1 overflow-x-auto">
                  <table class="w-full min-w-[46rem] text-[11px]">
                    <thead class="text-left text-slate-500">
                      <tr class="border-b border-slate-200 dark:border-slate-800">
                        <th class="py-1 pr-3 font-medium">Tool</th>
                        <th class="py-1 pr-3 font-medium">Version</th>
                        <th class="py-1 pr-3 font-medium">Location</th>
                        <th class="py-1 font-medium">Status</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100 dark:divide-slate-800/60">
                      {#each rep.host_tools as t (t.name)}
                        <!-- The chip comes from ./hostTools, the SAME helper the
                             row above uses: one verdict vocabulary, one set of
                             tooltips, no third opinion invented here. -->
                        {@const chip = toolChip(t.name, caps, a.tool_verdicts, toolMinimums)}
                        {@const cell = agentToolCell(t)}
                        {@const path = toolPathCell(t)}
                        <tr class="align-top">
                          <td class="py-1 pr-3">
                            {#if t.url}
                              <a class="text-[var(--accent)]" href={t.url} target="_blank" rel="noreferrer noopener">{t.name}</a>
                            {:else}
                              <span>{t.name}</span>
                            {/if}
                            <div class="text-slate-400">{orUnknown(t.purpose, "purpose not published")}</div>
                          </td>
                          <td class="py-1 pr-3 {ABOUT_TONE_CLASS[cell.tone]}" title={cell.hint}>{cell.text}</td>
                          <td class="py-1 pr-3 font-mono break-all {ABOUT_TONE_CLASS[path.tone]}" title={path.hint}>{path.text}</td>
                          <td class="py-1">
                            <span
                              class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {TOOL_CHIP_CLASS[chip.tone]}"
                              title={chip.title}>
                              {t.verdict}{t.verdict === "outdated" ? " ⚠" : ""}
                            </span>
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}
            </div>

            <!-- Go modules: collapsed by default (it is ~120 rows), and when the
                 agent trimmed it to fit its poll budget the row SAYS SO rather
                 than rendering an empty table that reads as "no dependencies". -->
            <div class="mt-3">
              <div class="flex flex-wrap items-center gap-2">
                <button
                  class="font-medium text-[var(--accent)] disabled:opacity-50"
                  disabled={!rep.modules?.length}
                  title="Every Go module linked into this agent binary. A Go binary is statically linked, so this list IS what is running — not a manifest of what was requested."
                  onclick={() => (modulesOpen[a.id] = !modulesOpen[a.id])}>
                  {modulesOpen[a.id] ? "▾" : "▸"} Go modules
                </button>
                <span class="{ABOUT_TONE_CLASS[mods.tone]}" title={mods.hint}>{mods.text}</span>
              </div>
              {#if modulesOpen[a.id] && rep.modules?.length}
                <div class="mt-1 max-h-80 overflow-y-auto">
                  <table class="w-full text-[11px]">
                    <thead class="text-left text-slate-500">
                      <tr class="border-b border-slate-200 dark:border-slate-800">
                        <th class="py-1 pr-3 font-medium">Module</th>
                        <th class="py-1 font-medium">Version</th>
                      </tr>
                    </thead>
                    <tbody class="divide-y divide-slate-100 dark:divide-slate-800/60">
                      {#each rep.modules as m (m.path)}
                        <tr>
                          <td class="py-1 pr-3 font-mono break-all">
                            <a class="text-[var(--accent)]" href={m.url} target="_blank" rel="noreferrer noopener">{m.path}</a>
                          </td>
                          <td class="py-1 font-mono text-slate-600 dark:text-slate-300">
                            {orUnknown(m.version, "version unknown")}
                          </td>
                        </tr>
                      {/each}
                    </tbody>
                  </table>
                </div>
              {/if}
            </div>
          {/if}
        {/if}
      </div>
    </div>
  {/snippet}

  <!-- Registered agents renders at the BOTTOM of the page (below enrollment +
       config groups): in a big fleet it is the longest element, and it pages
       server-side (AGENTS_PAGE per window) so thousands of agents never land
       in one response. Defined as a snippet here, rendered at the end. -->
  {#snippet registeredAgents()}
    <h3 class="mt-8 font-medium">Registered agents</h3>
    <p class="mt-1 text-xs text-slate-500">
      Every agent is in the permanent <b>Global</b> group plus any configuration
      groups you add it to. They layer in priority order — the lowest priority
      applies first and the last group to set a key wins it — so a value shown in
      one group is not necessarily the value an agent receives. Open a row's
      <b>details</b> to change its membership and to read its merged, per-key
      effective configuration. Changes land on the agent's next poll.
    </p>
    {#if agentsTotal === 0}
      <p class="py-2 text-slate-400">No agents registered.</p>
    {:else}
      <div class="mt-1 overflow-x-auto">
        <table class="w-full min-w-[64rem] text-sm">
          <thead class="text-left text-slate-500">
            <tr class="border-b border-slate-200 dark:border-slate-800">
              <th class="py-2 pr-3 font-medium">Name</th>
              <th class="py-2 pr-3 font-medium">Hostname</th>
              <th class="py-2 pr-3 font-medium">Platform</th>
              <th class="py-2 pr-3 font-medium">Status</th>
              <th class="py-2 pr-3 font-medium">Online</th>
              <th
                class="py-2 pr-3 font-medium"
                title="The configuration groups this agent belongs to, in merge order (lowest priority first). Global is implicit on every agent and is not listed. Edit membership in the row's details panel — an agent can be in any number of groups and they layer per key.">
                Config groups
              </th>
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
                {#if a.health?.suspended}
                  <span class="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title="Operator-suspended: this agent is not scanning or replicating (self-reported at its last check-in). It still polls for commands — resume it with the actions on the right.">suspended</span>
                {/if}
                {#if sweeping.has(a.id)}
                  <span class="ml-1 rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-900/40 dark:text-violet-300"
                    title="A re-extract sweep is queued or running: the agent is re-running extraction over its existing index and re-emitting the metadata. It resumes across restarts, so this can stay up for hours on a large library.">re-extracting</span>
                {/if}
                {#if rehashing.has(a.id)}
                  <span class="ml-1 rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-900/40 dark:text-violet-300"
                    title="A hash migration is queued or running: the agent is re-reading every indexed file in the 64-128 KiB band to correct hashes computed before the 2026-07-18 fix. Expect sustained disk/network I/O on that machine; it resumes across restarts, so this can stay up for hours.">re-hashing</span>
                {/if}
                {#if a.health?.central_maintenance}
                  <span class="ml-1 rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
                    title="This agent observed central's maintenance mode and paused its replication push; local scanning continues and its backlog drains when maintenance ends.">backing off</span>
                {/if}
                {#if a.health?.local_scan_paused}
                  <span class="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title="An operator paused scanning from THIS AGENT's own web UI (allowed by the local_scan_control policy key). Replication is unaffected. Resume it there, or revoke the permission here — the central Suspend action is a separate, stronger hold that a local resume cannot lift.">paused locally</span>
                {/if}
                {#if a.health?.local_overrides}
                  <span class="ml-1 rounded-full bg-violet-100 px-1.5 py-0.5 text-[10px] font-medium text-violet-700 dark:bg-violet-900/40 dark:text-violet-300"
                    title={healthTitle(a)}>local settings</span>
                {/if}
                {#if shareMapRejects(a) > 0}
                  <span class="ml-1 rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title="This agent skipped one or more malformed share-map entries, so those scan roots report no network location. A bad entry is never fatal (share hints are best-effort), which is why it needs saying here. Fix FILEARR_AGENT_SHARE_MAP where the agent is deployed, or the mapping in the agent's own roots editor.">share map errors</span>
                {/if}
                {#if a.last_auth_mode === "mtls"}
                  <span class="ml-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300"
                    title="Central verified this agent's last authenticated request via the mTLS proxy path (client-certificate SAN identity) — observed server-side, not self-reported.">mTLS</span>
                {:else if a.last_auth_mode === "bearer"}
                  <span class="ml-1 rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                    title="This agent's last authenticated request used the interim bearer (fingerprint) path — it has not been switched to the mTLS endpoint yet. See the mode-flip runbook (docs: TLS).">bearer</span>
                {/if}
              </td>
              <!-- NAMES, not a picker: an agent can be in many groups, so the
                   cell reports and the details panel edits. Global is left out
                   deliberately — it is on every row, so printing it on every row
                   is noise; the effective-config viewer names it where the
                   provenance actually matters. -->
              <td class="py-2 pr-3">
                {#each groupsOf(a) as g (g.id)}
                  <span
                    class="mr-1 inline-block rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                    title={`Priority ${g.priority} — a higher priority applies later and wins the keys it sets. Current version ${g.current_version}.`}>
                    {g.name}
                  </span>
                {:else}
                  <span class="text-[11px] text-slate-400" title="This agent is only in the implicit Global group, so it receives the fleet-wide baseline and nothing else.">Global only</span>
                {/each}
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
                {:else if a.update_available && a.update_hold}
                  <span
                    class="ml-1 rounded-full bg-slate-200 px-2 py-0.5 text-xs font-medium text-slate-700 dark:bg-slate-800 dark:text-slate-300"
                    title={`${a.update_target} is available but this agent's policy holds it: ${a.update_hold}. The update action still applies it immediately (the click is the authorization).`}>update held</span>
                {:else if a.update_available}
                  <span
                    class="ml-1 rounded-full bg-amber-100 px-2 py-0.5 text-xs font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                    title={`Central offers ${a.update_target}`}>update available</span>
                {/if}
              </td>
              <td class="py-2 text-right whitespace-nowrap">
                <button
                  class="text-slate-600 dark:text-slate-300"
                  title="Show what this agent can actually do (advertised capabilities + host tools) and which of its effective policy settings it will ignore."
                  onclick={() => toggleDetail(a)}>{expanded === a.id ? "hide" : "details"}</button>
                {#if a.status !== "revoked"}
                  {#if a.update_available && !a.update_pending && !a.capabilities?.container}
                    <button
                      class="ml-3 text-sky-600 disabled:opacity-50 dark:text-sky-400"
                      disabled={updating[a.id]}
                      title={`Queue an update to ${a.update_target} — applied at the agent's next check-in (~1 min)`}
                      onclick={() => updateAgentNow(a.id, a.name)}>update</button>
                  {/if}
                  <button
                    class="ml-3 text-amber-600 disabled:opacity-50 dark:text-amber-400"
                    disabled={suspending[a.id]}
                    title={a.health?.suspended
                      ? "Queue a resume — the agent restarts scanning + replication at its next check-in (~1 min)"
                      : "Queue a suspend — the agent stops scanning + replicating at its next check-in (~1 min); it keeps checking in so it can be resumed"}
                    onclick={() => toggleSuspend(a)}>{a.health?.suspended ? "resume" : "suspend"}</button>
                  <button
                    class="ml-3 text-slate-600 disabled:opacity-50 dark:text-slate-300"
                    disabled={maintaining[a.id]}
                    title="Queue a local maintenance pass: compact the agent's index (VACUUM), prune already-replicated outbox rows, sweep stale temp files. Runs at its next check-in; the result appears in the command history."
                    onclick={() => maintainAgent(a)}>maintain</button>
                  <button
                    class="ml-3 text-violet-600 disabled:opacity-50 dark:text-violet-400"
                    disabled={reextracting[a.id] || sweeping.has(a.id)}
                    title="Re-run extraction across this agent's EXISTING index and re-emit the metadata — the catch-up for items scanned before extraction (or before ffprobe/exiftool/poppler/tesseract) was available on that host. Only extracted metadata is re-sent; file contents never leave the agent. Resumable, and a repeat run at an unchanged extraction configuration does nothing."
                    onclick={() => reextract(a)}>{sweeping.has(a.id) ? "sweeping…" : "re-extract"}</button>
                  <!-- QH-T6, next to re-extract because they are the same SHAPE
                       of action (an hours-long resumable sweep of the agent's
                       existing index) even though they repair different things.
                       The band/max_items knobs live in the expanded detail row:
                       the default IS the answer for almost everyone, and putting
                       three number boxes in a table cell would imply otherwise. -->
                  <button
                    class="ml-3 text-violet-600 disabled:opacity-50 dark:text-violet-400"
                    disabled={rehashingNow[a.id] || rehashing.has(a.id)}
                    title="Re-read every indexed file between 64 KiB and 128 KiB on this agent and correct its hashes. Files in that band were under-hashed before 2026-07-18 (only the first 64 KiB was read), which produced false duplicate detections; the agent's ordinary scan will never revisit them, because their size and modification time have not changed. Re-reads whole files for hours — only the rows whose hash is actually wrong are re-sent, and file contents never leave the agent. Open 'details' to change the size band."
                    onclick={() => rehashSweep(a)}>{rehashing.has(a.id) ? "re-hashing…" : "re-hash"}</button>
                  <button class="ml-3 text-red-600" onclick={() => dropAgent(a.id, a.name)}>revoke</button>
                {/if}
                <button
                  class="ml-3 text-red-600"
                  title="Hard delete — only while the agent owns no libraries/items"
                  onclick={() => purgeAgent(a.id, a.name)}>delete</button>
              </td>
            </tr>
            {#if expanded === a.id}
              <tr class="bg-slate-50/60 dark:bg-slate-900/40">
                <td colspan="8" class="px-1 py-2">{@render agentDetail(a)}</td>
              </tr>
            {/if}
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
      <div class="text-xs text-slate-500"
        title="The configuration groups the enrolling agent joins, by NAME (a token is minted before the agent exists, so there is nothing to assign ids to yet). Global is joined automatically — selecting nothing is a complete answer, not an unconfigured one. A name that no longer exists at enrollment time is skipped rather than failing the enrollment.">
        configuration groups (joined at enrollment)
        <div class="mt-1 flex flex-wrap gap-2">
          {#each groups.filter((g) => !g.is_system) as g (g.id)}
            <label class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 dark:border-slate-800"
              title={`Priority ${g.priority}. ${g.member_count} member agent(s) today.`}>
              <input type="checkbox"
                checked={newGroupNames.includes(g.name)}
                onchange={(e) => toggleMintGroup(g.name, e.currentTarget.checked)} />
              {g.name}
            </label>
          {:else}
            <span class="text-slate-400">No groups beyond Global — the agent enrolls into the fleet-wide baseline.</span>
          {/each}
        </div>
      </div>
      <label class="text-xs text-slate-500"
        title="How long the minted token stays usable, in minutes. The token is single-use as well as short-lived: central consumes it on a successful enrollment. Range 1–1440. Blank uses the server default (60 minutes).">
        TTL (min, blank = default)
        <input class="mt-1 block w-40 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
          title="How long the minted token stays usable, in minutes. The token is single-use as well as short-lived: central consumes it on a successful enrollment. Range 1–1440. Blank uses the server default (60 minutes)."
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
        <label class="text-xs text-slate-500"
          title="Writes agent_name into the sidecar — the friendly name this agent shows under in the fleet table. Left blank, the agent uses the device's own hostname.">
          agent name (optional)
          <input class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
            title="Writes agent_name into the sidecar — the friendly name this agent shows under in the fleet table. Left blank, the agent uses the device's own hostname."
            placeholder="(auto from hostname)" bind:value={insName} />
        </label>
        <div class="text-xs text-slate-500"
          title="Writes config_group_names into the sidecar, so the agent joins these configuration groups at enrollment instead of needing an assignment afterwards. Global is implicit. The sidecar also keeps the legacy single-group config_group key (the first name) so an older agent binary still reads one of them.">
          configuration groups
          <div class="mt-1 flex flex-wrap gap-2">
            {#each groups.filter((g) => !g.is_system) as g (g.id)}
              <label class="inline-flex items-center gap-1.5 rounded-lg border border-slate-200 px-2 py-1 dark:border-slate-800"
                title={`Priority ${g.priority}. ${g.member_count} member agent(s) today.`}>
                <input type="checkbox"
                  checked={insGroupIds.includes(g.id)}
                  onchange={(e) => toggleInsGroup(g.id, e.currentTarget.checked)} />
                {g.name}
              </label>
            {:else}
              <span class="text-slate-400">Global only</span>
            {/each}
          </div>
        </div>
        <label class="text-xs text-slate-500"
          title="Writes log_level into the sidecar. This is the ONE log-level setting a shipped agent actually reads (the per-group Log level below is stored but not yet enforced). The host can still override it with FILEARR_AGENT_LOG_LEVEL or -log-level; the sidecar is the lowest-precedence source. Takes effect at agent start. (default) leaves the key out, so the agent uses info.">
          log level
          <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
            title="Writes log_level into the sidecar. This is the ONE log-level setting a shipped agent actually reads (the per-group Log level below is stored but not yet enforced). The host can still override it with FILEARR_AGENT_LOG_LEVEL or -log-level; the sidecar is the lowest-precedence source. Takes effect at agent start. (default) leaves the key out, so the agent uses info."
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
            <tr><th class="py-1 pr-3">hash</th><th class="pr-3">config groups</th><th class="pr-3">status</th><th class="pr-3">expires</th><th></th></tr>
          </thead>
          <tbody>
            {#each tokens as t (t.token_hash)}
              <tr class="border-t border-slate-200 dark:border-slate-800">
                <td class="py-1 pr-3 font-mono text-xs">{t.token_hash.slice(0, 12)}…</td>
                <td class="pr-3" title="Groups the enrolling agent joins on top of Global.">
                  {t.config_group_names.length ? t.config_group_names.join(", ") : "Global only"}
                </td>
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

  <!-- Live rollouts. Only rendered when something is in flight: an empty card
       would be permanent furniture for a feature most fleets use rarely, and
       "no rollouts" is already the visible state of the groups table. -->
  {#if rollouts.length}
    <div class="mt-8 rounded-xl border border-sky-300 p-4 dark:border-sky-800">
      <div class="flex items-center gap-3">
        <h3 class="font-medium">Rollouts in flight</h3>
        <span class="text-xs text-slate-500">
          promotion is evaluated on central's minute tick; coverage is by stable agent-id bucket
        </span>
      </div>
      <div class="mt-2 overflow-x-auto">
        <table class="w-full min-w-[48rem] text-sm">
          <thead class="text-left text-slate-500">
            <tr class="border-b border-slate-200 dark:border-slate-800">
              <th class="py-2 pr-3 font-medium">Group</th>
              <th class="py-2 pr-3 font-medium">Target</th>
              <th class="py-2 pr-3 font-medium">Progress</th>
              <th class="py-2 pr-3 font-medium">Covered</th>
              <th class="py-2 pr-3 font-medium">Next promotion</th>
              <th class="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
            {#each rollouts as r (r.id)}
              <tr>
                <td class="py-2 pr-3 font-medium">{r.group_name}</td>
                <td class="py-2 pr-3 tabular-nums text-slate-500"
                  title="The group version this rollout is delivering. The group's current_version is unchanged until the last tier completes, so uncovered agents keep receiving the old one.">
                  v{r.target_version}
                </td>
                <td class="py-2 pr-3 text-slate-500">{describeRollout(r)}</td>
                <td class="py-2 pr-3 tabular-nums text-slate-500"
                  title="Share of the fleet whose agent-id hash bucket is inside the active tier. Membership in the bucket is stable, so an agent never flaps in and out between polls.">
                  {r.covered_percent}%
                </td>
                <td class="py-2 pr-3 text-slate-500"
                  title={r.next_promotion_at
                    ? "When the engine widens to the next tier on its own. Promote now to skip the remaining wait."
                    : "No further tier is scheduled — this rollout finishes at its current tier."}>
                  {#if r.next_promotion_at}
                    {untilTime(r.next_promotion_at)} · {fmt(r.next_promotion_at)}
                  {:else if r.status === "scheduled"}
                    starts {fmt(r.starts_at)}
                  {:else}
                    —
                  {/if}
                </td>
                <td class="py-2 text-right whitespace-nowrap">
                  {#if r.status === "running"}
                    <button class="text-sky-600 disabled:opacity-50 dark:text-sky-400"
                      disabled={rolloutBusy[r.id]}
                      title="Advance to the next tier immediately instead of waiting out its delay."
                      onclick={() => promoteRollout(r)}>promote now</button>
                  {/if}
                  <button class="ml-3 text-red-600 disabled:opacity-50"
                    disabled={rolloutBusy[r.id]}
                    title="Stop the rollout. The group stays on its current version, so agents already covered roll BACK to it on their next poll."
                    onclick={() => cancelRollout(r)}>cancel</button>
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    </div>
  {/if}

  <!-- Configuration groups -->
  <div class="mt-8">
    <div class="flex items-center gap-3">
      <h3 class="font-medium">Configuration groups</h3>
      <span class="text-xs text-slate-500">layered per key, lowest priority first</span>
      <div class="grow"></div>
      <button class="rounded-lg bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={openCreate}>New group</button>
    </div>
    <p class="mt-1 text-xs text-slate-500">
      Rows are listed in <b>merge order</b>: each group applies over the ones
      above it and wins only the keys it actually sets. <b>Global</b> is
      permanent, contains every agent and always applies first, so it is the
      fleet-wide baseline. Use the arrows to change a group's priority, and
      <b>edit</b> to change its settings, its policy keys, or to publish a change
      as a phased rollout.
    </p>

    {#if groups.length === 0}
      <p class="py-2 text-slate-400">No configuration groups yet.</p>
    {:else}
      <div class="mt-2 overflow-x-auto">
        <table class="w-full min-w-[48rem] text-sm">
          <thead class="text-left text-slate-500">
            <tr class="border-b border-slate-200 dark:border-slate-800">
              <th class="py-2 pr-3 font-medium">Name</th>
              <th class="py-2 pr-3 font-medium"
                title="Merge rank. Lower applies FIRST, so a higher number wins any key both groups set. Ties are legal and break deterministically by name.">Priority</th>
              <th class="py-2 pr-3 font-medium">Members</th>
              <th class="py-2 pr-3 font-medium"
                title="The version uncovered agents receive. A phased rollout delivers a NEWER version to part of the fleet without changing this number until it completes.">Version</th>
              <th class="py-2 pr-3 font-medium">Rollout</th>
              <th class="py-2 pr-3 font-medium">Description</th>
              <th class="py-2 text-right font-medium">Actions</th>
            </tr>
          </thead>
          <tbody class="divide-y divide-slate-200 dark:divide-slate-800">
            {#each groups as g (g.id)}
              <tr>
                <td class="py-2 pr-3 font-medium whitespace-nowrap">
                  {g.name}
                  {#if g.is_system}
                    <span class="ml-1 rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                      title="The permanent Global group: every agent is a member, its name and priority are fixed, and it cannot be deleted. Its document is the fleet-wide baseline.">system</span>
                  {/if}
                </td>
                <td class="py-2 pr-3 whitespace-nowrap tabular-nums text-slate-500">
                  {g.priority}
                  {#if !g.is_system}
                    <!-- Two-PATCH swap rather than a drag handle: no DnD
                         dependency, and the priorities stay numbers an operator
                         can also type into the dialog. -->
                    <button class="ml-2 disabled:opacity-30"
                      disabled={reordering || reorderable[0]?.id === g.id}
                      title="Apply this group EARLIER (lower priority), so groups below it can override it."
                      onclick={() => reorderGroup(g, -1)}>↑</button>
                    <button class="ml-1 disabled:opacity-30"
                      disabled={reordering || reorderable[reorderable.length - 1]?.id === g.id}
                      title="Apply this group LATER (higher priority), so it overrides the groups above it."
                      onclick={() => reorderGroup(g, 1)}>↓</button>
                  {/if}
                </td>
                <td class="py-2 pr-3 tabular-nums text-slate-500"
                  title={g.is_system ? "Every enrolled agent — membership in Global is implicit." : "Agents explicitly added to this group."}>
                  {g.member_count}
                </td>
                <td class="py-2 pr-3 tabular-nums text-slate-500">v{g.current_version}</td>
                <td class="py-2 pr-3">
                  {#if g.active_rollout}
                    <span class="rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
                      title={`Version ${g.active_rollout.target_version} is being delivered in ${g.active_rollout.tiers.length} tier(s). Manage it in the rollouts card above.`}>
                      → v{g.active_rollout.target_version} · {describeRollout(g.active_rollout)}
                    </span>
                  {:else}
                    <span class="text-slate-400">—</span>
                  {/if}
                </td>
                <td class="max-w-[18rem] truncate py-2 pr-3 text-slate-500" title={g.description ?? ""}>{g.description ?? "—"}</td>
                <td class="py-2 text-right whitespace-nowrap">
                  <button class="text-[var(--accent)]" onclick={() => openEdit(g)}>edit</button>
                  {#if !g.is_system}
                    <button class="ml-3 text-red-600" onclick={() => removeGroup(g)}>delete</button>
                  {/if}
                </td>
              </tr>
            {/each}
          </tbody>
        </table>
      </div>
    {/if}
  </div>

  {@render registeredAgents()}
</div>

<!-- A collapsible dialog section. Everything except General starts closed: the
     dialog now carries the whole group document (~40 fields across settings and
     policy) and an operator almost always came to change exactly one thing. -->
{#snippet sectionHead(id: string, label: string, blurb: string)}
  <button
    class="flex w-full items-baseline gap-2 rounded-lg border border-slate-200 px-3 py-2 text-left dark:border-slate-800"
    onclick={() => toggleSection(id)}>
    <span class="text-slate-400">{openSections[id] ? "▾" : "▸"}</span>
    <span class="text-sm font-medium">{label}</span>
    <span class="text-xs text-slate-500">{blurb}</span>
  </button>
{/snippet}

<!-- Config-group create/edit dialog -->
{#if dialog}
  <div class="fixed inset-0 z-50 flex items-start justify-center overflow-y-auto bg-black/40 p-4">
    <div class="my-8 w-full max-w-3xl rounded-xl border border-slate-200 bg-white p-5 shadow-xl dark:border-slate-700 dark:bg-slate-900">
      <div class="flex flex-wrap items-center gap-3">
        <h3 class="text-lg font-semibold">
          {dialog.id === null
            ? "New configuration group"
            : dialog.isSystem
              ? "Edit the Global group"
              : `Edit “${dialog.name}”`}
        </h3>
        {#if dialog.id !== null}
          <span class="text-xs text-slate-500"
            title="The version uncovered members receive today. Every publish writes a NEW version — nothing is ever rewritten in place.">
            v{dialog.currentVersion} · {dialog.memberCount} member{dialog.memberCount === 1 ? "" : "s"}
          </span>
          <button class="text-xs text-[var(--accent)]"
            title="Past versions of this group's document, newest first — with a one-click restore that republishes an old snapshot as a new version."
            onclick={openHistory}>history</button>
        {/if}
        <div class="grow"></div>
        <button class="text-slate-500" onclick={() => (dialog = null)}>✕</button>
      </div>

      {#if dialogError}
        <p class="mt-2 rounded-lg border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">{dialogError}</p>
      {/if}

      {#if historyView}
        <!-- In-dialog history view. Deliberately a VIEW rather than a second
             modal: restoring re-seeds the form behind it, so stacking two
             dialogs would hide the thing that just changed. -->
        <div class="mt-3">
          <button class="text-xs text-[var(--accent)]" onclick={() => (historyView = false)}>← back to editing</button>
          {#if historyLoading}
            <p class="mt-2 text-sm text-slate-400">Loading version history…</p>
          {:else if historyError}
            <p class="mt-2 text-sm text-red-600">Could not load the history: {historyError}</p>
          {:else if historyRows.length === 0}
            <p class="mt-2 text-sm text-slate-400">No versions recorded for this group.</p>
          {:else}
            <table class="mt-2 w-full text-xs">
              <thead class="text-left text-slate-500">
                <tr>
                  <th class="py-1 pr-3 font-medium">Version</th>
                  <th class="py-1 pr-3 font-medium"
                    title="The fleet-wide generation counter this snapshot advanced. It is the number an agent echoes back as its applied generation.">Generation</th>
                  <th class="py-1 pr-3 font-medium">Actor</th>
                  <th class="py-1 pr-3 font-medium">Note</th>
                  <th class="py-1 pr-3 font-medium">Written</th>
                  <th class="py-1 font-medium"></th>
                </tr>
              </thead>
              <tbody>
                {#each historyRows as h (h.seq)}
                  <tr class="border-t border-slate-100 dark:border-slate-800/60">
                    <td class="py-1 pr-3 tabular-nums">
                      v{h.version}
                      {#if h.version === dialog.currentVersion}
                        <span class="ml-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300">current</span>
                      {/if}
                    </td>
                    <td class="py-1 pr-3 tabular-nums text-slate-500">{h.seq}</td>
                    <td class="py-1 pr-3 text-slate-500">{h.actor ?? "—"}</td>
                    <td class="max-w-[14rem] truncate py-1 pr-3 text-slate-500" title={h.note ?? ""}>{h.note ?? "—"}</td>
                    <td class="py-1 pr-3 text-slate-500">{fmt(h.created_at)}</td>
                    <td class="py-1 text-right whitespace-nowrap">
                      <button class="text-[var(--accent)]"
                        onclick={() => (historyOpenSeq = historyOpenSeq === h.seq ? null : h.seq)}>
                        {historyOpenSeq === h.seq ? "hide" : "view JSON"}
                      </button>
                      {#if h.version !== dialog.currentVersion}
                        <button class="ml-3 text-amber-600 disabled:opacity-50 dark:text-amber-400"
                          disabled={dialogBusy}
                          title="Copy this snapshot forward as a new version and publish it immediately."
                          onclick={() => restoreVersion(h)}>restore this version</button>
                      {/if}
                    </td>
                  </tr>
                  {#if historyOpenSeq === h.seq}
                    <tr>
                      <td colspan="6" class="pb-2">
                        <pre class="max-h-72 overflow-auto rounded bg-slate-900/90 p-2 font-mono text-[11px] text-slate-100">{JSON.stringify(
                            { settings: h.settings, policy: h.policy },
                            null,
                            2,
                          )}</pre>
                      </td>
                    </tr>
                  {/if}
                {/each}
              </tbody>
            </table>
          {/if}
        </div>
      {:else}
      <div class="mt-3 flex flex-col gap-3">
        <!-- General: the only section open on entry. -->
        {@render sectionHead("general", "General", "Name, description and merge priority.")}
        {#if openSections.general}
        <div class="flex flex-col gap-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
        {#if dialog.isSystem}
          <p class="rounded-lg border border-slate-300 bg-slate-50 p-2 text-xs text-slate-600 dark:border-slate-700 dark:bg-slate-900/40 dark:text-slate-300">
            This is the permanent <b>Global</b> group. Every agent is a member, it
            always applies first, and its name and priority are fixed — every
            other group layers on top of what you set here. Its document is the
            only sensible place for a fleet-wide default.
          </p>
        {/if}
        <label class="text-xs text-slate-500"
          title="How this group is identified everywhere in this console and in enrollment tokens. Unique across the fleet — a duplicate is rejected — and 1–128 characters. Renaming keeps every member assigned.">Name
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700"
            title="How this group is identified everywhere in this console and in enrollment tokens. Unique across the fleet — a duplicate is rejected — and 1–128 characters. Renaming keeps every member assigned."
            disabled={dialog.isSystem}
            bind:value={dialog.name} />
        </label>
        <label class="text-xs text-slate-500"
          title="Free-text note for other operators, up to 1024 characters. Never sent to the agent.">Description
          <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
            title="Free-text note for other operators, up to 1024 characters. Never sent to the agent."
            bind:value={dialog.description} />
        </label>
        <label class="text-xs text-slate-500"
          title="Merge rank, 0–1000000. Groups apply in ASCENDING priority and the last one to set a key wins it, so a HIGHER number means this group overrides the others. Ties are legal and break deterministically by name. Global is pinned at 0.">Priority
          <input type="number" min="0" max="1000000"
            class="mt-1 block w-40 rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm disabled:opacity-50 dark:border-slate-700"
            title="Merge rank, 0–1000000. Groups apply in ASCENDING priority and the last one to set a key wins it, so a HIGHER number means this group overrides the others. Ties are legal and break deterministically by name. Global is pinned at 0."
            disabled={dialog.isSystem}
            bind:value={dialog.priority} />
          <span class="mt-0.5 block text-[11px] text-slate-400">
            Lower applies first; a higher number wins the keys it sets.
          </span>
        </label>
        </div>
        {/if}

        {@render sectionHead(
          "delivery",
          SETTINGS_SECTIONS[0].label,
          SETTINGS_SECTIONS[0].blurb,
        )}
        {#if openSections.delivery}
        <div class="flex flex-wrap gap-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <label class="text-xs text-slate-500">
            <span class="inline-flex items-center gap-1.5">Log level
              {@render notEnforced("Delivered under the policy document's `group` section, but the agent's log level comes only from its sidecar config, FILEARR_AGENT_LOG_LEVEL, or the -log-level flag today. Set it in the installer sidecar to actually change an agent's logging.")}</span>
            <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
              title="Intended log verbosity for this group's members. STORED AND DELIVERED BUT NOT ENFORCED: no shipped agent build reads it. To actually change an agent's logging, use the installer sidecar's log level, FILEARR_AGENT_LOG_LEVEL, or the -log-level flag on the host. (unset) omits the key."
              bind:value={dialog.logLevel}>
              <option value="">(unset)</option>
              {#each AGENT_LOG_LEVELS as lvl}
                <option value={lvl}>{lvl}</option>
              {/each}
            </select>
          </label>
          <label class="text-xs text-slate-500"
            title="Arms the in-daemon scan scheduler for this group's members, so a lone `filearr-agent run` service scans itself with no external cron or scheduled task. 5-field cron in the AGENT's local time — not UTC, not this browser's timezone. A top-level scan_cron policy key outranks this; the host's FILEARR_AGENT_SCAN_CRON is the fallback when both are absent. Leave blank on container agents — they scan from their own entrypoint loop and would double-scan.">Scan schedule (cron)
            <input class="mt-1 block w-56 rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-sm dark:border-slate-700"
              title="Arms the in-daemon scan scheduler for this group's members. 5-field cron in the AGENT's local time — not UTC, not this browser's timezone. A top-level scan_cron policy key outranks this; the host's FILEARR_AGENT_SCAN_CRON is the fallback when both are absent. Leave blank on container agents — they scan from their own entrypoint loop and would double-scan."
              placeholder="0 3 * * *" bind:value={dialog.cron} />
            <span class="mt-0.5 block text-[11px] text-slate-400">5-field cron in the agent's local time.</span>
          </label>
        </div>
        {/if}

        {@render sectionHead(
          "surface",
          SETTINGS_SECTIONS[1].label,
          SETTINGS_SECTIONS[1].blurb,
        )}
        {#if openSections.surface}
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <p class="text-xs text-slate-400">
            Inherit = this group says nothing, so a lower-priority group or the
            agent's built-in default supplies the value. These three exist in
            BOTH halves of the document: a non-null value here is lifted over the
            policy key of the same name in the Local access policy section below,
            for this group's layer only.
          </p>
          <div class="mt-2 flex flex-wrap gap-4">
            <label class="text-xs text-slate-500"
              title="Read-only browser search served by the agent itself, on loopback 127.0.0.1:8686 by default. Off by default for an agent central has never reached. Fails closed when the agent's cached policy goes stale past its offline-grace window. This gates what is SERVED; the listener address is a host setting (FILEARR_AGENT_WEBUI_ADDR / _WEBUI_ALLOW_REMOTE).">
              Web UI
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                title="Read-only browser search served by the agent itself, on loopback 127.0.0.1:8686 by default. Off by default for an agent central has never reached. Fails closed when the agent's cached policy goes stale past its offline-grace window. This gates what is SERVED; the listener address is a host setting (FILEARR_AGENT_WEBUI_ADDR / _WEBUI_ALLOW_REMOTE)."
                bind:value={dialog.webUI}>
                <option value="">Inherit</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
            <label class="text-xs text-slate-500"
              title="Whether the local web UI demands the agent's bootstrap token before serving anything. Required is the default for an agent central has never reached. Never affects the CLI, which authenticates by peer credentials instead.">
              Web UI auth token
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                title="Whether the local web UI demands the agent's bootstrap token before serving anything. Required is the default for an agent central has never reached. Never affects the CLI, which authenticates by peer credentials instead."
                bind:value={dialog.authRequired}>
                <option value="">Inherit</option>
                <option value="on">Required</option>
                <option value="off">Not required</option>
              </select>
            </label>
            <label class="text-xs text-slate-500"
              title="The on-device `filearr query` socket — offline local search from the agent host's own shell. On by default. Unlike the web UI it keeps answering through a long disconnection; an explicit Off persists through offline periods too, because the policy is cached on the agent.">
              Local query API / CLI
              <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-2 text-sm dark:border-slate-700 dark:bg-slate-800"
                title="The on-device `filearr query` socket — offline local search from the agent host's own shell. On by default. Unlike the web UI it keeps answering through a long disconnection; an explicit Off persists through offline periods too, because the policy is cached on the agent."
                bind:value={dialog.localAccess}>
                <option value="">Inherit</option>
                <option value="on">On</option>
                <option value="off">Off</option>
              </select>
            </label>
          </div>
        </div>
        {/if}

        {@render sectionHead(
          "inventory",
          SETTINGS_SECTIONS[2].label,
          SETTINGS_SECTIONS[2].blurb,
        )}
        {#if openSections.inventory}
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex flex-wrap items-center gap-2">
            <label class="inline-flex items-center gap-2 text-sm"
              title="Master switch for host inventory collection (OS, hardware, installed packages, permissions) on this group's members. STORED AND DELIVERED BUT NOT ENFORCED: the collectors are agent-side scaffold and no shipped build acts on this yet.">
              <input type="checkbox" bind:checked={dialog.inventoryEnabled}
                title="Master switch for host inventory collection on this group's members. STORED AND DELIVERED BUT NOT ENFORCED: the collectors are agent-side scaffold and no shipped build acts on this yet." />
              Inventory collection enabled
            </label>
            {@render notEnforced("Central validates, stores and delivers these keys, but no shipped agent build reads them yet — the inventory collectors are agent-side scaffold. Authoring them now is safe and forward-looking; it changes nothing on the fleet today.")}
          </div>
          <!-- Collectors. The master switch above gates whether ANY of this runs,
               so the block dims (the page-family idiom — cf. AlertsPage's
               hash-only checkbox) and says so. It is deliberately NOT hard-
               disabled: this dialog's whole posture is that configuration can be
               authored inert and switched on later ("unticking keeps it authored
               but inert" on scan selections), and locking the list behind the
               master switch would force a fleet-visible toggle just to edit it. -->
          <div class="mt-3" class:opacity-50={!dialog.inventoryEnabled}>
            <div class="flex flex-wrap items-baseline gap-2">
              <span class="text-xs font-medium text-slate-500"
                title="Which inventory collectors this group's members should run. The list below is a CATALOGUE, not a whitelist: it is the union of the collectors this Filearr release can describe and every collector name your enrolled agents advertise. Central deliberately does not hard-code the vocabulary, so a newer agent build's collector still works — use 'add another' for a name nothing here knows yet. Max 64.">Collectors</span>
              {#if !dialog.inventoryEnabled}
                <span class="text-[11px] text-slate-400"
                  title="Inventory collection is switched off for this group, so nothing below runs. The selection is still saved — author it now and tick the master switch when you want it live.">inventory collection is off — this selection is saved but inert</span>
              {/if}
            </div>

            {#if collectorEditor.mode === "loading"}
              <p class="mt-1 text-xs text-slate-400"
                title="Fetching GET /agents/inventory-collectors — the shipped catalogue plus every collector your enrolled agents advertise. Only requested when this dialog opens, because it is admin-only and costs a query over the agents table.">Loading the collector catalogue…</p>
            {:else if collectorEditor.mode === "list"}
              <div class="mt-1 flex flex-col gap-1.5">
                {#each collectorEditor.choices as c (c.name)}
                  {@const st = collectorStanding(c)}
                  <label class="flex items-start gap-2 rounded-lg border border-slate-200 p-2 dark:border-slate-800"
                    title={st.note}>
                    <input type="checkbox" class="mt-1" checked={c.checked}
                      title={st.note}
                      onchange={(e) => setCollector(c.name, e.currentTarget.checked)} />
                    <span class="min-w-0">
                      <span class="flex flex-wrap items-center gap-1.5">
                        <span class="text-sm">{c.label}</span>
                        <code class="font-mono text-[11px] text-slate-500">{c.name}</code>
                        {#if st.kind === "active"}
                          <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                            title={st.note}>{c.advertisedBy} agent{c.advertisedBy === 1 ? "" : "s"}</span>
                        {:else if st.kind === "unmatched"}
                          <!-- Amber, not disabled: "nothing in your fleet reports
                               this" is a warning, never a prohibition — the host
                               that supports it may enroll tomorrow. -->
                          <span class="rounded-full bg-amber-100 px-1.5 py-0.5 text-[10px] font-medium text-amber-700 dark:bg-amber-900/40 dark:text-amber-300"
                            title={st.note}>{st.chip}</span>
                        {:else}
                          <span class="rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
                            title={st.note}>{st.chip}</span>
                        {/if}
                      </span>
                      <span class="mt-0.5 block text-xs text-slate-500">{c.description}</span>
                      {#if c.described}
                        <span class="mt-0.5 block text-[11px] text-slate-400"
                          title={st.note}>{c.platforms.length ? c.platforms.join(" · ") : "platform not recorded"} — cost: {c.cost}</span>
                      {/if}
                    </span>
                  </label>
                {/each}
              </div>

              {#if preservedUnknownCollectors(collectorEditor).length}
                <p class="mt-1.5 text-[11px] text-sky-700 dark:text-sky-300"
                  title="These names are stored on this group but are neither described by this Filearr release nor advertised by any enrolled agent. They are kept exactly as stored and re-sent on save — dropping them would silently discard configuration. Untick one to actually remove it.">
                  Kept from this group's saved settings, unrecognised here:
                  <code class="font-mono">{preservedUnknownCollectors(collectorEditor).join(", ")}</code>
                </p>
              {/if}

              <!-- Escape hatch: the checkbox list must not be a cage. Naming a
                   collector for an agent build that has not rolled out yet is a
                   legitimate thing to want, and central stores free strings. -->
              <div class="mt-2 flex flex-wrap items-center gap-2">
                <input
                  class="w-56 rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                  title="Add a collector name that is neither in this release's catalogue nor advertised by any enrolled agent — for example when pre-configuring a group for an agent build you have not rolled out yet. Stored verbatim; an agent that does not implement it simply ignores it. Max 128 characters."
                  placeholder="other-collector" bind:value={collectorAdd}
                  onkeydown={(e) => { if (e.key === "Enter") { e.preventDefault(); addCollector(); } }} />
                <button class="rounded border border-slate-300 px-2 py-1 text-xs dark:border-slate-700"
                  title="Add the typed name to the list as a ticked, unrecognised collector. Central never validates the vocabulary, so this always works; the name only does something on an agent build that implements it."
                  onclick={addCollector}>+ add another</button>
              </div>
              {#if collectorAddError}
                <p class="mt-1 text-xs text-red-600 dark:text-red-400">{collectorAddError}</p>
              {/if}
            {:else}
              <!-- Catalogue fetch failed. Falling back to the free-text field is
                   the honest degradation: an empty checkbox list would read as
                   "no collectors exist" and invite saving an emptied group. -->
              <p class="mt-1 rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
                title="GET /agents/inventory-collectors failed, so the console cannot show the checkbox list. Your stored collectors are unchanged and still editable as text below — nothing has been dropped. Reopen the dialog to retry.">
                Could not load the collector list ({collectorEditor.reason}) — editing
                as text instead. Known names: <code class="font-mono">stat</code>,
                <code class="font-mono">owner</code>, <code class="font-mono">perms</code>,
                <code class="font-mono">placeholder</code>, plus anything a newer agent
                build implements. Nothing has been dropped.
              </p>
              <label class="mt-1 block text-xs text-slate-500"
                title="Which inventory collectors to run, by name, comma or newline separated. Central deliberately does not hard-code the vocabulary — a name no agent implements is ignored, not rejected. Max 64 names. Naming `permissions` here is one of the two things the permissions block below needs.">
                Collectors (comma or newline separated)
                <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
                  title="Which inventory collectors to run, by name, comma or newline separated. Central deliberately does not hard-code the vocabulary — a name no agent implements is ignored, not rejected. Max 64 names."
                  placeholder="stat, owner, perms" bind:value={dialog.collectorsText} />
              </label>
            {/if}
          </div>

          <!-- W7 permissions collector (advanced, collapsed by default) -->
          <button
            class="mt-3 text-xs text-[var(--accent)]"
            onclick={() => (advancedOpen = !advancedOpen)}>
            {advancedOpen ? "▾" : "▸"} Advanced: permissions collector
            {dialog.permsConfigured ? "(configured)" : "(not configured)"}
          </button>
          {#if advancedOpen}
            <div class="mt-2 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              <p class="text-xs text-slate-500">
                Detailed knobs for the <code class="font-mono">permissions</code>
                collector. Only takes effect when <code class="font-mono">permissions</code>
                is <em>also</em> named in Collectors above — an admin must both name
                the collector and configure it. It has no checkbox of its own unless
                an agent advertises it; use <em>+ add another</em> to name it.
              </p>
              <label class="mt-2 inline-flex items-center gap-2 text-sm"
                title="Whether a permissions object is written into this group's settings at all. Unticked omits the whole block, which keeps 'never configured' distinguishable from 'configured, everything off'.">
                <input type="checkbox" bind:checked={dialog.permsConfigured} />
                Include a permissions block in this group's settings
              </label>
              {#if dialog.permsConfigured}
                <div class="mt-2 flex flex-col gap-1.5 text-sm">
                  <label class="inline-flex items-center gap-2"
                    title="Runs the permissions collector. Off by default — the block can be authored and staged before it is switched on.">
                    <input type="checkbox" bind:checked={dialog.permsEnabled} /> Enabled
                  </label>
                  <label class="inline-flex items-center gap-2"
                    title="Look each principal's display name up on the agent host. Best-effort: an unresolvable SID/uid is reported as-is rather than failing the collection. Costs a directory lookup per distinct principal.">
                    <input type="checkbox" bind:checked={dialog.permsResolveNames} />
                    Resolve principal names
                    <span class="text-xs text-slate-500">(best-effort SID/uid → display name)</span>
                  </label>
                  <label class="inline-flex items-center gap-2"
                    title="Report ACEs a path inherited from its parents as well as those set directly on it. Off keeps the report to explicit grants, which is what makes a first run readable; on produces a much larger report.">
                    <input type="checkbox" bind:checked={dialog.permsIncludeInherited} />
                    Include inherited ACEs
                    <span class="text-xs text-slate-500">(off = explicit grants only)</span>
                  </label>
                  <label class="inline-flex items-center gap-2"
                    title="Drop the baseline principals that appear on almost every path, so what is left is the grant someone actually made. On by default. Turn it off only when you are auditing the baseline itself.">
                    <input type="checkbox" bind:checked={dialog.permsExcludeWellKnown} />
                    Exclude well-known principals
                    <span class="text-xs text-slate-500">(SYSTEM, Administrators, root, Everyone, CREATOR OWNER)</span>
                  </label>
                  <label class="inline-flex items-center gap-2"
                    title="Also read the SMB share's own ACL, not just the filesystem's. Windows hosts only — a Linux or macOS agent has nothing to report here and skips it.">
                    <input type="checkbox" bind:checked={dialog.permsCollectShareAcls} />
                    Collect share-level ACLs
                    <span class="text-xs text-slate-500">(Windows only)</span>
                  </label>
                  <label class="inline-flex items-center gap-2"
                    title="Resolve each principal's net effective access rather than listing raw ACEs. Reserved for v2: central accepts and stores the flag, and the agent no-ops on it until the feature ships.">
                    <input type="checkbox" bind:checked={dialog.permsIncludeEffective} />
                    Include effective access
                    <span class="rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] font-medium text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                      title="Reserved for v2 of the permissions brief. Central accepts and stores it; the agent no-ops on it until the feature ships.">reserved v2</span>
                  </label>
                </div>
                <label class="mt-2 block text-xs text-slate-500"
                  title="Extra principals to drop from the report on top of the well-known list — typically backup and monitoring service accounts that hold access everywhere and drown out real findings. Canonical ids (a SID, or DOMAIN\user), max 64 entries of 128 characters.">
                  Exclude principals (comma or newline separated canonical ids; max 64)
                  <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
                    title="Extra principals to drop from the report on top of the well-known list — typically backup and monitoring service accounts that hold access everywhere. Canonical ids (a SID, or DOMAIN\user), max 64 entries of 128 characters."
                    placeholder="S-1-5-18, DOMAIN\\svc_backup" bind:value={dialog.permsExcludePrincipalsText} />
                </label>

                <div class="mt-3 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
                  <label class="inline-flex items-center gap-2 text-sm"
                    title="Whether an audit object is written into the permissions block at all. Unticked omits it entirely.">
                    <input type="checkbox" bind:checked={dialog.auditConfigured} />
                    Include a change-audit block
                  </label>
                  <p class="mt-0.5 text-xs text-slate-400">
                    Snapshot-diff + alert routing for watched paths. Stored ahead of
                    the collector; nothing consumes it yet.
                  </p>
                  {#if dialog.auditConfigured}
                    <div class="mt-2 flex flex-wrap items-center gap-4 text-sm">
                      <label class="inline-flex items-center gap-2"
                        title="Take a permissions snapshot of the watch paths on each collection and diff it against the previous one. Off by default.">
                        <input type="checkbox" bind:checked={dialog.auditEnabled} /> Enabled
                      </label>
                      <label class="inline-flex items-center gap-2"
                        title="Raise an alert when a diff is non-empty. Off means the snapshots are still taken and kept, just silently — useful while you learn what a normal week of churn looks like.">
                        <input type="checkbox" bind:checked={dialog.auditAlertOnChange} /> Alert on change
                      </label>
                      <label class="text-xs text-slate-500"
                        title="How many past snapshots to keep per watch path before the oldest is discarded. Bounds the disk this costs on the agent; more snapshots means a longer diff history to look back through. Default 10.">
                        Retain snapshots (1–{MAX_RETAIN_SNAPSHOTS})
                        <input type="number" min="1" max={MAX_RETAIN_SNAPSHOTS}
                          class="ml-1 w-24 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                          title="How many past snapshots to keep per watch path before the oldest is discarded. Bounds the disk this costs on the agent. Default 10."
                          bind:value={dialog.auditRetain} />
                      </label>
                    </div>
                    <label class="mt-2 block text-xs text-slate-500"
                      title="Which paths get snapshotted, one per line. Path specs — environment tokens (%USERPROFILE%, $HOME, ~) and globs are expanded ON THE AGENT, so a spec may resolve differently on each host and central cannot preview the result. Central only syntax-checks them. Max 200.">
                      Watch paths (one per line; max 200 — path specs, syntax-checked only)
                      <textarea rows="2"
                        class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
                        title="Which paths get snapshotted, one per line. Path specs — environment tokens (%USERPROFILE%, $HOME, ~) and globs are expanded ON THE AGENT, so a spec may resolve differently on each host. Central only syntax-checks them. Max 200."
                        bind:value={dialog.auditWatchPathsText}></textarea>
                    </label>
                  {/if}
                </div>
              {/if}
            </div>
          {/if}
        </div>

        {/if}

        {@render sectionHead(
          "selections",
          SETTINGS_SECTIONS[3].label,
          SETTINGS_SECTIONS[3].blurb,
        )}
        {#if openSections.selections}
        <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
          <div class="flex items-center gap-2">
            <span class="text-sm font-medium">Scan selections</span>
            <span class="rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-medium text-emerald-700 dark:bg-emerald-900/40 dark:text-emerald-300"
              title="Agents on this group derive their scan roots from these selections: every policy poll rewrites the agent's scan.json root list (its local roots editor is locked while a group manages roots). A bare Windows drive such as D: means the drive root. Agent build 2026-08-18 or newer.">drives scan roots</span>
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
                <label class="text-xs text-slate-500"
                  title="A predefined, per-OS folder set the AGENT resolves to real locations — Windows known folders (OneDrive-redirect aware), Linux XDG user-dirs, macOS user folders — with system files, thumbnails and caches excluded. Presets are why one selection works across a mixed fleet. (none) means this selection is defined purely by the path specs below.">Preset
                  <select class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
                    title="A predefined, per-OS folder set the AGENT resolves to real locations — Windows known folders (OneDrive-redirect aware), Linux XDG user-dirs, macOS user folders — with system files, thumbnails and caches excluded. (none) means this selection is defined purely by the path specs below."
                    bind:value={sel.preset}>
                    <option value="">(none)</option>
                    {#each SCAN_PRESET_NAMES as p}
                      <option value={p}>{p}</option>
                    {/each}
                  </select>
                </label>
                <label class="inline-flex items-center gap-2 text-sm"
                  title="Whether this selection counts. Unticking keeps it authored but inert, so a scaffold can be staged and switched on later instead of being deleted and retyped.">
                  <input type="checkbox" bind:checked={sel.enabled} /> enabled
                </label>
                <div class="grow"></div>
                <button class="text-xs text-red-600" onclick={() => dialog && (dialog.selections = dialog.selections.filter((_, j) => j !== i))}>remove</button>
              </div>
              <label class="mt-2 block text-xs text-slate-500"
                title="Extra locations for this selection, one per line. Environment tokens (%USERPROFILE%, $HOME, ~) and multi-user globs (/home/*/documents) are expanded ON THE AGENT, never centrally — so one spec covers every member of a mixed fleet, and central cannot show you what it will resolve to. Max 200 specs, 4096 characters each.">Path specs (one per line — env tokens / globs allowed)
                <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
                  title="Extra locations for this selection, one per line. Environment tokens (%USERPROFILE%, $HOME, ~) and multi-user globs (/home/*/documents) are expanded ON THE AGENT, never centrally. Max 200 specs, 4096 characters each."
                  placeholder={"%USERPROFILE%/Documents\n/home/*/documents"} bind:value={sel.pathsText}></textarea>
              </label>
              <div class="mt-2 flex flex-wrap gap-3">
                <label class="grow text-xs text-slate-500"
                  title="Only paths matching one of these expressions are kept, one per line. Empty means no include filter at all — everything the specs resolved that the excludes do not drop. Central compiles them with Python's re as a typo gate only; the agent's RE2 engine is the authority, so an exotic construct can pass here and behave differently there. Max 200.">Include regex (one per line)
                  <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
                    title="Only paths matching one of these expressions are kept, one per line. Empty means no include filter at all. Central compiles them with Python's re as a typo gate only; the agent's RE2 engine is the authority. Max 200."
                    bind:value={sel.includeText}></textarea>
                </label>
                <label class="grow text-xs text-slate-500"
                  title="Paths matching any of these are dropped, one per line, applied after the includes. Same typo-gate caveat: central syntax-checks with Python's re, the agent evaluates with RE2. Max 200.">Exclude regex (one per line)
                  <textarea rows="2" class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 font-mono text-xs dark:border-slate-700"
                    title="Paths matching any of these are dropped, one per line, applied after the includes. Same typo-gate caveat: central syntax-checks with Python's re, the agent evaluates with RE2. Max 200."
                    bind:value={sel.excludeText}></textarea>
                </label>
              </div>
            </div>
          {/each}
        </div>
        {/if}

        <!-- The POLICY half of the group document. Same catalogue the old
             standalone policy editor rendered (./agentPolicyDoc), now scoped to
             ONE group: every field is tri-state, and "Inherit (not set)" means
             this group contributes nothing for that key, so a lower-priority
             group — or the agent's built-in default — supplies it. -->
        {#each POLICY_SECTIONS as section (section.id)}
          {@render sectionHead(`policy:${section.id}`, section.label, section.blurb)}
          {#if openSections[`policy:${section.id}`]}
            <div class="rounded-lg border border-slate-200 p-3 dark:border-slate-800">
              {#each sectionFields(section.id) as f (f.key)}
                <div class="mt-3 grid gap-2 border-t border-slate-100 pt-3 first:mt-0 first:border-t-0 first:pt-0 md:grid-cols-[18rem_1fr] dark:border-slate-800/60">
                  <div>
                    <div class="flex flex-wrap items-center gap-1.5">
                      <span class="text-sm" title={fieldTitle(f)}>{f.label}</span>
                      {#if f.enforcedBy === "central"}
                        <span
                          class="rounded-full bg-sky-100 px-1.5 py-0.5 text-[10px] font-medium text-sky-700 dark:bg-sky-900/40 dark:text-sky-300"
                          title="Enforced by CENTRAL, not the agent: central applies this when the agent asks, so it takes effect for every agent build — including old ones.">
                          central-enforced
                        </span>
                      {/if}
                    </div>
                    <code class="text-[11px] text-slate-400" title={fieldTitle(f)}>{f.key}</code>
                    <p class="mt-0.5 text-xs text-slate-500" title={fieldTitle(f)}>{f.hint}</p>
                  </div>

                  <div>
                    {#if f.kind === "bool"}
                      <select
                        class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700 dark:bg-slate-800"
                        title={fieldTitle(f)}
                        value={boolMode(f.key)}
                        onchange={(e) => setBoolMode(f.key, e.currentTarget.value)}>
                        <option value="">Inherit (not set)</option>
                        <option value="true">On</option>
                        <option value="false">Off</option>
                      </select>
                    {:else}
                      <label
                        class="inline-flex items-center gap-2 text-xs text-slate-500"
                        title="Write this key into THIS group's policy section. Unticked leaves it absent, which is not the same as off — the key is then supplied by a lower-priority group, or falls back to {f.fallback}.">
                        <input
                          type="checkbox"
                          checked={dialog.policyForm[f.key]?.set ?? false}
                          onchange={(e) => setExplicit(f.key, e.currentTarget.checked)} />
                        set in this group
                      </label>
                      {#if dialog.policyForm[f.key]?.set}
                        {#if f.kind === "int"}
                          <input
                            type="number" min={f.min} max={f.max}
                            title={fieldTitle(f)}
                            class="mt-1 block w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                            value={dialog.policyForm[f.key].value}
                            oninput={(e) => setPolicyValue(f.key, e.currentTarget.value)} />
                        {:else if f.kind === "cron"}
                          <input
                            class="mt-1 block w-56 rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-slate-700"
                            title={fieldTitle(f)}
                            placeholder="0 3 * * *"
                            value={dialog.policyForm[f.key].value}
                            oninput={(e) => setPolicyValue(f.key, e.currentTarget.value)} />
                        {:else if f.kind === "text"}
                          <input
                            class="mt-1 block w-80 rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-slate-700"
                            title={fieldTitle(f)}
                            placeholder={f.placeholder ?? ""}
                            value={dialog.policyForm[f.key].value}
                            oninput={(e) => setPolicyValue(f.key, e.currentTarget.value)} />
                        {:else if f.kind === "presets"}
                          <div class="mt-1 flex flex-wrap gap-1">
                            {#each presetNames as p (p)}
                              {@const chosen = dialog.policyForm[f.key].value
                                .split(/[\n,]/)
                                .map((x) => x.trim())
                                .includes(p)}
                              <button type="button"
                                class="rounded-full border px-2 py-0.5 text-[11px] {chosen
                                  ? 'border-transparent bg-[var(--accent)] text-white'
                                  : 'border-slate-300 dark:border-slate-700'}"
                                onclick={() => {
                                  const cur = dialog!.policyForm[f.key].value
                                    .split(/[\n,]/)
                                    .map((x) => x.trim())
                                    .filter(Boolean);
                                  setPolicyValue(
                                    f.key,
                                    (chosen ? cur.filter((x) => x !== p) : [...cur, p]).join(", "),
                                  );
                                }}>{p}</button>
                            {/each}
                          </div>
                          <input
                            class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                            title={fieldTitle(f)}
                            placeholder="comma-separated preset names"
                            value={dialog.policyForm[f.key].value}
                            oninput={(e) => setPolicyValue(f.key, e.currentTarget.value)} />
                        {:else}
                          <textarea rows="3"
                            class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                            title={fieldTitle(f)}
                            placeholder="one per line"
                            value={dialog.policyForm[f.key].value}
                            oninput={(e) => setPolicyValue(f.key, e.currentTarget.value)}></textarea>
                        {/if}
                      {/if}
                    {/if}
                    {#if policyErrors[f.key]}
                      <p class="mt-1 text-xs text-red-600">{policyErrors[f.key]}</p>
                    {/if}
                    {#if !dialog.policyForm[f.key]?.set}
                      <p class="mt-1 text-[11px] text-slate-400">
                        Not set here → a lower-priority group supplies it, or {f.fallback}.
                      </p>
                    {/if}
                  </div>
                </div>
              {/each}
            </div>
          {/if}
        {/each}

        <!-- Advanced: the keys the form does not render. Both halves matter —
             the fixed keys explain why they have no field, and the raw view is
             the only way to author a forward-compat key an unshipped agent
             build reads. -->
        {@render sectionHead(
          "advanced",
          "Advanced: fixed and forward-compat policy keys",
          "Raw JSON for keys this console does not model.",
        )}
        {#if openSections.advanced}
          <div class="rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-800">
            <span class="text-sm font-medium">Fixed keys</span>
            <ul class="mt-1 space-y-1 text-slate-500">
              {#each Object.entries(RESERVED_POLICY_KEYS) as [key, why] (key)}
                <li>
                  <code class="font-mono text-slate-600 dark:text-slate-300">{key}</code> — {why}
                  {#if key in dialog.storedPolicy}
                    <span class="text-slate-400">(present in this group; preserved as-is)</span>
                  {/if}
                </li>
              {/each}
            </ul>

            {#if unknownKeys.length || unparsedKeys.length}
              <div class="mt-3 rounded-lg border border-slate-300 bg-slate-50 p-2 dark:border-slate-700 dark:bg-slate-900/40">
                {#if unknownKeys.length}
                  <p>
                    <b>{unknownKeys.length} key(s) this console does not model</b>
                    are in this group and are preserved untouched on save (the
                    policy schema allows forward-compat keys so a newer agent can
                    read them): <code class="font-mono">{unknownKeys.join(", ")}</code>.
                  </p>
                {/if}
                {#if unparsedKeys.length}
                  <p class="mt-1 text-amber-700 dark:text-amber-400">
                    <b>Unexpected value shape</b> for
                    <code class="font-mono">{unparsedKeys.join(", ")}</code> — the
                    fields above show these as “Inherit” but the stored value is
                    kept as-is. Fix them in the raw JSON below.
                  </p>
                {/if}
              </div>
            {/if}

            <button class="mt-3 rounded-lg border border-slate-300 px-3 py-1 dark:border-slate-700"
              onclick={openRaw}>{rawOpen ? "Hide" : "Show"} raw policy JSON</button>
            {#if rawOpen}
              <p class="mt-2 text-slate-500">
                The exact <code class="font-mono">policy</code> section that will be
                sent. Apply loads it back into the fields above; nothing is stored
                until you publish. The <code class="font-mono">settings</code>
                section is authored by the fields above only — it is typed and
                rejects unknown keys.
              </p>
              <textarea rows="10"
                class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                title="This group's whole policy section, including keys this console does not render (those round-trip untouched). Editing here is how you set a key the form has no field for."
                bind:value={rawText}></textarea>
              {#if rawError}<p class="mt-1 text-red-600">{rawError}</p>{/if}
              <button class="mt-1 rounded-lg border border-slate-300 px-3 py-1 dark:border-slate-700"
                onclick={applyRaw}>Apply JSON to the fields</button>
            {/if}
          </div>
        {/if}
      </div>

      <!-- Publish. Two buttons because they are two different acts, not one act
           with a checkbox: "apply now" makes the new version current for every
           member on its next poll; a phased rollout leaves current_version alone
           and widens coverage by tier, so cancelling it ROLLS BACK the agents
           already covered. -->
      <div class="mt-4 border-t border-slate-200 pt-3 dark:border-slate-800">
        {#if shadowedKeys.length}
          <!-- Layering preview, computed client-side because the draft is not
               saved yet. Amber rather than red: being overridden is a normal,
               intended state — it is only a surprise when nobody said so. -->
          <p class="mb-3 rounded-lg border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-300"
            title="These keys are set by a group with a HIGHER priority, which applies after this one. For any agent that is in both groups, that group's value wins. Agents in this group but not that one still get the value you set here.">
            <b>Overridden for agents that are also in a higher-priority group:</b>
            {#each shadowedKeys as s, i (s.key)}{i > 0 ? ", " : " "}<code class="font-mono">{s.key}</code> (by {s.by}){/each}
          </p>
        {/if}
        {#if dialog.id !== null}
          <label class="text-xs text-slate-500"
            title="Stored on the published snapshot and shown in the version history. The only place the reason for a change survives.">
            Change note (optional)
            <input class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-3 py-2 text-sm dark:border-slate-700"
              placeholder="why this change"
              bind:value={dialog.note} />
          </label>
        {/if}

        {#if rolloutOpen}
          <div class="mt-3 rounded-lg border border-sky-300 p-3 dark:border-sky-800">
            <div class="flex flex-wrap items-center gap-2">
              <span class="text-sm font-medium">Phased rollout</span>
              <span class="text-xs text-slate-500">
                up to {MAX_ROLLOUT_TIERS} tiers · percent of the fleet, and how long
                to wait after the previous tier
              </span>
              <div class="grow"></div>
              <button class="text-xs text-slate-500" onclick={() => (rolloutOpen = false)}>cancel rollout</button>
            </div>
            <p class="mt-1 text-xs text-slate-400">
              Coverage is picked by a stable hash of each agent's id, so an agent
              never flaps in and out between polls. Members outside the active
              tier keep receiving version {dialog.currentVersion} until the last
              tier completes.
            </p>
            {#each tiers as t, i (i)}
              <div class="mt-2 flex flex-wrap items-end gap-2">
                <span class="text-xs text-slate-500">Tier {i + 1}</span>
                <label class="text-xs text-slate-500">
                  percent
                  <input type="number" min="1" max="100"
                    class="mt-1 block w-24 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    title="Share of the fleet receiving the new version once this tier activates. Strictly ascending across tiers; the last tier must be 100."
                    bind:value={t.percent} />
                </label>
                <label class="text-xs text-slate-500">
                  delay (minutes)
                  <input type="number" min="0"
                    class="mt-1 block w-28 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                    title="How long to wait after the PREVIOUS tier activated before this one does. For tier 1 the wait counts from the rollout's start."
                    bind:value={t.delay_minutes} />
                </label>
                <button class="text-xs text-red-600"
                  onclick={() => (tiers = tiers.filter((_, j) => j !== i))}>remove</button>
                <!-- The running sum, because each row's number is a delay after
                     the PREVIOUS tier and operators reliably read it as "minutes
                     from the start". -->
                <span class="text-[11px] text-slate-400"
                  title="Cumulative wait from the rollout's start until this tier activates — the sum of every delay up to and including this row.">
                  ≈ {tierEtaMinutes(tiers, i)} min after start
                </span>
              </div>
            {/each}
            <div class="mt-2 flex flex-wrap items-end gap-3">
              <button class="rounded border border-slate-300 px-2 py-0.5 text-xs disabled:opacity-40 dark:border-slate-700"
                disabled={tiers.length >= MAX_ROLLOUT_TIERS}
                onclick={addTier}>+ add tier</button>
              <label class="text-xs text-slate-500"
                title="When the first tier activates. Blank starts at central's next minute tick. Interpreted in this browser's timezone and sent as UTC.">
                start at (optional)
                <input type="datetime-local"
                  class="mt-1 block rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                  bind:value={rolloutStartsAt} />
              </label>
            </div>
            {#if tierError}
              <p class="mt-2 text-xs text-red-600">{tierError}</p>
            {/if}
          </div>
        {/if}

        <div class="mt-3 flex flex-wrap items-center justify-end gap-2">
          <span class="mr-auto text-xs text-slate-400">
            {policyKeyCount} policy key(s) will be written for this group.
          </span>
          <button class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
            onclick={() => (dialog = null)}>Cancel</button>
          {#if dialog.id !== null && !rolloutOpen}
            <button class="rounded-lg border border-sky-400 px-3 py-1.5 text-sm text-sky-700 dark:border-sky-700 dark:text-sky-300"
              title="Publish the new version behind a tiered schedule instead of delivering it to every member at once."
              onclick={openRollout}>Save &amp; phased rollout…</button>
          {/if}
          {#if rolloutOpen}
            <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-50"
              disabled={dialogBusy || tierError !== null}
              onclick={() => saveGroup("rollout")}>
              {dialogBusy ? "Publishing…" : "Publish with rollout"}
            </button>
          {:else}
            <button class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-50"
              disabled={dialogBusy}
              title="Publish a new version immediately — every member receives it on its next poll (~1 min)."
              onclick={() => saveGroup("now")}>
              {dialogBusy ? "Saving…" : dialog.id === null ? "Create" : "Save & apply now"}
            </button>
          {/if}
        </div>
      </div>
      {/if}
    </div>
  </div>
{/if}
