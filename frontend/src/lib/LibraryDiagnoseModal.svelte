<script lang="ts">
  // Library DIAGNOSIS dialog — "why is this library red on the Libraries page?".
  // Calls GET /libraries/{id}/diagnose (backend/filearr/diagnose.py) and shows
  // the ordered verdicts first (severity badge, cause, what to do, evidence,
  // docs anchor), then the raw sections the verdicts were derived from, in
  // collapsible panels. "Copy report" puts the whole JSON on the clipboard so
  // the operator can paste it into an issue. All dynamic strings render as
  // text (no {@html}).
  import { onMount } from "svelte";
  import { diagnoseLibrary, type Library, type LibraryDiagnosis, type LibraryVerdict, errorMessage } from "./api";
  import { copyText } from "./clipboard";
  import DiagnoseSection from "./DiagnoseSection.svelte";
  import { Badge, Button, Modal, StateBlock, type Tone } from "./ui";

  let { library, onClose }: { library: Library; onClose: () => void } = $props();

  // Mount-style: the parent unmounts this from onClose.
  let open = $state(true);
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
      error = errorMessage(e);
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

  const SEV: Record<LibraryVerdict["severity"], { tone: Tone; border: string; label: string }> = {
    error: { tone: "danger", border: "border-danger/40", label: "error" },
    warn: { tone: "warn", border: "border-warn/40", label: "warning" },
    info: { tone: "neutral", border: "border-border", label: "info" },
    ok: { tone: "ok", border: "border-ok/40", label: "ok" },
  };
  const sev = (s: string) => SEV[s as LibraryVerdict["severity"]] ?? SEV.info;

  // Same colours as before: finished is ok, a stopped/cancelled run a warning.
  function scanTone(s: string): Tone {
    if (s === "failed") return "danger";
    if (s === "running") return "info";
    if (s === "stopped" || s === "cancelled") return "warn";
    return "ok";
  }
  const fmtTs = (s: string | null | undefined) => (s ? new Date(s).toLocaleString() : "—");
  const yn = (v: unknown) => (v === true ? "yes" : v === false ? "no" : v == null ? "unknown" : String(v));
  const stat = (st: Record<string, unknown>, k: string) => (st[k] == null ? "—" : String(st[k]));
  const nonEmpty = (o: Record<string, unknown> | null | undefined) => !!o && Object.keys(o).length > 0;
</script>

