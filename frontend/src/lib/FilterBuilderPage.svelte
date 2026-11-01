<script lang="ts">
  import { onMount } from "svelte";
  import ItemDetail from "./ItemDetail.svelte";
  import DslHelp from "./DslHelp.svelte";
  import { COPY_FAILED, copyText } from "./clipboard";
  import { toast } from "./ui/toast.svelte";
  import { Alert, Button, Checkbox, DataTable, Field, Input, Select, StateBlock, Textarea } from "./ui";
  import { encodeSearchHash } from "./searchparams";
  import { setReportPrefill, takeBuilderPrefill } from "./handoff";
  import {
    assistQuery,
    previewQuery,
    previewValidationErrors,
    queryKeys,
    search,
    searchTags,
    type QueryPreviewResponse,
    type QueryKeys,
    type ReportValidationError,
    errorMessage,
  } from "./api";
  import {
    newCondition,
    conditionsToDsl,
    dslToRows,
    conditionsToSearchParams,
    validateCondition,
    opsFor,
    defaultOp,
    type Condition,
    type FieldKind,
    type Op,
  } from "./filterBuilder";

  // ------------------------------------------------------------------ //
  // Visual filter builder (user-requested). Structured condition rows   //
  // compile to the querydsl string (single source of truth); a live     //
  // preview runs it through POST /query/preview (the exact custom-report //
  // machinery) and shows real, RBAC-scoped rows + a capped match count.  //
  // ------------------------------------------------------------------ //

  const FIELDS: { value: FieldKind; label: string }[] = [
    { value: "text", label: "Text" },
    { value: "kind", label: "Kind" },
    { value: "group", label: "Group" },
    { value: "ext", label: "Extension" },
    { value: "size", label: "Size" },
    { value: "modified", label: "Modified" },
    { value: "created", label: "Created" },
    { value: "path", label: "Path" },
    { value: "tag", label: "Tag" },
    { value: "hash", label: "Hash" },
    { value: "meta", label: "Metadata (meta.)" },
    { value: "cf", label: "Custom field (cf.)" },
  ];

  const OP_LABEL: Record<Op, string> = {
    contains: "contains",
    is: "is",
    is_not: "is not",
    "=": "=",
    ">": ">",
    ">=": "≥",
    "<": "<",
    "<=": "≤",
    range: "between",
    matches: "matches",
  };
  const SIZE_UNITS = ["", "K", "M", "G", "T"];
  const DURATION_UNITS = [
    { v: "s", l: "seconds" },
    { v: "m", l: "minutes" },
    { v: "h", l: "hours" },
    { v: "d", l: "days" },
    { v: "w", l: "weeks" },
  ];
  const COMPARATOR_FIELDS = new Set<FieldKind>(["size", "modified", "created", "meta", "cf"]);
  const NEGATABLE = new Set<FieldKind>([
    "text", "kind", "group", "ext", "tag", "size", "modified", "created", "path", "hash", "meta", "cf",
  ]);

  let conditions = $state<Condition[]>([newCondition("kind")]);
  let rawMode = $state(false);
  let rawQuery = $state("");
  let advancedBanner = $state(false);

  // value-picker vocabulary
  let keys = $state<QueryKeys | null>(null);
  let extFacet = $state<string[]>([]);
  let tagSuggest = $state<Record<number, string[]>>({});

  // preview state
  let preview = $state<QueryPreviewResponse | null>(null);
  let vErrors = $state<ReportValidationError[]>([]);
  let previewError = $state("");
  let loading = $state(false);
  let detailId = $state<string | null>(null);

  // search-handoff warning
  let unmappedWarn = $state<string[]>([]);

  const dsl = $derived(rawMode ? rawQuery.trim() : conditionsToDsl(conditions));
  // Distinguish "no rows at all" (matches everything) from "rows present but
  // none complete yet" (mid-typing) so the compiled pane never reads as an error.
  const hasRows = $derived(!rawMode && conditions.length > 0);

  const BYTE_COLUMNS = new Set(["size"]);
  function fmtBytes(b: number): string {
    if (!isFinite(b) || b <= 0) return "0";
    const u = ["B", "KB", "MB", "GB", "TB"];
    const i = Math.min(u.length - 1, Math.floor(Math.log(b) / Math.log(1024)));
    return `${(b / 1024 ** i).toFixed(i ? 1 : 0)} ${u[i]}`;
  }
  function cell(row: Record<string, unknown>, col: string): string {
    const v = row[col];
    if (v == null) return "";
    if (BYTE_COLUMNS.has(col) && typeof v === "number") return fmtBytes(v);
    return String(v);
  }

  // ------------------------------------------------------------------ //
  // Row editing                                                         //
  // ------------------------------------------------------------------ //
  function addRow() {
    conditions = [...conditions, newCondition("text")];
  }
  function removeRow(id: number) {
    conditions = conditions.filter((c) => c.id !== id);
  }
  function onFieldChange(c: Condition) {
    // Reset op + value shape when the field changes so the row stays coherent.
    c.op = defaultOp(c.field);
    c.value = "";
    c.value2 = "";
    c.key = "";
    c.unit = c.field === "size" ? "" : c.field === "modified" || c.field === "created" ? "d" : "";
    c.dateMode = "relative";
    conditions = [...conditions];
  }
  function isNegated(c: Condition): boolean {
    if (c.field === "kind" || c.field === "group" || c.field === "ext" || c.field === "tag") return c.op === "is_not";
    return c.negated;
  }
  function setNegated(c: Condition, val: boolean) {
    if (c.field === "kind" || c.field === "group" || c.field === "ext" || c.field === "tag") c.op = val ? "is_not" : "is";
    else c.negated = val;
    conditions = [...conditions];
  }

  async function onTagInput(c: Condition, q: string) {
    c.value = q;
    conditions = [...conditions];
    try {
      const res = await searchTags(q);
      tagSuggest = { ...tagSuggest, [c.id]: res.tags.map((t) => t.value) };
    } catch {
      /* type-ahead is best-effort */
    }
  }

  // ------------------------------------------------------------------ //
  // Live preview (debounced)                                            //
  // ------------------------------------------------------------------ //
  let debounce: ReturnType<typeof setTimeout> | undefined;
  let previewSeq = 0;
  $effect(() => {
    const current = dsl; // track
    clearTimeout(debounce);
    debounce = setTimeout(() => runPreview(current), 300);
  });

  async function runPreview(query: string) {
    const seq = ++previewSeq;
    loading = true;
    previewError = "";
    vErrors = [];
    try {
      const res = await previewQuery({ query, limit: 25 });
      if (seq !== previewSeq) return;
      preview = res;
    } catch (e) {
      if (seq !== previewSeq) return;
      const errs = previewValidationErrors(e);
      if (errs) {
        vErrors = errs;
        preview = null;
      } else {
        previewError = errorMessage(e);
      }
    } finally {
      if (seq === previewSeq) loading = false;
    }
  }

  // Kept: formats one validation item with its parse position, which
  // errorMessage (whole-error, msg/message only) does not render.
  function errText(e: ReportValidationError): string {
    if (e.error === "parse_error")
      return `Parse error (${e.code}) at position ${e.position}: ${e.reason}`;
    if (e.error === "translation_error") return e.message ?? "cannot translate query";
    return e.message ?? e.error;
  }

  // ------------------------------------------------------------------ //
  // Actions                                                             //
  // ------------------------------------------------------------------ //
  let copied = $state(false);
  async function copyDsl() {
    if (!(await copyText(dsl))) return toast(COPY_FAILED, { tone: "warn" });
    copied = true;
    setTimeout(() => (copied = false), 1200);
  }

  function saveAsReport() {
    setReportPrefill({ query: dsl, columns: ["rel_path", "library", "size"] });
    location.hash = "#/reports";
  }

  function openInSearch() {
    if (rawMode) return;
    const { params, unmapped } = conditionsToSearchParams(conditions);
    unmappedWarn = unmapped
      .map((c) => {
        const line = conditionsToDsl([c]);
        return line || `${c.field} condition`;
      })
      .filter(Boolean);
    location.hash = encodeSearchHash(params);
  }

  function reset() {
    conditions = [newCondition("kind")];
    rawMode = false;
    rawQuery = "";
    advancedBanner = false;
    unmappedWarn = [];
    nlText = "";
    nlNotes = [];
    nlSource = "";
    nlError = "";
  }

  // ------------------------------------------------------------------ //
  // Natural-language assist: describe the query in plain English; the   //
  // server translates to a guaranteed-valid DSL string (heuristic, or a //
  // configured local Ollama with heuristic fallback). Applied exactly   //
  // like a prefill: rows when representable, raw mode otherwise.        //
  // ------------------------------------------------------------------ //
  let nlText = $state("");
  let nlBusy = $state(false);
  let nlNotes = $state<string[]>([]);
  let nlSource = $state("");
  let nlError = $state("");

  async function runAssist() {
    const text = nlText.trim();
    if (!text || nlBusy) return;
    nlBusy = true;
    nlError = "";
    nlNotes = [];
    nlSource = "";
    try {
      const r = await assistQuery(text);
      if (!r.dsl) {
        nlError = "Couldn't derive any filters from that — try naming kinds, sizes, or dates.";
        return;
      }
      nlNotes = r.notes;
      nlSource = r.source;
      const res = dslToRows(r.dsl);
      if (res.advanced) {
        rawMode = true;
        rawQuery = r.dsl;
        advancedBanner = true;
      } else {
        conditions = res.rows.length ? res.rows : [newCondition("kind")];
        rawMode = false;
        advancedBanner = false;
      }
    } catch (e) {
      nlError = errorMessage(e);
    } finally {
      nlBusy = false;
    }
  }

  function toggleRaw() {
    if (!rawMode) {
      // entering raw mode: seed the textarea with the compiled DSL
      rawQuery = conditionsToDsl(conditions);
      rawMode = true;
    } else {
      // leaving raw mode: try to parse the raw DSL back into rows
      const res = dslToRows(rawQuery.trim());
      if (res.advanced) {
        advancedBanner = true;
        return; // keep raw mode; never destroy the query
      }
      conditions = res.rows.length ? res.rows : [newCondition("kind")];
      advancedBanner = false;
      rawMode = false;
    }
  }

  // ------------------------------------------------------------------ //
  // Mount: keys + facets + round-trip prefill                           //
  // ------------------------------------------------------------------ //
  onMount(async () => {
    try {
      keys = await queryKeys();
    } catch {
      /* keys are optional conveniences */
    }
    try {
      const r = await search({ limit: "1" });
      extFacet = Object.entries(r.facets?.extension ?? {})
        .sort((a, b) => b[1] - a[1])
        .slice(0, 40)
        .map(([e]) => e);
    } catch {
      /* facets optional */
    }
    const pf = takeBuilderPrefill();
    if (pf) {
      const res = dslToRows(pf.query);
      if (res.advanced) {
        rawMode = true;
        rawQuery = pf.query;
        advancedBanner = true;
      } else {
        conditions = res.rows.length ? res.rows : [newCondition("kind")];
      }
    }
  });

  const kindOptions = $derived(keys?.kinds ?? []);
  const groupOptions = $derived(keys?.groups ?? []);
  const metaKeyOptions = $derived(keys?.meta_keys.map((k) => k.key) ?? []);
  const cfKeyOptions = $derived(keys?.custom_fields.map((k) => k.name) ?? []);

  // Preview rows carry no stable id (item_id can be absent), so DataTable keys
  // them by position.
  type PreviewRow = { i: number; row: Record<string, unknown> };
  const previewRows = $derived<PreviewRow[]>((preview?.rows ?? []).map((row, i) => ({ i, row })));
