<script lang="ts">
  // Library DIAGNOSIS dialog — "why is this library red on the Admin page?".
  // Calls GET /libraries/{id}/diagnose (backend/filearr/diagnose.py) and shows
  // the ordered verdicts first (severity badge, cause, what to do, evidence,
  // docs anchor), then the raw sections the verdicts were derived from, in
  // collapsible panels. "Copy report" puts the whole JSON on the clipboard so
  // the operator can paste it into an issue. All dynamic strings render as
  // text (no {@html}).
  import { onMount } from "svelte";
  import { diagnoseLibrary, type Library, type LibraryDiagnosis, type LibraryVerdict } from "./api";
  import { copyText } from "./clipboard";
  import DiagnoseSection from "./DiagnoseSection.svelte";

  let { library, onClose }: { library: Library; onClose: () => void } = $props();

  let report = $state<LibraryDiagnosis | null>(null);
  let loading = $state(false);
  let error = $state("");
  let copied = $state<"" | "ok" | "fail">("");
  let evidenceOpen = $state<Record<number, boolean>>({});

  async function run() {
    loading = true;
    error = "";
    copied = "";
    try {
      report = await diagnoseLibrary(library.id);
    } catch (e) {
      error = String(e);
    } finally {
      loading = false;
    }
  }
  onMount(run);

  async function copyReport() {
    if (!report) return;
    copied = (await copyText(JSON.stringify(report, null, 2))) ? "ok" : "fail";
    setTimeout(() => (copied = ""), 2500);
  }

  function onKey(e: KeyboardEvent) {
    if (e.key === "Escape") onClose();
  }

  const SEV: Record<LibraryVerdict["severity"], { badge: string; border: string; label: string }> = {
    error: {
      badge: "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300",
      border: "border-red-300 dark:border-red-800",
      label: "error",
    },
    warn: {
      badge: "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300",
      border: "border-amber-300 dark:border-amber-800",
      label: "warning",
    },
    info: {
      badge: "bg-slate-200 text-slate-600 dark:bg-slate-700 dark:text-slate-300",
      border: "border-slate-300 dark:border-slate-700",
      label: "info",
    },
    ok: {
      badge: "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300",
      border: "border-emerald-300 dark:border-emerald-800",
      label: "ok",
    },
  };
  const sev = (s: string) => SEV[s as LibraryVerdict["severity"]] ?? SEV.info;

  function statusStyle(s: string): string {
    if (s === "failed") return "bg-red-100 text-red-700 dark:bg-red-900/50 dark:text-red-300";
    if (s === "running") return "bg-blue-100 text-blue-700 dark:bg-blue-900/50 dark:text-blue-300";
    if (s === "stopped" || s === "cancelled")
      return "bg-amber-100 text-amber-700 dark:bg-amber-900/50 dark:text-amber-300";
    return "bg-emerald-100 text-emerald-700 dark:bg-emerald-900/50 dark:text-emerald-300";
  }
  const fmtTs = (s: string | null | undefined) => (s ? new Date(s).toLocaleString() : "—");
  const yn = (v: unknown) => (v === true ? "yes" : v === false ? "no" : v == null ? "unknown" : String(v));
  const stat = (st: Record<string, unknown>, k: string) => (st[k] == null ? "—" : String(st[k]));
  const nonEmpty = (o: Record<string, unknown> | null | undefined) => !!o && Object.keys(o).length > 0;
</script>

<svelte:window onkeydown={onKey} />

