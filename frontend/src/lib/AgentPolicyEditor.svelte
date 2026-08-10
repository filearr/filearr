<script lang="ts">
  // Agent policy editor (2026-08-09) — the console surface for the append-only
  // agent-policy channel. Replaces the old three-toggle "Local access policy"
  // card, which only ever wrote three keys at the hardcoded `global` scope and
  // left every other PolicyModel key API-only.
  //
  // Three things this UI exists to make un-surprising:
  //
  //  1. **Scopes replace, they do not merge.** Resolution is most-specific-wins
  //     (agent > group:<rollout_group> > global) and the winner supplies the
  //     WHOLE document. Saving a narrow scope with one key therefore DROPS every
  //     key the broader scope was providing. The editor says so loudly and shows
  //     what the target inherits today.
  //  2. **Absent != false.** Every field is tri-state; "Inherit" omits the key.
  //  3. **Unknown keys must survive.** PolicyModel is extra="allow" and the row
  //     stores the body verbatim, so forward-compat keys round-trip untouched
  //     (see ./agentPolicyDoc.ts, which is unit-tested for exactly this).
  import { onMount } from "svelte";
  import {
    ApiError,
    getEffectivePolicy,
    listAgentPolicies,
    listAgentPolicyHistory,
    listPresets,
    putAgentPolicy,
    type AgentOut,
    type AgentPolicyRow,
    type EffectivePolicyOut,
  } from "./api";
  import {
    POLICY_FIELDS,
    POLICY_SECTIONS,
    RESERVED_POLICY_KEYS,
    SOURCE_LABELS,
    blankPolicyForm,
    buildPolicyDoc,
    describeScope,
    formFromDoc,
    passthroughFromDoc,
    unknownPolicyKeys,
    unparsedPolicyKeys,
    validatePolicyForm,
    type AgentPolicyDoc,
    type PolicyFieldSpec,
    type PolicyFormState,
  } from "./agentPolicyDoc";

  let { agents = [] }: { agents?: AgentOut[] } = $props();

  function errDetail(e: unknown): string {
    if (e instanceof ApiError) {
      try {
        const j = JSON.parse(e.body);
        if (j?.detail)
          return typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail);
      } catch {
        /* body not JSON */
      }
      return e.body || String(e);
    }
    return String(e);
  }

  // --- scope selection -------------------------------------------------------
  type ScopeKind = "global" | "group" | "agent";
  let scopeKind = $state<ScopeKind>("global");
  let groupName = $state("");
  let agentId = $state("");

  const scope = $derived(
    scopeKind === "global"
      ? "global"
      : scopeKind === "group"
        ? groupName.trim()
          ? `group:${groupName.trim()}`
          : ""
        : agentId
          ? `agent:${agentId}`
          : "",
  );

  // --- loaded state ----------------------------------------------------------
  let rows = $state<AgentPolicyRow[]>([]);
  let presetNames = $state<string[]>([]);
  let loadError = $state("");

  let form = $state<PolicyFormState>(blankPolicyForm());
  let passthrough = $state<Record<string, unknown>>({});
  let storedDoc = $state<AgentPolicyDoc | null>(null); // null = no doc at scope
  let storedVersion = $state<number | null>(null);
  let history = $state<AgentPolicyRow[]>([]);
  let historyOpen = $state<string | null>(null); // row id whose JSON is expanded
  let effective = $state<EffectivePolicyOut | null>(null);

  let saving = $state(false);
  let saveError = $state("");
  let saved = $state("");

  /** Rollout groups observed on the loaded agents page, plus any group that
   *  already has a policy document (so a group whose members are on another
   *  page — or none — stays reachable). Free text is still allowed. */
  const rolloutGroups = $derived(
    [
      ...new Set([
        ...agents.map((a) => a.rollout_group).filter(Boolean),
        ...rows.filter((r) => r.scope_type === "group").map((r) => r.scope_id ?? ""),
      ]),
    ]
      .filter(Boolean)
      .sort(),
  );

  const errors = $derived(validatePolicyForm(form, { knownPresets: presetNames }));
  const hasErrors = $derived(Object.keys(errors).length > 0);
  const draft = $derived(buildPolicyDoc(form, passthrough));
  const unknownKeys = $derived(unknownPolicyKeys(storedDoc));
  const unparsedKeys = $derived(unparsedPolicyKeys(storedDoc, form));
  const reservedPresent = $derived(
    Object.keys(RESERVED_POLICY_KEYS).filter((k) => storedDoc && k in storedDoc),
  );

  /** The document this scope's target would fall back to if the scope had no
   *  document — i.e. exactly what a save here REPLACES. */
  const inheritedFrom = $derived.by<{ scope: string; policy: AgentPolicyDoc } | null>(
    () => {
      const find = (s: string) => rows.find((r) => r.scope === s) ?? null;
      if (scopeKind === "global") return null;
      if (scopeKind === "group") {
        const g = find("global");
        return g ? { scope: g.scope, policy: g.policy } : null;
      }
      const agent = agents.find((a) => a.id === agentId);
      const parent =
        (agent ? find(`group:${agent.rollout_group}`) : null) ?? find("global");
      return parent ? { scope: parent.scope, policy: parent.policy } : null;
    },
  );

  /** Keys the broader scope supplies that this draft does NOT — they disappear
   *  the moment this narrower document is saved. The single most surprising
   *  consequence of whole-document replacement, so it gets its own callout. */
  const droppedKeys = $derived(
    inheritedFrom
      ? Object.keys(inheritedFrom.policy)
          .filter((k) => !(k in draft))
          .sort()
      : [],
  );

  onMount(async () => {
    try {
      presetNames = (await listPresets()).presets.map((p) => p.name);
    } catch {
      presetNames = []; // preset check degrades to server-side validation only
    }
    await reload();
  });

  async function reload() {
    loadError = "";
    try {
      rows = await listAgentPolicies();
    } catch (e) {
      loadError = errDetail(e);
      return;
    }
    await loadScope();
  }

  async function loadScope() {
    saveError = "";
    saved = "";
    historyOpen = null;
    const row = scope ? (rows.find((r) => r.scope === scope) ?? null) : null;
    storedDoc = row ? row.policy : null;
    storedVersion = row ? row.version : null;
    form = formFromDoc(storedDoc);
    passthrough = passthroughFromDoc(storedDoc, form);

    history = [];
    if (scope) {
      try {
        history = await listAgentPolicyHistory(scope, 10);
      } catch {
        /* history is a nicety — a failure must not block editing */
      }
    }

    effective = null;
    if (scopeKind === "agent" && agentId) {
      try {
        effective = await getEffectivePolicy(agentId);
      } catch (e) {
        saveError = `Could not load the effective policy: ${errDetail(e)}`;
      }
    }
  }

  function pickScopeKind(kind: ScopeKind) {
    scopeKind = kind;
    if (kind === "group" && !groupName) groupName = rolloutGroups[0] ?? "";
    if (kind === "agent" && !agentId) agentId = agents[0]?.id ?? "";
    loadScope();
  }

  // --- field editing ---------------------------------------------------------
  /** Bool tri-state select: "" = inherit, "true"/"false" = an explicit value. */
  function setBoolMode(key: string, mode: string) {
    if (mode === "") form[key] = { set: false, value: form[key]?.value ?? "" };
    else form[key] = { set: true, value: mode };
  }
  /** Inherit/explicit toggle for the non-bool kinds. Keeps the typed value while
   *  toggled off, so unticking and re-ticking does not wipe the operator's text
   *  (and an EMPTY explicit value stays explicit — for a list field that is a
   *  meaningful "[]", not "inherit"). */
  function setExplicit(key: string, on: boolean) {
    form[key] = { set: on, value: form[key]?.value ?? "" };
  }
  function setValue(key: string, value: string) {
    form[key] = { set: true, value };
  }
  function boolMode(key: string): string {
    const s = form[key];
    return s?.set ? s.value : "";
  }

  function sectionFields(section: string): PolicyFieldSpec[] {
    return POLICY_FIELDS.filter((f) => f.section === section);
  }

  /** Hover text for a field's label and its control. Same words as the visible
   *  hint (which is small grey text some operators hover rather than read),
   *  plus the key and the absent-key behaviour — the two things an operator has
   *  to know before deciding whether to set it at all. */
  function fieldTitle(f: PolicyFieldSpec): string {
    return `${f.label} (${f.key}) — ${f.hint} Not set → ${f.fallback}.`;
  }

  function fmtValue(v: unknown): string {
    if (v === undefined) return "—";
    if (Array.isArray(v) || (v && typeof v === "object")) return JSON.stringify(v);
    return String(v);
  }

  // --- save ------------------------------------------------------------------
  async function save() {
    if (!scope || hasErrors || saving) return;
    const target = describeScope(scope);
    if (scopeKind !== "global") {
      const extra = droppedKeys.length
        ? `\n\nThese keys are currently supplied by ${describeScope(
            inheritedFrom?.scope ?? "global",
          )} and will STOP applying to this target:\n  ${droppedKeys.join(", ")}`
        : "";
      if (
        !confirm(
          `Save a policy document for ${target}?\n\n` +
            "Policy scopes REPLACE each other — they do not merge. This document " +
            "becomes the complete policy for the target; anything not set here " +
            "falls back to the agent's built-in default, NOT to the broader scope." +
            extra,
        )
      )
        return;
    }
    saving = true;
    saveError = "";
    saved = "";
    try {
      const row = await putAgentPolicy(scope, draft as Record<string, unknown>);
      rows = await listAgentPolicies();
      await loadScope(); // re-seed from server truth (and clears `saved`)…
      saved = `Saved version ${row.version}.`; // …so the confirmation goes last
    } catch (e) {
      saveError = errDetail(e); // surfaces the server's 422 detail verbatim
    } finally {
      saving = false;
    }
  }

  function revert() {
    form = formFromDoc(storedDoc);
    passthrough = passthroughFromDoc(storedDoc, form);
    saveError = "";
    saved = "";
    rawError = "";
    rawText = "";
  }

  // --- raw JSON escape hatch -------------------------------------------------
  let rawOpen = $state(false);
  let rawText = $state("");
  let rawError = $state("");

  function openRaw() {
    rawOpen = !rawOpen;
    if (rawOpen) {
      rawText = JSON.stringify(draft, null, 2);
      rawError = "";
    }
  }
  function applyRaw() {
    rawError = "";
    let parsed: unknown;
    try {
      parsed = JSON.parse(rawText);
    } catch (e) {
      rawError = `Not valid JSON: ${String(e)}`;
      return;
    }
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) {
      rawError = "The policy must be a JSON object.";
      return;
    }
    const doc = parsed as AgentPolicyDoc;
    form = formFromDoc(doc);
    passthrough = passthroughFromDoc(doc, form);
    rawOpen = false;
  }
