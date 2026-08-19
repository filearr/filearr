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

  let presets = $state<Preset[]>([]);
  let error = $state("");
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
      error = "";
    } catch (e) {
      error = friendlyError(e, "load presets");
    }
  }
  $effect(() => {
    void load();
  });

  function openCreate() {
    editor = { mode: "create" };
    fName = ""; fLabel = ""; fPatterns = ""; fCaveat = "";
  }
  function openEdit(p: Preset) {
    editor = { mode: "edit", name: p.name };
    fName = p.name; fLabel = p.label; fPatterns = p.patterns.join("\n"); fCaveat = p.caveat ?? "";
  }
  function openFork(p: Preset) {
    editor = { mode: "fork", from: p.name };
    fName = `${p.name}-custom`; fLabel = `${p.label} (copy)`; fPatterns = p.patterns.join("\n"); fCaveat = p.caveat ?? "";
  }
  const patternList = $derived(fPatterns.split("\n").map((l) => l.trim()).filter((l) => l && !l.startsWith("#")));

  async function save() {
    if (!editor) return;
    busy = true;
    error = "";
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
      error = friendlyError(e, "save the preset");
    } finally {
      busy = false;
    }
  }
  async function remove(p: Preset) {
    if (!confirm(`Delete preset "${p.label}" (${p.name})? Libraries that still enable it block the delete.`)) return;
    try {
      await deletePreset(p.name);
      await load();
    } catch (e) {
      error = friendlyError(e, "delete the preset");
    }
  }
</script>

<section class="mt-8">
  <div class="flex items-center gap-3">
    <h2 class="text-lg font-semibold">Exclusion presets</h2>
    <button class="rounded bg-[var(--accent)] px-3 py-1 text-sm text-white" onclick={openCreate}>New preset</button>
  </div>
  <p class="mt-1 text-sm text-slate-500 dark:text-slate-400">
    Named bundles of gitignore-style exclude patterns that libraries toggle on
    (Edit library → Exclusion presets). Builtins ship with Filearr and are
    read-only — <em>fork</em> one to edit a copy. A custom bundle reaches the
    walk on the next scan of any library that enables it. A trailing
    <code>/</code> prunes a whole directory.
  </p>
  {#if error}
    <p class="mt-2 rounded bg-red-100 px-3 py-2 text-sm text-red-800 dark:bg-red-950 dark:text-red-300">{error}</p>
  {/if}

  {#if editor}
    <div class="mt-3 grid max-w-2xl gap-3 rounded-lg border border-slate-200 p-4 dark:border-slate-800">
      <div class="text-sm font-medium">
        {editor.mode === "create" ? "New preset" : editor.mode === "fork" ? `Fork of ${editor.from}` : `Edit ${editor.name}`}
      </div>
      <div class="grid grid-cols-2 gap-3">
        <label class="grid gap-1 text-sm">
          <span>Name (a-z, 0-9, - _)</span>
          <input class="rounded border border-slate-300 px-2 py-1 font-mono dark:border-slate-700 dark:bg-slate-900"
            bind:value={fName} disabled={editor.mode === "edit"} placeholder="no-raw-photos" />
        </label>
        <label class="grid gap-1 text-sm">
          <span>Label</span>
          <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={fLabel} placeholder="No RAW photos" />
        </label>
      </div>
      <label class="grid gap-1 text-sm">
        <span>Patterns (one per line, gitignore syntax; # comments ignored)</span>
        <textarea rows="6" class="rounded border border-slate-300 px-2 py-1 font-mono text-xs dark:border-slate-700 dark:bg-slate-900" bind:value={fPatterns} placeholder={"*.cr2\n*.nef\nRAW/"}></textarea>
      </label>
      <label class="grid gap-1 text-sm">
        <span>Caveat (optional — shown next to the toggle)</span>
        <input class="rounded border border-slate-300 px-2 py-1 dark:border-slate-700 dark:bg-slate-900" bind:value={fCaveat} />
      </label>
      <div class="flex gap-2">
        <button class="rounded bg-[var(--accent)] px-4 py-1.5 text-sm text-white disabled:opacity-50"
          disabled={busy || !fName.trim() || !fLabel.trim() || patternList.length === 0} onclick={save}>Save</button>
        <button class="rounded border border-slate-300 px-3 py-1.5 text-sm dark:border-slate-700" onclick={() => (editor = null)}>Cancel</button>
      </div>
    </div>
  {/if}

  {#if loaded}
    <div class="mt-3 overflow-x-auto">
      <table class="w-full text-sm">
        <thead class="bg-slate-50 text-left dark:bg-slate-900">
          <tr>
            <th class="px-3 py-2">Preset</th>
            <th class="px-3 py-2">Patterns</th>
            <th class="px-3 py-2">Kind</th>
            <th class="px-3 py-2"></th>
          </tr>
        </thead>
        <tbody>
          {#each presets as p (p.name)}
            <tr class="border-t border-slate-100 align-top dark:border-slate-800">
              <td class="px-3 py-2">
                <div class="font-medium">{p.label}</div>
                <div class="font-mono text-xs text-slate-500">{p.name}{#if p.default_enabled} · on by default{/if}</div>
                {#if p.caveat}<div class="mt-1 text-xs text-slate-500">{p.caveat}</div>{/if}
              </td>
              <td class="px-3 py-2 font-mono text-xs text-slate-600 dark:text-slate-300">
                {p.patterns.slice(0, 6).join(", ")}{#if p.patterns.length > 6} … (+{p.patterns.length - 6}){/if}
              </td>
              <td class="px-3 py-2">
                <span class="rounded bg-slate-200 px-2 py-0.5 text-xs dark:bg-slate-800">{p.builtin === false ? "custom" : "builtin"}</span>
              </td>
              <td class="px-3 py-2 text-right whitespace-nowrap">
                <button class="text-sky-600 dark:text-sky-400" onclick={() => openFork(p)}>fork</button>
                {#if p.builtin === false}
                  <button class="ml-3 text-sky-600 dark:text-sky-400" onclick={() => openEdit(p)}>edit</button>
                  <button class="ml-3 text-red-600 dark:text-red-400" onclick={() => remove(p)}>delete</button>
                {/if}
              </td>
            </tr>
          {/each}
        </tbody>
      </table>
    </div>
  {/if}
</section>
