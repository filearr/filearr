<script lang="ts">
  // UI-T1 — full library edit modal (kit Modal, mount-style). Exposes
  // every editable field consolidated into labelled sections: identity (name,
  // root_path + warning, native_prefix, enabled), content (types + include/exclude
  // globs), schedule (cron/watch), hashing (policy/ceiling), and the P2-T5 indexing
  // controls (exclusion presets) that previously lived in the Admin
  // expander. Edits collect into a local draft; Save sends only the changed fields
  // (libraryPayload.ts). Field errors (client checks, 409 name, 422 loc) render on
  // their Fields; anything else in the Alert beside Save. All dynamic strings
  // render as text (no {@html}).
  //
  // The SAME modal also CREATES a library (`library === null`): the old inline
  // "Add library" form on the Libraries page exposed only a subset of these fields
  // and cluttered the page; one dialog with one field set means an option that
  // exists on Edit also exists on Add, and the page shows a single button.
  import { untrack } from "svelte";
  import {
    createLibrary, resolveShareHint, updateLibrary,
    type HashPolicy, type Library, type Preset, type PresetsResponse,
    type ShareMapEntry, type TaxonomyNode,
  } from "./api";
  import { HELP } from "./help";
  import Help from "./Help.svelte";
  import {
    OVERRIDE_FIELDS, buildLibraryPayload, draftFromLibrary, libraryServerErrors,
    type FieldErrors, type LibraryDraft, type LibraryPayload,
  } from "./libraryPayload";
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

  const HASH_POLICIES: HashPolicy[] = ["auto", "full", "quick_only"];

  // Working draft, seeded ONCE from the library prop. The modal is remounted per
  // edit target (keyed by `editing` in LibrariesPage), so capturing the initial value
  // is the intended behaviour — `untrack` makes that explicit and keeps the draft
  // from re-binding to the prop. Field names are the API's; globs edit as
  // newline-joined text, overrides in display units (libraryPayload.ts).
  let draft = $state<LibraryDraft>(untrack(() => draftFromLibrary(library)));

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
    creating && draft.root_path.trim() ? resolveShareHint(shareMap, draft.root_path.trim()) : null,
  );
  const autoShareHint = $derived(
    creating ? detectedShare?.share_url ?? null
    : library?.share_prefix_source === "mount-map" ? library.share_prefix_effective ?? null
    : null,
  );

  // An unparseable number ("1e") binds null, which reads as blank and would
  // save the global default; validity.badInput is the only way to tell them
  // apart, so each number input reports it into the draft.
  function trackBadInput(key: string, el: HTMLInputElement) {
    const bad = new Set(draft.bad_input ?? []);
    if (el.validity.badInput) bad.add(key);
    else bad.delete(key);
    draft.bad_input = [...bad];
  }

  // The payload (or the field errors) for the current draft, recomputed live:
  // number errors show as you type, and Save stays disabled while any remain.
  const result = $derived(buildLibraryPayload($state.snapshot(draft), library));
  const clientErrors = $derived<FieldErrors>(result.ok ? {} : result.errors);
  const requiredBlank = $derived(!draft.name.trim() || !draft.root_path.trim());
  // ScheduleField's own field error (weekly with no day, empty minute/time,
  // a monthly time that would shift the day in UTC): it emits a null cron for
  // those, so Save must wait rather than store "no schedule" (B18, B19, B32).
  let scheduleError = $state<string | null>(null);

  // Required-field errors wait until the field has been edited (an empty Add
  // form is not an error); Save is disabled meanwhile either way (B4).
  let touched = $state<Record<string, boolean>>({});

  // Server-side field errors (409 name, 422 loc) are tied to the value that
  // was rejected: editing that field hides its error.
  let serverErrors = $state<Record<string, { msg: string; at: string }>>({});
  function fieldValue(key: string): string {
    const ov = draft.extract_overrides;
    const v = key in ov ? ov[key] : (draft as unknown as Record<string, unknown>)[key];
    return JSON.stringify(v ?? null);
  }
  function fieldError(key: string): string | undefined {
    const client = (key === "name" || key === "root_path") && !touched[key] ? "" : clientErrors[key];
    if (client) return client;
    const server = serverErrors[key];
    return server && server.at === fieldValue(key) ? server.msg : undefined;
  }

  // Anything the form cannot attach to a field (cron, presets, 5xx, network)
  // shows in the Alert beside Save.
  let error = $state("");
  let busy = $state(false);
  // The FolderPicker is its own Modal stacked over this one; Escape and clicks
  // inside it belong to the picker (bits-ui's layer stack + the Modal's
  // nested-dialog guard), so no Escape guard is needed here.
  let showPicker = $state(false);
  // Mount-style: the parent mounts this per target and unmounts it from
  // onClose / onSaved. A successful save sets `open = false` (a caller close,
  // which skips the dirty check and onClose) before calling onSaved.
  let open = $state(true);

  // The dirty check compares the whole draft against the snapshot taken at
  // mount, so reverting an edit by hand reads as clean again.
  const snapshot = (): string => JSON.stringify(draft);
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

  const rootChanged = $derived(library !== null && draft.root_path !== library.root_path);

  // Mirrors presets.resolve_effective_presets: a default-on bundle is active
  // unless a `-name` opt-out sentinel is present; others only when listed.
  function isPresetActive(p: Preset): boolean {
    if (draft.enabled_presets.includes("-" + p.name)) return false;
    if (draft.enabled_presets.includes(p.name)) return true;
    return p.default_enabled;
  }

  function togglePreset(p: Preset) {
    const active = isPresetActive(p);
    let next = draft.enabled_presets.filter((e) => e !== p.name && e !== "-" + p.name);
    if (active && p.default_enabled) next = [...next, "-" + p.name];
    else if (!active && !p.default_enabled) next = [...next, p.name];
    draft.enabled_presets = next;
  }

  async function save() {
    if (scheduleError) return;
    const built = buildLibraryPayload($state.snapshot(draft), library);
    if (!built.ok) {
      touched = { name: true, root_path: true };
      return;
    }
    // Edit with nothing changed: no PATCH, just close (B31).
    if (library !== null && Object.keys(built.payload).length === 0) {
      open = false;
      onClose();
      return;
    }
    busy = true;
    error = "";
    serverErrors = {};
    try {
      if (library === null) {
        await createLibrary(built.payload as LibraryPayload & { name: string; root_path: string });
      } else {
        // Only the changed fields: a full-form PATCH would revert anything
        // that changed server-side since the modal opened (B31).
        await updateLibrary(library.id, built.payload);
      }
      open = false;
      onSaved();
    } catch (e) {
      const { fields, message } = libraryServerErrors(e);
      serverErrors = Object.fromEntries(
        Object.entries(fields).map(([k, msg]) => [k, { msg, at: fieldValue(k) }]),
      );
      const n = Object.keys(fields).length;
      // A field error may be scrolled out of view, so Save always says why.
      error = message ?? `Not saved: ${n === 1 ? "a field needs" : `${n} fields need`} attention (marked in red).`;
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
        <Field label="Name" required class="flex-1" error={fieldError("name")}>
          <Input placeholder="Name" bind:value={draft.name} oninput={() => (touched.name = true)} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.name} label="name" /></span>
      </div>

      <div class="flex items-start gap-2">
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <Field label="Root path" required error={fieldError("root_path")}>
            <div class="flex gap-2">
              <span class="min-w-0 flex-1">
                <Input class="font-mono" placeholder="Root path (e.g. /data/media/media)"
                  bind:value={draft.root_path} oninput={() => (touched.root_path = true)} />
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
        <Field label="Native prefix" class="flex-1" error={fieldError("native_prefix")}>
          <Input class="font-mono" placeholder="(optional) e.g. /mnt/user/media" bind:value={draft.native_prefix} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.native_prefix} label="native prefix" /></span>
      </div>

      <div class="flex items-start gap-2">
        <div class="flex min-w-0 flex-1 flex-col gap-1">
          <Field label="Share location" error={fieldError("share_prefix")}>
            <Input
              class="font-mono"
              placeholder={autoShareHint
                ? `auto from mount: ${autoShareHint}`
                : "(optional) e.g. \\tower\media, smb://tower/media, /Volumes/media"}
              bind:value={draft.share_prefix} />
          </Field>
          {#if autoShareHint && !draft.share_prefix.trim()}
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
          <Checkbox
            label="Enabled"
            description="Scheduled, hot-folder and watch scans run. When off they pause; manual and targeted scans still run and items stay searchable."
            bind:checked={draft.enabled} />
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
        <TaxonomySelector tree={taxonomyTree} bind:categories={draft.enabled_categories} bind:groups={draft.enabled_groups} />
      </div>

      <div class="flex items-start gap-2">
        <Field label="Exclude globs" class="flex-1" error={fieldError("exclude_globs")}>
          <Textarea rows={2} size="sm" class="font-mono"
            placeholder="one pattern per line" bind:value={draft.exclude_globs} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.exclude_globs} label="exclude globs" /></span>
      </div>

      <!-- Applied AFTER the excludes as `!pattern` negations (B7): it re-admits
           files an exclude removed; it does not narrow what is indexed. -->
      <div class="flex items-start gap-2">
        <Field label="Re-include (overrides excludes)" class="flex-1" error={fieldError("include_globs")}>
          <Textarea rows={2} size="sm" class="font-mono"
            placeholder="one pattern per line (blank = no overrides)" bind:value={draft.include_globs} />
        </Field>
        <span class="pt-0.5"><Help text={HELP.include_globs} label="re-include" /></span>
      </div>
    </section>

    <!-- Schedule -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Schedule</h3>
      <div class="flex flex-col gap-1">
        <span class="flex items-center gap-1 text-xs font-medium text-fg">
          Scan schedule <Help text={HELP.scan_cron} label="scan schedule" />
        </span>
        <ScheduleField
          value={draft.scan_cron || null}
          onChange={(c) => (draft.scan_cron = c ?? "")}
          onValidity={(e) => (scheduleError = e)} />
      </div>

      <div class="flex items-start gap-2">
        <Checkbox label="Watch mode" description="watch filesystem (local paths only)" bind:checked={draft.watch_mode} />
        <Help text={HELP.watch_mode} label="watch mode" />
      </div>
    </section>

    <!-- Hashing -->
    <section class="flex flex-col gap-3">
      <h3 class="text-xs font-semibold uppercase tracking-wide text-muted">Hashing</h3>
      <div class="flex items-start gap-2">
        <Field label="Policy" class="w-48" error={fieldError("hash_policy")}>
          <Select bind:value={draft.hash_policy}>
            {#each HASH_POLICIES as hp}
              <option value={hp}>{hp === "quick_only" ? "quick only" : hp}</option>
            {/each}
          </Select>
        </Field>
        <span class="pt-0.5"><Help text={HELP.hash_policy} label="hash policy" /></span>
      </div>

      <div class="flex items-start gap-2">
        <Field label="Full-hash ceiling" class="w-56" error={fieldError("hash_full_max_bytes")}>
          <Input
            size="sm" type="number" min="1" step="1" placeholder="bytes (blank = global default)"
            bind:value={draft.hash_full_max_bytes}
            oninput={(e) => trackBadInput("hash_full_max_bytes", e.currentTarget)}
            onchange={(e) => trackBadInput("hash_full_max_bytes", e.currentTarget)} />
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
      {#if fieldError("extract_overrides")}
        <Alert tone="danger" live={false}>{fieldError("extract_overrides")}</Alert>
      {/if}
      <div class="grid grid-cols-1 gap-x-4 gap-y-3 sm:grid-cols-2">
        {#each OVERRIDE_FIELDS as f (f.key)}
          <Field label="{f.label} ({f.unit})" hint={f.help} error={fieldError(f.key)}>
            <Input size="sm" type="number" min="0" step="any" placeholder="global"
              bind:value={draft.extract_overrides[f.key]}
              oninput={(e) => trackBadInput(f.key, e.currentTarget)}
              onchange={(e) => trackBadInput(f.key, e.currentTarget)} />
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
          bind:checked={draft.ocr_enabled}
          disabled={agentOwned} />
        <Help text={HELP.ocr_enabled} label="OCR" />
      </div>

      <div class="flex flex-col gap-1">
        <div class="flex items-start gap-2">
          <Checkbox
            label="RAG chunking"
            description="Chunk extracted document text for LLM passage retrieval (retrieve_passages). Off by default; after enabling, run “Chunk documents for RAG” on the Jobs page."
            bind:checked={draft.chunking_enabled} />
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
          bind:checked={draft.expose_gps} />
        <Help text={HELP.expose_gps} label="expose GPS" />
      </div>

      <Checkbox
        label="Count pruned"
        description="Count files inside pruned folders (e.g. .git, .venv) so the scan's seen + excluded + pruned adds up to the file count your OS reports. Those folders are normally skipped without being read at all, which is why a library can show far fewer files than the folder's properties. Costs an extra directory listing per pruned folder — slow over SMB/rclone, so leave it off unless you are investigating a mismatch."
        bind:checked={draft.count_pruned_files} />
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

  </div>

  {#if showPicker}
    <FolderPicker
      initial={draft.root_path}
      onPick={(p) => { draft.root_path = p; touched.root_path = true; showPicker = false; }}
      onClose={() => (showPicker = false)}
    />
  {/if}

  {#snippet footer({ close })}
    {#if error}
      <!-- Errors with no field of their own (invalid cron, network watch path,
           unknown preset, 5xx, offline) land here, beside the Save that raised
           them; field errors also leave a pointer here in case they are
           scrolled out of view. -->
      <Alert tone="danger" class="w-full" onDismiss={() => (error = "")}>{error}</Alert>
    {/if}
    <Button onclick={close}>Cancel</Button>
    <Button
      variant="primary"
      disabled={busy || requiredBlank || !result.ok || scheduleError !== null}
      onclick={save}>
      {#if creating}{busy ? "Adding…" : "Add library"}{:else}{busy ? "Saving…" : "Save changes"}{/if}
    </Button>
  {/snippet}
</Modal>