</script>

<!-- Live-result cells (rendered twice by DataTable: table + card list, so no ids). -->
{#snippet filenameCell(r: PreviewRow)}
  <span title={String(r.row.rel_path ?? "")}>{cell(r.row, "filename")}</span>
{/snippet}
{#snippet openCell(r: PreviewRow)}
  <Button
    size="sm"
    onclick={() => r.row.item_id && (detailId = String(r.row.item_id))}
    disabled={!r.row.item_id}>Open</Button>
{/snippet}

<div class="mx-auto max-w-screen-xl">
  <div class="mb-4 flex items-center gap-3">
    <h2 class="text-lg font-semibold">Filter builder</h2>
    <span class="text-sm text-muted">Build a query visually, test it live, then save or search.</span>
    <div class="grow"></div>
    <Button size="sm" onclick={toggleRaw}>
      {rawMode ? "Back to rows" : "Edit raw DSL"}
    </Button>
    <Button size="sm" onclick={reset}>Reset</Button>
  </div>

  {#if advancedBanner}
    <Alert tone="warn" class="mb-3">
      This query contains advanced syntax the visual builder can't represent — editing raw. Your query is preserved.
    </Alert>
  {/if}

  <!-- NL assist: plain English in, DSL out (server-validated) -->
  <form
    class="mb-4"
    onsubmit={(e) => { e.preventDefault(); runAssist(); }}>
    <div class="flex flex-wrap items-center gap-2">
      <div class="min-w-64 grow">
        <Input
          aria-label="Describe the filter in plain English"
          bind:value={nlText}
          placeholder='Describe it: "videos over 2 GB modified last week, not tagged archived"' />
      </div>
      <Button type="submit" disabled={nlBusy || !nlText.trim()}>
        {nlBusy ? "Translating…" : "Translate"}
      </Button>
      {#if nlSource}
        <span class="text-xs text-muted" title="Which engine produced the filters — the deterministic pattern translator, or the configured local model.">
          via {nlSource === "ollama" ? "local model" : "pattern rules"}
        </span>
      {/if}
    </div>
    {#if nlError}
      <Alert tone="danger" class="mt-2" onDismiss={() => (nlError = "")}>{nlError}</Alert>
    {/if}
  </form>
  {#each nlNotes as note (note)}
    <Alert tone="warn" class="mb-3">{note}</Alert>
  {/each}

  <div class="grid gap-4 lg:grid-cols-2">
    <!-- LEFT: builder -->
    <div>
      {#if rawMode}
        <Field label="Raw filter DSL">
          <Textarea
            class="font-mono"
            rows={7}
            bind:value={rawQuery}
            placeholder={"kind:video meta.height:>=1080 -tag:archived   (meta.*:value matches any metadata key)"} />
        </Field>
      {:else}
        <div class="space-y-2">
          {#each conditions as c (c.id)}
            {@const st = validateCondition(c)}
            <div
              class="flex flex-wrap items-center gap-2 rounded-panel border p-2 {st.state === 'invalid'
                ? 'border-danger'
                : st.state === 'incomplete'
                  ? 'border-warn'
                  : 'border-border'}">
              {#if NEGATABLE.has(c.field)}
                <span class="text-xs text-muted" title="Exclude matches">
                  <Checkbox size="sm" label="not" checked={isNegated(c)} onchange={(e) => setNegated(c, e.currentTarget.checked)} />
                </span>
              {/if}
              <span class="w-40">
                <Select size="sm" aria-label="Field" bind:value={c.field} onchange={() => onFieldChange(c)}>
                  {#each FIELDS as f (f.value)}
                    <option value={f.value}>{f.label}</option>
                  {/each}
                </Select>
              </span>

              {#if (c.field === "meta" || c.field === "cf")}
                <span class="w-36">
                  <Input
                    size="sm"
                    class="font-mono"
                    aria-label={c.field === "meta" ? "Metadata key" : "Custom field name"}
                    list={c.field === "meta" ? "meta-keys" : "cf-keys"}
                    placeholder={c.field === "meta" ? "key (e.g. height, or * for any)" : "field name"}
                    title={c.field === "meta"
                      ? "The extracted-metadata key. Dotted paths reach nested objects (exif.camera). The FINAL segment may be * to match ANY key: meta.*:1080p finds the value wherever it lives; meta.exif.*:canon searches every EXIF field."
                      : "The registered custom field's name."}
                    bind:value={c.key} />
                </span>
              {/if}

              {#if COMPARATOR_FIELDS.has(c.field)}
                <span class="w-28">
                  <Select size="sm" aria-label="Operator" bind:value={c.op}>
                    {#each opsFor(c.field) as op (op)}
                      <option value={op}>{OP_LABEL[op]}</option>
                    {/each}
                  </Select>
                </span>
              {/if}

              <!-- value inputs, typed per field -->
              {#if c.field === "kind"}
                <span class="w-40">
                  <Select size="sm" aria-label="Value" bind:value={c.value}>
                    <option value="">choose…</option>
                    {#each kindOptions as k (k)}<option value={k}>{k}</option>{/each}
                  </Select>
                </span>
              {:else if c.field === "group"}
                <span class="w-40">
                  <Select size="sm" aria-label="Value" bind:value={c.value}>
                    <option value="">choose…</option>
                    {#each groupOptions as g (g)}<option value={g}>{g}</option>{/each}
                  </Select>
                </span>
              {:else if c.field === "ext"}
                <span class="w-40">
                  <Input size="sm" aria-label="Value" list="ext-facet" placeholder="pdf or mp4;mkv" bind:value={c.value} />
                </span>
              {:else if c.field === "tag"}
                <span class="w-40">
                  <Input
                    size="sm"
                    aria-label="Value"
                    list={`tags-${c.id}`} placeholder="tag" value={c.value}
                    oninput={(e) => onTagInput(c, e.currentTarget.value)} />
                </span>
                <datalist id={`tags-${c.id}`}>
                  {#each (tagSuggest[c.id] ?? []) as t (t)}<option value={t}></option>{/each}
                </datalist>
              {:else if c.field === "size"}
                <span class="w-24">
                  <Input size="sm" type="number" min="0" aria-label="Value" bind:value={c.value} />
                </span>
                {#if c.op === "range"}
                  <span class="text-xs text-muted">to</span>
                  <span class="w-24">
                    <Input size="sm" type="number" min="0" aria-label="Upper value" bind:value={c.value2} />
                  </span>
                {/if}
                <span class="w-20">
                  <Select size="sm" aria-label="Size unit" bind:value={c.unit}>
                    {#each SIZE_UNITS as u (u)}<option value={u}>{u === "" ? "B" : u + "iB"}</option>{/each}
                  </Select>
                </span>
              {:else if c.field === "modified" || c.field === "created"}
                <span class="w-28">
                  <Select size="sm" aria-label="Date mode" bind:value={c.dateMode}>
                    <option value="relative">relative</option>
                    <option value="absolute">date</option>
                  </Select>
                </span>
                {#if c.dateMode === "relative"}
                  <span class="w-20">
                    <Input size="sm" type="number" min="0" aria-label="Value" bind:value={c.value} />
                  </span>
                  {#if c.op === "range"}
                    <span class="text-xs text-muted">to</span>
                    <span class="w-20">
                      <Input size="sm" type="number" min="0" aria-label="Upper value" bind:value={c.value2} />
                    </span>
                  {/if}
                  <span class="w-36">
                    <Select size="sm" aria-label="Time unit" bind:value={c.unit}>
                      {#each DURATION_UNITS as u (u.v)}<option value={u.v}>{u.l} ago</option>{/each}
                    </Select>
                  </span>
                {:else}
                  <span class="w-40">
                    <Input size="sm" type="date" aria-label="Value" bind:value={c.value} />
                  </span>
                  {#if c.op === "range"}
                    <span class="text-xs text-muted">to</span>
                    <span class="w-40">
                      <Input size="sm" type="date" aria-label="Upper value" bind:value={c.value2} />
                    </span>
                  {/if}
                {/if}
              {:else if c.field === "meta" || c.field === "cf"}
                <span class="w-40">
                  <Input size="sm" aria-label="Value" placeholder="value" bind:value={c.value} />
                </span>
                {#if c.op === "range"}
                  <span class="text-xs text-muted">to</span>
                  <span class="w-40">
                    <Input size="sm" aria-label="Upper value" placeholder="value" bind:value={c.value2} />
                  </span>
                {/if}
              {:else}
                <!-- text / path / hash -->
                <span class="w-56">
                  <Input
                    size="sm"
                    aria-label="Value"
                    placeholder={c.field === "path" ? "*/backups/*" : c.field === "hash" ? "hex digest" : "text"}
                    bind:value={c.value} />
                </span>
              {/if}

              <div class="grow"></div>
              <Button
                size="sm"
                variant="ghost"
                onclick={() => removeRow(c.id)}
                aria-label="Remove condition">✕</Button>
              {#if st.state !== "ok"}
                <p class="w-full text-xs {st.state === 'invalid' ? 'text-danger' : 'text-warn'}">
                  {st.state === "invalid" ? "⚠ " : ""}{st.hint}{st.state === "invalid"
                    ? " — excluded until fixed"
                    : ""}
                </p>
              {/if}
            </div>
          {/each}
          {#if conditions.length === 0}
            <StateBlock
              state="empty"
              compact
              message="No conditions — matches all items. Add a condition to narrow it down." />
          {/if}
        </div>

        <Button size="sm" class="mt-3" onclick={addRow}>+ Add condition</Button>
        <p class="mt-1 text-xs text-muted">Conditions are combined with AND. (OR groups aren't in the query grammar yet.)</p>
        <DslHelp onInsert={(frag) => { rawQuery = (conditionsToDsl(conditions) + " " + frag).trim(); rawMode = true; }} context="search" />
      {/if}

      <!-- compiled DSL preview -->
      <div class="mt-4">
        <div class="mb-1 flex items-center gap-2">
          <span class="text-xs text-muted">Compiled filter DSL</span>
          <div class="grow"></div>
          <Button size="sm" onclick={copyDsl}>
            {copied ? "Copied" : "Copy"}
          </Button>
        </div>
        <pre class="overflow-x-auto rounded-control border border-border bg-surface-2 px-3 py-2 font-mono text-xs">{dsl || (hasRows ? "(no complete conditions yet)" : "(all items)")}</pre>
      </div>

      <div class="mt-3 flex flex-wrap gap-2">
        <Button variant="primary" onclick={saveAsReport}>Save as custom report</Button>
        <Button
          onclick={openInSearch} disabled={rawMode} title={rawMode ? "Switch back to rows to map into search" : ""}>Open in search</Button>
      </div>
      {#if unmappedWarn.length}
        <Alert tone="warn" class="mt-2">
          Some conditions have no equivalent search filter and were dropped when opening search:
          <ul class="mt-1 list-disc pl-5 font-mono text-xs">
            {#each unmappedWarn as w (w)}<li>{w}</li>{/each}
          </ul>
        </Alert>
      {/if}
    </div>

    <!-- RIGHT: live results -->
    <div>
      <div class="mb-2 flex items-center gap-2">
        <span class="text-sm font-medium">Live results</span>
        {#if preview}
          <span class="rounded-full bg-surface-3 px-2 py-0.5 text-xs text-muted">
            {preview.total}{preview.total_capped ? "+" : ""} match{preview.total === 1 && !preview.total_capped ? "" : "es"}
          </span>
        {/if}
        {#if loading}<span class="text-xs text-muted">testing…</span>{/if}
      </div>

      {#if vErrors.length}
        <Alert tone="warn">
          <ul class="space-y-1 text-xs">
            {#each vErrors as e, i (i)}
              <li>{errText(e)}</li>
            {/each}
          </ul>
        </Alert>
      {:else if previewError || preview}
        <DataTable
          caption="Live results"
          captionHidden
          columns={[
            { key: "filename", label: "filename", cell: filenameCell },
            { key: "library", label: "library" },
            { key: "file_category", label: "type" },
            { key: "size", label: "size" },
            { key: "open", label: "Open", align: "right", cell: openCell },
          ]}
          rows={previewRows}
          rowKey={(r) => r.i}
          accessor={(r, k) => cell(r.row, k)}
          {loading}
          error={previewError || null}
          onRetry={() => runPreview(dsl)}
          emptyMessage="No matches. Loosen or remove a condition to widen the result." />
        {#if preview?.has_more}
          <p class="mt-2 text-xs text-muted">Showing the first {preview.count}. Save as a custom report to export the full result.</p>
        {/if}
      {:else}
        <p class="text-sm text-muted">Add a condition to see matching items.</p>
      {/if}
    </div>
  </div>

  <!-- shared datalists for meta/cf/ext value pickers -->
  <datalist id="meta-keys">{#each metaKeyOptions as k (k)}<option value={k}></option>{/each}</datalist>
  <datalist id="cf-keys">{#each cfKeyOptions as k (k)}<option value={k}></option>{/each}</datalist>
  <datalist id="ext-facet">{#each extFacet as e (e)}<option value={e}></option>{/each}</datalist>
</div>

{#if detailId}
  <ItemDetail id={detailId} onClose={() => (detailId = null)} onOpen={(nid) => (detailId = nid)} />
{/if}
