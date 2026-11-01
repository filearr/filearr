<script lang="ts">
  // P2-T7: exclusion preset bundles as data. Builtins are read-only (fork to
  // edit); custom bundles are created/edited/deleted here and become available
  // in every library's "Exclusion presets" toggles and the walk on its next scan.
  import {
    listPresets,
    createPreset,
    forkPreset,
    patchPreset,
    deletePreset,
    friendlyError,
    type Preset,
  } from "./api";
  import { Alert, Badge, Button, DataTable, Field, Input, Textarea, confirm } from "./ui";

  let presets = $state<Preset[]>([]);
  let loadError = $state("");
  let saveError = $state(""); // editor Save
  let deleteError = $state(""); // row delete
  let loaded = $state(false);

  // editor state: null = closed; {mode:'create'} | {mode:'edit', name} | {mode:'fork', from}
  let editor = $state<null | { mode: "create" | "edit" | "fork"; name?: string; from?: string }>(null);
  let fName = $state("");
  let fLabel = $state("");
  let fPatterns = $state("");
  let fCaveat = $state("");
  let busy = $state(false);

  async function load() {
    try {
      presets = (await listPresets()).presets;
      loaded = true;
      loadError = "";
    } catch (e) {
      loadError = friendlyError(e, "load presets");
    }
  }
  $effect(() => {
    void load();
  });

  function openCreate() {
    saveError = "";
    editor = { mode: "create" };
    fName = ""; fLabel = ""; fPatterns = ""; fCaveat = "";
  }
  function openEdit(p: Preset) {
    saveError = "";
    editor = { mode: "edit", name: p.name };
    fName = p.name; fLabel = p.label; fPatterns = p.patterns.join("\n"); fCaveat = p.caveat ?? "";
  }
  function openFork(p: Preset) {
    saveError = "";
    editor = { mode: "fork", from: p.name };
    fName = `${p.name}-custom`; fLabel = `${p.label} (copy)`; fPatterns = p.patterns.join("\n"); fCaveat = p.caveat ?? "";
  }
  const patternList = $derived(fPatterns.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")));

  async function save() {
    if (!editor) return;
    busy = true;
    saveError = "";
    try {
      if (editor.mode === "create") {
        await createPreset({ name: fName.trim(), label: fLabel.trim(), patterns: patternList, caveat: fCaveat.trim() || null });
      } else if (editor.mode === "fork") {
        const made = await forkPreset(editor.from!, { name: fName.trim(), label: fLabel.trim() || null });
        // a fork copies the source verbatim; apply the operator's edits on top
        if (patternList.join("\n") !== made.patterns.join("\n") || (fCaveat.trim() || null) !== made.caveat)
          await patchPreset(made.name, { patterns: patternList, caveat: fCaveat.trim() || null });
      } else {
        await patchPreset(editor.name!, { label: fLabel.trim(), patterns: patternList, caveat: fCaveat.trim() || null });
      }
      editor = null;
      await load();
    } catch (e) {
      saveError = friendlyError(e, "save the preset");
    } finally {
      busy = false;
    }
  }
  async function remove(p: Preset) {
    const ok = await confirm({
      title: `Delete preset "${p.label}" (${p.name})?`,
      message: "Libraries that still enable it block the delete.",
      confirmLabel: "Delete",
      tone: "danger",
    });
    if (!ok) return;
    deleteError = "";
    try {
      await deletePreset(p.name);
      await load();
    } catch (e) {
      deleteError = friendlyError(e, "delete the preset");
    }
  }
</script>

{#snippet presetCell(p: Preset)}
  <div class="font-medium">{p.label}</div>
  <div class="font-mono text-xs text-muted">{p.name}{#if p.default_enabled} · on by default{/if}</div>
  {#if p.caveat}<div class="mt-1 text-xs text-muted">{p.caveat}</div>{/if}
{/snippet}
{#snippet patternsCell(p: Preset)}
  <span class="font-mono text-xs text-muted">
    {p.patterns.slice(0, 6).join(", ")}{#if p.patterns.length > 6} … (+{p.patterns.length - 6}){/if}
  </span>
{/snippet}
{#snippet kindCell(p: Preset)}
  <Badge tone={p.builtin === false ? "accent" : "neutral"}>{p.builtin === false ? "custom" : "builtin"}</Badge>
{/snippet}
{#snippet actionsCell(p: Preset)}
  <div class="flex justify-end gap-1 whitespace-nowrap">
    <Button size="sm" variant="ghost" onclick={() => openFork(p)}>fork</Button>
    {#if p.builtin === false}
      <Button size="sm" variant="ghost" onclick={() => openEdit(p)}>edit</Button>
      <Button size="sm" variant="danger" onclick={() => remove(p)}>delete</Button>
    {/if}
  </div>
{/snippet}

<section class="mt-8">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Exclusion presets</h2>
    <Button variant="primary" onclick={openCreate}>New preset</Button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Named bundles of gitignore-style exclude patterns that libraries toggle on
    (Edit library → Exclusion presets). Builtins ship with Filearr and are
    read-only — <em>fork</em> one to edit a copy. A custom bundle reaches the
    walk on the next scan of any library that enables it. A trailing
    <code>/</code> prunes a whole directory.
  </p>

  {#if editor}
    <div class="mt-3 grid max-w-2xl gap-3 rounded-control border border-border p-4">
      <div class="text-sm font-medium">
        {editor.mode === "create" ? "New preset" : editor.mode === "fork" ? `Fork of ${editor.from}` : `Edit ${editor.name}`}
      </div>
      <div class="grid grid-cols-2 gap-3">
        <Field label="Name (a-z, 0-9, - _)">
          <Input class="font-mono" bind:value={fName} disabled={editor.mode === "edit"} placeholder="no-raw-photos" />
        </Field>
        <Field label="Label">
          <Input bind:value={fLabel} placeholder="No RAW photos" />
        </Field>
      </div>
      <Field label="Patterns (one per line, gitignore syntax; # comments ignored)">
        <Textarea rows={6} class="font-mono text-xs" bind:value={fPatterns} placeholder={"*.cr2\n*.nef\nRAW/"} />
      </Field>
      <Field label="Caveat (optional — shown next to the toggle)">
        <Input bind:value={fCaveat} />
      </Field>
      <div class="flex gap-2">
        <Button variant="primary"
          disabled={busy || !fName.trim() || !fLabel.trim() || patternList.length === 0} onclick={save}>Save</Button>
        <Button onclick={() => (editor = null)}>Cancel</Button>
      </div>
      {#if saveError}
        <Alert tone="danger" onDismiss={() => (saveError = "")}>{saveError}</Alert>
      {/if}
    </div>
  {/if}

  <DataTable
    class="mt-3"
    caption="Exclusion presets"
    captionHidden
    columns={[
      { key: "label", label: "Preset", cell: presetCell },
      { key: "patterns", label: "Patterns", cell: patternsCell },
      { key: "builtin", label: "Kind", cell: kindCell },
      { key: "actions", label: "Actions", align: "right", cell: actionsCell },
    ]}
    rows={presets}
    rowKey={(p) => p.name}
    loading={!loaded}
    error={loadError || null}
    onRetry={load}
    emptyMessage="No presets yet. Use “New preset” to add one."
  />
  {#if deleteError}
    <Alert tone="danger" class="mt-2" onDismiss={() => (deleteError = "")}>{deleteError}</Alert>
  {/if}
</section>
