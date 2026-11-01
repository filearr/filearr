<script lang="ts">
  // UI-T1 — full library edit modal (kit Modal, mount-style). Exposes
  // every editable field consolidated into labelled sections: identity (name,
  // root_path + warning, native_prefix, enabled), content (types + include/exclude
  // globs), schedule (cron/watch), hashing (policy/ceiling), and the P2-T5 indexing
  // controls (presets + extension groups) that previously lived in the Admin
  // expander. Edits collect into a local draft and PATCH once on Save; a 422 body
  // surfaces inline. All dynamic strings render as text (no {@html}).
  //
  // The SAME modal also CREATES a library (`library === null`): the old inline
  // "Add library" form on the Libraries page exposed only a subset of these fields
  // and cluttered the page; one dialog with one field set means an option that
  // exists on Edit also exists on Add, and the page shows a single button.
  import { untrack } from "svelte";
  import {
    createLibrary, resolveShareHint, updateLibrary,
    type ExtensionGroup, type HashPolicy, type Library, type Preset, type PresetsResponse,
    type ShareMapEntry, type TaxonomyNode,
    errorMessage,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import { intOrNull, numOrNull } from "./numInput";
  import { formatShare, shareLocation } from "./osFormat";
  import { shareFormat, detectedPlatform } from "./osFormat.svelte";
  import FolderPicker from "./FolderPicker.svelte";
  import ScheduleField from "./ScheduleField.svelte";
  import TaxonomySelector from "./TaxonomySelector.svelte";
  import { Alert, Button, Checkbox, Field, Input, Modal, Select, Textarea } from "./ui";

  let {
    library,
    presetsMeta,
    taxonomyTree,
    shareMap = [],
    onSaved,
    onClose,
  }: {
    /** The library to edit, or null to create a new one. */
    library: Library | null;
    presetsMeta: PresetsResponse;
    taxonomyTree: TaxonomyNode[];
    /** OPS-T7 deploy mount map — previews the auto share location a NEW root
     *  would inherit (create mode only; an existing library carries its own). */
    shareMap?: ShareMapEntry[];
    onSaved: () => void;
    onClose: () => void;
  } = $props();

  // Mode is fixed for the modal's lifetime (LibrariesPage mounts a fresh instance per
  // target), so capturing it once is intended — same reasoning as `seed` below.
  const creating = untrack(() => library === null);

  const ALL_TYPES = [
    "video", "audio", "audiobook", "sample", "image", "model3d", "document", "spreadsheet",
  ];
  const HASH_POLICIES: HashPolicy[] = ["auto", "full", "quick_only"];

  // Working draft, seeded ONCE from the library prop. The modal is remounted per
  // edit target (keyed by `editing` in LibrariesPage), so capturing the initial value
  // is the intended behaviour — `untrack` makes that explicit and keeps the draft
  // fields from re-binding to the prop (silences state_referenced_locally). Globs
  // edit as newline-joined text.
  const seed = untrack(() => library === null
    ? {
        name: "",
        rootPath: "/data/media/",
        nativePrefix: "",
        sharePrefix: "",
        enabled: true,
        enabledCategories: [] as string[],
        enabledGroups: [] as string[],
        includeGlobs: "",
        excludeGlobs: "",
        scanCron: "",
        watchMode: false,
        hashPolicy: "auto" as HashPolicy,
        hashCeiling: null as number | null,
        presets: [] as string[],
        groups: [] as string[],
        ocrEnabled: false,
        chunkingEnabled: false,
        exposeGps: false,
        countPrunedFiles: false,
        extractOverrides: {} as Record<string, number>,
      }
    : {
        name: library.name,
        rootPath: library.root_path,
        nativePrefix: library.native_prefix ?? "",
        sharePrefix: library.share_prefix ?? "",
        enabled: library.enabled,
        enabledCategories: [...(library.enabled_categories ?? [])],
        enabledGroups: [...(library.enabled_groups ?? [])],
        includeGlobs: (library.include_globs ?? []).join("\n"),
        excludeGlobs: (library.exclude_globs ?? []).join("\n"),
        scanCron: library.scan_cron ?? "",
        watchMode: library.watch_mode,
        hashPolicy: library.hash_policy,
        hashCeiling: library.hash_full_max_bytes ?? null,
        presets: [...(library.enabled_presets ?? [])],
        groups: [...(library.enabled_extension_groups ?? [])],
        ocrEnabled: library.ocr_enabled,
        chunkingEnabled: library.chunking_enabled,
        exposeGps: library.expose_gps,
        countPrunedFiles: library.count_pruned_files,
        extractOverrides: { ...(library.extract_overrides ?? {}) },
      });

  let name = $state(seed.name);
  let rootPath = $state(seed.rootPath);
  let nativePrefix = $state(seed.nativePrefix);
  let sharePrefix = $state(seed.sharePrefix);
  // UI-T15: the auto (mount-map) network location rendered in the viewer's OS
  // spelling (smb:// vs UNC), following the header format selector.
  const autoShareText = $derived(
    library?.share_prefix_source === "mount-map"
      ? formatShare(
          shareLocation(library.share_prefix_effective, library.share_unc_effective),
          shareFormat.pref,
          detectedPlatform,
        )
      : null,
  );
  // Create mode: the auto share the typed root WOULD inherit from the mount map
  // (a hint, not a stored value). Edit mode reads the library's own effective one.
  const detectedShare = $derived(
    creating && rootPath.trim() ? resolveShareHint(shareMap, rootPath.trim()) : null,
  );
  const autoShareHint = $derived(
    creating ? detectedShare?.share_url ?? null
    : library?.share_prefix_source === "mount-map" ? library.share_prefix_effective ?? null
    : null,
  );
  let enabled = $state(seed.enabled);
  // W8: taxonomy type-gating (replaces the old flat `types`). Distinct from the
  // legacy `groups` below (P2-T5 extension groups) — these are taxonomy keys.
  let enabledCategories = $state<string[]>(seed.enabledCategories);
  let enabledGroups = $state<string[]>(seed.enabledGroups);
  let includeGlobs = $state(seed.includeGlobs);
  let excludeGlobs = $state(seed.excludeGlobs);
  let scanCron = $state(seed.scanCron);
  let watchMode = $state(seed.watchMode);
  let hashPolicy = $state<HashPolicy>(seed.hashPolicy);
  // bind:value on type="number" writes a number, or null when emptied.
  let hashCeiling = $state<number | null>(seed.hashCeiling);
  // Blank = global default; anything else must be a positive integer byte count.
  const hashCeilingValue = $derived(intOrNull(hashCeiling, { min: 1 }));
  // An unparseable entry ("1e") binds null, which reads as blank and would save
  // the global default; validity.badInput is the only way to tell them apart.
  let hashCeilingEl = $state<HTMLInputElement | null>(null);
  let hashCeilingBad = $state(false);
  const readHashCeilingValidity = () => (hashCeilingBad = hashCeilingEl?.validity.badInput ?? false);
  const hashCeilingError = $derived(
    hashCeilingBad
      ? "Enter a whole number of bytes."
      : hashCeiling !== null && hashCeilingValue === null
        ? "Enter a whole number of bytes (1 or more), or leave blank."
        : "",
  );
  let presets = $state<string[]>(seed.presets);
  let groups = $state<string[]>(seed.groups);
  let ocrEnabled = $state(seed.ocrEnabled);
  let chunkingEnabled = $state(seed.chunkingEnabled);
  let exposeGps = $state(seed.exposeGps);
  let countPrunedFiles = $state(seed.countPrunedFiles);

  // Per-library extraction-limit overrides. Stored server-side in raw units
  // (bytes / seconds / ratio); byte fields are edited in MiB here. Blank =
  // fall back to the global FILEARR_* setting.
  const OVERRIDE_FIELDS: { key: string; label: string; unit: string; scale: number; help: string }[] = [
    { key: "extract_timeout_seconds", label: "Extractor timeout", unit: "s", scale: 1,
      help: "Wall-clock budget per file before the parser is abandoned (FILEARR_EXTRACT_TIMEOUT_SECONDS). Raise for huge 3MF/PDF files on slow storage." },
    { key: "ffprobe_timeout_s", label: "ffprobe timeout", unit: "s", scale: 1,
      help: "Per-file ffprobe budget for video metadata (FILEARR_FFPROBE_TIMEOUT_S)." },
    { key: "document_max_bytes", label: "Document ceiling", unit: "MiB", scale: 1048576,
      help: "Documents/spreadsheets larger than this are guard-skipped (FILEARR_DOCUMENT_MAX_BYTES)." },
    { key: "doc_decompressed_max", label: "Decompressed ceiling", unit: "MiB", scale: 1048576,
      help: "Zip/docx whose DECLARED uncompressed total exceeds this are guard-skipped as decompression bombs (FILEARR_DOC_DECOMPRESSED_MAX). Raise for libraries of legitimately huge archives (e.g. 3D-print zips)." },
    { key: "doc_decompression_ratio", label: "Decompression ratio", unit: ":1", scale: 1,
      help: "Uncompressed:compressed ratio above which an archive is treated as a bomb (FILEARR_DOC_DECOMPRESSION_RATIO)." },
    { key: "doc_decompression_ratio_min_bytes", label: "Ratio guard floor", unit: "MiB", scale: 1048576,
      help: "The ratio guard only applies to payloads larger than this (FILEARR_DOC_DECOMPRESSION_RATIO_MIN_BYTES)." },
    { key: "model3d_max_bytes", label: "3D model ceiling", unit: "MiB", scale: 1048576,
      help: "3D models larger than this skip geometry extraction (FILEARR_MODEL3D_MAX_BYTES)." },
    { key: "email_max_bytes", label: "E-mail ceiling", unit: "MiB", scale: 1048576,
      help: "E-mail files larger than this are guard-skipped (FILEARR_EMAIL_MAX_BYTES)." },
  ];
  let overrideInputs = $state<Record<string, number | null>>(
    Object.fromEntries(
      OVERRIDE_FIELDS.map((f) => {
        const v = seed.extractOverrides[f.key];
        return [f.key, v != null ? v / f.scale : null];
      }),
    ),
  );

  function overridesPayload(): Record<string, number> | null {
    const out: Record<string, number> = {};
    for (const f of OVERRIDE_FIELDS) {
      const n = numOrNull(overrideInputs[f.key]);
      if (n === null || n <= 0) continue;
      // MiB fields become whole bytes; scale-1 fields (seconds, ratio) keep
      // their fractions (B20).
      out[f.key] = f.scale > 1 ? Math.round(n * f.scale) : n;
    }
    return Object.keys(out).length ? out : null;
  }

  let error = $state("");
  let busy = $state(false);
  // The FolderPicker is its own Modal stacked over this one; Escape and clicks
  // inside it belong to the picker (bits-ui's layer stack + the Modal's
  // nested-dialog guard), so no Escape guard is needed here.
  let showPicker = $state(false);
  // Mount-style: the parent mounts this per target and unmounts it from
  // onClose / onSaved. A successful save sets `open = false` (a caller close,
  // which skips the dirty check) before calling onSaved.
  let open = $state(true);

  // Every edited value, in one comparable shape. The dirty check compares it
  // against the snapshot taken at mount, so reverting an edit by hand reads as
  // clean again. An unparseable ceiling (badInput) binds null, so it is
  // counted separately.
  function snapshot(): string {
    return JSON.stringify({
      name, rootPath, nativePrefix, sharePrefix, enabled,
      enabledCategories, enabledGroups, includeGlobs, excludeGlobs, scanCron,
      watchMode, hashPolicy, hashCeiling, hashCeilingBad, presets, groups,
      ocrEnabled, chunkingEnabled, exposeGps, countPrunedFiles, overrideInputs,
    });
  }
  const initialSnapshot = untrack(snapshot);
  const isDirty = () => snapshot() !== initialSnapshot;
  let showPatterns = $state<Record<string, boolean>>({});

  // Agent-owned libraries hold their content on a remote machine central cannot
  // open, so no CENTRAL extraction pass runs for them. OCR is therefore still
  // inert here (the central OCR job would have to read the file) — the agent
  // does its own via the `extract_ocr` policy key instead, so the toggle stays
  // disabled rather than silently doing nothing.
  //
  // RAG chunking is NOT inert any more (2026-08-09 agent extraction parity): the
  // agent extracts locally and ships `body_text`/`ocr_text` on its replication
  // events, and `chunk_missing` selects purely on that text plus this flag. It
  // must stay operator-settable or agent items could never be chunked.
  const agentOwned = $derived(library?.source_agent_id != null);

  const rootChanged = $derived(library !== null && rootPath !== library.root_path);

  // Mirrors presets.resolve_effective_presets: a default-on bundle is active
  // unless a `-name` opt-out sentinel is present; others only when listed.
  function isPresetActive(p: Preset): boolean {
    if (presets.includes("-" + p.name)) return false;
    if (presets.includes(p.name)) return true;
    return p.default_enabled;
  }

  function togglePreset(p: Preset) {
    const active = isPresetActive(p);
    let next = presets.filter((e) => e !== p.name && e !== "-" + p.name);
    if (active && p.default_enabled) next = [...next, "-" + p.name];
    else if (!active && !p.default_enabled) next = [...next, p.name];
    presets = next;
  }

  function toggleGroup(name: string) {
    groups = groups.includes(name) ? groups.filter((g) => g !== name) : [...groups, name];
  }

  function groupsForType(mt: string): ExtensionGroup[] {
    return presetsMeta.extension_groups.filter((g) => g.file_category === mt);
  }

  function linesToArray(s: string): string[] {
    return s.split("\n").map((l) => l.trim()).filter(Boolean);
  }

  async function save() {
    if (hashCeilingError) return;
    busy = true;
    error = "";
    try {
      if (library === null) {
        await createLibrary({
          name: name.trim(),
          root_path: rootPath.trim(),
          native_prefix: nativePrefix.trim() ? nativePrefix.trim() : null,
          share_prefix: sharePrefix.trim() ? sharePrefix.trim() : null,
          enabled_categories: enabledCategories.length ? enabledCategories : undefined,
          enabled_groups: enabledGroups.length ? enabledGroups : undefined,
          include_globs: linesToArray(includeGlobs),
          exclude_globs: linesToArray(excludeGlobs),
          scan_cron: scanCron.trim() ? scanCron.trim() : null,
          watch_mode: watchMode,
          hash_policy: hashPolicy,
          hash_full_max_bytes: hashCeilingValue,
          enabled_presets: presets,
          enabled_extension_groups: groups,
          ocr_enabled: ocrEnabled,
          chunking_enabled: chunkingEnabled,
          expose_gps: exposeGps,
          count_pruned_files: countPrunedFiles,
          extract_overrides: overridesPayload(),
        });
        open = false;
        onSaved();
        return;
      }
      await updateLibrary(library.id, {
        name,
        root_path: rootPath,
        native_prefix: nativePrefix.trim() ? nativePrefix.trim() : null,
        share_prefix: sharePrefix.trim() ? sharePrefix.trim() : null,
        enabled,
        enabled_categories: enabledCategories,
        enabled_groups: enabledGroups,
        include_globs: linesToArray(includeGlobs),
        exclude_globs: linesToArray(excludeGlobs),
        scan_cron: scanCron.trim() ? scanCron.trim() : null,
        watch_mode: watchMode,
        hash_policy: hashPolicy,
        hash_full_max_bytes: hashCeilingValue,
        enabled_presets: presets,
        enabled_extension_groups: groups,
        ocr_enabled: ocrEnabled,
        chunking_enabled: chunkingEnabled,
        expose_gps: exposeGps,
        count_pruned_files: countPrunedFiles,
        extract_overrides: overridesPayload(),
      });
      open = false;
      onSaved();
    } catch (e) {
      // 422s (invalid cron, network watch path, unknown preset/group, non-positive
      // ceiling, …) are shown inline as the server's detail text.
      error = errorMessage(e, { verb: "save this library" });
    } finally {
      busy = false;
    }
  }

</script>

<Modal
  bind:open
  title={creating ? "Add library" : "Edit library"}
  size="lg"
  {onClose}
  dirty={isDirty}
>
  <!-- Help sits beside each Field/Checkbox, not inside its label, so the labels
       stay plain strings and the "?" button is not nested in a label. -->
  <div class="space-y-6 text-sm">
    <!-- Identity -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Identity</h3>
      <div class="flex items-start gap-2">
        <Field label="Name" required class="flex-1">
          <Input placeholder="Name" bind:value={name} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.name} label="name" /></span>
      </div>

      <div class="flex items-start gap-2">
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <Field label="Root path" required>
            <div class="flex gap-2">
              <span class="min-w-0 flex-1">
                <Input class="font-mono" placeholder="Root path (e.g. /data/media/media)" bind:value={rootPath} />
              </span>
              <Button onclick={() => (showPicker = true)}>Browse…</Button>
            </div>
          </Field>
          {#if rootChanged}
            <p class="text-xs text-warn">
              Identity is (library_id, rel_path); moving the root re-anchors rel_paths — the next
              scan treats unchanged rel_paths as the same items, but a different tree shape will
              tombstone/move items.
            </p>
          {/if}
        </div>
        <span class="pt-0.5"><Help text={HELP.root_path} label="root path" /></span>
      </div>

      <div class="flex items-start gap-2">
        <Field label="Native prefix" class="flex-1">
          <Input class="font-mono" placeholder="(optional) e.g. /mnt/user/media" bind:value={nativePrefix} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.native_prefix} label="native prefix" /></span>
      </div>

      <div class="flex items-start gap-2">
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <Field label="Share location">
            <Input
              class="font-mono"
              placeholder={autoShareHint
                ? `auto from mount: ${autoShareHint}`
                : "(optional) e.g. \\tower\media, smb://tower/media, /Volumes/media"}
              bind:value={sharePrefix} />
          </Field>
          {#if autoShareHint && !sharePrefix.trim()}
            <p class="text-xs text-muted">
              Auto-detected from the deploy mount map:
              <span class="font-mono text-accent-text">{autoShareHint}</span>.
              Leave blank to use it, or type a value to override.
            </p>
          {/if}
        </div>
        <span class="pt-0.5"><Help text={HELP.share_prefix} label="share location" /></span>
      </div>

      {#if !creating}
        <div class="flex items-start gap-2">
          <Checkbox label="Enabled" description="library participates in scheduled scans" bind:checked={enabled} />
          <Help text={HELP.enabled} label="enabled" />
        </div>
      {/if}
    </section>

    <!-- Content -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Content</h3>
      <div class="flex flex-col gap-1">
        <span class="flex items-center gap-1 text-xs font-medium text-fg">
          File types <Help text={HELP.media_types} label="file types" />
        </span>
        <TaxonomySelector tree={taxonomyTree} bind:categories={enabledCategories} bind:groups={enabledGroups} />
      </div>

      <div class="flex items-start gap-2">
        <Field label="Include globs" class="flex-1">
          <Textarea rows={2} size="sm" class="font-mono"
            placeholder="one glob per line (blank = include all)" bind:value={includeGlobs} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.include_globs} label="include globs" /></span>
      </div>

      <div class="flex items-start gap-2">
        <Field label="Exclude globs" class="flex-1">
          <Textarea rows={2} size="sm" class="font-mono"
            placeholder="one glob per line" bind:value={excludeGlobs} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.exclude_globs} label="exclude globs" /></span>
      </div>
    </section>

    <!-- Schedule -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Schedule</h3>
      <div class="flex flex-col gap-1">
        <span class="flex items-center gap-1 text-xs font-medium text-fg">
          Scan schedule <Help text={HELP.scan_cron} label="scan schedule" />
        </span>
        <ScheduleField value={scanCron || null} onChange={(c) => (scanCron = c ?? "")} />
      </div>

      <div class="flex items-start gap-2">
        <Checkbox label="Watch mode" description="watch filesystem (local paths only)" bind:checked={watchMode} />
        <Help text={HELP.watch_mode} label="watch mode" />
      </div>
    </section>

    <!-- Hashing -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Hashing</h3>
      <div class="flex items-start gap-2">
        <Field label="Policy" class="w-48">
          <Select bind:value={hashPolicy}>
            {#each HASH_POLICIES as hp}
              <option value={hp}>{hp === "quick_only" ? "quick only" : hp}</option>
            {/each}
          </Select>
        </Field>
        <span class="pt-0.5"><Help text={HELP.hash_policy} label="hash policy" /></span>
      </div>

      <div class="flex items-start gap-2">
        <Field label="Full-hash ceiling" class="w-56" error={hashCeilingError || undefined}>
          <Input
            size="sm" type="number" min="1" step="1" placeholder="bytes (blank = global default)"
            bind:value={hashCeiling} bind:ref={hashCeilingEl}
            oninput={readHashCeilingValidity} onchange={readHashCeilingValidity} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.hash_ceiling} label="hash ceiling" /></span>
      </div>
    </section>

    <!-- Per-library extraction-limit overrides -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Extraction limits</h3>
      <p class="text-xs text-muted">
        Override the global extraction ceilings for this library only — blank fields keep
        the global <code class="font-mono">FILEARR_*</code> defaults. Changes apply to future
        extractions; use <em>retry</em> on failed items to re-run them under the new limits.
      </p>
      <div class="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
        {#each OVERRIDE_FIELDS as f (f.key)}
          <Field label="{f.label} ({f.unit})" hint={f.help}>
            <Input size="sm" type="number" min="0" step="any" placeholder="global" bind:value={overrideInputs[f.key]} />
          </Field>
        {/each}
      </div>
    </section>

    <!-- Content processing (OCR + privacy) -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Content processing</h3>
      {#if agentOwned}
        <Alert tone="info" live={false}>
          This library's files live on a remote agent. Central never opens
          them, so <b>central-side OCR cannot run</b> — turn on
          <code class="font-mono">extract_ocr</code> in the agent's policy
          instead (Agents → Agent policy → Content extraction), on hosts that
          have tesseract. RAG chunking below <em>does</em> work: it consumes
          whatever text the agent extracted and replicated.
        </Alert>
      {/if}
      <div class="flex items-start gap-2">
        <Checkbox
          label="OCR text"
          description="OCR images & scanned PDFs so their text is searchable (CPU-heavy; off by default)."
          bind:checked={ocrEnabled}
          disabled={agentOwned} />
        <Help text={HELP.ocr_enabled} label="OCR" />
      </div>

      <div class="flex flex-col gap-1">
        <div class="flex items-start gap-2">
          <Checkbox
            label="RAG chunking"
            description="Chunk extracted document text for LLM passage retrieval (retrieve_passages). Off by default; after enabling, run “Chunk documents for RAG” on the Jobs page."
            bind:checked={chunkingEnabled} />
          <Help text={HELP.chunking_enabled} label="RAG chunking" />
        </div>
        {#if agentOwned}
          <p class="ml-6 text-2xs text-muted">
            For this agent-owned library the text comes from the agent's own
            extraction pass, so it needs <code class="font-mono">extract_enabled</code>
            + <code class="font-mono">extract_body_text</code> in the agent's policy too.
          </p>
        {/if}
      </div>

      <div class="flex items-start gap-2">
        <Checkbox
          label="Expose GPS"
          description="Show photo GPS/location in search & API. Hidden by default for privacy (CWE-1230)."
          bind:checked={exposeGps} />
        <Help text={HELP.expose_gps} label="expose GPS" />
      </div>

      <Checkbox
        label="Count pruned"
        description="Count files inside pruned folders (e.g. .git, .venv) so the scan's seen + excluded + pruned adds up to the file count your OS reports. Those folders are normally skipped without being read at all, which is why a library can show far fewer files than the folder's properties. Costs an extra directory listing per pruned folder — slow over SMB/rclone, so leave it off unless you are investigating a mismatch."
        bind:checked={countPrunedFiles} />
    </section>

    <!-- Presets -->
    <section class="flex flex-col gap-2">
      <h3 class="flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-muted">
        Preset exclusions <Help text={HELP.presets} label="presets" />
      </h3>
      {#each presetsMeta.presets as p (p.name)}
        {@const active = isPresetActive(p)}
        <div>
          <div class="flex flex-wrap items-start gap-2">
            <Checkbox
              label={p.default_enabled ? `${p.label} (default on)` : p.label}
              checked={active}
              onchange={() => togglePreset(p)} />
            <button type="button" class="text-xs text-accent-text underline"
              aria-expanded={!!showPatterns[p.name]}
              onclick={() => (showPatterns = { ...showPatterns, [p.name]: !showPatterns[p.name] })}>
              {showPatterns[p.name] ? "hide" : "show"} patterns
            </button>
          </div>
          {#if p.name === "hidden_dotfiles" && !active}
            <span class="ml-6 block text-xs text-warn">
              Warning: disabling will surface previously hidden files on the next scan.
            </span>
          {:else if p.caveat}
            <span class="ml-6 block text-xs text-muted">{p.caveat}</span>
          {/if}
          {#if showPatterns[p.name]}
            <code class="ml-6 block whitespace-pre-wrap font-mono text-xs text-muted">{p.patterns.join("  ")}</code>
          {/if}
        </div>
      {/each}
    </section>

    <!-- Extension groups -->
    <section class="flex flex-col gap-2">
      <h3 class="flex items-center gap-1 text-xs font-semibold uppercase tracking-wide text-muted">
        Extension groups <Help text={HELP.extension_groups} label="extension groups" />
      </h3>
      <p class="text-xs text-muted">
        Enabling a group narrows that media type to the listed extensions (union of enabled
        groups). No group = all extensions of the type.
      </p>
      {#each ALL_TYPES as mt}
        {@const gs = groupsForType(mt)}
        {#if gs.length}
          <div class="flex flex-wrap items-start gap-x-3 gap-y-1 text-xs">
            <span class="font-medium">{mt}</span>
            {#each gs as g (g.name)}
              <Checkbox
                size="sm"
                label={g.label}
                title={g.extensions.join(", ")}
                checked={groups.includes(g.name)}
                onchange={() => toggleGroup(g.name)} />
            {/each}
          </div>
        {/if}
      {/each}
    </section>
  </div>

  {#if showPicker}
    <FolderPicker
      initial={rootPath}
      onPick={(p) => { rootPath = p; showPicker = false; }}
      onClose={() => (showPicker = false)}
    />
  {/if}

  {#snippet footer({ close })}
    {#if error}
      <!-- 422s (invalid cron, network watch path, unknown preset/group, …)
           land here, right above the Save that raised them. -->
      <Alert tone="danger" class="w-full" onDismiss={() => (error = "")}>{error}</Alert>
    {/if}
    <Button onclick={close}>Cancel</Button>
    <Button
      variant="primary"
      disabled={busy || !!hashCeilingError || (creating && (!name.trim() || !rootPath.trim()))}
      onclick={save}>
      {#if creating}{busy ? "Adding…" : "Add library"}{:else}{busy ? "Saving…" : "Save changes"}{/if}
    </Button>
  {/snippet}
</Modal>
