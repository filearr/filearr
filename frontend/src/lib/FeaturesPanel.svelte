<script lang="ts">
  // Jobs page "Optional features" card: which optional subsystems are actually
  // ON right now, and where each one is turned on.
  //
  // WHY (live 2026-08-09): operators ran the semantic/chunking on-demand
  // maintenance tasks, each returned in 0 s, and nothing said why — the
  // features behind them were simply disabled. The queue stores no task
  // result, so the honest fix is to show the gate state up front.
  //
  // READ-ONLY on purpose: env-backed gates only take effect on restart, so a
  // toggle here would appear to work and silently not.
  import { systemFeatures, friendlyError, type SystemFeature } from "./api";

  let features = $state<SystemFeature[]>([]);
  let error = $state("");
  let loaded = $state(false);

  $effect(() => {
    void (async () => {
      try {
        features = (await systemFeatures()).features;
      } catch (e) {
        error = friendlyError(e, "load feature state");
      } finally {
        loaded = true;
      }
    })();
  });

  const ON = "bg-emerald-100 text-emerald-700 dark:bg-emerald-950 dark:text-emerald-300";
  const OFF = "bg-slate-100 text-slate-600 dark:bg-slate-800 dark:text-slate-300";

  /** The pill: a plain on/off for boolean gates, "3 of 12 libraries" for the
   *  per-library opt-ins, and the value itself where the feature IS a value. */
  function pill(f: SystemFeature): { text: string; cls: string } {
    if (f.value_gb != null)
      return {
        text: f.value_gb > 0 ? `${f.value_gb} GB` : "off",
        cls: f.value_gb > 0 ? ON : OFF,
      };
    if (f.scope === "libraries" && (f.count ?? 0) > 0)
      return { text: `${f.count} of ${f.total} libraries`, cls: ON };
    return { text: f.enabled ? "on" : "off", cls: f.enabled ? ON : OFF };
  }
</script>

<section class="rounded-2xl border border-slate-200 bg-white p-4 dark:border-slate-800 dark:bg-slate-900">
  <div class="mb-1 flex items-center gap-2">
    <h3
      class="mr-auto text-sm font-semibold text-slate-700 dark:text-slate-200"
      title="Optional subsystems and their current state. A maintenance task whose feature is off finishes instantly without doing anything — this card is where you find out why.">
      Optional features
    </h3>
  </div>
  <p class="mb-3 text-xs text-slate-500">
    Read-only. Environment settings apply at startup, so changing one needs a
    restart of the app and worker; per-library toggles apply immediately.
  </p>

  {#if error}
    <p class="text-sm text-red-600 dark:text-red-400">{error}</p>
  {:else if loaded && features.length === 0}
    <p class="text-sm text-slate-500">No feature state reported.</p>
  {:else}
    <div class="space-y-1.5">
      {#each features as f (f.key)}
        {@const p = pill(f)}
        <div class="flex flex-wrap items-baseline gap-2 text-xs">
          <span
            class="w-56 cursor-help font-medium text-slate-700 underline decoration-dotted decoration-slate-400 underline-offset-2 dark:text-slate-200"
            title={f.detail}>{f.title}</span>
          <span class="rounded-full px-1.5 py-0.5 text-[10px] font-medium {p.cls}">{p.text}</span>
          {#if f.scope === "libraries"}
            <span class="text-slate-400">per-library toggle in Libraries → library settings</span>
          {:else if f.env}
            <span class="text-slate-400">
              env: <code class="font-mono text-slate-500 dark:text-slate-400">{f.env}</code>
              <span
                class="cursor-help"
                title="Set it in ~/.config/filearr/env.overrides on a Proxmox deploy, or as the container template variable; then restart the app and worker.">
                — set via <code class="font-mono">~/.config/filearr/env.overrides</code> on a
                Proxmox deploy, or the container template variable</span>
            </span>
          {/if}
        </div>
      {/each}
    </div>
  {/if}
</section>
