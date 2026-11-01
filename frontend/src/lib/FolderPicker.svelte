<script lang="ts">
  // UI-T4 (frontend) — server-side folder browser dialog. Lists directories only,
  // rooted at the backend's allowlist (FILEARR_BROWSE_ROOTS). The user navigates
  // via breadcrumb / parent / dir list and confirms a selection that feeds
  // root_path. Free-text entry stays available on the parent form; this is an
  // optional aid. A 422 (outside allowlist / traversal) surfaces inline rather
  // than blowing up the dialog.
  import { onDestroy } from "svelte";
  import { browseFs, type FsBrowse, errorMessage } from "./api";
  import { latest } from "./latest";
  import { Alert, Button, Modal, StateBlock } from "./ui";

  let {
    initial = "",
    onPick,
    onClose,
  }: { initial?: string; onPick: (path: string) => void; onClose: () => void } = $props();

  // Mount-style: the parent mounts this while it wants the picker and
  // unmounts it from onClose / onPick. `open` lets "Use this folder" close
  // the Modal as a caller close before handing the path over.
  let open = $state(true);
  let view = $state<FsBrowse | null>(null);
  let error = $state("");
  let loading = $state(false);

  // Last-request-wins: clicking through folders faster than the server answers
  // aborts the older listing, so the view always matches the last click.
  const browser = latest((signal, path: string) => browseFs(path, signal));
  onDestroy(() => browser.cancel());

  async function go(path: string) {
    loading = true;
    error = "";
    try {
      const r = await browser.run(path);
      if (r === undefined) return;
      view = r;
      loading = false;
    } catch (e) {
      // Keep the previous listing visible so the user can recover.
      error = errorMessage(e, { verb: "browse this folder" });
      loading = false;
    }
  }

  // Breadcrumb: split the current absolute path into cumulative segments so each
  // ancestor is individually clickable. Derived from the server-reported `path`
  // (never from local string math on user input).
  function crumbs(path: string): { label: string; path: string }[] {
    if (!path) return [];
    const parts = path.split("/").filter(Boolean);
    const out: { label: string; path: string }[] = [];
    let acc = "";
    for (const p of parts) {
      acc = `${acc}/${p}`;
      out.push({ label: p, path: acc });
    }
    return out;
  }

  $effect(() => {
    void go(initial);
  });
</script>

<Modal bind:open title="Choose a folder" size="md" {onClose}>
  <!-- Breadcrumb: roots shortcut + each ancestor segment. -->
  <nav aria-label="Folder path" class="flex flex-wrap items-center gap-1 text-xs text-muted">
    <button type="button" class="rounded-control px-1 hover:text-accent-text hover:underline" onclick={() => go("")}>
      roots
    </button>
    {#if view?.path}
      {#each crumbs(view.path) as c (c.path)}
        <span aria-hidden="true">/</span>
        <button type="button" class="rounded-control px-1 hover:text-accent-text hover:underline" onclick={() => go(c.path)}>
          {c.label}
        </button>
      {/each}
    {/if}
  </nav>

  {#if error}
    <Alert tone="danger" class="mt-3" onDismiss={() => (error = "")}>{error}</Alert>
  {/if}

  <div class="mt-3 max-h-72 overflow-y-auto rounded-control border border-border" aria-busy={loading}>
    {#if loading && !view}
      <StateBlock state="loading" compact />
    {:else if view}
      <ul class="divide-y divide-border text-sm">
        {#if view.parent !== null}
          <li>
            <button
              type="button"
              class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-3"
              onclick={() => go(view!.parent ?? "")}>
              <span class="text-muted" aria-hidden="true">↑</span> ..
            </button>
          </li>
        {/if}
        {#if !view.path && view.roots.length}
          {#each view.roots as r (r)}
            <li>
              <button
                type="button"
                class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-3"
                onclick={() => go(r)}>
                <span class="text-muted" aria-hidden="true">▸</span>
                <span class="font-mono">{r}</span>
              </button>
            </li>
          {/each}
        {:else if view.dirs.length}
          {#each view.dirs as d (d.path)}
            <li>
              <button
                type="button"
                class="flex w-full items-center gap-2 px-3 py-2 text-left hover:bg-surface-3"
                onclick={() => go(d.path)}>
                <span class="text-muted" aria-hidden="true">▸</span> {d.name}
              </button>
            </li>
          {/each}
        {:else}
          <li><StateBlock state="empty" compact message="No sub-folders here — use this folder or go up a level." /></li>
        {/if}
      </ul>
    {/if}
  </div>

  {#snippet footer({ close })}
    <span class="min-w-0 grow self-center truncate font-mono text-xs text-muted" title={view?.path || undefined}>
      {view?.path || "(no folder selected)"}
    </span>
    <Button onclick={close}>Cancel</Button>
    <Button
      variant="primary"
      disabled={!view?.path}
      onclick={() => {
        if (!view?.path) return;
        const path = view.path;
        open = false;
        onPick(path);
      }}>
      Use this folder
    </Button>
  {/snippet}
</Modal>