<Modal bind:open title="Diagnose: {library.name}" size="xl" {onClose}>
  <div class="flex flex-wrap items-start justify-between gap-2">
    <div class="min-w-0">
      <p class="font-mono text-xs text-muted break-all">{library.root_path}</p>
      {#if report}
        <p class="mt-0.5 text-xs text-muted">Generated {fmtTs(report.generated_at)}</p>
      {/if}
    </div>
    <div class="flex gap-2">
      <Button size="sm" disabled={!report} onclick={copyReport}>
        {copied === "ok" ? "Copied" : copied === "fail" ? "Copy failed" : "Copy report"}
      </Button>
      <Button size="sm" disabled={loading} onclick={run}>Re-run</Button>
    </div>
  </div>

  {#if loading}
    <StateBlock state="loading" message="Probing path, reading scans, errors, jobs and logs…" />
  {:else if error}
    <StateBlock state="error" message="Diagnosis failed: {error}" onRetry={run} />
  {:else if report}
    <!-- Verdicts -->
    <ul class="mt-4 space-y-2">
      {#each report.verdicts as v, i (v.code + i)}
        {@const s = sev(v.severity)}
        <li class="rounded-control border p-3 {s.border}">
          <div class="flex flex-wrap items-baseline gap-2">
            <Badge tone={s.tone} class="shrink-0 uppercase tracking-wide">{s.label}</Badge>
            <span class="text-sm font-semibold">{v.title}</span>
            <a
              class="ml-auto text-xs text-accent-text hover:underline"
              href={v.doc}
              target="_blank"
              rel="noopener noreferrer"
              title="Open the troubleshooting docs for {v.code}">Docs ↗</a>
          </div>
          <p class="mt-1 text-sm text-muted break-words">{v.detail}</p>
          {#if v.actions.length}
            <p class="mt-2 text-xs font-semibold text-muted">What to do</p>
            <ul class="ml-4 list-disc text-sm text-muted">
              {#each v.actions as a, j (j)}<li>{a}</li>{/each}
            </ul>
          {/if}
          {#if nonEmpty(v.evidence)}
            <button
              type="button"
              class="mt-2 text-xs text-muted hover:underline"
              aria-expanded={!!evidenceOpen[i]}
              onclick={() => (evidenceOpen[i] = !evidenceOpen[i])}>
              {evidenceOpen[i] ? "Hide evidence" : "Evidence"}
            </button>
            {#if evidenceOpen[i]}
              <pre class="mt-1 max-h-64 overflow-auto rounded-control bg-surface-3 p-2 font-mono text-[11px] whitespace-pre-wrap">{JSON.stringify(v.evidence, null, 2)}</pre>
            {/if}
          {/if}
        </li>
      {/each}
    </ul>

    <!-- Raw sections. The three tables stay raw (nested detail inside a
         collapsible section): sr-only caption + scope="col". -->
    <div class="mt-4 space-y-2">
      <DiagnoseSection title="Path" count={report.path.skipped ? "skipped" : yn(report.path.exists)}>
        <p class="font-mono break-all">{report.path.root_path}</p>
        {#if report.path.skipped}
          <p class="mt-1 text-muted">Skipped: {report.path.skipped} — the path belongs to the agent, not this container.</p>
        {:else}
          <dl class="mt-2 grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
            <dt class="text-muted">exists</dt><dd>{yn(report.path.exists)}</dd>
            <dt class="text-muted">is dir</dt><dd>{yn(report.path.is_dir)}</dd>
            <dt class="text-muted">readable</dt><dd>{yn(report.path.readable)}</dd>
            <dt class="text-muted">network</dt><dd>{yn(report.path.network)}</dd>
            <dt class="text-muted">fstype</dt><dd>{report.path.fstype ?? "—"}</dd>
            <dt class="text-muted">listing</dt><dd>{report.path.listing_ms == null ? "—" : `${report.path.listing_ms} ms`}</dd>
            <dt class="text-muted">entries seen</dt><dd>{report.path.entries_seen ?? 0}</dd>
            <dt class="text-muted">empty</dt><dd>{yn(report.path.empty)}</dd>
          </dl>
          {#if report.path.error}
            <p class="mt-2 text-danger break-words">{report.path.error}</p>
          {/if}
          {#if report.path.sample?.length}
            <p class="mt-2 text-muted">Sample entries</p>
            <ul class="mt-1 flex flex-wrap gap-1">
              {#each report.path.sample as e (e.name)}
                <li class="rounded-control bg-surface-3 px-1.5 py-0.5 font-mono">{e.dir ? "📁 " : ""}{e.name}</li>
              {/each}
            </ul>
          {/if}
        {/if}
      </DiagnoseSection>

      <DiagnoseSection title="Recent scans" count={report.scans.length}>
        {#if report.scans.length === 0}
          <p class="text-muted">No scans yet.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-left">
              <caption class="sr-only">Recent scans</caption>
              <thead class="text-muted">
                <tr>
                  <th scope="col" class="pr-2">status</th><th scope="col" class="pr-2">started</th>
                  <th scope="col" class="pr-2">duration</th><th scope="col" class="pr-2">seen</th>
                  <th scope="col" class="pr-2">new</th><th scope="col" class="pr-2">changed</th>
                  <th scope="col" class="pr-2">missing</th><th scope="col" class="pr-2">excluded</th>
                  <th scope="col">error</th>
                </tr>
              </thead>
              <tbody>
                {#each report.scans as r (r.id)}
                  <tr class="border-t border-border align-top">
                    <td class="pr-2 py-1"><Badge tone={scanTone(r.status)} class="uppercase">{r.status}</Badge></td>
                    <td class="pr-2 py-1 whitespace-nowrap">{fmtTs(r.started_at)}</td>
                    <td class="pr-2 py-1">{r.duration_s == null ? "—" : `${r.duration_s} s`}</td>
                    <td class="pr-2 py-1">{stat(r.stats, "seen")}</td>
                    <td class="pr-2 py-1">{stat(r.stats, "new")}</td>
                    <td class="pr-2 py-1">{stat(r.stats, "changed")}</td>
                    <td class="pr-2 py-1">{stat(r.stats, "missing")}</td>
                    <td class="pr-2 py-1">{stat(r.stats, "excluded")}</td>
                    <td class="py-1 text-danger break-words">{r.error ?? ""}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
          </div>
        {/if}
      </DiagnoseSection>

      <DiagnoseSection title="Extraction errors" count={report.extract_errors.count}>
        {#if report.extract_errors.count === 0}
          <p class="text-muted">No extraction errors.</p>
        {:else}
          <div class="flex flex-wrap gap-1">
            {#each Object.entries(report.extract_errors.by_kind) as [kind, n] (kind)}
              <Badge class="font-semibold uppercase tracking-wide">{kind}: {n}</Badge>
            {/each}
          </div>
          <div class="overflow-x-auto"><table class="mt-2 w-full text-left">
            <caption class="sr-only">Most frequent extraction errors</caption>
            <thead class="text-muted"><tr><th scope="col" class="pr-2">count</th><th scope="col">message</th></tr></thead>
            <tbody>
              {#each report.extract_errors.top_messages as m, i (i)}
                <tr class="border-t border-border align-top">
                  <td class="pr-2 py-1">{m.count}</td>
                  <td class="py-1 font-mono break-words">{m.message}</td>
                </tr>
              {/each}
            </tbody>
          </table></div>
          <p class="mt-2 text-muted">The Errors column on the Libraries page opens the per-item list.</p>
        {/if}
      </DiagnoseSection>

      <DiagnoseSection title="Failed jobs" count={report.failed_jobs.length}>
        {#if report.failed_jobs.length === 0}
          <p class="text-muted">No failed jobs reference this library.</p>
        {:else}
          <div class="overflow-x-auto">
            <table class="w-full text-left">
              <caption class="sr-only">Failed jobs for this library</caption>
              <thead class="text-muted">
                <tr>
                  <th scope="col" class="pr-2">task</th><th scope="col" class="pr-2">queue</th>
                  <th scope="col" class="pr-2">attempts</th><th scope="col" class="pr-2">scheduled</th>
                  <th scope="col">error</th>
                </tr>
              </thead>
              <tbody>
                {#each report.failed_jobs as j (j.id)}
                  <tr class="border-t border-border align-top">
                    <td class="pr-2 py-1 font-mono">{j.task}</td>
                    <td class="pr-2 py-1">{j.queue}</td>
                    <td class="pr-2 py-1">{j.attempts ?? "—"}</td>
                    <td class="pr-2 py-1 whitespace-nowrap">{fmtTs(j.scheduled_at)}</td>
                    <td class="py-1 text-danger break-words">{j.error ?? ""}</td>
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
            <p class="text-danger">Agent {String(a.id)} no longer exists.</p>
          {:else}
            <dl class="grid grid-cols-2 gap-x-4 gap-y-1 sm:grid-cols-4">
              <dt class="text-muted">name</dt><dd>{String(a.name ?? "—")}</dd>
              <dt class="text-muted">hostname</dt><dd>{String(a.hostname ?? "—")}</dd>
              <dt class="text-muted">platform</dt><dd>{String(a.platform ?? "—")}</dd>
              <dt class="text-muted">last seen</dt><dd>{fmtTs(a.last_seen_at as string | null)}</dd>
              <dt class="text-muted">online</dt><dd>{yn(a.online)}</dd>
              <dt class="text-muted">revoked</dt><dd>{yn(a.revoked)}</dd>
            </dl>
          {/if}
        </DiagnoseSection>
      {/if}

      <DiagnoseSection title="Logs" count={report.logs.length}>
        {#if report.logs.length === 0}
          <p class="text-muted">No warning-or-higher log lines mention this library.</p>
        {:else}
          <ul class="space-y-1 font-mono">
            {#each report.logs as l, i (i)}
              <li class="break-words whitespace-pre-wrap">
                <span class="text-muted">{fmtTs(l.ts)}</span>
                <span class={l.level === "ERROR" || l.level === "CRITICAL" ? "text-danger" : "text-warn"}>{l.level}</span>
                <span class="text-muted">{l.source}</span>
                {l.message}
              </li>
            {/each}
          </ul>
        {/if}
      </DiagnoseSection>

      <DiagnoseSection title="Context">
        <dl class="grid grid-cols-2 gap-x-4 gap-y-1">
          <dt class="text-muted">recycle retention (days)</dt><dd>{String(report.context.recycle_retention_days ?? "—")}</dd>
          <dt class="text-muted">worker concurrency</dt><dd>{String(report.context.worker_concurrency ?? "—")}</dd>
        </dl>
      </DiagnoseSection>
    </div>
  {/if}

  {#snippet footer({ close })}
    <Button onclick={close}>Close</Button>
  {/snippet}
</Modal>