<div class="fixed inset-0 z-50 overflow-y-auto">
  <button
    type="button"
    class="absolute inset-0 h-full w-full cursor-default bg-black/50"
    aria-label="Close diagnosis"
    onclick={onClose}
  ></button>

  <div
    class="relative z-10 mx-auto my-8 w-full max-w-3xl rounded-2xl bg-white p-5 shadow-xl dark:bg-slate-900"
    role="dialog"
    aria-modal="true"
    aria-label="Library diagnosis">
    <div class="flex flex-wrap items-start justify-between gap-2">
      <div>
        <h3 class="text-base font-semibold">Diagnose: {library.name}</h3>
        <p class="mt-0.5 font-mono text-xs text-slate-500 break-all">{library.root_path}</p>
        {#if report}
          <p class="mt-0.5 text-xs text-slate-500">Generated {fmtTs(report.generated_at)}</p>
        {/if}
      </div>
      <div class="flex gap-2">
        <button
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs disabled:opacity-40 dark:border-slate-700"
          disabled={!report}
          onclick={copyReport}>
          {copied === "ok" ? "Copied" : copied === "fail" ? "Copy failed" : "Copy report"}
        </button>
        <button
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs disabled:opacity-40 dark:border-slate-700"
          disabled={loading}
          onclick={run}>Re-run</button>
        <button
          class="rounded-lg border border-slate-300 px-3 py-1.5 text-xs dark:border-slate-700"
          onclick={onClose}>Close</button>
      </div>
    </div>

    {#if loading}
      <p class="mt-4 flex items-center gap-2 text-sm text-slate-500">
        <span class="inline-block h-3 w-3 animate-spin rounded-full border-2 border-slate-400 border-t-transparent"></span>
        Probing path, reading scans, errors, jobs and logs…
      </p>
    {:else if error}
      <div class="mt-4 rounded-lg border border-red-300 bg-red-50 p-3 text-sm text-red-700 dark:border-red-800 dark:bg-red-900/30 dark:text-red-300">
        <p>Diagnosis failed: {error}</p>
        <button class="mt-2 rounded-lg border border-red-300 px-3 py-1 text-xs dark:border-red-700" onclick={run}>Retry</button>
      </div>
    {:else if report}
      <!-- Verdicts -->
      <ul class="mt-4 space-y-2">
        {#each report.verdicts as v, i (v.code + i)}
          {@const s = sev(v.severity)}
          <li class="rounded-lg border p-3 {s.border}">
            <div class="flex flex-wrap items-baseline gap-2">
              <span class="shrink-0 rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide {s.badge}">{s.label}</span>
              <span class="text-sm font-semibold">{v.title}</span>
              <a
                class="ml-auto text-xs text-[var(--accent)] hover:underline"
                href={v.doc}
                target="_blank"
                rel="noopener noreferrer"
                title="Open the troubleshooting docs for {v.code}">Docs ↗</a>
            </div>
            <p class="mt-1 text-sm text-slate-600 dark:text-slate-300 break-words">{v.detail}</p>
            {#if v.actions.length}
              <p class="mt-2 text-xs font-semibold text-slate-500">What to do</p>
              <ul class="ml-4 list-disc text-sm text-slate-600 dark:text-slate-300">
                {#each v.actions as a, j (j)}<li>{a}</li>{/each}
              </ul>
            {/if}
            {#if nonEmpty(v.evidence)}
              <button
                type="button"
                class="mt-2 text-xs text-slate-500 hover:underline"
                aria-expanded={!!evidenceOpen[i]}
                onclick={() => (evidenceOpen[i] = !evidenceOpen[i])}>
                {evidenceOpen[i] ? "Hide evidence" : "Evidence"}
              </button>
              {#if evidenceOpen[i]}
                <pre class="mt-1 max-h-64 overflow-auto rounded bg-slate-100 p-2 font-mono text-[11px] whitespace-pre-wrap dark:bg-slate-800">{JSON.stringify(v.evidence, null, 2)}</pre>
              {/if}
            {/if}
          </li>
        {/each}
      </ul>

      <!-- Raw sections -->
      <div class="mt-4 space-y-2">
        <DiagnoseSection title="Path" count={report.path.skipped ? "skipped" : yn(report.path.exists)}>
          <p class="font-mono break-all">{report.path.root_path}</p>
          {#if report.path.skipped}
            <p class="mt-1 text-slate-500">Skipped: {report.path.skipped} — the path belongs to the agent, not this container.</p>
          {:else}
            <dl class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
              <dt class="text-slate-500">exists</dt><dd>{yn(report.path.exists)}</dd>
              <dt class="text-slate-500">is dir</dt><dd>{yn(report.path.is_dir)}</dd>
              <dt class="text-slate-500">readable</dt><dd>{yn(report.path.readable)}</dd>
              <dt class="text-slate-500">network</dt><dd>{yn(report.path.network)}</dd>
              <dt class="text-slate-500">fstype</dt><dd>{report.path.fstype ?? "—"}</dd>
              <dt class="text-slate-500">listing</dt><dd>{report.path.listing_ms == null ? "—" : `${report.path.listing_ms} ms`}</dd>
              <dt class="text-slate-500">entries seen</dt><dd>{report.path.entries_seen ?? 0}</dd>
              <dt class="text-slate-500">empty</dt><dd>{yn(report.path.empty)}</dd>
            </dl>
            {#if report.path.error}
              <p class="mt-2 text-red-500 break-words">{report.path.error}</p>
            {/if}
            {#if report.path.sample?.length}
              <p class="mt-2 text-slate-500">Sample entries</p>
              <ul class="mt-1 flex flex-wrap gap-1">
                {#each report.path.sample as e (e.name)}
                  <li class="rounded bg-slate-100 px-1.5 py-0.5 font-mono dark:bg-slate-800">{e.dir ? "📁 " : ""}{e.name}</li>
                {/each}
              </ul>
            {/if}
          {/if}
        </DiagnoseSection>

        <DiagnoseSection title="Recent scans" count={report.scans.length}>
          {#if report.scans.length === 0}
            <p class="text-slate-500">No scans yet.</p>
          {:else}
            <div class="overflow-x-auto">
              <table class="w-full text-left">
                <thead class="text-slate-500">
                  <tr><th class="pr-2">status</th><th class="pr-2">started</th><th class="pr-2">duration</th><th class="pr-2">seen</th><th class="pr-2">new</th><th class="pr-2">changed</th><th class="pr-2">missing</th><th class="pr-2">excluded</th><th>error</th></tr>
                </thead>
                <tbody>
                  {#each report.scans as r (r.id)}
                    <tr class="border-t border-slate-100 align-top dark:border-slate-800">
                      <td class="pr-2 py-1"><span class="rounded px-1.5 py-0.5 text-[10px] font-semibold uppercase {statusStyle(r.status)}">{r.status}</span></td>
                      <td class="pr-2 py-1 whitespace-nowrap">{fmtTs(r.started_at)}</td>
                      <td class="pr-2 py-1">{r.duration_s == null ? "—" : `${r.duration_s} s`}</td>
                      <td class="pr-2 py-1">{stat(r.stats, "seen")}</td>
                      <td class="pr-2 py-1">{stat(r.stats, "new")}</td>
                      <td class="pr-2 py-1">{stat(r.stats, "changed")}</td>
                      <td class="pr-2 py-1">{stat(r.stats, "missing")}</td>
                      <td class="pr-2 py-1">{stat(r.stats, "excluded")}</td>
                      <td class="py-1 text-red-500 break-words">{r.error ?? ""}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </DiagnoseSection>

        <DiagnoseSection title="Extraction errors" count={report.extract_errors.count}>
          {#if report.extract_errors.count === 0}
            <p class="text-slate-500">No extraction errors.</p>
          {:else}
            <div class="flex flex-wrap gap-1">
              {#each Object.entries(report.extract_errors.by_kind) as [kind, n] (kind)}
                <span class="rounded bg-slate-200 px-1.5 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-slate-600 dark:bg-slate-700 dark:text-slate-300">{kind}: {n}</span>
              {/each}
            </div>
            <table class="mt-2 w-full text-left">
              <thead class="text-slate-500"><tr><th class="pr-2">count</th><th>message</th></tr></thead>
              <tbody>
                {#each report.extract_errors.top_messages as m, i (i)}
                  <tr class="border-t border-slate-100 align-top dark:border-slate-800">
                    <td class="pr-2 py-1">{m.count}</td>
                    <td class="py-1 font-mono break-words">{m.message}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <p class="mt-2 text-slate-500">The Errors column on the Admin page opens the per-item list.</p>
          {/if}
        </DiagnoseSection>

        <DiagnoseSection title="Failed jobs" count={report.failed_jobs.length}>
          {#if report.failed_jobs.length === 0}
            <p class="text-slate-500">No failed jobs reference this library.</p>
          {:else}
            <div class="overflow-x-auto">
              <table class="w-full text-left">
                <thead class="text-slate-500"><tr><th class="pr-2">task</th><th class="pr-2">queue</th><th class="pr-2">attempts</th><th class="pr-2">scheduled</th><th>error</th></tr></thead>
                <tbody>
                  {#each report.failed_jobs as j (j.id)}
                    <tr class="border-t border-slate-100 align-top dark:border-slate-800">
                      <td class="pr-2 py-1 font-mono">{j.task}</td>
                      <td class="pr-2 py-1">{j.queue}</td>
                      <td class="pr-2 py-1">{j.attempts ?? "—"}</td>
                      <td class="pr-2 py-1 whitespace-nowrap">{fmtTs(j.scheduled_at)}</td>
                      <td class="py-1 text-red-500 break-words">{j.error ?? ""}</td>
                    </tr>
                  {/each}
                </tbody>
              </table>
            </div>
          {/if}
        </DiagnoseSection>

        {#if report.agent}
          {@const a = report.agent}
          <DiagnoseSection title="Agent" count={a.missing ? "missing" : a.online ? "online" : "offline"}>
            {#if a.missing}
              <p class="text-red-500">Agent {String(a.id)} no longer exists.</p>
            {:else}
              <dl class="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
                <dt class="text-slate-500">name</dt><dd>{String(a.name ?? "—")}</dd>
                <dt class="text-slate-500">hostname</dt><dd>{String(a.hostname ?? "—")}</dd>
                <dt class="text-slate-500">platform</dt><dd>{String(a.platform ?? "—")}</dd>
                <dt class="text-slate-500">last seen</dt><dd>{fmtTs(a.last_seen_at as string | null)}</dd>
                <dt class="text-slate-500">online</dt><dd>{yn(a.online)}</dd>
                <dt class="text-slate-500">revoked</dt><dd>{yn(a.revoked)}</dd>
              </dl>
            {/if}
          </DiagnoseSection>
        {/if}

        <DiagnoseSection title="Logs" count={report.logs.length}>
          {#if report.logs.length === 0}
            <p class="text-slate-500">No warning-or-higher log lines mention this library.</p>
          {:else}
            <ul class="space-y-1 font-mono">
              {#each report.logs as l, i (i)}
                <li class="break-words whitespace-pre-wrap">
                  <span class="text-slate-500">{fmtTs(l.ts)}</span>
                  <span class={l.level === "ERROR" || l.level === "CRITICAL" ? "text-red-500" : "text-amber-600"}>{l.level}</span>
                  <span class="text-slate-500">{l.source}</span>
                  {l.message}
                </li>
              {/each}
            </ul>
          {/if}
        </DiagnoseSection>

        <DiagnoseSection title="Context">
          <dl class="grid grid-cols-2 gap-x-4 gap-y-1">
            <dt class="text-slate-500">recycle retention (days)</dt><dd>{String(report.context.recycle_retention_days ?? "—")}</dd>
            <dt class="text-slate-500">worker concurrency</dt><dd>{String(report.context.worker_concurrency ?? "—")}</dd>
          </dl>
        </DiagnoseSection>
      </div>
    {/if}
  </div>
</div>