</script>

<div class="mt-8 rounded-xl border border-slate-200 p-4 dark:border-slate-800">
  <div class="flex flex-wrap items-center gap-3">
    <h3 class="font-medium">Agent policy</h3>
    <span class="text-xs text-slate-500">
      append-only, per-scope documents delivered on the agents' next poll
    </span>
    <div class="grow"></div>
    <button
      class="rounded-lg border border-slate-300 px-3 py-1 text-xs text-slate-600 dark:border-slate-700 dark:text-slate-300"
      onclick={reload}>Reload</button>
  </div>

  <p class="mt-1 text-xs text-slate-500">
    Resolution is <b>most-specific-wins</b>: an agent document outranks its
    rollout-group document, which outranks the global one — and the winner
    supplies the <b>whole document</b>. There is no key merging: a key absent
    from the winning document falls back to the agent's built-in default, never
    to the broader scope. Writes are append-only versions; nothing is ever
    mutated in place.
  </p>

  {#if loadError}
    <p class="mt-2 text-sm text-red-600">Could not load policies: {loadError}</p>
  {/if}

  <!-- Scope selector -->
  <div class="mt-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
    <div class="flex flex-wrap items-center gap-2">
      <span
        class="text-xs font-medium text-slate-500"
        title="Which document you are editing. Resolution is most-specific-wins with NO key merging: agent beats rollout group beats global, and the winning document supplies the agent's WHOLE policy.">
        Scope
      </span>
      {#each [["global", "Global"], ["group", "Rollout group"], ["agent", "Specific agent"]] as [kind, label] (kind)}
        <button
          type="button"
          title={kind === "global"
            ? "The fleet-wide document. Applies to every agent that has no rollout-group or per-agent document of its own."
            : kind === "group"
              ? "One document for every agent whose ENROLLMENT put it in this rollout group. Not the same thing as a configuration group. Outranks global for those agents."
              : "One document for one agent. Outranks both other scopes — and replaces them outright, so it must carry every key that agent needs."}
          class="rounded-full border px-3 py-1 text-xs {scopeKind === kind
            ? 'border-transparent bg-[var(--accent)] text-white'
            : 'border-slate-300 dark:border-slate-700'}"
          onclick={() => pickScopeKind(kind as ScopeKind)}>{label}</button>
      {/each}

      {#if scopeKind === "group"}
        <input
          list="filearr-rollout-groups"
          class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700"
          title="Rollout group name, assigned at enrollment. Existing groups are offered as suggestions; a name that matches no agent stores a document that nothing reads yet."
          placeholder="rollout group name"
          value={groupName}
          onchange={(e) => {
            groupName = (e.currentTarget as HTMLInputElement).value;
            loadScope();
          }} />
        <datalist id="filearr-rollout-groups">
          {#each rolloutGroups as g (g)}<option value={g}></option>{/each}
        </datalist>
      {:else if scopeKind === "agent"}
        <select
          class="rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-xs dark:border-slate-700 dark:bg-slate-800"
          title="The one agent this document applies to. Saving here stops the global and rollout-group documents from applying to it entirely — this document must then carry every key it needs."
          value={agentId}
          onchange={(e) => {
            agentId = (e.currentTarget as HTMLSelectElement).value;
            loadScope();
          }}>
          <option value="">(choose an agent)</option>
          {#each agents as a (a.id)}
            <option value={a.id}>{a.name} — {a.hostname} ({a.rollout_group})</option>
          {/each}
        </select>
        <span class="text-[11px] text-slate-400">
          Only agents on the current page of the table below are listed.
        </span>
      {/if}
    </div>

    <p class="mt-2 text-xs text-slate-500">
      {#if !scope}
        Pick a target to edit.
      {:else if storedDoc === null}
        <b>{describeScope(scope)}</b> — no document at this scope. It inherits
        {#if inheritedFrom}
          from <b>{describeScope(inheritedFrom.scope)}</b> today.
        {:else}
          the agents' built-in defaults today.
        {/if}
        Saving creates version 1 here.
      {:else}
        <b>{describeScope(scope)}</b> — current version {storedVersion}, with
        {Object.keys(storedDoc).length} key(s).
      {/if}
    </p>
  </div>

  {#if scope}
    <!-- Wholesale-replacement warning (narrow scopes only) -->
    {#if scopeKind !== "global"}
      <div
        class="mt-3 rounded-lg border border-amber-400 bg-amber-50 p-3 text-xs dark:border-amber-700 dark:bg-amber-950/30">
        <p class="font-medium text-amber-800 dark:text-amber-300">
          This scope replaces — it does not merge.
        </p>
        <p class="mt-1 text-slate-600 dark:text-slate-300">
          Whatever you save here becomes the target's <em>entire</em> policy. Any
          key you leave on “Inherit” is simply absent, so the agent uses its
          built-in default for it — it does <b>not</b> fall through to
          {inheritedFrom ? describeScope(inheritedFrom.scope) : "the global document"}.
          To keep a broader value, set it explicitly here too.
        </p>
        {#if droppedKeys.length}
          <p class="mt-2 text-slate-700 dark:text-slate-200">
            Saving now would stop applying
            <b>{droppedKeys.length}</b> key(s) currently supplied by
            <b>{describeScope(inheritedFrom?.scope ?? "global")}</b>:
            <code class="font-mono">{droppedKeys.join(", ")}</code>
          </p>
        {/if}
      </div>
    {/if}

    {#if effective}
      <p class="mt-3 text-xs text-slate-500">
        <b>Effective now</b> for this agent: winning document
        <b>{describeScope(effective.scope)}</b>
        {#if effective.version}(version {effective.version}){/if}
        {#if effective.group}
          · config group <b>{effective.group.name}</b>
        {:else}
          · no config group
        {/if}
        . The “effective” column below shows the value the agent actually has and
        where it came from.
      </p>
    {/if}

    {#if saveError}
      <p
        class="mt-3 rounded-lg border border-red-300 bg-red-50 p-2 text-sm text-red-700 dark:border-red-800 dark:bg-red-950/40 dark:text-red-300">
        {saveError}
      </p>
    {/if}
    {#if saved}<p class="mt-3 text-sm text-emerald-600">{saved}</p>{/if}

    <!-- Grouped fields -->
    {#each POLICY_SECTIONS as section (section.id)}
      <div class="mt-4 rounded-lg border border-slate-200 p-3 dark:border-slate-800">
        <div class="flex flex-wrap items-baseline gap-2">
          <span class="text-sm font-medium">{section.label}</span>
          <span class="text-xs text-slate-500">{section.blurb}</span>
        </div>

        {#each sectionFields(section.id) as f (f.key)}
          <div
            class="mt-3 grid gap-2 border-t border-slate-100 pt-3 md:grid-cols-[18rem_1fr_14rem] dark:border-slate-800/60">
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
                  onchange={(e) =>
                    setBoolMode(f.key, (e.currentTarget as HTMLSelectElement).value)}>
                  <option value="">Inherit (not set)</option>
                  <option value="true">On</option>
                  <option value="false">Off</option>
                </select>
              {:else}
                <label
                  class="inline-flex items-center gap-2 text-xs text-slate-500"
                  title="Write this key into the document at the selected scope. Unticked leaves it absent, which is not the same as off — {f.label} then falls back to {f.fallback}.">
                  <input
                    type="checkbox"
                    checked={form[f.key]?.set ?? false}
                    onchange={(e) =>
                      setExplicit(f.key, (e.currentTarget as HTMLInputElement).checked)} />
                  set explicitly
                </label>
                {#if form[f.key]?.set}
                  {#if f.kind === "int"}
                    <input
                      type="number"
                      min={f.min}
                      max={f.max}
                      title={fieldTitle(f)}
                      class="mt-1 block w-48 rounded-lg border border-slate-300 bg-transparent px-2 py-1 text-sm dark:border-slate-700"
                      value={form[f.key].value}
                      oninput={(e) =>
                        setValue(f.key, (e.currentTarget as HTMLInputElement).value)} />
                  {:else if f.kind === "cron"}
                    <input
                      class="mt-1 block w-56 rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-sm dark:border-slate-700"
                      title={fieldTitle(f)}
                      placeholder="0 3 * * *"
                      value={form[f.key].value}
                      oninput={(e) =>
                        setValue(f.key, (e.currentTarget as HTMLInputElement).value)} />
                  {:else if f.kind === "presets"}
                    <div class="mt-1 flex flex-wrap gap-1">
                      {#each presetNames as p (p)}
                        {@const chosen = form[f.key].value
                          .split(/[\n,]/)
                          .map((x) => x.trim())
                          .includes(p)}
                        <button
                          type="button"
                          class="rounded-full border px-2 py-0.5 text-[11px] {chosen
                            ? 'border-transparent bg-[var(--accent)] text-white'
                            : 'border-slate-300 dark:border-slate-700'}"
                          onclick={() => {
                            const cur = form[f.key].value
                              .split(/[\n,]/)
                              .map((x) => x.trim())
                              .filter(Boolean);
                            const next = chosen
                              ? cur.filter((x) => x !== p)
                              : [...cur, p];
                            setValue(f.key, next.join(", "));
                          }}>{p}</button>
                      {/each}
                    </div>
                    <input
                      class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                      title={fieldTitle(f)}
                      placeholder="comma-separated preset names"
                      value={form[f.key].value}
                      oninput={(e) =>
                        setValue(f.key, (e.currentTarget as HTMLInputElement).value)} />
                  {:else}
                    <textarea
                      rows="3"
                      class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
                      title={fieldTitle(f)}
                      placeholder="one per line"
                      value={form[f.key].value}
                      oninput={(e) =>
                        setValue(
                          f.key,
                          (e.currentTarget as HTMLTextAreaElement).value,
                        )}></textarea>
                  {/if}
                {/if}
              {/if}
              {#if errors[f.key]}
                <p class="mt-1 text-xs text-red-600">{errors[f.key]}</p>
              {/if}
              {#if !form[f.key]?.set}
                <p class="mt-1 text-[11px] text-slate-400">
                  Not set → {f.fallback}.
                </p>
              {/if}
            </div>

            <div class="text-xs">
              {#if effective}
                <span class="text-slate-400">effective now</span>
                <div class="font-mono break-all text-slate-600 dark:text-slate-300">
                  {fmtValue(effective.policy[f.key])}
                </div>
                <span
                  class="mt-0.5 inline-block rounded-full bg-slate-100 px-1.5 py-0.5 text-[10px] text-slate-600 dark:bg-slate-800 dark:text-slate-300"
                  title="Which document supplied this value. Remember the winning document supplies ALL of its keys — there is no per-key merging.">
                  {SOURCE_LABELS[effective.source_keys[f.key]] ??
                    effective.source_keys[f.key] ??
                    "agent default"}
                </span>
              {:else}
                <span class="text-slate-300 dark:text-slate-600">
                  select a specific agent to see its effective value
                </span>
              {/if}
            </div>
          </div>
        {/each}
      </div>
    {/each}

    <!-- Reserved / non-editable keys -->
    <div class="mt-4 rounded-lg border border-slate-200 p-3 text-xs dark:border-slate-800">
      <span class="text-sm font-medium">Fixed keys</span>
      <ul class="mt-1 space-y-1 text-slate-500">
        {#each Object.entries(RESERVED_POLICY_KEYS) as [key, why] (key)}
          <li>
            <code class="font-mono text-slate-600 dark:text-slate-300">{key}</code>
            — {why}
            {#if reservedPresent.includes(key)}
              <span class="text-slate-400">(present in this document; preserved as-is)</span>
            {/if}
          </li>
        {/each}
      </ul>
    </div>

    <!-- Forward-compat keys -->
    {#if unknownKeys.length || unparsedKeys.length}
      <div
        class="mt-3 rounded-lg border border-slate-300 bg-slate-50 p-3 text-xs dark:border-slate-700 dark:bg-slate-900/40">
        {#if unknownKeys.length}
          <p>
            <b>{unknownKeys.length} key(s) this console does not model</b> are in
            this document and are preserved untouched on save (the policy schema
            allows forward-compat keys so a newer agent can read them):
            <code class="font-mono">{unknownKeys.join(", ")}</code>. Edit them in
            the raw JSON view below.
          </p>
        {/if}
        {#if unparsedKeys.length}
          <p class="mt-1 text-amber-700 dark:text-amber-400">
            <b>Unexpected value shape</b> for
            <code class="font-mono">{unparsedKeys.join(", ")}</code> — the form
            shows these as “Inherit” but the stored value is kept as-is. Fix them
            in the raw JSON view.
          </p>
        {/if}
      </div>
    {/if}

    <!-- Raw JSON escape hatch -->
    <div class="mt-3">
      <button
        class="rounded-lg border border-slate-300 px-3 py-1 text-xs dark:border-slate-700"
        onclick={openRaw}>
        {rawOpen ? "Hide" : "Show"} raw JSON document
      </button>
      {#if rawOpen}
        <p class="mt-2 text-xs text-slate-500">
          The exact body that will be sent as
          <code class="font-mono">PUT /api/v1/agent-policies/{scope}</code>. Apply
          to load it back into the form.
        </p>
        <textarea
          rows="12"
          class="mt-1 block w-full rounded-lg border border-slate-300 bg-transparent px-2 py-1 font-mono text-xs dark:border-slate-700"
          title="The whole policy document, including keys this console does not render (those round-trip untouched). Editing here is how you set a key the form has no field for. Apply to load it back into the form; nothing is stored until you Save."
          bind:value={rawText}></textarea>
        {#if rawError}<p class="mt-1 text-xs text-red-600">{rawError}</p>{/if}
        <button
          class="mt-1 rounded-lg border border-slate-300 px-3 py-1 text-xs dark:border-slate-700"
          onclick={applyRaw}>Apply JSON to form</button>
      {/if}
    </div>

    <!-- Save -->
    <div class="mt-4 flex flex-wrap items-center gap-2">
      <button
        class="rounded-lg bg-[var(--accent)] px-3 py-1.5 text-sm text-white disabled:opacity-50"
        disabled={saving || hasErrors || !scope}
        onclick={save}>
        {saving ? "Saving…" : `Save new version for ${describeScope(scope)}`}
      </button>
      <button
        class="rounded-lg border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700"
        onclick={revert}>Revert</button>
      {#if hasErrors}
        <span class="text-xs text-red-600">Fix the highlighted fields first.</span>
      {/if}
      <span class="text-xs text-slate-400">
        {Object.keys(draft).length} key(s) will be written.
      </span>
    </div>

    <!-- History -->
    <div class="mt-5 border-t border-slate-200 pt-3 dark:border-slate-800">
      <span class="text-sm font-medium">Recent versions</span>
      <span class="ml-2 text-xs text-slate-500">read-only, newest first</span>
      {#if history.length === 0}
        <p class="mt-1 text-xs text-slate-400">No versions written at this scope yet.</p>
      {:else}
        <table class="mt-1 w-full text-xs">
          <thead class="text-left text-slate-500">
            <tr>
              <th class="py-1 pr-3 font-medium">Version</th>
              <th class="py-1 pr-3 font-medium">Actor</th>
              <th class="py-1 pr-3 font-medium">Written</th>
              <th class="py-1 font-medium">Keys</th>
            </tr>
          </thead>
          <tbody>
            {#each history as h (h.id)}
              <tr class="border-t border-slate-100 dark:border-slate-800/60">
                <td class="py-1 pr-3 tabular-nums">{h.version}</td>
                <td class="py-1 pr-3 text-slate-500">{h.actor ?? "—"}</td>
                <td class="py-1 pr-3 text-slate-500">
                  {new Date(h.created_at).toLocaleString()}
                </td>
                <td class="py-1">
                  {Object.keys(h.policy).length}
                  <button
                    class="ml-2 text-[var(--accent)]"
                    onclick={() => (historyOpen = historyOpen === h.id ? null : h.id)}>
                    {historyOpen === h.id ? "hide" : "view JSON"}
                  </button>
                </td>
              </tr>
              {#if historyOpen === h.id}
                <tr>
                  <td colspan="4" class="pb-2">
                    <pre
                      class="max-h-64 overflow-auto rounded bg-slate-900/90 p-2 font-mono text-[11px] text-slate-100">{JSON.stringify(
                        h.policy,
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
  {/if}
</div>
